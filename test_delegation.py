import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from delegation import (
    DelegationEngine,
    GateResult,
    TaskPhase,
    detect_check_command,
)


class DelegationEngineTests(unittest.TestCase):
    def test_successful_run_reaches_ready_and_requires_explicit_acceptance(self):
        events = []
        engine = DelegationEngine(
            worker=lambda goal: f"نفذت: {goal}",
            diff_reader=lambda: "--- a/app.py\n+++ b/app.py\n+print('ok')",
            reviewer=lambda _goal, _diff, _summary: GateResult.passed(
                "التغيير محدود وآمن"
            ),
            test_runner=lambda: GateResult.passed("12 passed"),
            on_update=events.append,
        )

        ready = engine.run("أضف السجل")

        self.assertEqual(ready.phase, TaskPhase.READY)
        self.assertEqual(ready.review.status, "passed")
        self.assertEqual(ready.tests.status, "passed")
        self.assertFalse(ready.accepted)
        accepted = engine.accept()
        self.assertEqual(accepted.phase, TaskPhase.ACCEPTED)
        self.assertTrue(accepted.accepted)
        self.assertEqual(events[-1], accepted)

    def test_reviewer_rejection_blocks_acceptance(self):
        engine = DelegationEngine(
            worker=lambda _goal: "تم التنفيذ",
            diff_reader=lambda: "+unsafe()",
            reviewer=lambda *_args: GateResult.failed("تغيير غير آمن"),
            test_runner=lambda: GateResult.passed("ok"),
        )

        blocked = engine.run("نفّذ تعديلًا")

        self.assertEqual(blocked.phase, TaskPhase.BLOCKED)
        self.assertEqual(blocked.review.status, "failed")
        self.assertEqual(blocked.tests.status, "pending")
        with self.assertRaises(RuntimeError):
            engine.accept()

    def test_failed_tests_block_acceptance_and_preserve_review_result(self):
        engine = DelegationEngine(
            worker=lambda _goal: "تم التنفيذ",
            diff_reader=lambda: "+safe_change()",
            reviewer=lambda *_args: GateResult.passed("لا توجد ملاحظات"),
            test_runner=lambda: GateResult.failed("1 failed"),
        )

        blocked = engine.run("عدّل الدالة")

        self.assertEqual(blocked.phase, TaskPhase.BLOCKED)
        self.assertEqual(blocked.review.status, "passed")
        self.assertEqual(blocked.tests.status, "failed")

    def test_exceptions_fail_closed_and_snapshots_are_immutable(self):
        engine = DelegationEngine(
            worker=lambda _goal: (_ for _ in ()).throw(ValueError("boom")),
            diff_reader=lambda: "unused",
            reviewer=lambda *_args: GateResult.passed("unused"),
            test_runner=lambda: GateResult.passed("unused"),
        )

        failed = engine.run("مهمة")

        self.assertEqual(failed.phase, TaskPhase.FAILED)
        self.assertIn("boom", failed.error)
        with self.assertRaises(FrozenInstanceError):
            failed.error = "changed"


class CheckDetectionTests(unittest.TestCase):
    def test_detects_supported_project_checks_without_running_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("pyproject.toml").write_text("[tool.pytest.ini_options]", encoding="utf-8")
            self.assertEqual(
                detect_check_command(root),
                (["python", "-m", "pytest", "-q"], "."),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
            self.assertEqual(detect_check_command(root), (["npm", "test"], "."))

    def test_returns_none_when_no_supported_check_is_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(detect_check_command(Path(temp_dir)))

    def test_javascript_tests_directory_does_not_shadow_package_script(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("tests").mkdir()
            root.joinpath("tests", "app.test.js").write_text("test('ok')", encoding="utf-8")
            root.joinpath("package.json").write_text(
                '{"scripts":{"test":"vitest"}}', encoding="utf-8"
            )
            self.assertEqual(detect_check_command(root), (["npm", "test"], "."))


if __name__ == "__main__":
    unittest.main()
