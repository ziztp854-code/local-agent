import difflib
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path


TEXT_EXTENSIONS = {
    ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsx", ".md",
    ".py", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
SKIP_DIRECTORIES = {
    ".aws", ".azure", ".docker", ".git", ".gnupg", ".kube", ".local-agent",
    ".next", ".pytest_cache", ".ruff_cache", ".ssh", ".venv", "__pycache__",
    "coverage", "dist", "node_modules", "venv",
}
SKIP_FILES = {".coverage"}
SENSITIVE_FILENAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials.json", "id_ed25519", "id_rsa"}
SENSITIVE_EXTENSIONS = {".key", ".p12", ".pem", ".pfx"}
WINDOWS_RESERVED_NAMES = {
    "aux", "con", "nul", "prn",
    *{f"com{number}" for number in range(1, 10)},
    *{f"lpt{number}" for number in range(1, 10)},
}


def safe_terminal_text(text):
    return "".join(
        character
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        else f"\\u{ord(character):04x}"
        for character in str(text)
    )

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files below a path inside the allowed workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the allowed workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Find exact text inside workspace text files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": "Find conceptually related workspace passages using Nomic embeddings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]

CODING_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create one new UTF-8 text file after explicit user approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": "Replace text that occurs exactly once, after showing a diff for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_changes",
            "description": "Review all unified diffs created during this process, even without Git.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

COMMAND_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "Run an argv command on the host after a security warning and explicit approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "cwd": {"type": "string", "default": "."},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
}


class ToolError(Exception):
    pass


class ApprovalDenied(ToolError):
    pass


