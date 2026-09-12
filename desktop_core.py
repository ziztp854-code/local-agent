import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from agent import DEFAULT_BASE_URL, DEFAULT_MODEL, LMStudioClient, LocalAgent
from mcp_client import MCPApprovalDenied, MCPRegistry
from semantic_memory import SemanticMemory
from session_store import SessionStore
from self_learning import SelfLearner
from skill_catalog import SkillCatalog, SkillError
from workspace_tools import (
    SENSITIVE_FILENAMES,
    ApprovalDenied,
    WorkspaceTools,
    safe_terminal_text,
)


SNAPSHOT_ID = re.compile(r"snap_[0-9]{20}_[0-9a-f]{8}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CONTAINER_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,190}@sha256:[0-9a-f]{64}\Z")
DEFAULT_CONTAINER_IMAGE = ""

LANGUAGE_LABELS = {
    "py": "Python",
    "js": "JavaScript",
    "ts": "TypeScript",
    "jsx": "React",
    "tsx": "React",
    "md": "Markdown",
    "json": "JSON",
    "html": "HTML",
    "css": "CSS",
    "c": "C",
    "cpp": "C++",
    "h": "C",
    "cs": "C#",
    "java": "Java",
    "go": "Go",
    "rs": "Rust",
    "rb": "Ruby",
    "php": "PHP",
    "sh": "Shell",
    "ps1": "PowerShell",
    "sql": "SQL",
    "yml": "YAML",
    "yaml": "YAML",
    "txt": "نصوص",
}

SKIP_DIRS = {
    ".git",
    ".local-agent",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    ".idea",
    ".vscode",
}


def is_reparse_point(path):
    try:
        return path.is_symlink() or bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (AttributeError, OSError):
        return path.is_symlink()


