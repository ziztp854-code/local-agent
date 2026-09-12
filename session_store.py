import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from workspace_tools import ToolError, WINDOWS_RESERVED_NAMES


class SessionStore:
    def __init__(
        self,
        root,
        name,
        max_bytes=1_000_000,
        max_messages=100,
        state_root=None,
    ):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 64
            or not all(character.isalnum() or character in "-_" for character in name)
            or name.casefold() in WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("Session name may contain only letters, numbers, - and _")
        if type(max_bytes) is not int or max_bytes < 1 or type(max_messages) is not int or max_messages < 1:
            raise ValueError("Session limits must be positive integers")
        self.max_bytes = max_bytes
        self.max_messages = max_messages
        if state_root is None:
            platform_state = os.getenv("LOCALAPPDATA") or os.getenv("XDG_STATE_HOME")
            state_root = (
                Path(platform_state) if platform_state else Path.home() / ".local" / "state"
            ) / "local-agent" / "sessions"
        state_root = Path(state_root).expanduser().resolve(strict=False)
        session_id = hashlib.sha256(
            f"{os.path.normcase(str(self.root))}\0{name}".encode("utf-8")
        ).hexdigest()
        self.path = state_root / f"{session_id}.json"

    @staticmethod
    def _is_reparse(path):
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        return path.is_symlink() or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def _validate(self, history):
        if not isinstance(history, list) or len(history) > self.max_messages:
            raise ToolError("ملف الجلسة غير صالح")
        for message in history:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") not in {"user", "assistant"}
                or not isinstance(message.get("content"), str)
            ):
                raise ToolError("ملف الجلسة غير صالح")
        return history

    def load(self):
        if not self.path.exists():
            return []
        if self._is_reparse(self.path.parent) or self._is_reparse(self.path):
            raise ToolError("مسار الجلسة غير آمن")
        try:
            with self.path.open("rb") as source:
                data = source.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise ToolError("ملف الجلسة أكبر من الحد المسموح")
            history = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ToolError("ملف الجلسة غير صالح") from error
        return self._validate(history)

    def save(self, history):
        clean_history = self._validate(history)
        data = json.dumps(clean_history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > self.max_bytes:
            raise ToolError("ملف الجلسة أكبر من الحد المسموح")
        state_dir = self.path.parent
        if state_dir.exists() and (not state_dir.is_dir() or self._is_reparse(state_dir)):
            raise ToolError("مسار الجلسة غير آمن")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=state_dir, prefix=".session-", suffix=".tmp", delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