class WorkspaceTools:
    def __init__(
        self,
        root,
        max_file_bytes=64_000,
        max_files=2_000,
        max_list_entries=300,
        chunk_chars=2_000,
        coding=False,
        allow_host_commands=False,
        approver=None,
        command_timeout=120,
        max_command_output_bytes=64_000,
        semantic_memory=None,
    ):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")
        if allow_host_commands and not coding:
            raise ValueError("--allow-host-commands requires --coding")
        if (
            type(command_timeout) not in {int, float}
            or command_timeout <= 0
            or not math.isfinite(command_timeout)
            or type(max_command_output_bytes) is not int
            or max_command_output_bytes < 1
        ):
            raise ValueError("Command limits must be positive")
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        if type(max_list_entries) is not int or max_list_entries < 1:
            raise ValueError("List entries limit must be a positive integer")
        self.max_list_entries = max_list_entries
        self.chunk_chars = chunk_chars
        self.coding = bool(coding)
        self.allow_host_commands = bool(allow_host_commands)
        self.approver = approver
        self.command_timeout = command_timeout
        self.max_command_output_bytes = max_command_output_bytes
        self.semantic_memory = semantic_memory
        self.originals = {}
        self.denied_operations = set()
        self.snapshot_manager = None
        self.command_sandbox = None
        self._process_lock = threading.Lock()
        self._active_process = None
        self._closed = False

    def tool_schemas(self):
        schemas = list(TOOL_SCHEMAS)
        if self.coding:
            schemas.extend(CODING_TOOL_SCHEMAS)
        if self.allow_host_commands:
            schemas.append(COMMAND_TOOL_SCHEMA)
        return schemas

    @staticmethod
    def _raw_relative_path(path):
        if not isinstance(path, (str, os.PathLike)):
            raise ToolError("المسار يجب أن يكون نصًا")
        raw = os.fspath(path)
        candidate = Path(raw)
        invalid_part = any(
            part == ".."
            or part != part.strip(" .")
            or any(character in '<>"|?*:' or ord(character) < 32 for character in part)
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
            for part in candidate.parts
        )
        if (
            not raw
            or "\x00" in raw
            or candidate.is_absolute()
            or candidate.drive
            or raw.startswith(("\\\\", "\\\\?\\", "\\\\.\\"))
            or invalid_part
        ):
            raise ToolError("المسار غير مسموح")
        return candidate

    @staticmethod
    def _is_reparse(path):
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        return path.is_symlink() or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def _check_path_chain(self, relative):
        current = self.root
        for part in relative.parts:
            current /= part
            if self._is_reparse(current):
                raise ToolError("الروابط ونقاط إعادة التحليل غير مسموحة")

    def _resolve(self, path):
        relative = self._raw_relative_path(path)
        self._check_path_chain(relative)
        try:
            candidate = (self.root / relative).resolve(strict=True)
        except FileNotFoundError as error:
            raise ToolError("المسار غير موجود") from error
        try:
            confined = candidate.relative_to(self.root)
        except ValueError as error:
            raise ToolError("المسار خارج مساحة العمل المسموحة") from error
        if any(part.casefold() in SKIP_DIRECTORIES for part in confined.parts):
            raise ToolError("المسار مستبعد من أدوات مساحة العمل")
        if self._is_sensitive(candidate):
            raise ToolError("قراءة ملف حساس غير مسموحة")
        return candidate

    def _resolve_new_file(self, path):
        relative = self._raw_relative_path(path)
        self._check_path_chain(relative)
        if not relative.parts or self._is_sensitive(relative):
            raise ToolError("مسار الكتابة غير مسموح")
        parent_relative = relative.parent
        parent = self._resolve(parent_relative if parent_relative.parts else ".")
        if not parent.is_dir():
            raise ToolError("مجلد الملف غير موجود")
        candidate = parent / relative.name
        if candidate.exists() or self._is_reparse(candidate):
            raise ToolError("الملف موجود بالفعل")
        return candidate

    @staticmethod
    def _is_sensitive(path):
        name = path.name.casefold()
        return (
            name == ".env"
            or name.startswith(".env.")
            or name in SENSITIVE_FILENAMES
            or path.suffix.casefold() in SENSITIVE_EXTENSIONS
        )

    def _files(self, path=".", extensions=None):
        base = self._resolve(path)
        if base.is_file():
            return [base] if extensions is None or base.suffix.lower() in extensions else []
        if not base.is_dir():
            raise ToolError("المسار ليس مجلدًا")

        found = []
        for current, directories, filenames in os.walk(base, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if name.casefold() not in SKIP_DIRECTORIES
                and not getattr(os.path, "isjunction", lambda _path: False)(Path(current, name))
            )
            for filename in sorted(filenames):
                file_path = Path(current, filename)
                if (
                    file_path.is_symlink()
                    or filename.casefold() in SKIP_FILES
                    or self._is_sensitive(file_path)
                    or (extensions is not None and file_path.suffix.lower() not in extensions)
                ):
                    continue
                found.append(file_path)
                if len(found) >= self.max_files:
                    return found
        return found

    def list_files(self, path="."):
        names = [file.relative_to(self.root).as_posix() for file in self._files(path)]
        if len(names) > self.max_list_entries:
            return [
                *names[: self.max_list_entries],
                f"...[قُطعت قائمة الملفات: أُظهرت أول {self.max_list_entries} من "
                f"{len(names)} ملفًا. استدعِ الأداة بمسار مجلد فرعي أدق]",
            ]
        return names

    def read_file(self, path):
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise ToolError("الملف غير موجود")
        with file_path.open("rb") as source:
            data = source.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            raise ToolError("الملف أكبر من الحد المسموح للقراءة")
        if b"\x00" in data:
            raise ToolError("الملفات الثنائية غير مدعومة")
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _diff(path, before, after):
        diff = list(
            difflib.unified_diff(
                [] if before is None else before.splitlines(),
                after.splitlines(),
                fromfile="/dev/null" if before is None else f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
        )
        if before is not None:
            def endings(text):
                crlf = text.count("\r\n")
                return crlf, text.count("\n") - crlf, text.count("\r") - crlf, text.endswith(("\r", "\n"))

            if endings(before) != endings(after):
                diff.append(f"[line endings: before={endings(before)}, after={endings(after)}]")
        return "\n".join(diff)

    def _approve(self, action, preview):
        if not self.coding:
            raise ToolError("وضع البرمجة غير مفعّل")
        if not callable(self.approver):
            raise ApprovalDenied("لا توجد قناة موافقة تفاعلية")
        preview = safe_terminal_text(preview)
        fingerprint = hashlib.sha256(f"{action}\0{preview}".encode("utf-8")).digest()
        if fingerprint in self.denied_operations:
            raise ApprovalDenied("رفض المستخدم هذه العملية سابقًا")
        try:
            approved = self.approver(action, preview)
        except (EOFError, KeyboardInterrupt) as error:
            raise ApprovalDenied("تعذرت الموافقة على العملية") from error
        if approved is not True:
            self.denied_operations.add(fingerprint)
            raise ApprovalDenied("لم يوافق المستخدم على العملية")

    def _atomic_write(self, target, data, expected):
        relative = target.relative_to(self.root)
        if expected is None:
            target = self._resolve_new_file(relative)
        else:
            target = self._resolve(relative)
            if not target.is_file():
                raise ToolError("تغير الملف منذ عرض المعاينة")
            with target.open("rb") as source:
                current = source.read(self.max_file_bytes + 1)
            if current != expected:
                raise ToolError("تغير الملف منذ عرض المعاينة")
        temporary = None
        committed = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=".local-agent-", suffix=".tmp", delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if expected is None:
                if target.exists() or self._is_reparse(target):
                    raise ToolError("الملف موجود بالفعل")
                os.link(temporary, target)
                committed = True
            else:
                os.chmod(temporary, target.stat().st_mode)
                os.replace(temporary, target)
                temporary = None
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    if not committed:
                        raise

    def _write_with_snapshot(self, label, target, relative, data, expected):
        snapshot_id = None
        if self.snapshot_manager is not None:
            snapshot_id = self.snapshot_manager.take_snapshot(
                label, [relative], {relative: data}
            )
        try:
            self._atomic_write(target, data, expected)
        except Exception:
            if snapshot_id:
                self.snapshot_manager.discard(snapshot_id)
            raise

    def create_file(self, path, content):
        if not isinstance(content, str) or "\x00" in content:
            raise ToolError("محتوى الملف يجب أن يكون نص UTF-8")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ToolError("الملف أكبر من الحد المسموح للكتابة")
        target = self._resolve_new_file(path)
        relative = target.relative_to(self.root).as_posix()
        preview = self._diff(relative, None, content)
        operation_hash = hashlib.sha256(
            json.dumps(
                {"action": "create_file", "path": relative, "content": content},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self._approve("create_file", f"SHA256 العملية: {operation_hash}\n{preview}")
        self._write_with_snapshot("create_file", target, relative, encoded, None)
        self.originals.setdefault(relative, None)
        return {"status": "created", "path": relative, "bytes": len(encoded)}

    def replace_text(self, path, old_text, new_text):
        if (
            not isinstance(old_text, str)
            or not old_text
            or not isinstance(new_text, str)
            or "\x00" in new_text
        ):
            raise ToolError("نص الاستبدال غير صالح")
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError("الملف غير موجود")
        with target.open("rb") as source:
            original_bytes = source.read(self.max_file_bytes + 1)
        if b"\x00" in original_bytes or len(original_bytes) > self.max_file_bytes:
            raise ToolError("الملف ليس نصًا مدعومًا")
        try:
            before = original_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolError("الملف ليس نص UTF-8 صالحًا") from error
        if before.count(old_text) != 1:
            raise ToolError("يجب أن يظهر النص القديم مرة واحدة بالضبط")
        after = before.replace(old_text, new_text, 1)
        encoded = after.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ToolError("الملف أكبر من الحد المسموح للكتابة")
        relative = target.relative_to(self.root).as_posix()
        preview = self._diff(relative, before, after)
        operation_hash = hashlib.sha256(
            json.dumps(
                {
                    "action": "replace_text",
                    "path": relative,
                    "before_sha256": hashlib.sha256(original_bytes).hexdigest(),
                    "after_sha256": hashlib.sha256(encoded).hexdigest(),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self._approve("replace_text", f"SHA256 العملية: {operation_hash}\n{preview}")
        self._write_with_snapshot("replace_text", target, relative, encoded, original_bytes)
        self.originals.setdefault(relative, original_bytes)
        return {"status": "updated", "path": relative, "bytes": len(encoded)}

    def review_changes(self):
        changes = []
        for relative, original in sorted(self.originals.items()):
            target = self.root / relative
            if target.exists() or self._is_reparse(target):
                try:
                    current = self.read_file(relative)
                except ToolError as error:
                    changes.append(f"{relative}: تعذرت المراجعة الآمنة: {error}")
                    continue
            else:
                current = ""
            before = None if original is None else original.decode("utf-8", errors="replace")
            diff = self._diff(relative, before, current)
            if diff:
                changes.append(diff)
        review = "\n\n".join(changes) or "لا توجد تغييرات."
        encoded = review.encode("utf-8")
        limit = self.max_file_bytes * 4
        if len(encoded) > limit:
            marker = b"\n...[truncated]"
            encoded = encoded[: limit - len(marker)] + marker
            review = encoded.decode("utf-8", errors="replace")
        return review

    @staticmethod
    def _safe_environment():
        environment = {
            name: value
            for name in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if (value := os.environ.get(name))
        }
        environment.update(
            {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1"}
        )
        return environment

    def _terminate_process_tree(self, process):
        try:
            taskkill = Path(os.environ.get("SYSTEMROOT", "C:\\Windows")) / "System32" / "taskkill.exe"
            if os.name == "nt" and taskkill.is_file():
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    env=self._safe_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            elif os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def _run_bounded_process(self, argv, cwd, timeout):
        with self._process_lock:
            if self._closed:
                raise ToolError("أدوات مساحة العمل مغلقة")
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=self._safe_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except OSError as error:
            raise ToolError(f"تعذر تشغيل الأمر: {error}") from error
        with self._process_lock:
            if self._closed:
                self._terminate_process_tree(process)
                raise ToolError("ألغي الأمر أثناء إغلاق التطبيق")
            self._active_process = process

        captured = bytearray()
        truncated = False

        def drain_output():
            nonlocal truncated
            try:
                while chunk := process.stdout.read(8_192):
                    remaining = self.max_command_output_bytes - len(captured)
                    if remaining > 0:
                        captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
            except (OSError, ValueError):
                truncated = True

        reader = threading.Thread(target=drain_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None
            reader.join(timeout=1)
            if not reader.is_alive():
                process.stdout.close()
            raise
        except KeyboardInterrupt:
            self._terminate_process_tree(process)
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None
            reader.join(timeout=1)
            if not reader.is_alive():
                process.stdout.close()
            raise
        reader.join(timeout=max(0, deadline - time.monotonic()))
        if reader.is_alive():
            self._terminate_process_tree(process)
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None
            raise subprocess.TimeoutExpired(argv, timeout)
        process.stdout.close()
        output = bytes(captured)
        if truncated:
            marker = b"\n...[truncated]"
            output = (
                output[: self.max_command_output_bytes - len(marker)] + marker
                if self.max_command_output_bytes >= len(marker)
                else marker[: self.max_command_output_bytes]
            )
        with self._process_lock:
            if self._active_process is process:
                self._active_process = None
        return return_code, output.decode("utf-8", errors="replace"), truncated

    def close(self):
        with self._process_lock:
            self._closed = True
            process = self._active_process
        if process is not None:
            self._terminate_process_tree(process)

    @staticmethod
    def _resolve_executable(command):
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ToolError("اسم الأمر غير صالح")
        path = Path(command)
        if not path.is_absolute() and len(path.parts) > 1:
            raise ToolError("استخدم اسم أمر من PATH أو مسارًا مطلقًا")
        executable = str(path.resolve(strict=True)) if path.is_absolute() else shutil.which(command)
        if not executable:
            raise ToolError("الأمر غير موجود")
        executable = str(Path(executable).resolve(strict=True))
        name = Path(executable).name.casefold()
        if Path(executable).suffix.casefold() in {".bat", ".cmd"} or name in {
            "cmd.exe",
            "powershell.exe",
            "pwsh.exe",
        }:
            raise ToolError("مضيفات shell وملفات batch غير مسموحة")
        return executable

    def run_command(self, argv, cwd="."):
        if not self.allow_host_commands:
            raise ToolError("تشغيل أوامر المضيف غير مفعّل")
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
            or sum(len(item) for item in argv) > 8_192
        ):
            raise ToolError("argv يجب أن تكون قائمة نصية صالحة")
        working_directory = self._resolve(cwd)
        if not working_directory.is_dir():
            raise ToolError("cwd يجب أن يكون مجلدًا داخل مساحة العمل")
        relative_cwd = working_directory.relative_to(self.root).as_posix() or "."
        sandboxed = self.command_sandbox is not None
        exact_argv = (
            self.command_sandbox.wrap(argv, working_directory)
            if sandboxed
            else [self._resolve_executable(argv[0]), *argv[1:]]
        )
        execution_cwd = self.root if sandboxed else working_directory
        operation = {"argv": exact_argv, "cwd": relative_cwd}
        operation_hash = hashlib.sha256(
            json.dumps(operation, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        preview = (
            (
                "الأمر سيعمل داخل حاوية بلا شبكة وبربط مساحة العمل فقط.\n"
                if sandboxed
                else "تحذير: هذا الأمر يعمل بصلاحيات المستخدم، وليس داخل sandbox، وقد يقرأ خارج المشروع أو يصل إلى الشبكة.\n"
            )
            +
            f"SHA256 العملية: {operation_hash}\n"
            f"cwd: {relative_cwd}\nargv: {json.dumps(exact_argv, ensure_ascii=False)}"
        )
        self._approve("run_command", preview)
        try:
            return_code, output, truncated = self._run_bounded_process(
                exact_argv, execution_cwd, self.command_timeout
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError("انتهت المهلة المحددة للأمر") from error
        return {"status": "completed", "exit_code": return_code, "output": output, "truncated": truncated}

    def search_text(self, query, path=".", limit=50):
        if not isinstance(query, str) or not query.strip():
            raise ToolError("نص البحث مطلوب")
        results = []
        for file_path in self._files(path, TEXT_EXTENSIONS):
            try:
                content = self.read_file(file_path.relative_to(self.root))
            except ToolError:
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if query.casefold() in line.casefold():
                    relative = file_path.relative_to(self.root).as_posix()
                    results.append(f"{relative}:{line_number}: {line.strip()[:300]}")
                    if len(results) >= limit:
                        return results
        return results

    def semantic_search(self, query, client, model, path=".", limit=5):
        if not isinstance(query, str) or not query.strip():
            raise ToolError("نص البحث مطلوب")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ToolError("limit يجب أن يكون بين 1 و10")

        chunks = []
        chunk_limit = min(128, getattr(self.semantic_memory, "max_texts", 129) - 1)
        if chunk_limit < 1:
            raise ToolError("ذاكرة البحث لا تسمح بدفعة كافية")
        for file_path in self._files(path, TEXT_EXTENSIONS):
            try:
                content = self.read_file(file_path.relative_to(self.root))
            except ToolError:
                continue
            for start in range(0, len(content), self.chunk_chars):
                text = content[start : start + self.chunk_chars].strip()
                if text:
                    chunks.append({"path": file_path.relative_to(self.root).as_posix(), "text": text})
                if len(chunks) >= chunk_limit:
                    break
            if len(chunks) >= chunk_limit:
                break
        if not chunks:
            return []

        texts = [query, *[chunk["text"] for chunk in chunks]]
        vectors = (
            self.semantic_memory.embed_texts(texts, client, model)
            if self.semantic_memory is not None
            else client.embed(texts, model)
        )
        query_vector, document_vectors = vectors[0], vectors[1:]
        ranked = sorted(
            (
                {
                    **chunk,
                    "score": round(self._cosine(query_vector, vector), 4),
                }
                for chunk, vector in zip(chunks, document_vectors, strict=True)
            ),
            key=lambda item: item["score"],
            reverse=True,
        )
        return ranked[:limit]

    @staticmethod
    def _cosine(left, right):
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
            sum(value * value for value in right)
        )
        return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0

    def dispatch(self, name, arguments, client, embed_model):
        if not isinstance(arguments, dict):
            raise ToolError("معاملات الأداة يجب أن تكون كائن JSON")
        allowed_arguments = {
            "list_files": {"path"},
            "read_file": {"path"},
            "search_text": {"path", "query"},
            "semantic_search": {"limit", "path", "query"},
        }
        if self.coding:
            allowed_arguments.update(
                {
                    "create_file": {"path", "content"},
                    "replace_text": {"path", "old_text", "new_text"},
                    "review_changes": set(),
                }
            )
        if self.allow_host_commands:
            allowed_arguments["run_command"] = {"argv", "cwd"}
        if name not in allowed_arguments:
            raise ToolError(f"أداة غير مسموحة: {name}")
        if set(arguments) - allowed_arguments[name]:
            raise ToolError("معاملات غير معروفة للأداة")
        required_arguments = {
            "read_file": {"path"},
            "search_text": {"query"},
            "semantic_search": {"query"},
            "create_file": {"path", "content"},
            "replace_text": {"path", "old_text", "new_text"},
            "run_command": {"argv"},
        }
        if not required_arguments.get(name, set()) <= set(arguments):
            raise ToolError("معاملات مطلوبة مفقودة")
        if name == "list_files":
            return self.list_files(arguments.get("path", "."))
        if name == "read_file":
            return self.read_file(arguments.get("path", ""))
        if name == "search_text":
            return self.search_text(arguments.get("query", ""), arguments.get("path", "."))
        if name == "semantic_search":
            return self.semantic_search(
                arguments.get("query", ""),
                client,
                embed_model,
                arguments.get("path", "."),
                arguments.get("limit", 5),
            )
        if name == "create_file":
            return self.create_file(arguments.get("path", ""), arguments.get("content", ""))
        if name == "replace_text":
            return self.replace_text(
                arguments.get("path", ""),
                arguments.get("old_text", ""),
                arguments.get("new_text", ""),
            )
        if name == "review_changes":
            return self.review_changes()
        if name == "run_command":
            return self.run_command(arguments.get("argv", []), arguments.get("cwd", "."))
