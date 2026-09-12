import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent import (
    ApprovalDenied,
    LMStudioClient,
    LocalAgent,
    SessionStore,
    ToolError,
    WorkspaceTools,
    main,
    safe_terminal_text,
)
from mcp_client import MCPApprovalDenied


class SessionStoreTests(unittest.TestCase):
    def test_session_round_trip_is_bounded_hidden_and_atomic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_store = SessionStore(root, "default-location")
            self.assertFalse(default_store.path.is_relative_to(root))
            store = SessionStore(root, "demo", max_bytes=1_000, state_root=root / ".state")
            history = [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
            ]
            store.save(history)

            self.assertEqual(store.load(), history)
            before = store.path.read_bytes()
            with patch("session_store.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    store.save([{"role": "user", "content": "changed"}])
            self.assertEqual(store.path.read_bytes(), before)

    def test_session_rejects_unsafe_names_and_invalid_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("", "../escape", "a/b", ".env", "name:stream", "CON"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    SessionStore(root, name, state_root=root / ".state")

            store = SessionStore(root, "demo", max_bytes=100, state_root=root / ".state")
            store.path.parent.mkdir()
            invalid_states = [
                b"not json",
                b"{}",
                json.dumps([{"role": "tool", "content": "x"}]).encode(),
                json.dumps([{"role": "user", "content": 1}]).encode(),
                json.dumps([{"role": "user", "content": "x", "extra": True}]).encode(),
                b"[" + (b" " * 101) + b"]",
            ]
            for state in invalid_states:
                with self.subTest(state=state[:20]):
                    store.path.write_bytes(state)
                    with self.assertRaises(ToolError):
                        store.load()


class LocalAgentTests(unittest.TestCase):
    def test_denied_approval_stops_the_turn_without_model_retry(self):
        class RetryingClient:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools, model):
                self.calls += 1
                return {"choices": [{"message": {
                    "role": "assistant",
                    "tool_calls": [{"id": f"call-{self.calls}", "function": {
                        "name": "create_file",
                        "arguments": json.dumps(
                            {"path": "denied.txt", "content": "x" * self.calls}
                        ),
                    }}],
                }}]}

        approvals = []
        with tempfile.TemporaryDirectory() as temp_dir:
            client = RetryingClient()
            agent = LocalAgent(
                client,
                WorkspaceTools(
                    temp_dir,
                    coding=True,
                    approver=lambda *_args: approvals.append(True) or False,
                ),
            )
            with self.assertRaises(ApprovalDenied):
                agent.answer("create")
            self.assertFalse(Path(temp_dir, "denied.txt").exists())

        self.assertEqual(client.calls, 1)
        self.assertEqual(len(approvals), 1)

    def test_agent_exposes_tools_for_the_selected_mode_only(self):
        class SchemaClient:
            def __init__(self):
                self.tool_names = []

            def chat(self, messages, tools, model):
                self.tool_names = [tool["function"]["name"] for tool in tools]
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            read_client = SchemaClient()
            LocalAgent(read_client, WorkspaceTools(temp_dir)).answer("read")
            self.assertNotIn("create_file", read_client.tool_names)
            self.assertNotIn("run_command", read_client.tool_names)

            coding_client = SchemaClient()
            LocalAgent(
                coding_client,
                WorkspaceTools(temp_dir, coding=True, approver=lambda *_args: True),
            ).answer("edit")
            self.assertIn("create_file", coding_client.tool_names)
            self.assertIn("replace_text", coding_client.tool_names)
            self.assertIn("review_changes", coding_client.tool_names)
            self.assertNotIn("run_command", coding_client.tool_names)

            command_client = SchemaClient()
            LocalAgent(
                command_client,
                WorkspaceTools(
                    temp_dir,
                    coding=True,
                    allow_host_commands=True,
                    approver=lambda *_args: True,
                ),
            ).answer("test")
            self.assertIn("run_command", command_client.tool_names)

    def test_agent_executes_an_allowed_tool_then_returns_the_answer(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def chat(self, messages, tools, model):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": json.dumps(
                                                    {"path": "note.txt"}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": "تمت القراءة"}}
                    ]
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "note.txt").write_text("محتوى", encoding="utf-8")
            client = FakeClient()
            agent = LocalAgent(client, WorkspaceTools(temp_dir), model="gemma")

            answer = agent.answer("اقرأ الملف")

        self.assertEqual(answer, "تمت القراءة")
        tool_message = client.calls[1][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["content"], "محتوى")

    def test_agent_loads_matching_skill_as_untrusted_text(self):
        class FakeCatalog:
            def __init__(self):
                self.calls = []

            @staticmethod
            def tool_schemas():
                return [{
                    "type": "function",
                    "function": {
                        "name": "read_skill",
                        "description": "read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]

            def dispatch(self, name, arguments):
                self.calls.append((name, arguments))
                return {
                    "instructions": "تعليمات تجريبية",
                    "trust": "untrusted_skill_instructions",
                }

        class FakeClient:
            def __init__(self):
                self.calls = []

            def chat(self, messages, tools, model):
                self.calls.append((messages, tools))
                if len(self.calls) == 1:
                    return {"choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "skill-1",
                            "function": {
                                "name": "read_skill",
                                "arguments": json.dumps({"name": "demo-skill"}),
                            },
                        }],
                    }}]}
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = FakeCatalog()
            client = FakeClient()
            agent = LocalAgent(
                client,
                WorkspaceTools(temp_dir),
                skill_catalog=catalog,
            )
            answer = agent.answer("استخدم المهارة")

        self.assertEqual(answer, "تم")
        self.assertEqual(catalog.calls, [("read_skill", {"name": "demo-skill"})])
        first_messages, first_tools = client.calls[0]
        self.assertIn("تعليمات المهارات", first_messages[0]["content"])
        self.assertIn("read_skill", [tool["function"]["name"] for tool in first_tools])
        tool_result = json.loads(client.calls[1][0][-1]["content"])
        self.assertEqual(tool_result["trust"], "untrusted_skill_instructions")

    def test_agent_returns_tool_errors_to_the_model(self):
        class FakeClient:
            def __init__(self):
                self.count = 0

            def chat(self, messages, tools, model):
                self.count += 1
                if self.count == 1:
                    return {
                        "choices": [{"message": {"role": "assistant", "tool_calls": [{
                            "id": "bad-call",
                            "function": {"name": "delete_file", "arguments": "{}"},
                        }]}}]
                    }
                self.tool_error = messages[-1]["content"]
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": "رفضت الأداة"}}
                    ]
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeClient()
            answer = LocalAgent(client, WorkspaceTools(temp_dir)).answer("احذف")

        self.assertEqual(answer, "رفضت الأداة")
        self.assertIn("أداة غير مسموحة", client.tool_error)

    def test_agent_rejects_empty_prompts_and_invalid_responses(self):
        class InvalidClient:
            def chat(self, messages, tools, model):
                return {}

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(InvalidClient(), WorkspaceTools(temp_dir))
            with self.assertRaises(ValueError):
                agent.answer(" ")
            with self.assertRaises(RuntimeError):
                agent.answer("اختبار")

        # ملاحظة: {"content": None} لم يعد خطأً صلبًا؛ يُعالَج بلطف (محاولة ثم رسالة
        # ودّية) ويُغطّى في اختبارات الرد الفارغ المخصّصة.
        invalid_messages = [
            [],
            {"role": "assistant", "tool_calls": {}},
            {"role": "assistant", "tool_calls": ["invalid"]},
        ]
        for message in invalid_messages:
            class InvalidMessageClient:
                def chat(self, messages, tools, model):
                    return {"choices": [{"message": message}]}

            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_dir:
                agent = LocalAgent(InvalidMessageClient(), WorkspaceTools(temp_dir))
                with self.assertRaises(RuntimeError):
                    agent.answer("اختبار")

    def test_agent_caps_tool_calls_and_keeps_bounded_clean_history(self):
        class RecordingWorkspace:
            def __init__(self):
                self.calls = 0

            def dispatch(self, name, arguments, client, embed_model):
                self.calls += 1
                return "ok"

        class TooManyToolsClient:
            def chat(self, messages, tools, model):
                return {"choices": [{"message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": f"call-{index}", "function": {
                            "name": "read_file", "arguments": '{"path":"note.txt"}'
                        }}
                        for index in range(3)
                    ],
                }}]}

        workspace = RecordingWorkspace()
        agent = LocalAgent(TooManyToolsClient(), workspace, max_tool_calls=2)
        with self.assertRaises(RuntimeError):
            agent.answer("اقرأ")
        self.assertEqual(workspace.calls, 0)

        workspace = RecordingWorkspace()
        agent = LocalAgent(TooManyToolsClient(), workspace, max_steps=1, max_tool_calls=3)
        # بدل خطأ صلب عند استنفاد الجولات: يُنتج خلاصة نهائية، والأدوات نُفّذت 3 مرات.
        answer = agent.answer("اقرأ")
        self.assertIn("الحد الأقصى للخطوات", answer)
        self.assertEqual(workspace.calls, 3)

        class HistoryClient:
            def __init__(self):
                self.responses = iter([
                    {"choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "call-1", "function": {
                            "name": "read_file", "arguments": '{"path":"note.txt"}'
                        }}],
                    }}]},
                    {"choices": [{"message": {"role": "assistant", "content": "الأول"}}]},
                    {"choices": [{"message": {"role": "assistant", "content": "الثاني"}}]},
                ])

            def chat(self, messages, tools, model):
                return next(self.responses)

        agent = LocalAgent(HistoryClient(), RecordingWorkspace(), max_history_turns=1)
        self.assertEqual(agent.answer("واحد"), "الأول")
        self.assertEqual([item["role"] for item in agent.history], ["system", "user", "assistant"])
        self.assertEqual(agent.answer("اثنان"), "الثاني")
        self.assertEqual([item["content"] for item in agent.history[1:]], ["اثنان", "الثاني"])

        class LargeResultClient:
            def __init__(self):
                self.count = 0

            def chat(self, messages, tools, model):
                self.count += 1
                if self.count == 1:
                    return {"choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "large", "function": {
                            "name": "read_file", "arguments": '{"path":"large.txt"}'
                        }}],
                    }}]}
                self.tool_result = messages[-1]["content"]
                return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

        client = LargeResultClient()
        workspace = RecordingWorkspace()
        workspace.dispatch = lambda *_args: "x" * 1_000
        LocalAgent(client, workspace, max_tool_result_bytes=64).answer("large")
        self.assertIn("truncated", client.tool_result)
        self.assertLessEqual(len(client.tool_result.encode("utf-8")), 64)

    def test_second_tool_result_survives_when_the_first_fills_the_budget(self):
        class Workspace:
            coding = False

            @staticmethod
            def tool_schemas():
                return [{
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object"},
                    },
                }]

            @staticmethod
            def dispatch(name, arguments, _client, _model):
                return "a" * 50_000 if arguments.get("path") == "a.txt" else "b" * 20

        class StarvingClient:
            def __init__(self):
                self.calls = 0
                self.tool_results = []

            def chat(self, messages, tools, model):
                self.calls += 1
                if self.calls == 1:
                    return {"choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "call-1", "function": {
                                "name": "read_file", "arguments": '{"path":"a.txt"}'
                            }},
                            {"id": "call-2", "function": {
                                "name": "read_file", "arguments": '{"path":"b.txt"}'
                            }},
                        ],
                    }}]}
                self.tool_results = [
                    message["content"] for message in messages if message.get("role") == "tool"
                ]
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        # الميزانية تُستهلك بالكامل بالنتيجة الأولى؛ الثانية تظل مع علامة قطع ظاهرة.
        client = StarvingClient()
        LocalAgent(client, Workspace(), max_tool_result_bytes=64).answer("اقرأ")
        self.assertEqual(len(client.tool_results), 2)
        self.assertIn("truncated", client.tool_results[0])
        self.assertLessEqual(len(client.tool_results[0].encode("utf-8")), 64)
        self.assertTrue(client.tool_results[1].strip().endswith("...[truncated]"))
        self.assertTrue(client.tool_results[1].strip())

        # السقف لكل نتيجة يمنع نتيجة ضخمة من ابتلاع الميزانية؛ ما بعدها يصل سليمًا.
        client = StarvingClient()
        LocalAgent(client, Workspace(), max_tool_result_bytes=64_000).answer("اقرأ")
        self.assertEqual(len(client.tool_results), 2)
        self.assertLessEqual(len(client.tool_results[0].encode("utf-8")), 8_000)
        self.assertIn("truncated", client.tool_results[0])
        self.assertEqual(client.tool_results[1], "b" * 20)

    def test_agent_loads_and_saves_the_optional_session(self):
        class AnswerClient:
            def chat(self, messages, tools, model):
                self.messages = messages
                return {"choices": [{"message": {"role": "assistant", "content": "new"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SessionStore(temp_dir, "demo", state_root=Path(temp_dir) / ".state")
            store.save(
                [
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "answer"},
                ]
            )
            client = AnswerClient()
            agent = LocalAgent(
                client,
                WorkspaceTools(temp_dir),
                max_history_turns=1,
                session=store,
            )

            self.assertEqual(agent.answer("next"), "new")
            self.assertEqual([message["content"] for message in client.messages[1:]], [
                "old",
                "answer",
                "next",
            ])
            self.assertEqual(store.load(), [
                {"role": "user", "content": "next"},
                {"role": "assistant", "content": "new"},
            ])

    def test_agent_sends_images_but_keeps_base64_out_of_history(self):
        class CaptureClient:
            def chat(self, messages, tools, model):
                self.messages = messages
                return {"choices": [{"message": {"role": "assistant", "content": "صورة"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "tiny.png")
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
            client = CaptureClient()
            agent = LocalAgent(client, WorkspaceTools(temp_dir))

            self.assertEqual(agent.answer("ما هذه؟", image_paths=[image]), "صورة")

        content = client.messages[-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "ما هذه؟"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIsInstance(agent.history[-2]["content"], str)
        self.assertNotIn("base64", agent.history[-2]["content"])

    def test_agent_routes_mcp_and_persistent_memory_tools(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools, model):
                self.calls += 1
                self.messages = messages
                self.tool_names = [item["function"]["name"] for item in tools]
                if self.calls == 1:
                    return {"choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "m1", "function": {
                            "name": "mcp_demo_ping",
                            "arguments": '{"value":"ok"}',
                        }}],
                    }}]}
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        class FakeRegistry:
            def __init__(self):
                self.calls = []
                self.closed = False

            @staticmethod
            def tool_schemas():
                return [{"type": "function", "function": {
                    "name": "mcp_demo_ping",
                    "description": "Ping",
                    "parameters": {"type": "object"},
                }}]

            def dispatch(self, name, arguments):
                self.calls.append((name, arguments))
                return {"pong": arguments["value"]}

            def close(self):
                self.closed = True

        class FakeMemory:
            def __init__(self):
                self.remembered = []

            def remember(self, texts, client, model):
                self.remembered.append((texts, client, model))

            @staticmethod
            def recall(query, client, model, limit=5):
                return [{"text": query, "score": 1.0}]

        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeClient()
            registry = FakeRegistry()
            memory = FakeMemory()
            agent = LocalAgent(
                client,
                WorkspaceTools(temp_dir),
                mcp_registry=registry,
                memory=memory,
            )
            self.assertEqual(agent.answer("افحص"), "تم")
            agent.close()

        self.assertIn("mcp_demo_ping", client.tool_names)
        self.assertIn("recall_memory", client.tool_names)
        self.assertEqual(registry.calls, [("mcp_demo_ping", {"value": "ok"})])
        self.assertIn("untrusted_mcp_output", client.messages[-1]["content"])
        self.assertEqual(memory.remembered[0][0], ["المستخدم: افحص", "الوكيل: تم"])
        self.assertTrue(registry.closed)

    def test_tool_name_collision_fails_before_model_call(self):
        class Client:
            def chat(self, *_args):
                raise AssertionError("model must not receive ambiguous tools")

        class Registry:
            @staticmethod
            def tool_schemas():
                return [{"type": "function", "function": {
                    "name": "list_files",
                    "parameters": {"type": "object"},
                }}]

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(Client(), WorkspaceTools(temp_dir), mcp_registry=Registry())
            with self.assertRaisesRegex(RuntimeError, "collide"):
                agent.answer("افحص")

    def test_mcp_connection_denial_stops_before_model_call(self):
        class Client:
            def chat(self, *_args):
                raise AssertionError("model must not run after denied MCP startup")

        class Registry:
            @staticmethod
            def tool_schemas():
                raise MCPApprovalDenied("denied")

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(Client(), WorkspaceTools(temp_dir), mcp_registry=Registry())
            with self.assertRaises(ApprovalDenied):
                agent.answer("افحص")

    def test_optional_memory_failure_does_not_hide_completed_answer(self):
        class Client:
            @staticmethod
            def chat(*_args):
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        class Memory:
            @staticmethod
            def remember(*_args):
                raise ValueError("sensitive text")

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(Client(), WorkspaceTools(temp_dir), memory=Memory())
            self.assertEqual(agent.answer("secret"), "تم")

    def test_relevant_memory_is_injected_as_context_automatically(self):
        class Client:
            def __init__(self):
                self.system_messages = []

            def chat(self, messages, tools, model):
                self.system_messages = [
                    m["content"] for m in messages if m["role"] == "system"
                ]
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        class Memory:
            @staticmethod
            def recall(query, client, model, limit=5):
                return [
                    {"text": "المستخدم يفضّل العربية الفصحى", "score": 0.82},
                    {"text": "تفصيل غير مرتبط", "score": 0.11},
                ]

            @staticmethod
            def remember(*_args):
                return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            client = Client()
            agent = LocalAgent(client, WorkspaceTools(temp_dir), memory=Memory())
            self.assertEqual(agent.answer("ما تفضيلاتي؟"), "تم")

        injected = "\n".join(client.system_messages)
        self.assertIn("ذكريات مطابقة", injected)
        self.assertIn("العربية الفصحى", injected)
        self.assertNotIn("تفصيل غير مرتبط", injected)  # دون العتبة الدلالية

    def test_failed_proactive_recall_does_not_break_the_turn(self):
        class Client:
            @staticmethod
            def chat(*_args):
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        class Memory:
            @staticmethod
            def recall(*_args):
                raise RuntimeError("no embedding server")

            @staticmethod
            def remember(*_args):
                return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(Client(), WorkspaceTools(temp_dir), memory=Memory())
            self.assertEqual(agent.answer("سؤال"), "تم")

    def test_session_save_failure_keeps_the_answer_and_warns(self):
        class Client:
            @staticmethod
            def chat(*_args):
                return {"choices": [{"message": {"role": "assistant", "content": "الجواب"}}]}

        class Session:
            @staticmethod
            def load():
                return []

            @staticmethod
            def save(_history):
                raise ToolError("full")

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(Client(), WorkspaceTools(temp_dir), session=Session())
            answer = agent.answer("السؤال")

        self.assertIn("الجواب", answer)
        self.assertIn("لم تُحفظ الجلسة", answer)
        self.assertEqual(agent.history[-1]["content"], "الجواب")

    def test_malformed_memory_tool_arguments_return_a_tool_error(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, _tools, _model):
                self.calls += 1
                self.messages = messages
                if self.calls == 1:
                    return {"choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "memory-1", "function": {
                            "name": "recall_memory",
                            "arguments": "null",
                        }}],
                    }}]}
                return {"choices": [{"message": {"role": "assistant", "content": "تم"}}]}

        class Memory:
            @staticmethod
            def recall(query, *_args):
                # الاستدعاء الاستباقي يمرّ نصًا سليمًا؛ الوسائط المشوّهة تُرفض
                # في مسار الأداة قبل بلوغ الذاكرة، فلا تصل هنا مطلقًا.
                if query != "تذكر":
                    raise AssertionError("invalid arguments must not reach memory")
                return []

            @staticmethod
            def remember(*_args):
                return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            client = Client()
            agent = LocalAgent(client, WorkspaceTools(temp_dir), memory=Memory())
            self.assertEqual(agent.answer("تذكر"), "تم")

        tool_message = next(item for item in client.messages if item["role"] == "tool")
        self.assertIn("Invalid memory arguments", tool_message["content"])

    def test_answer_stream_splits_thoughts_and_assembles_tool_calls(self):
        class StreamingClient:
            def __init__(self):
                self.calls = []

            def chat_stream(self, messages, tools, model):
                self.calls.append((messages, tools, model))
                if len(self.calls) == 1:
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": "read_", "arguments": '{"path":'},
                    }]}}]}
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "function": {"name": "file", "arguments": '"note.txt"}'},
                    }]}, "finish_reason": "tool_calls"}]}
                    return
                for content in ("<tho", "ught>خطة", "</thought>النت", "يجة"):
                    yield {"choices": [{"delta": {"content": content}}]}
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        class Workspace:
            coding = False

            @staticmethod
            def tool_schemas():
                return [{
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object"},
                    },
                }]

            @staticmethod
            def dispatch(name, arguments, _client, _model):
                if (name, arguments) != ("read_file", {"path": "note.txt"}):
                    raise AssertionError("streamed tool call was not assembled")
                return "x" * 100

        client = StreamingClient()
        agent = LocalAgent(client, Workspace(), max_tool_result_bytes=32)

        events = list(agent.answer_stream("نفذ"))

        self.assertEqual(
            "".join(event["delta"] for event in events if event["type"] == "thought"),
            "خطة",
        )
        self.assertEqual(
            "".join(event["delta"] for event in events if event["type"] == "token"),
            "النتيجة",
        )
        start = next(event for event in events if event["type"] == "tool_start")
        end = next(event for event in events if event["type"] == "tool_end")
        self.assertEqual((start["name"], start["id"]), ("read_file", "call-1"))
        self.assertEqual(end["name"], "read_file")
        self.assertIsInstance(end["status"], str)
        tool_result = next(
            message
            for message in client.calls[1][0]
            if message.get("role") == "tool"
        )
        self.assertLessEqual(len(tool_result["content"].encode("utf-8")), 32)
        self.assertNotIn("خطة", json.dumps(agent.history, ensure_ascii=False))
        self.assertEqual(agent.history[-1]["content"], "النتيجة")

    def test_answer_stream_keeps_length_truncated_reply_with_notice(self):
        class TruncatedClient:
            @staticmethod
            def chat_stream(*_args):
                yield {
                    "choices": [{
                        "delta": {"content": "partial"},
                        "finish_reason": "length",
                    }]
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(TruncatedClient(), WorkspaceTools(temp_dir))
            tokens = "".join(
                event["delta"]
                for event in agent.answer_stream("اختبر")
                if event["type"] == "token"
            )
            # النص الجزئي محفوظ ويظهر مع تنبيه الاقتطاع بدل إسقاطه كخطأ.
            self.assertIn("partial", tokens)
            self.assertIn("اقتُطع", tokens)
            self.assertEqual(agent.history[-1]["role"], "assistant")
            self.assertIn("partial", agent.history[-1]["content"])

        class MissingFinishClient:
            @staticmethod
            def chat_stream(*_args):
                yield {"choices": [{"delta": {"content": "partial"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = LocalAgent(MissingFinishClient(), WorkspaceTools(temp_dir))
            with self.assertRaisesRegex(RuntimeError, "finish reason"):
                list(agent.answer_stream("اختبر"))

    def test_empty_answer_retries_once_then_recovers(self):
        class EmptyThenAnswerClient:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools, model):
                self.calls += 1
                content = "<thought>تفكير فقط</thought>" if self.calls == 1 else "الرد النهائي"
                return {"choices": [{"message": {"role": "assistant", "content": content}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            client = EmptyThenAnswerClient()
            agent = LocalAgent(client, WorkspaceTools(temp_dir), model="gemma")
            answer = agent.answer("اقترح تحسينات")
            self.assertEqual(answer, "الرد النهائي")
            self.assertEqual(client.calls, 2)  # أعاد المحاولة مرة واحدة فنجح

    def test_empty_answer_twice_yields_friendly_message_without_error(self):
        class AlwaysEmptyClient:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools, model):
                self.calls += 1
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": "<thought>x</thought>"}}
                    ]
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            client = AlwaysEmptyClient()
            agent = LocalAgent(client, WorkspaceTools(temp_dir), model="gemma")
            answer = agent.answer("اقترح تحسينات")
            self.assertIn("اكتفى الوكيل بالتفكير", answer)
            self.assertEqual(client.calls, 2)  # الأصلية + محاولة واحدة


    def test_thoughts_stay_on_the_wire_between_steps_but_not_in_history(self):
        class WireClient:
            def __init__(self):
                self.calls = []

            def chat(self, messages, tools, model):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    return {"choices": [{"message": {
                        "role": "assistant",
                        "content": "<thought>سأقرأ الملف</thought>",
                        "tool_calls": [{"id": "call-1", "function": {
                            "name": "read_file", "arguments": '{"path":"note.txt"}'
                        }}],
                    }}]}
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "<thought>تحققت</thought>تمت القراءة",
                }}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "note.txt").write_text("محتوى", encoding="utf-8")
            client = WireClient()
            agent = LocalAgent(client, WorkspaceTools(temp_dir))

            answer = agent.answer("اقرأ")

        self.assertEqual(answer, "تمت القراءة")
        wire_step = client.calls[1][-2]
        self.assertEqual(wire_step["role"], "assistant")
        self.assertIn("سأقرأ الملف", wire_step["content"])
        self.assertNotIn("<thought>", json.dumps(agent.history[1:], ensure_ascii=False))
        self.assertEqual(agent.history[-1]["content"], "تمت القراءة")

    def test_older_turns_become_a_bounded_context_note_on_the_wire(self):
        class NoteClient:
            def __init__(self):
                self.messages = []

            def chat(self, messages, tools, model):
                self.messages.append(messages)
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": f"جواب {len(self.messages)}",
                }}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            client = NoteClient()
            agent = LocalAgent(client, WorkspaceTools(temp_dir), max_history_turns=1)
            agent.answer("واحد")
            agent.answer("اثنان")
            agent.answer("ثلاثة")

        self.assertEqual(len(agent.history), 3)
        self.assertEqual(agent.history[-1]["content"], "جواب 3")
        wire = client.messages[-1]
        note = wire[1]
        self.assertEqual(note["role"], "system")
        self.assertIn("ملخص محادثة سابقة", note["content"])
        self.assertIn("المستخدم: واحد", note["content"])
        self.assertIn("الوكيل: جواب 1", note["content"])
        self.assertNotIn("ملخص محادثة سابقة", json.dumps(agent.history, ensure_ascii=False))
        self.assertIn("ثلاثة", wire[-1]["content"])

    def test_tool_errors_carry_a_single_retry_hint(self):
        class HintClient:
            def __init__(self):
                self.count = 0

            def chat(self, messages, tools, model):
                self.count += 1
                if self.count == 1:
                    return {"choices": [{"message": {"role": "assistant", "tool_calls": [
                        {"id": "bad", "function": {"name": "delete_file", "arguments": "{}"}},
                    ]}}]}
                self.tool_error = messages[-1]["content"]
                return {"choices": [{"message": {"role": "assistant", "content": "توقفت"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            client = HintClient()
            answer = LocalAgent(client, WorkspaceTools(temp_dir)).answer("احذف")

        self.assertEqual(answer, "توقفت")
        self.assertIn("أداة غير مسموحة", client.tool_error)
        self.assertIn("أعد المحاولة مرة واحدة", client.tool_error)

    def test_repeated_identical_failing_call_is_short_circuited(self):
        class RepeatingWorkspace:
            def __init__(self):
                self.calls = 0

            def dispatch(self, name, arguments, client, embed_model):
                self.calls += 1
                raise ToolError("الملف غير موجود")

        class RepeatingClient:
            def __init__(self):
                self.count = 0

            def chat(self, messages, tools, model):
                self.count += 1
                if self.count <= 2:
                    return {"choices": [{"message": {"role": "assistant", "tool_calls": [
                        {"id": f"c{self.count}", "function": {
                            "name": "read_file", "arguments": '{"path": "note.txt"}'
                        }},
                    ]}}]}
                self.second_tool_error = messages[-1]["content"]
                return {"choices": [{"message": {"role": "assistant", "content": "توقفت"}}]}

        workspace = RepeatingWorkspace()
        client = RepeatingClient()
        answer = LocalAgent(client, workspace).answer("اقرأ")

        self.assertEqual(answer, "توقفت")
        self.assertEqual(workspace.calls, 1)
        self.assertIn("كرّرت استدعاء الأداة نفسه", client.second_tool_error)


class LMStudioClientTests(unittest.TestCase):
    def test_chat_stream_parses_sse_and_enforces_the_byte_limit(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b"".join((
                    b": keep-alive\n\n",
                    b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
                    b"data: invalid-json\n\n",
                    b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n',
                    b"data: [DONE]\n\n",
                    b'data: {"ignored":true}\n\n',
                ))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LMStudioClient(f"http://127.0.0.1:{server.server_port}/v1")
            chunks = list(client.chat_stream([], [], "qwen"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(
            [chunk["choices"][0]["delta"]["content"] for chunk in chunks],
            ["one", "two"],
        )
        self.assertTrue(requests[0]["stream"])

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter([b"data: " + b"x" * 80 + b"\n"])

        limited = LMStudioClient(max_response_bytes=32)
        with patch.object(limited, "_open", return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                list(limited.chat_stream([], [], "qwen"))

        incomplete = LMStudioClient()
        with patch.object(incomplete, "_open", return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, r"before \[DONE\]"):
                list(incomplete.chat_stream([], [], "qwen"))

    def test_close_interrupts_an_active_stream(self):
        started = threading.Event()
        closed = threading.Event()
        errors = []

        class Response:
            def __iter__(self):
                return self

            def __next__(self):
                started.set()
                closed.wait(2)
                raise ValueError("closed")

            @staticmethod
            def close():
                closed.set()

        client = LMStudioClient()
        with patch.object(client, "_open", return_value=Response()):
            worker = threading.Thread(
                target=lambda: errors.append(self._capture_stream_error(client))
            )
            worker.start()
            self.assertTrue(started.wait(1))
            client.close()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], RuntimeError)

        open_started = threading.Event()
        release_open = threading.Event()
        blocked = LMStudioClient()

        def open_response(*_args, **_kwargs):
            open_started.set()
            release_open.wait(2)
            return Response()

        with patch.object(blocked, "_open", side_effect=open_response):
            worker = threading.Thread(
                target=lambda: errors.append(self._capture_stream_error(blocked))
            )
            worker.start()
            self.assertTrue(open_started.wait(1))
            blocked.close()
            worker.join(1)
            release_open.set()
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[-1], RuntimeError)

    @staticmethod
    def _capture_stream_error(client):
        try:
            list(client.chat_stream([], [], "qwen"))
        except Exception as error:
            return error
        return None
    def test_client_uses_the_local_openai_compatible_endpoints(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                if self.path.endswith("/embeddings"):
                    response = {"data": [{"embedding": [1.0, 0.0]}]}
                else:
                    response = {
                        "choices": [
                            {"message": {"role": "assistant", "content": "مرحبًا"}}
                        ]
                    }
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LMStudioClient(f"http://127.0.0.1:{server.server_port}/v1")
            reply = client.chat([{"role": "user", "content": "مرحبًا"}], [], "gemma")
            embeddings = client.embed(["نص"], "nomic")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(reply["choices"][0]["message"]["content"], "مرحبًا")
        self.assertEqual(embeddings, [[1.0, 0.0]])
        self.assertEqual([request[0] for request in requests], [
            "/v1/chat/completions",
            "/v1/embeddings",
        ])

    def test_remote_servers_are_disabled(self):
        with self.assertRaises(ValueError):
            LMStudioClient("https://example.com/v1")
        with self.assertRaises(ValueError):
            LMStudioClient("http://example.com/v1")
        with self.assertRaises(ValueError):
            LMStudioClient("not-a-url")

    def test_client_rejects_invalid_embedding_responses(self):
        client = LMStudioClient()
        invalid_responses = [
            {},
            {"data": ["invalid"]},
            {"data": [{"embedding": "invalid"}]},
            {"data": [{"embedding": [float("nan")]}]},
            {"data": [{"embedding": [1.0]}, {"embedding": [1.0, 2.0]}]},
        ]
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(client, "_post", return_value=response):
                with self.assertRaises(RuntimeError):
                    client.embed(["one", "two"] if len(response.get("data", [])) == 2 else ["one"], "nomic")

    def test_client_rejects_redirects_and_oversized_responses(self):
        forwarded_authorization = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                forwarded_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(307)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target.server_port}/v1/chat/completions",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            client = LMStudioClient(
                f"http://127.0.0.1:{redirect.server_port}/v1", api_key="secret"
            )
            with self.assertRaises(RuntimeError):
                client.chat([], [], "gemma")
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join()
            target.shutdown()
            target.server_close()
            target_thread.join()

        self.assertEqual(forwarded_authorization, [])

        class OversizedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"12345"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), OversizedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LMStudioClient(
                f"http://127.0.0.1:{server.server_port}/v1", max_response_bytes=4
            )
            with self.assertRaises(RuntimeError):
                client.chat([], [], "gemma")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CLITests(unittest.TestCase):
    def test_terminal_output_escapes_controls_and_bidi(self):
        rendered = safe_terminal_text("ok\x1b[2J\u202efake\ud800")
        self.assertEqual(rendered, "ok\\u001b[2J\\u202efake\\ud800")

    def test_help_documents_the_workspace_boundary(self):
        result = subprocess.run(
            [sys.executable, "agent.py", "--help"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--workspace", result.stdout)
        self.assertIn("--coding", result.stdout)
        self.assertIn("--allow-host-commands", result.stdout)
        self.assertIn("--session", result.stdout)
        self.assertIn("--image", result.stdout)
        self.assertIn("--mcp-config", result.stdout)
        self.assertIn("--semantic-memory", result.stdout)
        self.assertIn("--command-timeout", result.stdout)
        self.assertIn("--max-command-output", result.stdout)
        self.assertIn("--max-tool-result-bytes", result.stdout)

    def test_cli_rejects_host_commands_without_coding_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                exit_code = main(["--workspace", temp_dir, "--allow-host-commands"])

        self.assertEqual(exit_code, 1)
        self.assertIn("--coding", error.getvalue())

    def test_cli_requires_a_named_session_for_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                exit_code = main(["--workspace", temp_dir, "--semantic-memory"])

        self.assertEqual(exit_code, 1)
        self.assertIn("--session", error.getvalue())

    def test_cli_rejects_an_explicit_empty_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            error = io.StringIO()
            with (
                patch("builtins.input", side_effect=EOFError),
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(["--workspace", temp_dir, "--prompt", ""])

        self.assertEqual(exit_code, 1)
        self.assertIn("Prompt cannot be empty", error.getvalue())

    def test_cli_answers_one_prompt_through_the_local_api(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                response = {
                    "choices": [{"message": {"role": "assistant", "content": "جاهز"}}]
                }
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(
                    [
                        "--workspace",
                        str(Path(__file__).parent),
                        "--base-url",
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "--prompt",
                        "اختبار",
                    ]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().strip(), "جاهز")

    def test_interactive_cli_can_exit_without_contacting_the_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with (
                patch("builtins.input", return_value="خروج"),
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(["--workspace", temp_dir])

        self.assertEqual(exit_code, 0)
        self.assertIn("الوكيل جاهز", output.getvalue())

    def test_cli_emits_utf8_on_windows(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": "جاهز"}}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "agent.py",
                    "--workspace",
                    str(Path(__file__).parent),
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "--prompt",
                    "اختبار",
                ],
                cwd=Path(__file__).parent,
                capture_output=True,
                timeout=10,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "جاهز")


if __name__ == "__main__":
    unittest.main()
