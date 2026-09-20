"""Fail-closed orchestration for delegated coding tasks.

The module deliberately knows nothing about Tkinter or a specific model provider.  A
controller injects four small capabilities: execute, read the diff, review, and test.
That keeps the state machine deterministic and makes provider adapters replaceable.
"""

from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import secrets
from typing import Callable


class TaskPhase(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    REVIEWING = "reviewing"
    TESTING = "testing"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class GateResult:
    status: str
    summary: str = ""

    def __post_init__(self):
        if self.status not in {"pending", "passed", "failed", "skipped"}:
            raise ValueError("حالة البوابة غير صالحة")

    @classmethod
    def pending(cls):
        return cls("pending", "لم تبدأ بعد")

    @classmethod
    def passed(cls, summary):
        return cls("passed", _clean_text(summary, 16_000))

    @classmethod
    def failed(cls, summary):
        return cls("failed", _clean_text(summary, 16_000))

    @classmethod
    def skipped(cls, summary):
        return cls("skipped", _clean_text(summary, 16_000))


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    goal: str
    phase: TaskPhase = TaskPhase.IDLE
    revision: int = 0
    worker_summary: str = ""
    diff: str = ""
    review: GateResult = GateResult("pending", "لم تبدأ بعد")
    tests: GateResult = GateResult("pending", "لم تبدأ بعد")
    accepted: bool = False
    error: str = ""

    @property
    def can_accept(self):
        return (
            self.phase == TaskPhase.READY
            and self.review.status == "passed"
            and self.tests.status == "passed"
            and not self.accepted
        )


class DelegationEngine:
    """Runs a worker → independent review → tests pipeline."""

    def __init__(
        self,
        worker: Callable[[str], str],
        diff_reader: Callable[[], str],
        reviewer: Callable[[str, str, str], GateResult],
        test_runner: Callable[[], GateResult],
        on_update: Callable[[TaskSnapshot], None] | None = None,
    ):
        for callback in (worker, diff_reader, reviewer, test_runner):
            if not callable(callback):
                raise TypeError("كل مكوّن في مسار التفويض يجب أن يكون قابلًا للاستدعاء")
        self._worker = worker
        self._diff_reader = diff_reader
        self._reviewer = reviewer
        self._test_runner = test_runner
        self._on_update = on_update
        self._snapshot = TaskSnapshot(task_id=f"task-{secrets.token_hex(4)}", goal="")

    @property
    def snapshot(self):
        return self._snapshot

    def _publish(self, **changes):
        self._snapshot = replace(
            self._snapshot,
            revision=self._snapshot.revision + 1,
            **changes,
        )
        if self._on_update is not None:
            self._on_update(self._snapshot)
        return self._snapshot

    def run(self, goal):
        if self._snapshot.phase != TaskPhase.IDLE:
            raise RuntimeError("بدأت هذه المهمة بالفعل")
        clean_goal = _clean_text(goal, 8_000)
        if not clean_goal.strip():
            raise ValueError("هدف المهمة مطلوب")
        self._publish(goal=clean_goal.strip(), phase=TaskPhase.WORKING, error="")
        try:
            summary = _clean_text(self._worker(clean_goal.strip()), 32_000)
            diff = _clean_text(self._diff_reader(), 256_000)
            self._publish(
                phase=TaskPhase.REVIEWING,
                worker_summary=summary,
                diff=diff,
            )
            review = self._reviewer(clean_goal.strip(), diff, summary)
            if not isinstance(review, GateResult):
                raise TypeError("نتيجة المراجعة غير صالحة")
            if review.status != "passed":
                return self._publish(phase=TaskPhase.BLOCKED, review=review)
            self._publish(phase=TaskPhase.TESTING, review=review)
            tests = self._test_runner()
            if not isinstance(tests, GateResult):
                raise TypeError("نتيجة الاختبارات غير صالحة")
            phase = TaskPhase.READY if tests.status == "passed" else TaskPhase.BLOCKED
            return self._publish(phase=phase, tests=tests)
        except Exception as error:
            return self._publish(phase=TaskPhase.FAILED, error=_clean_text(error, 4_000))

    def accept(self):
        if not self._snapshot.can_accept:
            raise RuntimeError("لا يمكن اعتماد المهمة قبل نجاح المراجعة والاختبارات")
        return self._publish(phase=TaskPhase.ACCEPTED, accepted=True)


def detect_check_command(root):
    """Return one conservative project check command without executing it."""

    root = Path(root)
    if not root.is_dir():
        raise ValueError("مساحة العمل غير موجودة")
    if any(root.joinpath(name).is_file() for name in ("pyproject.toml", "pytest.ini")):
        return ["python", "-m", "pytest", "-q"], "."
    tests_dir = root / "tests"
    has_python_tests = any(root.glob("test_*.py")) or (
        tests_dir.is_dir() and next(tests_dir.glob("test*.py"), None) is not None
    )
    if has_python_tests:
        return ["python", "-m", "unittest"], "."
    package_file = root / "package.json"
    if package_file.is_file() and package_file.stat().st_size <= 128_000:
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            package = {}
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
            return ["npm", "test"], "."
    if root.joinpath("Cargo.toml").is_file():
        return ["cargo", "test", "--quiet"], "."
    if root.joinpath("go.mod").is_file():
        return ["go", "test", "./..."], "."
    return None


def snapshot_payload(snapshot):
    if not isinstance(snapshot, TaskSnapshot):
        raise TypeError("لقطة المهمة غير صالحة")
    return {
        "task_id": snapshot.task_id,
        "goal": snapshot.goal,
        "phase": snapshot.phase.value,
        "revision": snapshot.revision,
        "worker_summary": snapshot.worker_summary,
        "diff": snapshot.diff,
        "review": {
            "status": snapshot.review.status,
            "summary": snapshot.review.summary,
        },
        "tests": {
            "status": snapshot.tests.status,
            "summary": snapshot.tests.summary,
        },
        "accepted": snapshot.accepted,
        "can_accept": snapshot.can_accept,
        "error": snapshot.error,
    }


def _clean_text(value, limit):
    text = str(value or "")
    text = "".join(character if character >= " " or character in "\n\t" else "�" for character in text)
    return text if len(text) <= limit else text[: limit - 14] + "\n...[truncated]"
