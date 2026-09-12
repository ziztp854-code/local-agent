import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import mcp_client
from mcp_client import MCPRegistry


FAKE_SERVER = r"""
import json
import os
import sys
import time

mode = os.environ.get("FAKE_MCP_MODE", "legacy")
initialized = False


def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    request = json.loads(raw)
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {})
    if method == "initialize":
        if mode == "modern":
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": "Method not found"}})
            continue
        if mode == "init_error":
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32000, "message": "authentication failed"}})
            continue
        if mode == "wrong_version":
            version = "2024-11-05"
        else:
            version = "2025-06-18"
        send({"jsonrpc": "2.0", "method": "notifications/message",
              "params": {"data": "before initialize response"}})
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        initialized = True
        sys.stderr.write("ready\n" + "x" * 100000)
        sys.stderr.flush()
    elif method == "tools/list":
        if mode != "modern" and not initialized:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32000, "message": "not initialized"}})
            continue
        if mode == "modern":
            meta = params.get("_meta", {})
            if (meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28"
                    or not isinstance(meta.get("io.modelcontextprotocol/clientInfo"), dict)
                    or meta.get("io.modelcontextprotocol/clientCapabilities") != {}):
                send({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": "missing modern metadata"}})
                continue
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        if mode == "collision":
            tools = [
                {"name": "a.b", "description": "first",
                 "inputSchema": {"type": "object", "description": "ignore me",
                                 "properties": {}}},
                {"name": "a_b", "description": "second",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]
            result = {"tools": tools}
        elif params.get("cursor") == "page-2":
            tools = [{"name": "space tool", "description": "Second page",
                      "inputSchema": {"type": "object", "properties": {}}}]
            result = {"tools": tools}
        else:
            tools = [{"name": "echo", "description": "Echo text",
                      "inputSchema": {"type": "object", "properties": {
                          "value": {"type": "string"}}, "required": ["value"]},
                      "annotations": {"readOnlyHint": True}}]
            result = {"tools": tools, "nextCursor": "page-2"}
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "tools/call":
        if mode == "modern":
            meta = params.get("_meta", {})
            if meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28":
                send({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": "missing modern metadata"}})
                continue
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "hang":
            time.sleep(10)
        elif name == "large":
            send({"jsonrpc": "2.0", "id": request_id,
                  "result": {"content": [{"type": "text", "text": "x" * 200000}]}})
        elif name == "surrogate":
            send({"jsonrpc": "2.0", "id": request_id,
                  "result": {"content": [{"type": "text", "text": chr(0xD800)}]}})
        elif name == "image_only":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [
                {"type": "image", "data": "secret-image", "mimeType": "image/png"},
                {"type": "audio", "data": "secret-audio", "mimeType": "audio/wav"},
                {"type": "resource_link", "uri": "file:///secret", "name": "secret"},
            ]}})
        elif name == "rpc_error":
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32602, "message": "secret server detail"}})
        else:
            structured = {"seen": arguments}
            if mode == "env_check":
                structured = {
                    "ambient": os.environ.get("LEAK_ME"),
                    "explicit": os.environ.get("EXPLICIT_OK"),
                }
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [
                    {"type": "text", "text": f"echo:{arguments.get('value', '')}"},
                    {"type": "image", "data": "ignored", "mimeType": "image/png"}],
                "structuredContent": structured, "isError": False}})
"""


class MCPRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.server = self.root / "fake_server.py"
        self.server.write_text(FAKE_SERVER, encoding="utf-8")
        self.registries = []

    def tearDown(self):
        for registry in self.registries:
            registry.close()
        self.temp_dir.cleanup()

    def registry(self, *, mode="legacy", approver=lambda *_args: True, server_env=None, **limits):
        config = self.root / f"mcp-{len(self.registries)}.json"
        environment = {"FAKE_MCP_MODE": mode, **(server_env or {})}
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "demo": {
                            "command": sys.executable,
                            "args": [str(self.server)],
                            "env": environment,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        registry = MCPRegistry(config, approver, self.root, **limits)
        self.registries.append(registry)
        return registry

    def test_legacy_initialize_pagination_schemas_and_call_approval(self):
        approvals = []
        registry = self.registry(approver=lambda action, preview: approvals.append((action, preview)) or True)

        schemas = registry.tool_schemas()

        self.assertEqual(
            [item["function"]["name"] for item in schemas],
            ["mcp_demo_echo", "mcp_demo_space_tool"],
        )
        self.assertEqual(schemas[0]["function"]["parameters"]["required"], ["value"])
        self.assertEqual([action for action, _preview in approvals], ["connect_mcp"])
        self.assertIn("SHA256", approvals[0][1])
        self.assertIn("unisolated", approvals[0][1])
        self.assertIn('"FAKE_MCP_MODE":"legacy"', approvals[0][1])

        result = registry.dispatch("mcp_demo_echo", {"value": "hello"})

        self.assertEqual(
            result,
            {
                "text": ["echo:hello"],
                "structuredContent": {"seen": {"value": "hello"}},
                "isError": False,
                "unsupportedContentTypes": ["image"],
            },
        )
        self.assertEqual([action for action, _preview in approvals], ["connect_mcp", "call_mcp"])
        self.assertIn('"value":"hello"', approvals[1][1])
        self.assertIn("SHA256", approvals[1][1])

    def test_modern_fallback_adds_per_request_metadata(self):
        registry = self.registry(mode="modern")

        self.assertEqual(len(registry.tool_schemas()), 2)
        result = registry.dispatch("mcp_demo_echo", {"value": "modern"})

        self.assertEqual(result["text"], ["echo:modern"])

    def test_initialize_non_compatibility_error_does_not_downgrade(self):
        registry = self.registry(mode="init_error")
        with self.assertRaisesRegex(RuntimeError, "initialize"):
            registry.tool_schemas()

    def test_protocol_version_mismatch_is_rejected(self):
        registry = self.registry(mode="wrong_version")
        with self.assertRaisesRegex(RuntimeError, "protocol version"):
            registry.tool_schemas()

    def test_connection_and_every_call_require_fresh_explicit_approval(self):
        denied = self.registry(approver=lambda *_args: False)
        with self.assertRaisesRegex(RuntimeError, "approval"):
            denied.tool_schemas()

        calls = 0

        def approve_connect_only(action, _preview):
            nonlocal calls
            calls += 1
            return action == "connect_mcp"

        registry = self.registry(approver=approve_connect_only)
        registry.tool_schemas()
        with self.assertRaisesRegex(RuntimeError, "approval"):
            registry.dispatch("mcp_demo_echo", {"value": "no"})
        self.assertEqual(calls, 2)

    def test_close_during_approval_prevents_late_subprocess_start(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def blocking_approval(_action, _preview):
            entered.set()
            release.wait(2)
            return True

        registry = self.registry(approver=blocking_approval)

        def connect():
            try:
                registry.tool_schemas()
            except Exception as error:
                errors.append(error)

        with patch("mcp_client.subprocess.Popen", side_effect=AssertionError("late start")) as popen:
            worker = threading.Thread(target=connect)
            worker.start()
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=registry.close)
            closer.start()
            closer.join(0.5)
            self.assertFalse(closer.is_alive(), "close waited for connect approval")
            release.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(popen.called)
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "closed")

    def test_close_sees_registered_server_and_invalidates_inflight_connect(self):
        starting = threading.Event()
        release = threading.Event()
        errors = []
        registry = self.registry()

        def blocked_start(_server):
            starting.set()
            release.wait(2)
            return []

        def connect():
            try:
                registry.tool_schemas()
            except Exception as error:
                errors.append(error)

        with patch.object(mcp_client._MCPServer, "start", blocked_start):
            worker = threading.Thread(target=connect)
            worker.start()
            self.assertTrue(starting.wait(1))
            self.assertIn("demo", registry._servers)
            closer = threading.Thread(target=registry.close)
            closer.start()
            closer.join(0.5)
            self.assertFalse(closer.is_alive(), "close waited for server startup")
            release.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "closed")

    def test_approval_preview_escapes_terminal_controls(self):
        previews = []
        registry = self.registry(approver=lambda _action, preview: previews.append(preview) or True)
        registry.tool_schemas()

        registry.dispatch("mcp_demo_echo", {"value": "\x1b[2J\u202ereversed"})

        self.assertNotIn("\x1b", previews[-1])
        self.assertNotIn("\u202e", previews[-1])
        self.assertIn("\\u001b", previews[-1])
        self.assertIn("\\u202e", previews[-1])

    def test_invalid_configs_are_rejected_before_approval(self):
        invalid_servers = [
            {"command": sys.executable, "shell": True},
            {"command": sys.executable, "args": "not-a-list"},
            {"command": sys.executable, "env": {"TOKEN": 1}},
            {"command": ""},
            {"command": sys.executable, "disabled": "yes"},
            {"command": sys.executable, "unknown": True},
            {"command": "cmd.exe"},
            {"command": "sh"},
            {"command": "tool.cmd"},
            {"command": sys.executable, "env": {"Path": "untrusted"}},
            {"command": sys.executable, "env": {"API_TOKEN": "literal-secret"}},
            {"command": sys.executable, "env": {"PYTHONPATH": "untrusted"}},
            {"command": sys.executable, "env": {"PYTHONHOME": "untrusted"}},
            {"command": sys.executable, "env": {"NODE_OPTIONS": "--require=bad"}},
            {"command": sys.executable, "env": {"NODE_PATH": "untrusted"}},
            {"command": sys.executable, "env": {"LD_PRELOAD": "untrusted"}},
            {"command": sys.executable, "env": {"DYLD_INSERT_LIBRARIES": "untrusted"}},
        ]
        for index, server in enumerate(invalid_servers):
            with self.subTest(index=index):
                config = self.root / f"invalid-{index}.json"
                config.write_text(json.dumps({"mcpServers": {"bad": server}}), encoding="utf-8")
                approvals = []
                with self.assertRaises(ValueError):
                    MCPRegistry(config, lambda *_args: approvals.append(True), self.root)
                self.assertEqual(approvals, [])

    def test_disabled_server_may_omit_command_and_is_not_started(self):
        config = self.root / "disabled.json"
        config.write_text(json.dumps({"mcpServers": {"off": {"disabled": True}}}), encoding="utf-8")
        approvals = []
        registry = MCPRegistry(config, lambda *_args: approvals.append(True), self.root)
        self.registries.append(registry)

        self.assertEqual(registry.tool_schemas(), [])
        self.assertEqual(approvals, [])

    def test_subprocess_gets_only_allowlisted_and_explicit_environment(self):
        with patch.dict(os.environ, {"LEAK_ME": "must-not-leak"}):
            registry = self.registry(mode="env_check", server_env={"EXPLICIT_OK": "visible"})
            registry.tool_schemas()
            result = registry.dispatch("mcp_demo_echo", {"value": "env"})

        self.assertEqual(result["structuredContent"], {"ambient": None, "explicit": "visible"})

    def test_secret_environment_uses_reference_and_redacted_stable_fingerprint(self):
        with patch.dict(os.environ, {"MCP_TEST_SECRET": "top-secret"}):
            registry = self.registry(mode="env_check", server_env={"EXPLICIT_OK": "${MCP_TEST_SECRET}"})
            registry.tool_schemas()
            result = registry.dispatch("mcp_demo_echo", {"value": "env"})
        self.assertEqual(result["structuredContent"]["explicit"], "top-secret")

        fingerprints = []
        for secret in ("first-value", "second-value"):
            previews = []
            with patch.dict(os.environ, {"MCP_TEST_SECRET": secret}):
                denied = self.registry(
                    mode="env_check",
                    server_env={"EXPLICIT_OK": "${MCP_TEST_SECRET}"},
                    approver=lambda _action, preview: previews.append(preview) or False,
                )
                with self.assertRaisesRegex(RuntimeError, "approval"):
                    denied.tool_schemas()
            self.assertNotIn(secret, previews[0])
            fingerprints.append(previews[0].split("SHA256: ", 1)[1])
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_connect_preview_and_fingerprint_bind_nonsecret_values_and_references(self):
        previews = []
        with patch.dict(os.environ, {"MCP_TEST_SECRET": "resolved-secret"}):
            first = self.registry(
                server_env={"MODE": "safe", "API_TOKEN": "${MCP_TEST_SECRET}"},
                approver=lambda _action, preview: previews.append(preview) or False,
            )
            with self.assertRaisesRegex(RuntimeError, "approval"):
                first.tool_schemas()
            second = self.registry(
                server_env={"MODE": "changed", "API_TOKEN": "${MCP_TEST_SECRET}"},
                approver=lambda _action, preview: previews.append(preview) or False,
            )
            with self.assertRaisesRegex(RuntimeError, "approval"):
                second.tool_schemas()

        self.assertIn('"MODE":"safe"', previews[0])
        self.assertIn('"API_TOKEN":"${MCP_TEST_SECRET}"', previews[0])
        self.assertNotIn("resolved-secret", previews[0])
        self.assertNotEqual(
            previews[0].split("SHA256: ", 1)[1],
            previews[1].split("SHA256: ", 1)[1],
        )

    @unittest.skipUnless(os.name == "nt", "Windows npx launcher rule")
    def test_npx_is_rewritten_to_node_and_js_without_spawning(self):
        bin_dir = self.root / "node-bin"
        cli = bin_dir / "node_modules" / "npm" / "bin" / "npx-cli.js"
        cli.parent.mkdir(parents=True)
        (bin_dir / "node.exe").write_bytes(b"not executed")
        cli.write_text("// not executed", encoding="utf-8")
        config = self.root / "npx.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "npm": {
                            "command": "npx",
                            "args": ["example-mcp"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        previews = []
        registry = MCPRegistry(
            config,
            lambda _action, preview: previews.append(preview) or False,
            self.root,
        )
        self.registries.append(registry)

        with patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaisesRegex(RuntimeError, "approval"):
                registry.tool_schemas()

        self.assertIn("node.exe", previews[0])
        self.assertIn("npx-cli.js", previews[0])
        self.assertIn("example-mcp", previews[0])

    def test_tool_name_collisions_are_casefold_safe_and_schema_text_is_stripped(self):
        registry = self.registry(mode="collision")

        schemas = registry.tool_schemas()
        names = [schema["function"]["name"] for schema in schemas]

        self.assertEqual(len(names), 2)
        self.assertEqual(len({name.casefold() for name in names}), 2)
        self.assertTrue(all(name.startswith("mcp_demo_a_b") for name in names))
        self.assertNotIn("description", schemas[0]["function"]["parameters"])
        self.assertTrue(schemas[0]["function"]["description"].startswith("Untrusted MCP"))

    def test_call_preview_redacts_nested_secrets_but_keeps_other_arguments(self):
        previews = []
        registry = self.registry(approver=lambda _action, preview: previews.append(preview) or True)
        registry.tool_schemas()

        registry.dispatch(
            "mcp_demo_echo",
            {
                "value": "visible",
                "api_key": "hidden",
                "nested": {"password": "hidden2"},
            },
        )

        call_preview = previews[-1]
        self.assertIn("visible", call_preview)
        self.assertNotIn("hidden", call_preview)
        self.assertNotIn("hidden2", call_preview)
        self.assertGreaterEqual(call_preview.count("<redacted>"), 2)

    def test_duplicate_json_config_keys_are_rejected(self):
        config = self.root / "duplicate.json"
        config.write_text(
            '{"mcpServers":{"one":{"command":"a"},"one":{"command":"b"}}}',
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            MCPRegistry(config, lambda *_args: True, self.root)

    def test_unknown_tool_and_invalid_arguments_fail_without_approval(self):
        approvals = []
        registry = self.registry(approver=lambda action, preview: approvals.append((action, preview)) or True)
        registry.tool_schemas()

        with self.assertRaises(ValueError):
            registry.dispatch("not_mcp", {})
        with self.assertRaises(ValueError):
            registry.dispatch("mcp_demo_echo", [])

        self.assertEqual([action for action, _preview in approvals], ["connect_mcp"])

    def test_timeout_and_oversized_result_fail_closed(self):
        registry = self.registry(request_timeout=0.15, max_result_bytes=1_000)
        registry.tool_schemas()
        registry._tools["mcp_demo_hang"] = (registry._servers["demo"], "hang")
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            registry.dispatch("mcp_demo_hang", {})

        registry = self.registry(max_result_bytes=1_000)
        registry.tool_schemas()
        registry._tools["mcp_demo_large"] = (registry._servers["demo"], "large")
        with self.assertRaisesRegex(RuntimeError, "too large"):
            registry.dispatch("mcp_demo_large", {})

    def test_lone_unicode_surrogate_from_server_is_rejected(self):
        registry = self.registry()
        registry.tool_schemas()
        registry._tools["mcp_demo_surrogate"] = (registry._servers["demo"], "surrogate")

        with self.assertRaisesRegex(RuntimeError, "Unicode"):
            registry.dispatch("mcp_demo_surrogate", {})

    def test_nontext_result_reports_types_without_payloads(self):
        registry = self.registry()
        registry.tool_schemas()
        registry._tools["mcp_demo_image_only"] = (registry._servers["demo"], "image_only")

        result = registry.dispatch("mcp_demo_image_only", {})

        self.assertEqual(
            result,
            {
                "text": [],
                "structuredContent": None,
                "isError": False,
                "unsupportedContentTypes": ["image", "audio", "resource_link"],
            },
        )
        self.assertNotIn("secret", json.dumps(result))

    def test_rpc_error_is_safe_and_close_is_idempotent(self):
        registry = self.registry()
        registry.tool_schemas()
        registry._tools["mcp_demo_rpc_error"] = (registry._servers["demo"], "rpc_error")

        with self.assertRaisesRegex(RuntimeError, "-32602") as caught:
            registry.dispatch("mcp_demo_rpc_error", {})
        self.assertNotIn("secret server detail", str(caught.exception))

        registry.close()
        registry.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            registry.tool_schemas()

    def test_loopback_http_transport_connects_and_calls_a_tool(self):
        methods = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                methods.append(request.get("method"))
                request_id = request.get("id")
                if request_id is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if request["method"] == "initialize":
                    result = {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "http-test", "version": "1"},
                    }
                elif request["method"] == "tools/list":
                    result = {"tools": [{
                        "name": "ping",
                        "description": "Ping",
                        "inputSchema": {"type": "object", "properties": {}},
                    }]}
                else:
                    result = {
                        "content": [{"type": "text", "text": "pong"}],
                        "structuredContent": {"ok": True},
                        "isError": False,
                    }
                message = {"jsonrpc": "2.0", "id": request_id, "result": result}
                if request["method"] == "tools/list":
                    body = (
                        ": keep-alive\n"
                        + "data: "
                        + json.dumps(message)
                        + "\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    body = json.dumps(message).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if request["method"] == "initialize":
                    self.send_header("Mcp-Session-Id", "test-session")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self):
                methods.append("DELETE")
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        approvals = []
        config = self.root / "http.json"
        config.write_text(json.dumps({"mcpServers": {"local": {
            "type": "http",
            "url": f"http://127.0.0.1:{server.server_port}/mcp",
            "disabled": False,
        }}}), encoding="utf-8")
        registry = MCPRegistry(
            config,
            lambda action, preview: approvals.append((action, preview)) or True,
            self.root,
        )
        self.registries.append(registry)
        try:
            schemas = registry.tool_schemas()
            result = registry.dispatch("mcp_local_ping", {})
        finally:
            registry.close()
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual([item["function"]["name"] for item in schemas], ["mcp_local_ping"])
        self.assertEqual(result["text"], ["pong"])
        self.assertEqual(result["structuredContent"], {"ok": True})
        self.assertIn("initialize", methods)
        self.assertIn("tools/list", methods)
        self.assertIn("tools/call", methods)
        self.assertIn("DELETE", methods)
        self.assertEqual([action for action, _preview in approvals], [
            "connect_mcp",
            "call_mcp",
        ])

    def test_legacy_sse_transport_connects_and_calls_a_tool(self):
        outbound = queue.Queue()
        stop = object()
        methods = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"event: endpoint\ndata: /messages\n\n")
                self.wfile.flush()
                try:
                    while (message := outbound.get(timeout=5)) is not stop:
                        body = (
                            "event: message\ndata: "
                            + json.dumps(message)
                            + "\n\n"
                        ).encode()
                        self.wfile.write(body)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, queue.Empty):
                    pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = request.get("method")
                methods.append(method)
                request_id = request.get("id")
                if request_id is not None:
                    if method == "initialize":
                        result = {
                            "protocolVersion": mcp_client.LEGACY_PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "sse-test", "version": "1"},
                        }
                    elif method == "tools/list":
                        result = {"tools": [{
                            "name": "echo",
                            "description": "Echo",
                            "inputSchema": {"type": "object", "properties": {}},
                        }]}
                    else:
                        result = {
                            "content": [{"type": "text", "text": "sse-ok"}],
                            "isError": False,
                        }
                    outbound.put({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": result,
                    })
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = self.root / "sse.json"
        config.write_text(json.dumps({"mcpServers": {"legacy": {
            "type": "sse",
            "url": f"http://127.0.0.1:{server.server_port}/sse",
        }}}), encoding="utf-8")
        registry = MCPRegistry(config, lambda *_args: True, self.root, request_timeout=2)
        self.registries.append(registry)
        try:
            schemas = registry.tool_schemas()
            result = registry.dispatch("mcp_legacy_echo", {})
        finally:
            registry.close()
            outbound.put(stop)
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual([item["function"]["name"] for item in schemas], [
            "mcp_legacy_echo",
        ])
        self.assertEqual(result["text"], ["sse-ok"])
        self.assertEqual(methods, [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ])

    def test_http_configs_are_loopback_only_and_can_be_saved_disabled(self):
        for url in (
            "http://example.com/mcp",
            "https://127.0.0.1/mcp",
            "http://user@127.0.0.1/mcp",
            "http://0.0.0.0/mcp",
            "http://127.0.0.1/mcp#fragment",
        ):
            config = self.root / f"bad-{len(list(self.root.glob('bad-*')))}.json"
            config.write_text(json.dumps({"mcpServers": {"bad": {
                "type": "sse",
                "url": url,
            }}}), encoding="utf-8")
            with self.subTest(url=url), self.assertRaises(ValueError):
                MCPRegistry(config, lambda *_args: True, self.root)

        config = self.root / "managed.json"
        config.write_text(json.dumps({"mcpServers": {"local": {
            "type": "sse",
            "url": "http://localhost:8000/sse",
            "disabled": True,
        }}}), encoding="utf-8")
        approvals = []
        registry = MCPRegistry(
            config,
            lambda action, preview: approvals.append((action, preview)) or True,
            self.root,
        )
        self.registries.append(registry)

        self.assertEqual(registry.list_servers(), [{
            "name": "local",
            "type": "sse",
            "disabled": True,
        }])
        registry.enable_server("local")
        self.assertFalse(registry.enable_server("local"))
        saved = self.root / "saved.json"
        registry.save_config(saved)
        self.assertFalse(json.loads(saved.read_text(encoding="utf-8"))[
            "mcpServers"
        ]["local"]["disabled"])

        registry.disable_server("local")
        self.assertFalse(registry.disable_server("local"))
        registry.save_config(saved)
        self.assertTrue(json.loads(saved.read_text(encoding="utf-8"))[
            "mcpServers"
        ]["local"]["disabled"])
        with self.assertRaises(ValueError):
            registry.enable_server("missing")

        before = saved.read_bytes()
        registry.approver = lambda *_args: False
        with self.assertRaises(mcp_client.MCPApprovalDenied):
            registry.save_config(saved)
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(
            [action for action, _preview in approvals].count("save_mcp_config"),
            2,
        )

        registry.approver = lambda *_args: True
        with patch("mcp_client.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                registry.save_config(saved)
        self.assertEqual(saved.read_bytes(), before)

        registry.approver = lambda *_args: (
            saved.write_text('{"external":true}', encoding="utf-8") is not None
        )
        with self.assertRaisesRegex(mcp_client.MCPError, "changed during approval"):
            registry.save_config(saved)
        self.assertEqual(saved.read_text(encoding="utf-8"), '{"external":true}')

        config.write_text('{"external_edit":true}', encoding="utf-8")
        registry.approver = lambda *_args: True
        with self.assertRaisesRegex(mcp_client.MCPError, "since it was loaded"):
            registry.save_config()
        self.assertEqual(config.read_text(encoding="utf-8"), '{"external_edit":true}')

        disabled_config = self.root / "disabled-stdio.json"
        disabled_config.write_text(
            '{"mcpServers":{"off":{"type":"stdio","disabled":true}}}',
            encoding="utf-8",
        )
        disabled = MCPRegistry(disabled_config, lambda *_args: True, self.root)
        self.registries.append(disabled)
        with self.assertRaises(ValueError):
            disabled.enable_server("off")
        disabled.close()
        with self.assertRaises(mcp_client.MCPError):
            disabled.disable_server("off")

    def test_http_close_interrupts_an_active_response(self):
        started = threading.Event()
        closed = threading.Event()
        errors = []

        class Headers:
            @staticmethod
            def get(*_args):
                return None

            @staticmethod
            def get_content_type():
                return "application/json"

        class Response:
            headers = Headers()
            status = 200

            @staticmethod
            def read(*_args):
                started.set()
                closed.wait(2)
                raise ValueError("closed")

            @staticmethod
            def close():
                closed.set()

        server = mcp_client._MCPHTTPServer(
            "local", "http://127.0.0.1:8000/mcp", self.root,
            10, 0.5, 1_000_000, 64_000, 20, 256,
        )
        server._open = lambda *_args, **_kwargs: Response()

        def request():
            try:
                server._post({"jsonrpc": "2.0", "method": "ping"})
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=request)
        worker.start()
        self.assertTrue(started.wait(1))
        server.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], mcp_client.MCPError)

    def test_legacy_sse_close_interrupts_an_active_post(self):
        started = threading.Event()
        closed = threading.Event()
        errors = []

        class Response:
            status = 202

            @staticmethod
            def read(*_args):
                started.set()
                closed.wait(2)
                raise ValueError("closed")

            @staticmethod
            def close():
                closed.set()

        server = mcp_client._MCPLegacySSEServer(
            "legacy", "http://127.0.0.1:8000/sse", self.root,
            10, 0.5, 1_000_000, 64_000, 20, 256,
        )
        server._post_url = "http://127.0.0.1:8000/messages"
        server._open = lambda *_args, **_kwargs: Response()

        def send():
            try:
                server._send({"jsonrpc": "2.0", "method": "ping"})
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=send)
        worker.start()
        self.assertTrue(started.wait(1))
        server.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], mcp_client.MCPError)

    def test_config_and_schema_limits(self):
        config = self.root / "too-many.json"
        config.write_text(
            json.dumps({"mcpServers": {f"server-{index}": {"disabled": True} for index in range(17)}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "servers"):
            MCPRegistry(config, lambda *_args: True, self.root)

        oversized = self.root / "oversized.json"
        oversized.write_bytes(b" " * 64_001)
        with self.assertRaisesRegex(ValueError, "read MCP config"):
            MCPRegistry(oversized, lambda *_args: True, self.root)


if __name__ == "__main__":
    unittest.main()
