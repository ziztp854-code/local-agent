import gc
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import ANY, Mock, patch

import desktop_app
import desktop_core
from desktop_app import ApprovalDialog, COLORS, LocalAgentApp
from command_palette import CommandPalette
from desktop_core import (
    AgentController,
    ApprovalBroker,
    ApprovalRequest,
    AppSettings,
    DesktopConfig,
    build_local_agent,
    build_workspace_briefing,
    describe_workspace,
)
from skill_catalog import SkillCatalog, SkillError
from skill_panel import SkillsPanel
from workspace_tools import ToolError


class AppSettingsTests(unittest.TestCase):
    def test_roundtrip_filters_unknown_keys_and_survives_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "nested", "settings.json")
            settings = AppSettings(path)
            self.assertEqual(settings.load(), {})
            self.assertTrue(
                settings.save({
                    "dark_theme": True,
                    "workspace": "C:\\proj",
                    "evil": "drop",
                    "mode": 5,
                })
            )
            self.assertEqual(
                settings.load(),
                {"dark_theme": True, "workspace": "C:\\proj"},
            )
            path.write_bytes(b"{not json")
            self.assertEqual(settings.load(), {})
            path.write_bytes(b"x" * 20_000)
            self.assertEqual(settings.load(), {})


class WorkspaceBriefingTests(unittest.TestCase):
    def test_describe_counts_files_languages_and_skips_hidden(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.py").write_text("x", encoding="utf-8")
            (root / "b.py").write_text("x", encoding="utf-8")
            (root / "c.md").write_text("x", encoding="utf-8")
            (root / ".env").write_text("T=1", encoding="utf-8")
            (root / "id_rsa").write_text("k", encoding="utf-8")
            nested = root / "sub"
            nested.mkdir()
            (nested / "d.py").write_text("x", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "cfg").write_text("x", encoding="utf-8")
            (root / "node_modules").mkdir()

            stats = describe_workspace(root)

        self.assertEqual(stats["files"], 4)
        self.assertEqual(stats["directories"], 1)
        self.assertFalse(stats["truncated"])
        self.assertEqual(stats["top"][0], ("Python", 3))
        self.assertEqual(stats["top"][1], ("Markdown", 1))

    def test_describe_caps_the_scan_and_reports_truncation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(5):
                (root / f"f{index}.txt").write_text("x", encoding="utf-8")
            stats = describe_workspace(root, max_files=2)
        self.assertEqual(stats["files"], 2)
        self.assertTrue(stats["truncated"])

    def test_describe_labels_extensionless_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Dockerfile").write_text("x", encoding="utf-8")
            (root / "LICENSE").write_text("x", encoding="utf-8")
            (root / "main.py").write_text("x", encoding="utf-8")
            stats = describe_workspace(root)
        self.assertEqual(stats["top"][0], ("بدون امتداد", 2))
        self.assertEqual(stats["top"][1], ("Python", 1))

    def test_briefing_text_is_actionable(self):
        empty = build_workspace_briefing({
            "files": 0, "directories": 0, "truncated": False, "top": [],
        })
        self.assertIn("مساحة العمل فارغة", empty)
        text = build_workspace_briefing({
            "files": 4, "directories": 1, "truncated": False, "top": [("Python", 3)],
        })
        self.assertIn("4 ملفًا", text)
        self.assertIn("1 مجلدًا", text)
        self.assertIn("Python (3)", text)
        self.assertIn("لخّص المشروع", text)


class DesktopConfigTests(unittest.TestCase):
    def test_parse_validates_workspace_mode_and_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mcp_config = Path(temp_dir, "mcp.json")
            mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
            config = DesktopConfig.parse(
                temp_dir,
                "coding",
                session=" demo ",
                model=" local-model ",
                base_url=" http://127.0.0.1:1234/v1 ",
                mcp_config=f" {mcp_config} ",
            )

            self.assertEqual(config.workspace, Path(temp_dir).resolve())
            self.assertEqual(config.mode, "coding")
            self.assertEqual(config.session, "demo")
            self.assertEqual(config.model, "local-model")
            self.assertEqual(config.mcp_config, mcp_config.resolve())
            self.assertFalse(config.semantic_memory)
            self.assertTrue(config.coding)
            self.assertFalse(config.allow_host_commands)

            host = DesktopConfig.parse(temp_dir, "host")
            self.assertTrue(host.coding)
            self.assertTrue(host.allow_host_commands)

            for mode in ("", "admin"):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    DesktopConfig.parse(temp_dir, mode)
            with self.assertRaises(ValueError):
                DesktopConfig.parse("", "read")
            with self.assertRaises(ValueError):
                DesktopConfig.parse(temp_dir, "read", model=" ")
            with self.assertRaises(ValueError):
                DesktopConfig.parse(Path(temp_dir) / "missing", "read")
            with self.assertRaises(ValueError):
                DesktopConfig.parse(temp_dir, "read", mcp_config=Path(temp_dir, "missing.json"))
            with self.assertRaises(ValueError):
                DesktopConfig.parse(temp_dir, "read", semantic_memory=True)
            file_path = Path(temp_dir, "file.txt")
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                DesktopConfig.parse(file_path, "read")

            unchanged = DesktopConfig.parse(temp_dir, "coding", mcp_config=mcp_config)
            mcp_config.write_text(
                '{"mcpServers":{"off":{"disabled":true}}}', encoding="utf-8"
            )
            changed = DesktopConfig.parse(temp_dir, "coding", mcp_config=mcp_config)
            self.assertNotEqual(unchanged, changed)

    def test_build_local_agent_wires_existing_components_without_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = DesktopConfig.parse(temp_dir, "host", session="demo")
            approver = Mock()
            client = object()
            workspace = Mock(root=Path(temp_dir))
            session = object()
            embedding_cache = object()
            cognitive_memory = Mock(memory=object())
            skill_catalog = object()
            built = object()
            with (
                patch.dict(os.environ, {"LM_STUDIO_API_KEY": "local-secret"}),
                patch("desktop_core.LMStudioClient", return_value=client) as client_class,
                patch("desktop_core.SemanticMemory") as memory_class,
                patch("desktop_core.WorkspaceTools", return_value=workspace) as tools_class,
                patch("desktop_core.SessionStore", return_value=session) as session_class,
                patch(
                    "desktop_core.LocalCognitiveMemoryEngine.for_workspace",
                    return_value=cognitive_memory,
                ),
                patch(
                    "desktop_core.SkillCatalog.default", return_value=skill_catalog
                ),
                patch("desktop_core.LocalAgent", return_value=built) as agent_class,
            ):
                memory_class.for_workspace.return_value = embedding_cache
                result = build_local_agent(config, approver)

            self.assertIs(result, built)
            client_class.assert_called_once_with(config.base_url, api_key="local-secret")
            tools_class.assert_called_once_with(
                config.workspace,
                coding=True,
                allow_host_commands=True,
                approver=approver,
                semantic_memory=embedding_cache,
            )
            session_class.assert_called_once_with(workspace.root, "demo")
            agent_class.assert_called_once_with(
                client,
                workspace,
                model=config.model,
                session=session,
                mcp_registry=None,
                memory=None,
                skill_catalog=skill_catalog,
                learner=ANY,
                cognitive_memory=cognitive_memory,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            config = DesktopConfig.parse(temp_dir, "read")
            with (
                patch("desktop_core.LMStudioClient", return_value=object()),
                patch("desktop_core.SemanticMemory"),
                patch("desktop_core.WorkspaceTools", return_value=Mock(root=Path(temp_dir))),
                patch("desktop_core.SessionStore") as session_class,
                patch("desktop_core.SkillCatalog.default", return_value=object()),
                patch("desktop_core.LocalAgent", return_value=object()),
            ):
                build_local_agent(config, Mock())
            session_class.assert_not_called()

    def test_build_local_agent_wires_opt_in_memory_and_mcp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mcp_path = Path(temp_dir, "mcp.json")
            mcp_path.write_text('{"mcpServers": {}}', encoding="utf-8")
            config = DesktopConfig.parse(
                temp_dir,
                "read",
                session="demo",
                mcp_config=mcp_path,
                semantic_memory=True,
            )
            client = object()
            embedding_cache = object()
            workspace = Mock(root=Path(temp_dir))
            session = Mock(path=Path(temp_dir, "session.json"))
            registry = object()
            conversation_memory = object()
            cognitive_memory = Mock(memory=object())
            skill_catalog = object()
            built = object()
            with (
                patch("desktop_core.LMStudioClient", return_value=client),
                patch("desktop_core.SemanticMemory") as memory_class,
                patch("desktop_core.WorkspaceTools", return_value=workspace),
                patch("desktop_core.SessionStore", return_value=session),
                patch("desktop_core.MCPRegistry", return_value=registry) as registry_class,
                patch(
                    "desktop_core.LocalCognitiveMemoryEngine.for_workspace",
                    return_value=cognitive_memory,
                ),
                patch(
                    "desktop_core.SkillCatalog.default", return_value=skill_catalog
                ),
                patch("desktop_core.LocalAgent", return_value=built) as agent_class,
            ):
                memory_class.for_workspace.return_value = embedding_cache
                memory_class.return_value = conversation_memory
                result = build_local_agent(config, Mock())

            self.assertIs(result, built)
            registry_class.assert_called_once()
            memory_class.assert_called_once_with(session.path.with_suffix(".memory.sqlite"))
            agent_class.assert_called_once_with(
                client,
                workspace,
                model=config.model,
                session=session,
                mcp_registry=registry,
                memory=conversation_memory,
                skill_catalog=skill_catalog,
                learner=ANY,
                cognitive_memory=cognitive_memory,
            )

    def test_missing_skills_pack_does_not_disable_the_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Mock(root=Path(temp_dir))
            built = object()
            with (
                patch("desktop_core.LMStudioClient", return_value=object()),
                patch("desktop_core.SemanticMemory") as memory_class,
                patch("desktop_core.WorkspaceTools", return_value=workspace),
                patch(
                    "desktop_core.SkillCatalog.default",
                    side_effect=SkillError("missing"),
                ),
                patch("desktop_core.LocalAgent", return_value=built) as agent_class,
            ):
                memory_class.for_workspace.return_value = object()
                result = build_local_agent(
                    DesktopConfig.parse(temp_dir, "read"),
                    Mock(),
                )

            self.assertIs(result, built)
            self.assertIsNone(agent_class.call_args.kwargs["skill_catalog"])

    def test_container_engine_is_explicit_and_validated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = DesktopConfig.parse(temp_dir, "coding", container_engine="docker")
            self.assertEqual(config.container_engine, "docker")
            self.assertEqual(
                DesktopConfig.parse(temp_dir, "coding").container_engine,
                "",
            )
            for engine in ("wsl", "powershell", True):
                with self.subTest(engine=engine), self.assertRaises(ValueError):
                    DesktopConfig.parse(temp_dir, "coding", container_engine=engine)


class CoreSafetyTests(unittest.TestCase):
    def test_snapshot_restores_a_file_and_rejects_unsafe_identifiers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "note.txt"
            note.write_text("before", encoding="utf-8")
            manager = desktop_core.SnapshotManager(root, max_snapshots=2)

            snapshot_id = manager.take_snapshot(
                "edit", ["note.txt"], {"note.txt": b"after"}
            )
            note.write_text("after", encoding="utf-8")

            self.assertTrue(manager.rollback(snapshot_id))
            self.assertEqual(note.read_text(encoding="utf-8"), "before")

            guarded = manager.take_snapshot(
                "replace_text",
                ["note.txt"],
                {"note.txt": b"agent-output"},
            )
            note.write_text("external-output", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                manager.rollback(guarded)
            self.assertEqual(note.read_text(encoding="utf-8"), "external-output")
            note.write_bytes(b"agent-output")
            self.assertTrue(manager.rollback(guarded))
            self.assertEqual(note.read_text(encoding="utf-8"), "before")

            created = manager.take_snapshot(
                "create_file",
                ["created.txt"],
                {"created.txt": b"created-by-agent"},
            )
            root.joinpath("created.txt").write_bytes(b"created-by-agent")
            self.assertTrue(manager.rollback(created))
            self.assertFalse(root.joinpath("created.txt").exists())
            for unsafe in ("../outside", str(root), "snap/child"):
                with self.subTest(snapshot_id=unsafe), self.assertRaises(ValueError):
                    manager.rollback(unsafe)
            labelled = manager.take_snapshot(
                "../escape", ["note.txt"], {"note.txt": b"after"}
            )
            self.assertNotIn("escape", labelled)
            self.assertEqual((manager.snapshot_dir / labelled).parent, manager.snapshot_dir)

    def test_snapshot_store_rejects_links_and_binds_rollback_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            note = root / "note.txt"
            note.write_text("before", encoding="utf-8")
            manager = desktop_core.SnapshotManager(root)
            try:
                (root / ".local-agent").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory links unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "links"):
                manager.take_snapshot("edit", ["note.txt"], {"note.txt": b"after"})
            self.assertEqual(list(Path(outside).iterdir()), [])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "note.txt"
            note.write_text("before", encoding="utf-8")
            manager = desktop_core.SnapshotManager(root)
            snapshot_id = manager.take_snapshot(
                "edit", ["note.txt"], {"note.txt": b"after"}
            )
            note.write_text("after", encoding="utf-8")
            selected, manifest_hash, _preview = manager.prepare_rollback(snapshot_id)
            manifest = manager.snapshot_dir / snapshot_id / "manifest.json"
            manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after approval"):
                manager.rollback(selected, manifest_hash)
            self.assertEqual(note.read_text(encoding="utf-8"), "after")

    def test_snapshot_latest_uses_creation_order_not_random_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "note.txt"
            note.write_text("zero", encoding="utf-8")
            manager = desktop_core.SnapshotManager(root)
            with patch("desktop_core.time.time_ns", side_effect=[1, 2]), patch(
                "desktop_core.secrets.token_hex", side_effect=["ffffffff", "00000000"]
            ):
                manager.take_snapshot("first", ["note.txt"], {"note.txt": b"one"})
                note.write_text("one", encoding="utf-8")
                manager.take_snapshot("second", ["note.txt"], {"note.txt": b"two"})
            note.write_text("two", encoding="utf-8")
            self.assertTrue(manager.rollback())
            self.assertEqual(note.read_text(encoding="utf-8"), "one")

    def test_snapshot_full_capture_and_failed_group_rollback_are_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "a.txt"
            second = root / "b.txt"
            first.write_text("old-a", encoding="utf-8")
            second.write_text("old-b", encoding="utf-8")
            manager = desktop_core.SnapshotManager(root)
            self.assertIsInstance(manager.take_snapshot("full"), str)
            snapshot_id = manager.take_snapshot(
                "group",
                ["a.txt", "b.txt"],
                {"a.txt": b"new-a", "b.txt": b"new-b"},
            )
            first.write_bytes(b"new-a")
            second.write_bytes(b"new-b")
            real_replace = os.replace

            def fail_second(source, target):
                if Path(source) == second:
                    raise OSError("blocked")
                return real_replace(source, target)

            with patch("desktop_core.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    manager.rollback(snapshot_id)
            self.assertEqual(first.read_bytes(), b"new-a")
            self.assertEqual(second.read_bytes(), b"new-b")

    def test_container_sandbox_builds_argv_without_a_shell_or_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            sandbox = desktop_core.ContainerSandbox(
                root,
                "docker",
                image="local/python@sha256:" + "0" * 64,
            )

            wrapped = sandbox.wrap(
                ["python", "-c", "print('a&b')"],
                cwd=".",
            )
            argv = wrapped[0] if isinstance(wrapped, tuple) else wrapped

            self.assertIsInstance(argv, list)
            self.assertIn(Path(argv[0]).name.casefold(), {"docker", "docker.exe"})
            self.assertEqual(argv[1:3], ["run", "--rm"])
            self.assertIn("--network=none", argv)
            self.assertIn(f"{root}:/workspace:rw", argv)
            self.assertIn("/workspace/.local-agent:rw,noexec,nosuid,size=1m", argv)
            self.assertEqual(argv[-3:], ["python", "-c", "print('a&b')"])
            self.assertNotIn("sh", argv)
            with self.assertRaises(ValueError):
                desktop_core.ContainerSandbox(root, "wsl")
            with self.assertRaisesRegex(ValueError, "digest"):
                desktop_core.ContainerSandbox(root, "docker", image="python:3.12-slim")


class ApprovalBrokerTests(unittest.TestCase):
    def test_approval_round_trip_and_close_fail_closed(self):
        broker = ApprovalBroker()
        answers = []
        worker = threading.Thread(
            target=lambda: answers.append(broker.ask("create_file", "diff"))
        )
        worker.start()
        request = broker.poll(timeout=1)
        self.assertEqual((request.action, request.preview), ("create_file", "diff"))
        self.assertTrue(broker.resolve(request, True))
        self.assertFalse(broker.resolve(request, True))
        worker.join(1)
        self.assertEqual(answers, [True])

        blocked = threading.Thread(target=lambda: answers.append(broker.ask("run", "argv")))
        blocked.start()
        broker.poll(timeout=1)
        broker.close()
        blocked.join(1)
        self.assertEqual(answers, [True, False])
        self.assertFalse(broker.ask("later", "preview"))
        closed_request = ApprovalRequest("later", "preview", Queue(maxsize=1))
        self.assertFalse(broker.resolve(closed_request, True))
        self.assertFalse(closed_request.response.get_nowait())

    def test_poll_returns_none_when_empty(self):
        broker = ApprovalBroker()
        self.assertIsNone(broker.poll())


class AgentControllerTests(unittest.TestCase):
    def test_controller_forwards_stream_events_without_waiting_for_an_answer(self):
        class StreamingAgent:
            history = [{"role": "system", "content": "system"}]

            @staticmethod
            def answer(_prompt, _images=()):
                raise AssertionError("the controller must use answer_stream")

            @staticmethod
            def answer_stream(_prompt, image_paths=()):
                yield {"type": "thought", "delta": "خطة"}
                yield {"type": "token", "delta": "جزء"}
                yield {"type": "tool_start", "name": "read_file", "id": "c1"}
                yield {"type": "tool_end", "name": "read_file", "status": "success"}

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = AgentController(agent_factory=lambda *_args: StreamingAgent())
            controller.configure(DesktopConfig.parse(temp_dir, "read"))
            self.assertTrue(controller.submit("go"))
            controller.worker.join(1)
            events = controller.poll_events()

        self.assertIn(("thought", "خطة"), events)
        self.assertIn(("chunk", "جزء"), events)
        self.assertEqual(sum(kind == "status" for kind, _content in events), 2)
        self.assertEqual(events[-1], ("done", ""))
        self.assertNotIn("answer", [kind for kind, _content in events])

    def test_controller_rollback_requires_approval(self):
        manager = Mock()
        manager.prepare_rollback.return_value = ("snapshot-id", "abc", "diff\nSHA256: abc")
        manager.rollback.return_value = True

        class Agent:
            history = [{"role": "system", "content": "system"}]
            workspace = Mock(snapshot_manager=manager)

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = AgentController(agent_factory=lambda *_args: Agent())
            controller.configure(DesktopConfig.parse(temp_dir, "coding"))

            self.assertTrue(controller.rollback())
            denied = controller.approvals.poll(timeout=1)
            self.assertEqual(denied.action, "rollback")
            controller.approvals.resolve(denied, False)
            controller.worker.join(1)
            controller.poll_events()
            manager.rollback.assert_not_called()

            self.assertTrue(controller.rollback())
            approved = controller.approvals.poll(timeout=1)
            controller.approvals.resolve(approved, True)
            controller.worker.join(1)
            events = controller.poll_events()

        manager.rollback.assert_called_once_with("snapshot-id", "abc")
        self.assertIn("status", [kind for kind, _content in events])
        self.assertEqual(events[-1], ("done", ""))

    def test_submit_runs_once_in_background_and_reports_events(self):
        entered = threading.Event()
        release = threading.Event()
        thread_ids = []

        class FakeAgent:
            history = [{"role": "system", "content": "system"}]

            def answer(self, prompt, image_paths=()):
                thread_ids.append(threading.get_ident())
                entered.set()
                self.prompt = (prompt, tuple(image_paths))
                if not release.wait(1):
                    raise RuntimeError("test timeout")
                return "النتيجة"

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = FakeAgent()
            controller = AgentController(agent_factory=lambda *_args: agent)
            history = controller.configure(DesktopConfig.parse(temp_dir, "read"))
            self.assertEqual(history, [])
            self.assertTrue(controller.submit(" نفّذ ", ["a.png"]))
            self.assertTrue(entered.wait(1))
            self.assertTrue(controller.busy)
            self.assertFalse(controller.submit("طلب ثان"))
            self.assertNotEqual(thread_ids, [threading.get_ident()])

            release.set()
            controller.worker.join(1)
            events = controller.poll_events()

        self.assertEqual(agent.prompt, ("نفّذ", ("a.png",)))
        self.assertEqual(events, [("answer", "النتيجة"), ("done", "")])
        self.assertFalse(controller.busy)

    def test_errors_are_sanitized_and_failed_config_preserves_agent(self):
        class BadAgent:
            history = [{"role": "system", "content": "system"}]

            def answer(self, _prompt):
                raise RuntimeError("bad\x1b[31m")

        calls = 0

        def factory(*_args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("invalid")
            return BadAgent()

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = AgentController(agent_factory=factory)
            config = DesktopConfig.parse(temp_dir, "read")
            controller.configure(config)
            original = controller.agent
            self.assertEqual(controller.configure(config), [])
            self.assertIs(controller.agent, original)
            with self.assertRaises(ValueError):
                controller.configure(DesktopConfig.parse(temp_dir, "coding"))
            self.assertIs(controller.agent, original)

            self.assertFalse(controller.submit(" "))
            self.assertTrue(controller.submit("go"))
            controller.worker.join(1)
            events = controller.poll_events()

        self.assertEqual(events, [("error", "bad\\u001b[31m"), ("done", "")])

    def test_review_changes_and_close_release_approval(self):
        class FakeWorkspace:
            @staticmethod
            def review_changes():
                return "diff"

        class WaitingAgent:
            history = [{"role": "system", "content": "system"}]
            workspace = FakeWorkspace()

            def __init__(self, approver):
                self.approver = approver

            def answer(self, _prompt):
                return "yes" if self.approver("replace_text", "preview") else "no"

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = AgentController(
                agent_factory=lambda _config, approver: WaitingAgent(approver)
            )
            controller.configure(DesktopConfig.parse(temp_dir, "coding"))
            self.assertEqual(controller.review_changes(), "diff")
            self.assertTrue(controller.submit("edit"))
            request = controller.approvals.poll(timeout=1)
            self.assertIsNotNone(request)
            controller.request_close()
            controller.worker.join(1)
            events = controller.poll_events()

        self.assertTrue(controller.closing)
        self.assertTrue(controller.ready_to_close)
        self.assertEqual(events, [("answer", "no"), ("done", "")])

    def test_requires_configuration_before_submit_or_review(self):
        controller = AgentController()
        self.assertFalse(controller.submit("hello"))
        with self.assertRaises(RuntimeError):
            controller.review_changes()
        controller.request_close()
        with self.assertRaises(RuntimeError):
            controller.configure(Mock())

    def test_close_does_not_block_the_ui_thread(self):
        close_started = threading.Event()
        release = threading.Event()

        class Agent:
            history = [{"role": "system", "content": "system"}]

            def close(self):
                close_started.set()
                release.wait(1)

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = AgentController(agent_factory=lambda *_args: Agent())
            controller.configure(DesktopConfig.parse(temp_dir, "read"))
            controller.request_close()
            self.assertTrue(close_started.wait(0.2))
            self.assertFalse(controller.ready_to_close)
            release.set()
            for thread in controller._closers:
                thread.join(1)

        self.assertTrue(controller.ready_to_close)


class DesktopUiTests(unittest.TestCase):
    class FakeApprovals:
        def __init__(self):
            self.requests = []
            self.resolutions = []
            self.closed = False

        def poll(self):
            return self.requests.pop(0) if self.requests else None

        def resolve(self, request, approved):
            self.resolutions.append((request, approved))
            return True

        def close(self):
            self.closed = True

    class FakeController:
        def __init__(self):
            self.approvals = DesktopUiTests.FakeApprovals()
            self.busy = False
            self.ready_to_close = True
            self.agent = None
            self.configs = []
            self.prompts = []
            self.events = []
            self.closed = False
            self.rollbacks = 0

        def configure(self, config):
            self.configs.append(config)
            self.agent = object()
            return [
                {"role": "user", "content": "سابق"},
                {"role": "assistant", "content": "محفوظ"},
            ]

        def submit(self, prompt, image_paths=()):
            if self.busy:
                return False
            self.busy = True
            self.prompts.append((prompt, tuple(image_paths)))
            return True

        def poll_events(self):
            events, self.events = self.events, []
            if any(kind == "done" for kind, _content in events):
                self.busy = False
            return events

        @staticmethod
        def review_changes():
            return "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"

        def rollback(self):
            self.rollbacks += 1
            return True

        def request_close(self):
            self.closed = True
            self.approvals.close()

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass
        gc.collect()

    def test_app_applies_config_sends_and_renders_results(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)
        self.assertGreaterEqual(self.root.minsize()[1], 740)
        with tempfile.TemporaryDirectory() as temp_dir:
            app.workspace_var.set(temp_dir)
            app.session_var.set("demo")
            self.assertTrue(app.apply_configuration())
            transcript = app.transcript.get("1.0", "end")
            self.assertIn("سابق", transcript)
            self.assertIn("محفوظ", transcript)

            app.prompt_text.insert("1.0", "اختبر المشروع")
            self.assertTrue(app.send())
            self.assertEqual(controller.prompts, [("اختبر المشروع", ())])
            self.assertEqual(app.send_button.cget("state"), "disabled")

            controller.events = [("answer", "تم"), ("done", "")]
            app._pump()
            self.assertIn("تم", app.transcript.get("1.0", "end"))
            # بعد الاكتمال يبقى الإرسال معطّلًا لأن الحقل فارغ (سلوك مقصود)،
            # ويُفعّل فور كتابة نص جديد.
            self.assertEqual(app.send_button.cget("state"), "disabled")
            app.prompt_text.insert("1.0", "طلب جديد")
            app._update_composer_state()
            self.assertEqual(app.send_button.cget("state"), "normal")
            app.prompt_text.delete("1.0", "end")
            app._update_composer_state()
            app.refresh_changes()
            self.assertIn("+++ b/file.py", app.changes_text.get("1.0", "end"))

            app.mode_var.set("برمجة + أوامر المضيف")
            app._update_mode_ui()
            self.assertIn("غير معزولة", app.mode_note_var.get())
            with patch("desktop_app.filedialog.askdirectory", return_value=temp_dir):
                app.choose_workspace()
            self.assertEqual(app.workspace_var.get(), temp_dir)

            image = Path(temp_dir, "shot.png")
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
            with patch("desktop_app.filedialog.askopenfilenames", return_value=(str(image),)):
                app.choose_images()
            self.assertEqual(app.pending_images, [str(image)])
            self.assertIn("shot.png", app.image_note_var.get())
            with patch(
                "desktop_app.filedialog.askopenfilenames",
                return_value=tuple(str(image) for _index in range(5)),
            ):
                app.choose_images()
            self.assertEqual(app.pending_images, [str(image)])
            self.assertIn("أربع", app.status_var.get())
            app.prompt_text.insert("1.0", "حلل الصورة")
            self.assertTrue(app.send())
            self.assertEqual(controller.prompts[-1], ("حلل الصورة", (str(image),)))
            self.assertEqual(app.pending_images, [])
            self.assertIn("صورة مرفقة: 1", app.transcript.get("1.0", "end"))

            mcp = Path(temp_dir, "mcp.json")
            mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
            with patch("desktop_app.filedialog.askopenfilename", return_value=str(mcp)):
                app.choose_mcp_config()
            self.assertEqual(app.mcp_config_var.get(), str(mcp))
            self.assertFalse(app.semantic_memory_var.get())

    def test_stream_theme_diff_undo_and_drop(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)

        with patch.object(app.transcript, "see") as see:
            app._handle_event("chunk", "أ")
            app._handle_event("chunk", "ب")
        self.assertIn("أب", app.transcript.get("1.0", "end"))
        see.assert_called_with("end")

        app._stream_result = "pending"
        app._handle_event("error", "stream failed")
        failed_status = app.status_var.get()
        app._handle_event("done", "")
        self.assertEqual(app.status_var.get(), failed_status)

        app._handle_event("thought", "خطة")
        self.assertIn("خطة", app.thought_text.get("1.0", "end"))
        self.assertIn("thought", app.thought_text.tag_names())

        if app._dark_theme:  # baseline على الوضع الفاتح بصرف النظر عن الافتراضي
            app.toggle_theme()
        app.refresh_changes()
        self.assertTrue(app.changes_text.tag_ranges("diff_add"))
        self.assertTrue(app.changes_text.tag_ranges("diff_remove"))
        self.assertTrue(app.changes_text.tag_ranges("diff_hunk"))
        self.assertEqual(app.changes_text.tag_cget("diff_add", "background"), "#E6F4EA")

        original_background = self.root.cget("background")
        app.toggle_theme()
        self.assertEqual(self.root.cget("background"), desktop_app.DARK_COLORS["fog"])
        app.toggle_theme()
        self.assertEqual(self.root.cget("background"), original_background)

        app.undo_button.invoke()
        self.assertEqual(controller.rollbacks, 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "drop.png")
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
            app._handle_drop(Mock(data=str(image)))
            self.assertEqual(app.pending_images, [str(image)])
            app._handle_drop(Mock(data=temp_dir))
            self.assertEqual(app.workspace_var.get(), temp_dir)

    def test_skills_panel_searches_and_previews_untrusted_instructions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir, "skills", "arabic-ui")
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\r\nname: arabic-ui\r\ndescription: واجهات عربية واضحة\r\n---\r\n"
                "\r\n# واجهات\r\n\r\nحافظ على اتجاه RTL.\r\n",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_root(Path(temp_dir, "skills"))
            host = tk.Frame(self.root)
            panel = SkillsPanel(host, catalog=catalog)

            self.assertEqual(panel.count, 1)
            self.assertIn("arabic-ui", panel.listbox.get(0))
            panel.listbox.selection_set(0)
            panel._show_selected()
            detail = panel.detail.get("1.0", "end")
            self.assertIn("واجهات عربية واضحة", detail)
            self.assertIn("RTL", detail)
            self.assertNotIn("description:", detail)
            self.assertIn("غير موثوقة", panel.safety_var.get())

            panel.search_var.set("missing")
            panel._filter()
            self.assertEqual(panel.listbox.size(), 0)
            self.assertIn("لا توجد", panel.status_var.get())

    def test_app_exposes_skills_tab_and_keyboard_friendly_task_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir, "skills", "demo")
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\nname: demo\ndescription: تجربة\n---\n\n# Demo\n",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_root(Path(temp_dir, "skills"))
            app = LocalAgentApp(
                self.root,
                controller=self.FakeController(),
                skill_catalog=catalog,
            )

            self.assertEqual(app.skill_panel.count, 1)
            tabs = [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()]
            labels = [tab.split("  ")[0] for tab in tabs]  # النص قبل الأيقونة
            self.assertEqual(
                labels,
                ["التكاملات", "الذاكرة", "المهارات", "التغييرات", "المحادثة"],
            )
            self.assertEqual(app.notebook.select(), str(app.chat_page))
            self.assertIn("المهارات: 1", app.skills_badge_var.get())

            app._set_busy(True)
            self.assertEqual(app.send_button.cget("text"), "…")
            app._set_busy(False)
            self.assertEqual(app.send_button.cget("text"), "↑")

            self.root.update_idletasks()
            content = app.integrations_canvas.bbox("all")
            self.assertIsNotNone(content)
            self.assertGreater(content[3], 400)
            self.assertEqual(str(app.integrations_scrollbar.cget("orient")), "vertical")

    def test_memory_dashboard_uses_live_engine_data(self):
        controller = self.FakeController()
        engine = Mock(workspace=Path("C:/Projects/demo"))
        engine.stats.return_value = {
            "memories": 4,
            "verified_successes": 3,
            "verified_failures": 1,
            "learned_skills": 2,
        }
        engine.list_memories.return_value = [{
            "id": 9,
            "text": "أمر البناء python -m pytest",
            "kind": "project",
            "importance": 8,
            "pinned": False,
            "archived": False,
        }]
        engine.list_experiences.return_value = [{
            "status": "success",
            "task": "تشغيل الاختبارات",
            "lesson": "نجح الأمر",
            "confidence": 0.95,
        }]
        engine.list_learned_skills.return_value = [{
            "id": 3,
            "name": "run-tests",
            "version": 1,
            "status": "candidate",
            "enabled": True,
            "success_count": 2,
            "failure_count": 0,
            "last_used_at": None,
        }]
        controller.agent = Mock(cognitive_memory=engine)
        app = LocalAgentApp(self.root, controller=controller)

        self.assertTrue(app.memory_panel.refresh())
        self.assertEqual(app.memory_panel.stat_vars["memories"].get(), "4")
        self.assertEqual(len(app.memory_panel.memory_tree.get_children()), 1)
        app.memory_panel.memory_tree.selection_set("9")
        app.memory_panel.toggle_pin()
        engine.pin.assert_called_once_with(9)

    def test_app_handles_invalid_config_events_approval_and_close(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)
        app.workspace_var.set("missing-workspace")
        self.assertFalse(app.apply_configuration())
        self.assertFalse(app.send())

        with tempfile.TemporaryDirectory() as temp_dir:
            app.workspace_var.set(temp_dir)
            with patch.object(controller, "configure", side_effect=ToolError("جلسة تالفة")):
                self.assertFalse(app.apply_configuration())

        response = Queue(maxsize=1)
        request = ApprovalRequest("create_file", "diff", response)
        app._queue_approval(request)
        self.assertIsNotNone(app._approval_dialog)
        app._approval_dialog.reject()
        self.assertEqual(controller.approvals.resolutions, [(request, False)])

        app._handle_event("denied", "رفض")
        app._handle_event("error", "فشل")
        app._handle_event("done", "")
        transcript = app.transcript.get("1.0", "end")
        self.assertIn("تم رفض العملية", transcript)
        self.assertIn("تعذر إكمال الطلب", transcript)
        app.close()
        self.assertTrue(controller.closed)

    def test_approval_dialog_allows_once_and_close_waits_for_worker(self):
        results = []
        request = ApprovalRequest("replace_text", "preview", Queue(maxsize=1))
        dialog = ApprovalDialog(
            self.root,
            request,
            lambda *result: results.append(result),
            COLORS,
        )
        dialog.allow()
        dialog.allow()
        self.assertEqual(results, [(request, True)])

        controller = self.FakeController()
        controller.busy = True
        controller.ready_to_close = False
        app = LocalAgentApp(self.root, controller=controller)
        app.close()
        self.assertTrue(app._closing)
        controller.ready_to_close = True
        app._pump()
        self.assertTrue(app._destroyed)

    def test_command_palette_filters_selects_and_runs_actions(self):
        hits = []
        palette = CommandPalette(
            self.root,
            (("واحد", lambda: hits.append("1")), ("اثنان", lambda: hits.append("2"))),
            COLORS,
        )
        self.assertEqual(palette.listbox.size(), 2)

        palette.entry.insert("0", "وا")
        palette._refresh()
        self.assertEqual(palette.listbox.size(), 1)
        self.assertEqual(palette.listbox.get(0), "واحد")

        palette.entry.delete(0, "end")
        palette.entry.insert("0", "غير موجود")
        palette._refresh()
        self.assertEqual(palette.listbox.size(), 0)
        self.assertEqual(palette._run(), "break")
        self.assertEqual(hits, [])
        self.assertTrue(palette.winfo_exists())

        palette.entry.delete(0, "end")
        palette._refresh()
        self.assertEqual(palette._move(type("Event", (), {"keysym": "Down"})()), "break")
        self.assertEqual(palette.listbox.curselection(), (1,))
        self.assertEqual(palette._move(type("Event", (), {"keysym": "Up"})()), "break")
        self.assertEqual(palette.listbox.curselection(), (0,))

        self.assertEqual(palette._run(), "break")
        self.assertEqual(hits, ["1"])
        self.assertFalse(palette.winfo_exists())

        palette = CommandPalette(self.root, (("أغلق", lambda: None),), COLORS)
        palette._cancel()
        self.assertFalse(palette.winfo_exists())

    def test_app_command_palette_lists_usable_actions(self):
        app = LocalAgentApp(self.root, controller=self.FakeController())
        actions = app._palette_actions()
        self.assertGreaterEqual(len(actions), 12)
        labels = [label for label, _callback in actions]
        self.assertIn("تصدير المحادثة إلى Markdown", labels)
        self.assertTrue(all(callable(callback) for _label, callback in actions))
        self.assertEqual(app.open_command_palette(), "break")

    def test_app_exports_chat_log_to_markdown(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)
        app._clear_transcript()
        self.assertFalse(app.export_chat())

        with tempfile.TemporaryDirectory() as temp_dir:
            app.workspace_var.set(temp_dir)
            app.session_var.set("demo")
            self.assertTrue(app.apply_configuration())
            app.prompt_text.insert("1.0", "اختبر")
            self.assertTrue(app.send())
            with patch.object(app.transcript, "see"):
                controller.events = [("chunk", "مرح"), ("chunk", "با"), ("done", "")]
                app._pump()
            with patch(
                "desktop_app.filedialog.asksaveasfilename",
                return_value="",
            ):
                self.assertFalse(app.export_chat())
            target = Path(temp_dir, "chat.md")
            with patch(
                "desktop_app.filedialog.asksaveasfilename",
                return_value=str(target),
            ):
                self.assertTrue(app.export_chat())
            content = target.read_text(encoding="utf-8")
            self.assertIn("# محادثة الوكيل المحلي", content)
            self.assertIn("## أنت\n\nاختبر", content)
            self.assertIn("## الوكيل\n\nمرحبا", content)
            self.assertIn("## الوكيل\n\nمحفوظ", content)
            self.assertIn("- مساحة العمل:", content)
            self.assertIn("- النموذج:", content)

    def test_app_quick_prompts_and_session_stats(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)
        self.assertEqual(app.stats_var.get(), "الجلسة: 0 طلب")

        app.insert_quick_prompt("لخّص")
        self.assertIn("لخّص", app.prompt_text.get("1.0", "end-1c"))
        app._clear_prompt()
        self.assertEqual(app.prompt_text.get("1.0", "end-1c"), "")

        app.prompt_text.insert("1.0", "اختبر")
        self.assertTrue(app.send())
        self.assertIn("1 طلب", app.stats_var.get())

        with patch.object(app.transcript, "see"):
            controller.events = [("status", "تشغيل الأداة: read_file"), ("done", "")]
            app._pump()
        self.assertIn("1 أداة", app.stats_var.get())

        app.prompt_text.insert("1.0", "مرّة أخرى")
        self.assertTrue(app.send())
        with patch.object(app.transcript, "see"):
            controller.events = [("answer", "تم"), ("done", "")]
            app._pump()
        self.assertIn("2 طلب", app.stats_var.get())
        self.assertIn("1 اكتمل", app.stats_var.get())

        app.prompt_text.insert("1.0", "أخيرة")
        self.assertTrue(app.send())
        with patch.object(app.transcript, "see"):
            controller.events = [("denied", "رفض"), ("done", "")]
            app._pump()
        self.assertIn("1 متوقف", app.stats_var.get())

    def test_tool_activity_line_and_busy_timer(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)
        with patch.object(app.transcript, "see"):
            controller.events = [("status", "تشغيل الأداة: read_file"), ("done", "")]
            app._pump()
        self.assertIn("◆ أداة: read_file", app.transcript.get("1.0", "end"))
        self.assertIn("tool", app.transcript.tag_names())

        app._set_busy(True)
        app._set_status("الوكيل يعمل…", "copper")
        app._busy_since = time.monotonic() - 7
        app._tick_busy_timer()
        self.assertEqual(app.status_var.get(), "الوكيل يعمل… 7 ث")
        app._set_busy(False)
        self.assertIsNone(app._busy_since)

    def test_settings_persist_theme_workspace_and_smart_briefing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir, "proj")
            workspace.mkdir()
            (workspace / "a.py").write_text("x", encoding="utf-8")
            (workspace / "b.py").write_text("x", encoding="utf-8")
            (workspace / ".env").write_text("T=1", encoding="utf-8")
            settings_path = Path(temp_dir, "settings.json")
            controller = self.FakeController()
            app = LocalAgentApp(
                self.root, controller=controller, settings=AppSettings(settings_path)
            )
            self.assertTrue(app._dark_theme)  # الداكن هو الافتراضي الآن

            app.workspace_var.set(str(workspace))
            with patch.object(controller, "configure", return_value=[]):
                self.assertTrue(app.apply_configuration())
            transcript = app.transcript.get("1.0", "end")
            self.assertIn("مساحة العمل جاهزة: 2 ملفًا", transcript)
            self.assertIn("Python (2)", transcript)

            toggled = not app._dark_theme
            app.toggle_theme()  # يتحقق أن أي تبديل يُحفظ ويُستعاد بعد إعادة الفتح
            self.assertEqual(app._dark_theme, toggled)
            saved = AppSettings(settings_path).load()
            self.assertEqual(saved["dark_theme"], toggled)
            self.assertEqual(saved["workspace"], str(workspace))

            second_root = tk.Tk()
            second_root.withdraw()
            try:
                app2 = LocalAgentApp(
                    second_root,
                    controller=self.FakeController(),
                    settings=AppSettings(settings_path),
                )
                self.assertEqual(app2._dark_theme, toggled)
                self.assertEqual(app2.workspace_var.get(), str(workspace))
            finally:
                if second_root.winfo_exists():
                    second_root.destroy()

    @staticmethod
    def _wait_probe(app):
        thread = app._probe_thread
        if thread is not None:
            thread.join(timeout=2)

    def test_design_elements_exist_and_follow_the_theme(self):
        app = LocalAgentApp(self.root, controller=self.FakeController())
        self.assertIn("Ctrl+K", app.footer_hint.cget("text"))
        self.assertEqual(app.status_dot.cget("text"), "●")
        self.assertEqual(len(app.starter_cards), 4)
        self.assertTrue(app.navigation_panel.winfo_exists())
        self.assertTrue(app.privacy_card.winfo_exists())
        app._navigation_buttons[2].invoke()
        self.assertEqual(app.notebook.select(), str(app.skills_page))
        app.permissions_button.invoke()
        self.assertEqual(app.notebook.select(), str(app.integrations_page))
        self.assertIn("hand2", str(app.apply_button.cget("cursor")))
        self.assertIn("hand2", str(app.send_button.cget("cursor")))

        app._set_status("تجربة", "copper")
        self.assertEqual(app.status_dot.cget("fg"), app.status_label.cget("fg"))

        app.toggle_theme()
        self.assertEqual(app.status_dot.cget("bg"), app.status_label.cget("bg"))
        self.assertEqual(app.footer_hint.cget("bg"), app.colors["surface"])

    def test_message_formatting_tags_and_copy_last_answer(self):
        app = LocalAgentApp(self.root, controller=self.FakeController())
        app._clear_transcript()
        self.assertFalse(app.copy_last_answer())
        app._append_chat("assistant", "# الخلاصة\n- نقطة أولى\n2) نقطة ثانية\nنص عادي")
        transcript = app.transcript
        self.assertIn("msg_heading", transcript.tag_names())
        self.assertIn("msg_bullet", transcript.tag_names())
        content = transcript.get("1.0", "end")
        self.assertIn("الخلاصة", content)
        self.assertIn("نقطة أولى", content)
        self.assertIn("نص عادي", content)

        app._append_chat("assistant", "رد قابل للنسخ")
        self.assertTrue(app.copy_last_answer())
        self.assertIn("تم نسخ", app.status_var.get())
        self.assertEqual(self.root.clipboard_get(), "رد قابل للنسخ")

        menu_holder = []

        def fake_popup(self_menu, x, y):
            menu_holder.append(self_menu)
            self_menu.grab_release()

        with patch.object(tk.Menu, "tk_popup", fake_popup):
            try:
                app._show_text_menu(
                    type("Event", (), {"x_root": 0, "y_root": 0})(),
                    app.transcript,
                )
            except tk.TclError:
                pass
        self.assertEqual(len(menu_holder), 1)
        self.assertEqual(menu_holder[0].index("end") + 1, 4)

    def test_connection_probe_reports_status_and_guidance(self):
        controller = self.FakeController()
        app = LocalAgentApp(self.root, controller=controller)
        self.assertEqual(app.connection_var.get(), "LM Studio: لم يُفحص")

        with patch(
            "desktop_app.probe_model_server",
            return_value=(False, "رفض الاتصال"),
        ):
            app.check_connection()
            self._wait_probe(app)
            app._pump()
        self.assertEqual(app.connection_var.get(), "LM Studio: لا استجابة")
        transcript = app.transcript.get("1.0", "end")
        self.assertIn("تعذر الوصول إلى LM Studio (رفض الاتصال)", transcript)
        self.assertIn("lms server start --port 1234", transcript)

        with patch(
            "desktop_app.probe_model_server",
            return_value=(True, "HTTP 200"),
        ):
            app.check_connection(quiet=True)
            self._wait_probe(app)
            app._pump()
        self.assertEqual(app.connection_var.get(), "LM Studio: متصل")

    def test_remote_probe_url_is_rejected_before_contacting_it(self):
        from desktop_core import probe_model_server

        ok, detail = probe_model_server("http://example.com/v1")
        self.assertFalse(ok)
        self.assertIn("المحلي فقط", detail)
        ok, detail = probe_model_server("not-a-url")
        self.assertFalse(ok)
        self.assertIn("غير صالح", detail)

    def test_list_models_rejects_remote_and_parses_local_ids(self):
        from desktop_core import list_models

        # عناوين غير محلية تُرفض دون اتصال.
        self.assertEqual(list_models("http://example.com/v1"), [])
        self.assertEqual(list_models("not-a-url"), [])

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *_a):
                return self._data

        payload = b'{"data":[{"id":"b-model"},{"id":"a-model"},{"id":"a-model"},{"bad":1}]}'

        class FakeOpener:
            def open(self, *_a, **_k):
                return FakeResponse(payload)

        with patch("desktop_core.build_opener", return_value=FakeOpener()):
            models = list_models("http://127.0.0.1:1234/v1")
        self.assertEqual(models, ["a-model", "b-model"])  # مرتّبة وبلا تكرار

    def test_refresh_models_populates_combobox(self):
        app = LocalAgentApp(self.root, controller=self.FakeController())
        with patch("desktop_app.list_models", return_value=["m1", "m2"]):
            app.refresh_models()
            thread = app._models_thread
            if thread is not None:
                thread.join(timeout=2)
            app._pump()
        self.assertEqual(app.model_combo.cget("values"), ("m1", "m2"))


if __name__ == "__main__":
    unittest.main()
