import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import ToolError, WorkspaceTools


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "alpha.txt").write_text("تفاحة حمراء", encoding="utf-8")
        (self.root / "beta.txt").write_text("بحر أزرق", encoding="utf-8")
        self.tools = WorkspaceTools(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_file_tools_are_confined_to_the_workspace(self):
        (self.root / ".coverage").write_bytes(b"runtime data")
        self.assertEqual(self.tools.list_files("."), ["alpha.txt", "beta.txt"])
        self.assertEqual(self.tools.read_file("alpha.txt"), "تفاحة حمراء")
        self.assertEqual(self.tools.search_text("أزرق"), ["beta.txt:1: بحر أزرق"])

        with self.assertRaises(ToolError):
            self.tools.read_file("../outside.txt")

    def test_semantic_search_uses_embeddings_to_rank_chunks(self):
        class FakeEmbeddingClient:
            def embed(self, texts, model):
                vectors = []
                for text in texts:
                    vectors.append([0.0, 1.0] if "بحر" in text else [1.0, 0.0])
                return vectors

        results = self.tools.semantic_search(
            "أين البحر؟", FakeEmbeddingClient(), "nomic", limit=1
        )

        self.assertEqual(results[0]["path"], "beta.txt")
        self.assertEqual(results[0]["text"], "بحر أزرق")

    def test_unsafe_or_invalid_file_inputs_fail_closed(self):
        (self.root / "binary.bin").write_bytes(b"a\x00b")
        (self.root / "large.txt").write_text("12345", encoding="utf-8")
        (self.root / ".env.production").write_text("TOKEN=secret", encoding="utf-8")
        (self.root / "private.pem").write_text("secret", encoding="utf-8")

        with self.assertRaises(ToolError):
            self.tools.read_file("missing.txt")
        with self.assertRaises(ToolError):
            self.tools.read_file("binary.bin")
        with self.assertRaises(ToolError):
            WorkspaceTools(self.root, max_file_bytes=4).read_file("large.txt")
        with self.assertRaises(ToolError):
            self.tools.read_file(".env.production")
        with self.assertRaises(ToolError):
            self.tools.read_file("private.pem")
        with self.assertRaises(ToolError):
            self.tools.list_files(None)
        with self.assertRaises(ToolError):
            self.tools.search_text("")
        with self.assertRaises(ToolError):
            self.tools.semantic_search("", None, "nomic")
        with self.assertRaises(ToolError):
            self.tools.semantic_search("query", None, "nomic", limit=11)
        with self.assertRaises(ToolError):
            self.tools.semantic_search("query", None, "nomic", limit=True)

    def test_list_files_caps_entries_and_reports_truncation(self):
        capped = self.root / "many"
        capped.mkdir()
        for number in range(12):
            (capped / f"file-{number:03d}.txt").write_text("نص", encoding="utf-8")

        tools = WorkspaceTools(self.root, max_list_entries=10)
        names = tools.list_files("many")

        self.assertEqual(len(names), 11)
        self.assertTrue(names[-1].startswith("...[قُطعت قائمة الملفات"))
        self.assertIn("من 12 ملفًا", names[-1])
        self.assertEqual(names[:10], [f"many/file-{number:03d}.txt" for number in range(10)])

        uncapped = WorkspaceTools(self.root)
        self.assertEqual(uncapped.list_files("many"), [f"many/file-{number:03d}.txt" for number in range(12)])
        with self.assertRaises(ValueError):
            WorkspaceTools(self.root, max_list_entries=0)

    def test_search_ignores_runtime_noise_and_counts_only_text_files(self):
        search_root = self.root / "search"
        search_root.mkdir()
        for directory in (".pytest_cache", ".ruff_cache", "__pycache__", ".Git", "Node_Modules"):
            path = search_root / directory
            path.mkdir()
            (path / "noise.txt").write_text("إبرة", encoding="utf-8")
        (search_root / "a.bin").write_bytes(b"binary")
        (search_root / "z.txt").write_text("إبرة", encoding="utf-8")

        tools = WorkspaceTools(self.root, max_files=1)

        self.assertEqual(tools.search_text("إبرة", "search"), ["search/z.txt:1: إبرة"])

    def test_dispatch_exposes_only_the_declared_read_tools(self):
        class FakeEmbeddingClient:
            def embed(self, texts, model):
                return [[1.0] for _ in texts]

        client = FakeEmbeddingClient()
        self.assertEqual(
            self.tools.dispatch("search_text", {"query": "تفاحة"}, client, "nomic"),
            ["alpha.txt:1: تفاحة حمراء"],
        )
        self.assertEqual(
            self.tools.dispatch(
                "semantic_search", {"query": "بحر", "limit": 1}, client, "nomic"
            )[0]["path"],
            "alpha.txt",
        )
        with self.assertRaises(ToolError):
            self.tools.dispatch("delete_file", {}, client, "nomic")
        with self.assertRaises(ToolError):
            self.tools.dispatch("list_files", [], client, "nomic")
        with self.assertRaises(ToolError):
            self.tools.dispatch("read_file", {"path": "alpha.txt", "extra": True}, client, "nomic")

    def test_semantic_search_uses_the_persistent_embedding_cache(self):
        class Cache:
            def __init__(self):
                self.calls = []

            def embed_texts(self, texts, client, model):
                self.calls.append((list(texts), client, model))
                return [[1.0, 0.0], *[[1.0, 0.0] for _ in texts[1:]]]

        class Client:
            def embed(self, *_args):
                raise AssertionError("the persistent cache must own embedding")

        cache = Cache()
        tools = WorkspaceTools(self.root, semantic_memory=cache)
        results = tools.semantic_search("تفاحة", Client(), "nomic", limit=1)

        self.assertEqual(results[0]["path"], "alpha.txt")
        self.assertEqual(cache.calls[0][0][0], "تفاحة")

    def test_semantic_search_respects_memory_batch_limit(self):
        (self.root / "many.txt").write_text("a" * 200, encoding="utf-8")

        class Cache:
            max_texts = 128

            def __init__(self):
                self.batch_size = 0

            def embed_texts(self, texts, _client, _model):
                self.batch_size = len(texts)
                if len(texts) > 128:
                    raise ValueError("too many texts")
                return [[1.0, 0.0] for _text in texts]

        cache = Cache()
        tools = WorkspaceTools(self.root, chunk_chars=1, semantic_memory=cache)
        tools.semantic_search("a", object(), "nomic", limit=1)

        self.assertEqual(cache.batch_size, 128)

    def test_coding_tools_are_opt_in_and_require_approval(self):
        with self.assertRaises(ToolError):
            self.tools.dispatch(
                "create_file", {"path": "new.txt", "content": "new"}, None, "nomic"
            )
        self.assertFalse((self.root / "new.txt").exists())

        coding_dispatch = WorkspaceTools(
            self.root, coding=True, approver=lambda *_args: True
        )
        with self.assertRaises(ToolError):
            coding_dispatch.dispatch("create_file", {"path": "empty.txt"}, None, "nomic")
        self.assertFalse((self.root / "empty.txt").exists())

        for approver in (None, lambda _action, _preview: False):
            tools = WorkspaceTools(self.root, coding=True, approver=approver)
            with self.assertRaises(ToolError):
                tools.create_file("new.txt", "new")
            self.assertFalse((self.root / "new.txt").exists())

        denied_calls = []
        denied = WorkspaceTools(
            self.root,
            coding=True,
            approver=lambda *_args: denied_calls.append(True) or False,
        )
        for _ in range(2):
            with self.assertRaises(ToolError):
                denied.create_file("denied.txt", "same")
        self.assertEqual(len(denied_calls), 1)

        approvals = []
        tools = WorkspaceTools(
            self.root,
            coding=True,
            approver=lambda action, preview: approvals.append((action, preview)) or True,
        )
        result = tools.create_file("new.txt", "new\n")

        self.assertEqual(result["status"], "created")
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "new\n")
        self.assertEqual(approvals[0][0], "create_file")
        self.assertIn("+new", approvals[0][1])

        safe_preview = []
        preview_tools = WorkspaceTools(
            self.root,
            coding=True,
            approver=lambda _action, preview: safe_preview.append(preview) or True,
        )
        preview_tools.create_file("controls.txt", "\x1b[2J\u202ereversed")
        self.assertNotIn("\x1b", safe_preview[0])
        self.assertNotIn("\u202e", safe_preview[0])
        self.assertIn("\\u001b", safe_preview[0])
        self.assertIn("\\u202e", safe_preview[0])
        with self.assertRaises(ValueError):
            WorkspaceTools(self.root, allow_host_commands=True)

    def test_writes_snapshot_the_approved_change_before_applying_it(self):
        timeline = []

        class Snapshots:
            @staticmethod
            def take_snapshot(label, paths, expected_current):
                timeline.append(("snapshot", label, paths, expected_current))

        tools = WorkspaceTools(
            self.root,
            coding=True,
            approver=lambda action, _preview: timeline.append(("approval", action)) or True,
        )
        tools.snapshot_manager = Snapshots()

        tools.create_file("new.txt", "new")
        tools.replace_text("alpha.txt", "حمراء", "خضراء")

        self.assertEqual(timeline[0], ("approval", "create_file"))
        self.assertEqual(
            timeline[1],
            ("snapshot", "create_file", ["new.txt"], {"new.txt": b"new"}),
        )
        self.assertEqual(timeline[2], ("approval", "replace_text"))
        self.assertEqual(
            timeline[3],
            (
                "snapshot",
                "replace_text",
                ["alpha.txt"],
                {"alpha.txt": "تفاحة خضراء".encode()},
            ),
        )

    def test_failed_write_discards_uncommitted_snapshot(self):
        discarded = []

        class Snapshots:
            @staticmethod
            def take_snapshot(*_args):
                return "snap-id"

            @staticmethod
            def discard(snapshot_id):
                discarded.append(snapshot_id)

        tools = WorkspaceTools(self.root, coding=True, approver=lambda *_args: True)
        tools.snapshot_manager = Snapshots()
        with patch.object(tools, "_atomic_write", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                tools.create_file("failed.txt", "content")
        self.assertEqual(discarded, ["snap-id"])

    def test_committed_create_keeps_snapshot_when_temp_cleanup_fails(self):
        discarded = []

        class Snapshots:
            @staticmethod
            def take_snapshot(*_args):
                return "snap-id"

            @staticmethod
            def discard(snapshot_id):
                discarded.append(snapshot_id)

        tools = WorkspaceTools(self.root, coding=True, approver=lambda *_args: True)
        tools.snapshot_manager = Snapshots()
        real_unlink = Path.unlink

        def fail_temp_cleanup(path, *args, **kwargs):
            if path.name.startswith(".local-agent-") and path.suffix == ".tmp":
                raise OSError("cleanup blocked")
            return real_unlink(path, *args, **kwargs)

        with patch("workspace_tools.Path.unlink", new=fail_temp_cleanup):
            self.assertEqual(tools.create_file("committed.txt", "ok")["status"], "created")
        self.assertEqual((self.root / "committed.txt").read_text(encoding="utf-8"), "ok")
        self.assertEqual(discarded, [])

    def test_close_stops_an_active_command(self):
        tools = WorkspaceTools(self.root, coding=True, allow_host_commands=True)
        errors = []

        def run():
            try:
                tools._run_bounded_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"], self.root, 30
                )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 2
        while tools._active_process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(tools._active_process)
        tools.close()
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_replace_text_is_exact_confined_atomic_and_reviewable(self):
        approvals = []
        tools = WorkspaceTools(
            self.root,
            coding=True,
            approver=lambda action, preview: approvals.append((action, preview)) or True,
        )

        result = tools.replace_text("alpha.txt", "حمراء", "خضراء")

        self.assertEqual(result["status"], "updated")
        self.assertEqual((self.root / "alpha.txt").read_text(encoding="utf-8"), "تفاحة خضراء")
        self.assertIn("-تفاحة حمراء", approvals[0][1])
        self.assertIn("+تفاحة خضراء", tools.review_changes())

        (self.root / "repeated.txt").write_text("x x", encoding="utf-8")
        for old_text in ("missing", "x"):
            before = (self.root / "repeated.txt").read_bytes()
            with self.assertRaises(ToolError):
                tools.replace_text("repeated.txt", old_text, "y")
            self.assertEqual((self.root / "repeated.txt").read_bytes(), before)

        for unsafe_path in (
            "../outside.txt",
            str(self.root / "alpha.txt"),
            "alpha.txt:stream",
            "bad?.txt",
            "CON.txt",
            ".env",
        ):
            with self.subTest(path=unsafe_path), self.assertRaises(ToolError):
                tools.create_file(unsafe_path, "unsafe")

        (self.root / "atomic.txt").write_text("before", encoding="utf-8")
        with patch("workspace_tools.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                tools.replace_text("atomic.txt", "before", "after")
        self.assertEqual((self.root / "atomic.txt").read_text(encoding="utf-8"), "before")
        self.assertEqual(list(self.root.glob(".local-agent-*.tmp")), [])

        (self.root / "invalid.txt").write_bytes(b"\xff")
        with self.assertRaises(ToolError):
            tools.replace_text("invalid.txt", "x", "y")
        self.assertEqual((self.root / "invalid.txt").read_bytes(), b"\xff")

        stale = self.root / "stale.txt"
        stale.write_text("before", encoding="utf-8")

        def mutate_before_approval(_action, _preview):
            stale.write_text("external", encoding="utf-8")
            return True

        stale_tools = WorkspaceTools(self.root, coding=True, approver=mutate_before_approval)
        with self.assertRaisesRegex(ToolError, "تغير الملف"):
            stale_tools.replace_text("stale.txt", "before", "after")
        self.assertEqual(stale.read_text(encoding="utf-8"), "external")

        endings = self.root / "endings.txt"
        endings.write_bytes(b"one\r\n")
        endings_preview = []
        endings_tools = WorkspaceTools(
            self.root,
            coding=True,
            approver=lambda _action, preview: endings_preview.append(preview) or True,
        )
        endings_tools.replace_text("endings.txt", "\r\n", "\n")
        self.assertIn("line endings", endings_preview[0])
        self.assertEqual(endings.read_bytes(), b"one\n")

        with tempfile.TemporaryDirectory() as outside_dir:
            safe_parent = self.root / "safe-parent"
            moved_parent = self.root / "moved-parent"
            safe_parent.mkdir()

            def swap_parent(_action, _preview):
                safe_parent.rename(moved_parent)
                safe_parent.symlink_to(outside_dir, target_is_directory=True)
                return True

            race_tools = WorkspaceTools(self.root, coding=True, approver=swap_parent)
            try:
                with self.assertRaises(ToolError):
                    race_tools.create_file("safe-parent/escaped.txt", "blocked")
            except OSError:
                pass
            self.assertFalse(Path(outside_dir, "escaped.txt").exists())

        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir, "secret.txt")
            outside.write_text("SECRET_OUTSIDE", encoding="utf-8")
            linked = self.root / "alpha.txt"
            linked.unlink()
            try:
                linked.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            review = tools.review_changes()
            self.assertNotIn("SECRET_OUTSIDE", review)
            self.assertIn("تعذرت المراجعة الآمنة", review)

    def test_run_command_is_explicit_bounded_and_has_no_shell(self):
        denied = WorkspaceTools(self.root, coding=True, approver=lambda *_args: True)
        with self.assertRaises(ToolError):
            denied.run_command([sys.executable, "--version"])

        tools = WorkspaceTools(
            self.root,
            coding=True,
            allow_host_commands=True,
            approver=lambda _action, _preview: True,
            command_timeout=0.2,
            max_command_output_bytes=512,
        )
        marker = self.root / "marker.txt"
        literal = f"a&echo injected>{marker}"
        result = tools.run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", literal]
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertIn(literal, result["output"])
        self.assertFalse(marker.exists())

        with patch.dict(os.environ, {"LOCAL_AGENT_TEST_SECRET": "hidden"}):
            result = tools.run_command(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('LOCAL_AGENT_TEST_SECRET', 'missing'))",
                ]
            )
        self.assertIn("missing", result["output"])
        self.assertNotIn("hidden", result["output"])

        limited_tools = WorkspaceTools(
            self.root,
            coding=True,
            allow_host_commands=True,
            approver=lambda _action, _preview: True,
            command_timeout=0.2,
            max_command_output_bytes=64,
        )
        result = limited_tools.run_command([sys.executable, "-c", "print('x' * 1000)"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"].encode("utf-8")), 100)

        with self.assertRaisesRegex(ToolError, "المهلة"):
            tools.run_command([sys.executable, "-c", "import time; time.sleep(2)"])
        for argv in ("python --version", [], [sys.executable, None], ["bad\x00name"]):
            with self.subTest(argv=argv), self.assertRaises(ToolError):
                tools.run_command(argv)
        with self.assertRaises(ToolError):
            tools.run_command([sys.executable, "--version"], "..")
        for invalid_timeout in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                WorkspaceTools(
                    self.root,
                    coding=True,
                    allow_host_commands=True,
                    approver=lambda *_args: True,
                    command_timeout=invalid_timeout,
                )