class AppSettings:
    MAX_BYTES = 16_000

    VALIDATORS = {
        "dark_theme": lambda value: type(value) is bool,
        "workspace": lambda value: isinstance(value, str) and len(value) <= 1024,
        "mode": lambda value: isinstance(value, str) and len(value) <= 32,
        "session": lambda value: isinstance(value, str) and len(value) <= 128,
        "model": lambda value: isinstance(value, str) and len(value) <= 128,
        "base_url": lambda value: isinstance(value, str) and len(value) <= 512,
    }

    def __init__(self, path=None):
        base = os.getenv("LOCALAPPDATA") or str(Path.home())
        self.path = Path(path) if path else Path(base, "local-agent", "settings.json")

    def load(self):
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        if len(raw) > self.MAX_BYTES:
            return {}
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: data[key]
            for key, validator in self.VALIDATORS.items()
            if key in data and validator(data[key])
        }

    def save(self, values):
        clean = {
            key: values[key]
            for key, validator in self.VALIDATORS.items()
            if key in values and validator(values[key])
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                dir=self.path.parent,
                prefix=".settings-",
                suffix=".tmp",
                delete=False,
            )
            with handle:
                handle.write(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            os.replace(handle.name, self.path)
        except OSError:
            return False
        return True


def describe_workspace(root, max_files=4000):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("مساحة العمل يجب أن تكون مجلدًا")
    files = 0
    directories = 0
    ext_counts = {}
    truncated = False
    for directory, names, filenames in os.walk(root):
        base = Path(directory)
        names[:] = [
            name for name in names
            if name not in SKIP_DIRS and not is_reparse_point(base / name)
        ]
        directories += len(names)
        for name in filenames:
            path = base / name
            if is_reparse_point(path) or not path.is_file():
                continue
            if name in SENSITIVE_FILENAMES or name == ".env" or name.startswith(".env."):
                continue
            if files >= max_files:
                truncated = True
                break
            files += 1
            suffix = path.suffix.lstrip(".").casefold()
            ext_counts[suffix] = ext_counts.get(suffix, 0) + 1
        if truncated:
            names[:] = []
    ranked = sorted(ext_counts.items(), key=lambda item: (-item[1], item[0]))
    top = [
        (LANGUAGE_LABELS.get(suffix, f".{suffix}" if suffix else "بدون امتداد"), count)
        for suffix, count in ranked[:3]
        if count
    ]
    return {"files": files, "directories": directories, "truncated": truncated, "top": top}


def build_workspace_briefing(stats):
    if not stats["files"]:
        return (
            "مساحة العمل فارغة. في وضع «برمجة» اطلب إنشاء ملف جديد وسيُعرض عليك فرق "
            "الموافقة قبل أي كتابة."
        )
    parts = [f"مساحة العمل جاهزة: {stats['files']} ملفًا"]
    if stats["directories"]:
        parts.append(f"في {stats['directories']} مجلدًا")
    summary = " ".join(parts) + "."
    if stats["top"]:
        labels = "، ".join(f"{name} ({count})" for name, count in stats["top"])
        summary += f" أغلب العمل فيها: {labels}."
    if stats["truncated"]:
        summary += " الفحص مقتطع عند الحد الأقصى."
    return (
        summary
        + "\nجرب زر «لخّص المشروع» أسفل المربع، أو اسأل عن ملف بعينه. "
        "لن يكتب الوكيل شيئًا دون موافقتك."
    )


def probe_model_server(base_url, timeout=2):
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "عنوان غير صالح"
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False, "يُسمح بالخادم المحلي فقط"
    request = Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with build_opener().open(request, timeout=timeout) as response:
            return True, f"HTTP {getattr(response, 'status', 200)}"
    except HTTPError as error:
        return True, f"HTTP {error.code}"
    except (URLError, OSError, ValueError) as error:
        return False, safe_terminal_text(error)


def list_models(base_url, timeout=3):
    """أعِد قائمة معرّفات النماذج المتاحة من خادم LM Studio المحلي (GET /models).

    محلي فقط تماشيًا مع سياسة الوكيل؛ أي فشل يُعيد قائمة فارغة دون رفع استثناء.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return []
    request = Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with build_opener().open(request, timeout=timeout) as response:
            data = response.read(1_000_000)
    except (HTTPError, URLError, OSError, ValueError):
        return []
    try:
        payload = json.loads(data.decode("utf-8"))
        items = payload["data"] if isinstance(payload, dict) else []
        ids = [
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
        ]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return []
    return sorted(dict.fromkeys(ids))


class SnapshotManager:
    def __init__(self, workspace_root: Path, max_snapshots: int = 10):
        self.root = Path(workspace_root).expanduser().resolve(strict=True)
        if not self.root.is_dir() or type(max_snapshots) is not int or max_snapshots < 1:
            raise ValueError("Invalid snapshot settings")
        self.snapshot_dir = self.root / ".local-agent" / "snapshots"
        self.max_snapshots = max_snapshots
        self._lock = threading.Lock()

    @staticmethod
    def _is_reparse(path):
        try:
            return path.is_symlink() or bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (AttributeError, OSError):
            return path.is_symlink()

    def _target(self, value):
        candidate = Path(value)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise ValueError("Snapshot path must stay inside the workspace")
        target = self.root.joinpath(*candidate.parts)
        parent = target.parent.resolve(strict=True)
        if parent != self.root and self.root not in parent.parents:
            raise ValueError("Snapshot path escaped the workspace")
        current = self.root
        for part in candidate.parts[:-1]:
            current /= part
            if self._is_reparse(current):
                raise ValueError("Snapshot paths cannot cross links")
        if (target.exists() or target.is_symlink()) and self._is_reparse(target):
            raise ValueError("Snapshot paths cannot be links")
        return target, candidate.as_posix()

    def _workspace_files(self):
        files = []
        for directory, names, filenames in os.walk(self.root):
            base = Path(directory)
            names[:] = [
                name
                for name in names
                if name != ".local-agent" and not self._is_reparse(base / name)
            ]
            for name in filenames:
                path = base / name
                if path.is_file() and not self._is_reparse(path):
                    files.append(path.relative_to(self.root).as_posix())
        return files

    def _snapshot_store(self, create=False):
        metadata_dir = self.root / ".local-agent"
        for path in (metadata_dir, self.snapshot_dir):
            if path.exists() or path.is_symlink():
                if self._is_reparse(path) or not path.is_dir():
                    raise ValueError("Snapshot store cannot cross links")
            elif create:
                path.mkdir()
            else:
                return False
            if self._is_reparse(path):
                raise ValueError("Snapshot store cannot cross links")
        if self.snapshot_dir.resolve(strict=True) != self.snapshot_dir:
            raise ValueError("Snapshot store escaped the workspace")
        return True

    def _remove_snapshot(self, directory):
        self._snapshot_store()
        if (
            directory.parent != self.snapshot_dir
            or not SNAPSHOT_ID.fullmatch(directory.name)
            or self._is_reparse(directory)
            or not directory.is_dir()
        ):
            raise ValueError("Invalid snapshot directory")
        for child in directory.iterdir():
            if self._is_reparse(child) or not child.is_file() or child.parent != directory:
                raise ValueError("Snapshot directory contains unsafe entries")
            child.unlink()
        directory.rmdir()

    def take_snapshot(self, label: str, paths=None, expected_current=None) -> str:
        if not isinstance(label, str) or not label.strip() or len(label) > 128:
            raise ValueError("Invalid snapshot label")
        selected = self._workspace_files() if paths is None else list(dict.fromkeys(paths))
        expected_current = expected_current or {}
        entries = []
        contents = []
        for index, value in enumerate(selected):
            target, relative = self._target(value)
            if target.exists() and not target.is_file():
                raise ValueError("Snapshots support regular files only")
            data = target.read_bytes() if target.exists() else None
            expected = expected_current.get(relative, data)
            if not isinstance(expected, bytes):
                raise ValueError("Expected snapshot content must be bytes")
            entries.append(
                {
                    "path": relative,
                    "exists": data is not None,
                    "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
                    "expected_sha256": (
                        hashlib.sha256(expected).hexdigest() if expected is not None else None
                    ),
                    "content": f"{index}.bin" if data is not None else None,
                }
            )
            contents.append(data)
        with self._lock:
            self._snapshot_store(create=True)
            snapshot_id = f"snap_{time.time_ns():020d}_{secrets.token_hex(4)}"
            target_dir = self.snapshot_dir / snapshot_id
            target_dir.mkdir()
            try:
                for entry, data in zip(entries, contents, strict=True):
                    if data is not None:
                        (target_dir / entry["content"]).write_bytes(data)
                manifest = {"version": 1, "label": label.strip(), "files": entries}
                (target_dir / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
                )
            except Exception:
                self._remove_snapshot(target_dir)
                raise
            snapshots = sorted(
                path
                for path in self.snapshot_dir.iterdir()
                if path.is_dir() and SNAPSHOT_ID.fullmatch(path.name)
            )
            for stale in snapshots[: -self.max_snapshots]:
                self._remove_snapshot(stale)
            return snapshot_id

    def _snapshot(self, snapshot_id=None):
        if not self._snapshot_store():
            return None
        if snapshot_id is not None:
            if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
                raise ValueError("Invalid snapshot id")
            candidate = self.snapshot_dir / snapshot_id
        else:
            candidates = (
                sorted(
                    path
                    for path in self.snapshot_dir.iterdir()
                    if path.is_dir()
                    and SNAPSHOT_ID.fullmatch(path.name)
                    and not self._is_reparse(path)
                )
            )
            if not candidates:
                return None
            candidate = candidates[-1]
        if not candidate.is_dir() or self._is_reparse(candidate):
            raise ValueError("Snapshot does not exist")
        return candidate

    def _load(self, snapshot_id=None):
        directory = self._snapshot(snapshot_id)
        if directory is None:
            return None, None, None
        try:
            manifest_path = directory / "manifest.json"
            if self._is_reparse(manifest_path) or not manifest_path.is_file():
                raise ValueError
            raw = manifest_path.read_bytes()
            if len(raw) > 1_000_000:
                raise ValueError
            manifest = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("Snapshot manifest is invalid") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 1
            or not isinstance(manifest.get("label"), str)
            or not isinstance(manifest.get("files"), list)
        ):
            raise ValueError("Snapshot manifest is invalid")
        return directory, manifest, hashlib.sha256(raw).hexdigest()

    def prepare_rollback(self, snapshot_id=None):
        directory, manifest, digest = self._load(snapshot_id)
        if directory is None:
            return None, None, ""
        paths = [entry.get("path", "?") for entry in manifest["files"]]
        preview = (
            f"نقطة الاستعادة: {directory.name}\nالوصف: {manifest['label']}\n"
            f"الملفات: {json.dumps(paths, ensure_ascii=False)}\nSHA256: {digest}"
        )
        return directory.name, digest, preview

    def preview(self, snapshot_id=None):
        return self.prepare_rollback(snapshot_id)[2]

    def rollback(self, snapshot_id: str | None = None, manifest_sha256=None) -> bool:
        with self._lock:
            directory, manifest, actual_manifest_hash = self._load(snapshot_id)
            if directory is None:
                return False
            if manifest_sha256 is not None and actual_manifest_hash != manifest_sha256:
                raise RuntimeError("Snapshot changed after approval")
            restore = []
            seen = set()
            for index, entry in enumerate(manifest["files"]):
                if not isinstance(entry, dict) or set(entry) != {
                    "path", "exists", "sha256", "expected_sha256", "content"
                }:
                    raise ValueError("Snapshot entry is invalid")
                target, relative = self._target(entry["path"])
                expected_hash = entry["expected_sha256"]
                if (
                    relative in seen
                    or type(entry["exists"]) is not bool
                    or not isinstance(expected_hash, str)
                    or not SHA256.fullmatch(expected_hash)
                ):
                    raise ValueError("Snapshot entry is invalid")
                seen.add(relative)
                current = target.read_bytes() if target.is_file() else None
                current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
                if current_hash != expected_hash:
                    raise RuntimeError(f"رفض التراجع لأن الملف تغير لاحقًا: {relative}")
                if entry["exists"]:
                    if (
                        entry["content"] != f"{index}.bin"
                        or not isinstance(entry["sha256"], str)
                        or not SHA256.fullmatch(entry["sha256"])
                    ):
                        raise ValueError("Snapshot content is invalid")
                    content_path = directory / entry["content"]
                    if (
                        content_path.parent != directory
                        or not content_path.is_file()
                        or self._is_reparse(content_path)
                    ):
                        raise ValueError("Snapshot content is invalid")
                    data = content_path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        raise ValueError("Snapshot content hash is invalid")
                else:
                    if entry["sha256"] is not None or entry["content"] is not None:
                        raise ValueError("Snapshot entry is invalid")
                    data = None
                restore.append((relative, data, expected_hash))
            completed = []
            try:
                for relative, data, expected_hash in restore:
                    target, checked_relative = self._target(relative)
                    current = target.read_bytes() if target.is_file() else None
                    current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
                    if current_hash != expected_hash:
                        raise RuntimeError(
                            f"رفض التراجع لأن الملف تغير لاحقًا: {checked_relative}"
                        )
                    if not target.is_file() or self._is_reparse(target):
                        raise ValueError("Rollback target is not a regular file")
                    backup_handle = tempfile.NamedTemporaryFile(
                        dir=target.parent,
                        prefix=".local-agent-rollback-",
                        suffix=".bak",
                        delete=False,
                    )
                    backup_handle.close()
                    backup = Path(backup_handle.name)
                    backup.unlink()
                    restored = None
                    try:
                        if data is not None:
                            with tempfile.NamedTemporaryFile(
                                dir=target.parent,
                                prefix=".local-agent-restore-",
                                suffix=".tmp",
                                delete=False,
                            ) as output:
                                output.write(data)
                                output.flush()
                                os.fsync(output.fileno())
                                restored = Path(output.name)
                        self._target(relative)
                        os.replace(target, backup)
                        moved_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
                        if moved_hash != expected_hash:
                            if not target.exists():
                                os.link(backup, target)
                                backup.unlink()
                            raise RuntimeError(
                                f"رفض التراجع لأن الملف تغير لاحقًا: {checked_relative}"
                            )
                        if restored is not None:
                            os.link(restored, target)
                        completed.append(
                            (
                                target,
                                backup,
                                hashlib.sha256(data).hexdigest() if data is not None else None,
                            )
                        )
                    except Exception:
                        if backup.exists() and not target.exists():
                            os.replace(backup, target)
                        raise
                    finally:
                        if restored is not None and restored.exists():
                            restored.unlink()
            except Exception as error:
                recovery = []
                for target, backup, installed_hash in reversed(completed):
                    current = target.read_bytes() if target.is_file() else None
                    current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
                    if current_hash == installed_hash:
                        if target.exists():
                            target.unlink()
                        os.replace(backup, target)
                    else:
                        recovery.append(str(backup))
                if recovery:
                    raise RuntimeError(
                        "Rollback conflict; preserved recovery files: " + ", ".join(recovery)
                    ) from error
                raise
            for _target, backup, _installed_hash in completed:
                try:
                    backup.unlink()
                except OSError:
                    pass
            self._remove_snapshot(directory)
            return True

    def discard(self, snapshot_id):
        with self._lock:
            directory = self._snapshot(snapshot_id)
            if directory is not None:
                self._remove_snapshot(directory)


class ContainerSandbox:
    def __init__(self, workspace_root, engine, image=DEFAULT_CONTAINER_IMAGE):
        self.root = Path(workspace_root).expanduser().resolve(strict=True)
        self.engine = str(engine).strip().casefold()
        if self.engine == "wsl":
            raise ValueError("WSL لا يعزل بقية ملفات المضيف؛ استخدم Docker أو Podman")
        if self.engine not in {"docker", "podman"}:
            raise ValueError("محرك العزل يجب أن يكون docker أو podman")
        if not isinstance(image, str) or not CONTAINER_IMAGE.fullmatch(image):
            raise ValueError("Container image must use an immutable sha256 digest")
        self.image = image

    def wrap(self, argv, cwd="."):
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
        ):
            raise ValueError("argv غير صالح")
        if Path(argv[0]).is_absolute() or len(Path(argv[0]).parts) != 1:
            raise ValueError("استخدم اسم الأمر داخل الحاوية دون مسار مضيف")
        working = Path(cwd)
        working = working if working.is_absolute() else self.root / working
        working = working.resolve(strict=True)
        if working != self.root and self.root not in working.parents:
            raise ValueError("cwd خارج مساحة العمل")
        engine = shutil.which(self.engine)
        if not engine:
            raise ValueError("محرك الحاوية غير موجود")
        relative = working.relative_to(self.root).as_posix()
        container_cwd = "/workspace" + (f"/{relative}" if relative != "." else "")
        return [
            str(Path(engine).resolve()),
            "run", "--rm", "--pull=never", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256",
            "--memory=1g", "--cpus=2", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--tmpfs", "/workspace/.local-agent:rw,noexec,nosuid,size=1m",
            "-v", f"{self.root}:/workspace:rw", "-w", container_cwd, self.image, *argv,
        ]


@dataclass(frozen=True)
class DesktopConfig:
    workspace: Path
    mode: str
    session: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    mcp_config: Path | None = None
    semantic_memory: bool = False
    mcp_fingerprint: str = ""
    container_engine: str = ""

    @classmethod
    def parse(
        cls,
        workspace,
        mode,
        session="",
        model=DEFAULT_MODEL,
        base_url=DEFAULT_BASE_URL,
        mcp_config="",
        semantic_memory=False,
        container_engine="",
    ):
        if mode not in {"read", "coding", "host"}:
            raise ValueError("وضع التشغيل غير صالح")
        if isinstance(workspace, str) and not workspace.strip():
            raise ValueError("مجلد مساحة العمل مطلوب")
        try:
            root = Path(workspace).expanduser().resolve(strict=True)
        except (OSError, TypeError) as error:
            raise ValueError("مجلد مساحة العمل غير موجود") from error
        if not root.is_dir():
            raise ValueError("مساحة العمل يجب أن تكون مجلدًا")
        if not isinstance(session, str):
            raise ValueError("اسم الجلسة يجب أن يكون نصًا")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("اسم النموذج مطلوب")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("عنوان LM Studio مطلوب")
        if type(semantic_memory) is not bool:
            raise ValueError("إعداد الذاكرة غير صالح")
        clean_session = session.strip()
        if semantic_memory and not clean_session:
            raise ValueError("اسم الجلسة مطلوب لتفعيل الذاكرة الدلالية")
        if not isinstance(container_engine, str):
            raise ValueError("محرك العزل غير صالح")
        clean_engine = container_engine.strip().casefold()
        if clean_engine not in {"", "docker", "podman"}:
            raise ValueError("محرك العزل غير صالح")
        config_path = None
        config_fingerprint = ""
        if mcp_config:
            try:
                config_path = Path(str(mcp_config).strip()).expanduser().resolve(strict=True)
            except (OSError, TypeError) as error:
                raise ValueError("ملف إعداد MCP غير موجود") from error
            if not config_path.is_file():
                raise ValueError("إعداد MCP يجب أن يكون ملفًا")
            try:
                with config_path.open("rb") as source:
                    config_bytes = source.read(64_001)
                if len(config_bytes) > 64_000:
                    raise ValueError("ملف إعداد MCP كبير جدًا")
                config_fingerprint = hashlib.sha256(config_bytes).hexdigest()
            except OSError as error:
                raise ValueError("تعذر قراءة ملف إعداد MCP") from error
        return cls(
            workspace=root,
            mode=mode,
            session=clean_session,
            model=model.strip(),
            base_url=base_url.strip(),
            mcp_config=config_path,
            semantic_memory=semantic_memory,
            mcp_fingerprint=config_fingerprint,
            container_engine=clean_engine,
        )

    @property
    def coding(self):
        return self.mode != "read"

    @property
    def allow_host_commands(self):
        return self.mode == "host"


def build_local_agent(config, approver):
    client = LMStudioClient(config.base_url, api_key=os.getenv("LM_STUDIO_API_KEY"))
    embedding_cache = SemanticMemory.for_workspace(config.workspace, "workspace")
    workspace = WorkspaceTools(
        config.workspace,
        coding=config.coding,
        allow_host_commands=config.allow_host_commands,
        approver=approver,
        semantic_memory=embedding_cache,
    )
    workspace.snapshot_manager = SnapshotManager(config.workspace) if config.coding else None
    workspace.command_sandbox = (
        ContainerSandbox(
            config.workspace,
            config.container_engine,
            os.getenv("LOCAL_AGENT_CONTAINER_IMAGE", DEFAULT_CONTAINER_IMAGE),
        )
        if config.container_engine
        else None
    )
    session = SessionStore(workspace.root, config.session) if config.session else None
    registry = (
        MCPRegistry(config.mcp_config, approver, workspace.root)
        if config.mcp_config
        else None
    )
    memory = (
        SemanticMemory(session.path.with_suffix(".memory.sqlite"))
        if config.semantic_memory
        else None
    )
    # تعلّم ذاتي دائم لكل مساحة عمل، مستقل عن الجلسة، ويعمل تلقائيًا.
    try:
        learner = SelfLearner(
            SemanticMemory.for_workspace(config.workspace, "learned")
        )
    except (ValueError, RuntimeError):
        learner = None
    try:
        skill_catalog = SkillCatalog.default()
    except SkillError:
        skill_catalog = None
    return LocalAgent(
        client,
        workspace,
        model=config.model,
        session=session,
        mcp_registry=registry,
        memory=memory,
        skill_catalog=skill_catalog,
        learner=learner,
    )


@dataclass(eq=False)
class ApprovalRequest:
    action: str
    preview: str
    response: Queue


class ApprovalBroker:
    def __init__(self):
        self._requests = Queue()
        self._closed = threading.Event()
        self._pending = []
        self._lock = threading.Lock()

    def ask(self, action, preview):
        if self._closed.is_set():
            return False
        response = Queue(maxsize=1)
        request = ApprovalRequest(action, preview, response)
        with self._lock:
            if self._closed.is_set():
                return False
            self._pending.append(response)
            self._requests.put(request)
        try:
            while not self._closed.is_set():
                try:
                    approved = response.get(timeout=0.1) is True
                    with self._lock:
                        return approved and not self._closed.is_set()
                except Empty:
                    continue
            return False
        finally:
            with self._lock:
                if response in self._pending:
                    self._pending.remove(response)

    def poll(self, timeout=0):
        try:
            return self._requests.get(timeout=timeout) if timeout else self._requests.get_nowait()
        except Empty:
            return None

    def resolve(self, request, approved):
        with self._lock:
            open_for_approval = not self._closed.is_set()
            try:
                request.response.put_nowait(approved is True and open_for_approval)
                return open_for_approval
            except Full:
                return False

    def close(self):
        with self._lock:
            self._closed.set()
            pending = list(self._pending)
        for response in pending:
            try:
                response.put_nowait(False)
            except Full:
                pass


class AgentController:
    def __init__(self, agent_factory=build_local_agent, approvals=None):
        self.agent_factory = agent_factory
        self.approvals = approvals or ApprovalBroker()
        self._agent = None
        self._config = None
        self._events = Queue()
        self._worker = None
        self._closers = []
        self._busy = False
        self._closing = False
        self._lock = threading.Lock()

    @property
    def agent(self):
        return self._agent

    @property
    def config(self):
        return self._config

    @property
    def worker(self):
        return self._worker

    @property
    def busy(self):
        with self._lock:
            return self._busy

    @property
    def closing(self):
        return self._closing

    @property
    def ready_to_close(self):
        worker_done = self._worker is None or not self._worker.is_alive()
        return worker_done and not any(thread.is_alive() for thread in self._closers)

    def _close_agent_async(self, agent):
        close = getattr(agent, "close", None)
        if not callable(close):
            return

        def close_safely():
            try:
                close()
            except Exception as error:  # Cleanup boundary; report without blocking Tk.
                self._events.put(("error", safe_terminal_text(error)))

        thread = threading.Thread(
            target=close_safely,
            name="local-agent-cleanup",
            daemon=False,
        )
        self._closers = [item for item in self._closers if item.is_alive()]
        self._closers.append(thread)
        thread.start()

    def configure(self, config):
        if self._closing:
            raise RuntimeError("التطبيق قيد الإغلاق")
        if self.busy:
            raise RuntimeError("انتظر انتهاء الطلب الحالي")
        if self._agent is not None and config == self._config:
            return [dict(message) for message in self._agent.history[1:]]
        agent = self.agent_factory(config, self.approvals.ask)
        previous = self._agent
        self._agent = agent
        self._config = config
        self._close_agent_async(previous)
        return [dict(message) for message in agent.history[1:]]

    def submit(self, prompt, image_paths=()):
        if not isinstance(prompt, str) or not prompt.strip() or self._agent is None:
            return False
        with self._lock:
            if self._busy or self._closing:
                return False
            self._busy = True
            worker = threading.Thread(
                target=self._run,
                args=(prompt.strip(), tuple(image_paths)),
                name="local-agent-worker",
                daemon=False,
            )
            self._worker = worker
        worker.start()
        return True

    def _run(self, prompt, image_paths):
        try:
            stream = getattr(self._agent, "answer_stream", None)
            if callable(stream):
                events = stream(prompt, image_paths) if image_paths else stream(prompt)
                for event in events:
                    if not isinstance(event, dict):
                        raise RuntimeError("حدث بث غير صالح")
                    event_type = event.get("type")
                    if event_type == "token":
                        self._events.put(("chunk", safe_terminal_text(event.get("delta", ""))))
                    elif event_type == "thought":
                        self._events.put(("thought", safe_terminal_text(event.get("delta", ""))))
                    elif event_type == "tool_start":
                        self._events.put(("status", f"تشغيل الأداة: {safe_terminal_text(event.get('name', ''))}"))
                    elif event_type == "tool_end":
                        self._events.put(("status", f"انتهت الأداة: {safe_terminal_text(event.get('name', ''))}"))
            else:
                answer = (
                    self._agent.answer(prompt, image_paths)
                    if image_paths
                    else self._agent.answer(prompt)
                )
                self._events.put(("answer", answer))
        except ApprovalDenied as error:
            self._events.put(("denied", safe_terminal_text(error)))
        except Exception as error:  # GUI boundary: keep the app alive on model/tool failures.
            self._events.put(("error", safe_terminal_text(error)))
        finally:
            self._events.put(("done", ""))

    def poll_events(self):
        events = []
        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            events.append(event)
            if event[0] == "done":
                with self._lock:
                    self._busy = False
        return events

    def review_changes(self):
        if self._agent is None:
            raise RuntimeError("طبّق الإعدادات أولًا")
        if self.busy:
            raise RuntimeError("انتظر انتهاء الطلب الحالي")
        return self._agent.workspace.review_changes()

    def rollback(self, snapshot_id=None):
        manager = getattr(getattr(self._agent, "workspace", None), "snapshot_manager", None)
        if manager is None:
            return False
        with self._lock:
            if self._busy or self._closing:
                return False
            self._busy = True
            worker = threading.Thread(
                target=self._run_rollback,
                args=(manager, snapshot_id),
                name="local-agent-rollback",
                daemon=False,
            )
            self._worker = worker
        worker.start()
        return True

    def _run_rollback(self, manager, snapshot_id):
        try:
            selected_id, manifest_hash, preview = manager.prepare_rollback(snapshot_id)
            if not preview:
                self._events.put(("status", "لا توجد نقطة استعادة"))
            elif not self.approvals.ask("rollback", preview):
                raise ApprovalDenied("لم تتم الموافقة على التراجع")
            elif manager.rollback(selected_id, manifest_hash):
                self._events.put(("status", "تم التراجع عن آخر تعديل"))
        except ApprovalDenied as error:
            self._events.put(("denied", safe_terminal_text(error)))
        except Exception as error:
            self._events.put(("error", safe_terminal_text(error)))
        finally:
            self._events.put(("done", ""))

    def update_mcp_server(self, name, enabled, config=None):
        config = config or self._config
        if config is None or config.mcp_config is None:
            return False
        with self._lock:
            if self._busy or self._closing:
                return False
            self._busy = True
            worker = threading.Thread(
                target=self._run_mcp_update,
                args=(config, name, enabled),
                name="local-agent-mcp-config",
                daemon=False,
            )
            self._worker = worker
        worker.start()
        return True

    def _run_mcp_update(self, config, name, enabled):
        registry = None
        try:
            registry = MCPRegistry(config.mcp_config, self.approvals.ask, config.workspace)
            (registry.enable_server if enabled else registry.disable_server)(name)
            registry.save_config()
            self._events.put(("status", "تم حفظ إعداد خادم MCP"))
        except MCPApprovalDenied as error:
            self._events.put(("denied", safe_terminal_text(error)))
        except Exception as error:
            self._events.put(("error", safe_terminal_text(error)))
        finally:
            if registry is not None:
                registry.close()
            self._events.put(("done", ""))

    def request_close(self):
        self._closing = True
        self.approvals.close()
        self._close_agent_async(self._agent)
