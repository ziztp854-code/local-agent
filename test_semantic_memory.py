import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from semantic_memory import SemanticMemory


class FakeClient:
    def __init__(self, vectors=None, dimension=2):
        self.vectors = vectors or {}
        self.dimension = dimension
        self.calls = []

    def embed(self, texts, model):
        self.calls.append((list(texts), model))
        return [
            self.vectors.get(text, [1.0, *([0.0] * (self.dimension - 1))])
            for text in texts
        ]


class SemanticMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_remember_persists_records_and_reuses_duplicate_embeddings(self):
        client = FakeClient({"alpha": [1.0, 0.0], "beta": [0.0, 1.0]})
        memory = SemanticMemory(self.path)

        memory.remember(["alpha", "alpha", "beta"], client, "nomic")
        SemanticMemory(self.path).remember(["alpha"], client, "nomic")

        self.assertEqual(client.calls, [(["alpha", "beta"], "nomic")])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            [record["text"] for record in payload["records"]], ["alpha", "beta"]
        )
        self.assertEqual(payload["records"][0]["metadata"]["model"], "nomic")
        self.assertEqual(payload["records"][0]["embedding"], [1.0, 0.0])

    def test_for_workspace_uses_a_stable_safe_state_path(self):
        state = Path(self.temporary.name) / "state"
        workspace = Path(self.temporary.name) / "project"
        workspace.mkdir()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(state)}):
            first = SemanticMemory.for_workspace(workspace)
            again = SemanticMemory.for_workspace(workspace)
            other = SemanticMemory.for_workspace(workspace, "documents")

        self.assertEqual(first.path, again.path)
        self.assertEqual(first.path.parent, state / "local-agent" / "memory")
        self.assertNotEqual(first.path, other.path)

        for root, namespace in [
            (state / "missing", "workspace"),
            (workspace, "../bad"),
        ]:
            with (
                self.subTest(root=root, namespace=namespace),
                self.assertRaises(ValueError),
            ):
                SemanticMemory.for_workspace(root, namespace)
        self.assertRegex(first.path.name, r"^[0-9a-f]{64}\.json$")
        with self.assertRaises(ValueError):
            SemanticMemory.for_workspace(workspace, "../unsafe")

    def test_embed_texts_is_public_persistent_and_does_not_store_source_text(self):
        client = FakeClient({"private workspace passage": [0.5, 0.5]})
        memory = SemanticMemory(self.path)

        first = memory.embed_texts(
            ["private workspace passage", "private workspace passage"], client, "nomic"
        )
        second = SemanticMemory(self.path).embed_texts(
            ["private workspace passage"], client, "nomic"
        )

        self.assertEqual(first, [[0.5, 0.5], [0.5, 0.5]])
        self.assertEqual(second, [[0.5, 0.5]])
        self.assertEqual(client.calls, [(["private workspace passage"], "nomic")])
        self.assertNotIn(
            "private workspace passage", self.path.read_text(encoding="utf-8")
        )

    def test_model_change_invalidates_cached_embedding(self):
        first = FakeClient({"alpha": [1.0, 0.0]})
        second = FakeClient({"alpha": [0.0, 1.0]})
        memory = SemanticMemory(self.path)

        memory.embed_texts(["alpha"], first, "old-model")
        result = memory.embed_texts(["alpha"], second, "new-model")

        self.assertEqual(result, [[0.0, 1.0]])
        self.assertEqual(second.calls, [(["alpha"], "new-model")])

    def test_fresh_dimension_invalidates_incompatible_cached_hits(self):
        memory = SemanticMemory(self.path)
        memory.embed_texts(["old"], FakeClient({"old": [1.0, 0.0]}), "nomic")
        client = FakeClient(
            {"new": [0.0, 1.0, 0.0], "old": [1.0, 0.0, 0.0]}, dimension=3
        )

        result = memory.embed_texts(["old", "new"], client, "nomic")

        self.assertEqual(result, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        self.assertEqual(client.calls, [(["new"], "nomic"), (["old"], "nomic")])

    def test_load_ignores_cache_entries_from_an_outdated_dimension(self):
        old_hash = hashlib.sha256(b"old").hexdigest()
        new_hash = hashlib.sha256(b"new").hexdigest()
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "records": [],
                    "cache": [
                        {
                            "hash": old_hash,
                            "model": "nomic",
                            "dimension": 2,
                            "embedding": [1, 0],
                        },
                        {
                            "hash": new_hash,
                            "model": "nomic",
                            "dimension": 3,
                            "embedding": [0, 1, 0],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        client = FakeClient({"old": [1.0, 0.0, 0.0]}, dimension=3)

        self.assertEqual(
            SemanticMemory(self.path).embed_texts(["old"], client, "nomic"), [[1, 0, 0]]
        )
        self.assertEqual(client.calls, [(["old"], "nomic")])

    def test_recall_returns_cosine_ranked_records_with_metadata(self):
        client = FakeClient(
            {
                "fruit": [1.0, 0.0],
                "apple": [0.9, 0.1],
                "ocean": [0.0, 1.0],
            }
        )
        memory = SemanticMemory(self.path)
        memory.remember(["ocean", "apple"], client, "nomic")

        result = memory.recall("fruit", client, "nomic", limit=1)

        self.assertEqual(result[0]["text"], "apple")
        self.assertEqual(result[0]["metadata"]["dimension"], 2)
        self.assertAlmostEqual(result[0]["score"], 0.9939, places=4)

    def test_recall_ignores_records_from_another_model(self):
        memory = SemanticMemory(self.path)
        memory.remember(["old"], FakeClient({"old": [1.0, 0.0]}), "old-model")
        client = FakeClient({"query": [1.0, 0.0]})

        self.assertEqual(memory.recall("query", client, "new-model", limit=3), [])
        self.assertEqual(client.calls, [])

    def test_limits_and_argument_types_are_enforced(self):
        client = FakeClient()
        memory = SemanticMemory(
            self.path,
            max_records=2,
            max_cache_entries=2,
            max_text_chars=5,
            max_texts=2,
            max_dimensions=2,
        )
        invalid_calls = [
            lambda: memory.embed_texts("one", client, "nomic"),
            lambda: memory.embed_texts(["one", "two", "three"], client, "nomic"),
            lambda: memory.embed_texts(["longer"], client, "nomic"),
            lambda: memory.embed_texts(["one"], client, ""),
            lambda: memory.recall("one", client, "nomic", limit=0),
        ]
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

        memory.remember(["one", "two"], client, "nomic")
        memory.remember(["tri"], client, "nomic")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            [record["text"] for record in payload["records"]], ["two", "tri"]
        )
        self.assertLessEqual(len(payload["cache"]), 2)

    def test_byte_limit_rolls_oldest_records_and_keeps_the_latest(self):
        memory = SemanticMemory(self.path, max_bytes=1_000, max_records=10)
        texts = [
            f"{label} " + "detail " * 40 for label in ("oldest", "middle", "newest")
        ]
        client = FakeClient()

        for text in texts:
            memory.remember([text], client, "nomic")

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([record["text"] for record in payload["records"]], texts[-2:])
        self.assertLessEqual(self.path.stat().st_size, 1_000)

    def test_secrets_are_redacted_before_embedding_and_storage(self):
        memory = SemanticMemory(self.path)
        text = """password=short
token: abc123
api_key='key-value'
OPENAI_API_KEY=env-value
"client_secret": "json-value"
secret = hidden
Authorization: Bearer bearer-value
-----BEGIN PRIVATE KEY-----
private-material
-----END PRIVATE KEY-----"""
        client = FakeClient()

        memory.remember([text], client, "nomic")

        stored = self.path.read_text(encoding="utf-8")
        embedded = client.calls[0][0][0]
        for secret in [
            "short",
            "abc123",
            "key-value",
            "env-value",
            "json-value",
            "hidden",
            "bearer-value",
            "private-material",
        ]:
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, embedded)
        self.assertIn("[REDACTED]", stored)

    def test_base64_records_are_rejected(self):
        memory = SemanticMemory(self.path)
        unsafe = [
            "data:image/png;base64,AAAA",
            "A" * 80,
            "AKIA1234567890ABCDEF",
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value",
        ]
        for text in unsafe:
            with self.subTest(text=text), self.assertRaises(ValueError):
                memory.remember([text], FakeClient(), "nomic")
        self.assertFalse(self.path.exists())

    def test_invalid_client_embeddings_are_rejected(self):
        class InvalidClient:
            def __init__(self, response):
                self.response = response

            def embed(self, _texts, _model):
                return self.response

        invalid = [
            None,
            [],
            [[1.0], [2.0]],
            ["vector"],
            [[]],
            [[True]],
            [[float("nan")]],
            [[1.0, 2.0, 3.0]],
        ]
        memory = SemanticMemory(self.path, max_dimensions=2)
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                memory.embed_texts(["one"], InvalidClient(response), "nomic")

    def test_malformed_oversized_and_unsafe_files_are_rejected(self):
        memory = SemanticMemory(self.path, max_bytes=256)
        invalid_documents = [
            b"not-json",
            b"[]",
            b"{" + (b" " * 256) + b"}",
            json.dumps(
                {
                    "version": 1,
                    "records": [
                        {
                            "text": "token=stored-secret",
                            "metadata": {
                                "hash": "0" * 64,
                                "model": "nomic",
                                "dimension": 1,
                            },
                            "embedding": [1.0],
                        }
                    ],
                    "cache": [],
                }
            ).encode(),
        ]
        for document in invalid_documents:
            with self.subTest(document=document[:20]):
                self.path.write_bytes(document)
                with self.assertRaises(RuntimeError):
                    memory.embed_texts([], FakeClient(), "nomic")

    def test_file_schema_rejects_boolean_versions_and_non_string_models(self):
        documents = [
            {"version": True, "records": [], "cache": []},
            {
                "version": 1,
                "records": [
                    {
                        "text": "safe",
                        "metadata": {
                            "hash": hashlib.sha256(b"safe").hexdigest(),
                            "model": None,
                            "dimension": 1,
                        },
                        "embedding": [1],
                    }
                ],
                "cache": [],
            },
        ]
        for document in documents:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    SemanticMemory(self.path).embed_texts([], FakeClient(), "nomic")

        self.path.write_text(
            '{"version":1,"records":[],"cache":[],"cache":[]}', encoding="utf-8"
        )
        with self.assertRaises(RuntimeError):
            SemanticMemory(self.path).embed_texts([], FakeClient(), "nomic")

    def test_reparse_ancestor_is_rejected_before_contacting_client(self):
        memory = SemanticMemory(self.path)
        client = FakeClient()
        with patch.object(
            memory,
            "_is_reparse",
            side_effect=lambda candidate: candidate == self.path.parent,
        ):
            with self.assertRaises(RuntimeError):
                memory.embed_texts(["safe"], client, "nomic")
        self.assertEqual(client.calls, [])

    def test_failed_atomic_replace_preserves_the_previous_file(self):
        memory = SemanticMemory(self.path)
        memory.remember(["old"], FakeClient(), "nomic")
        before = self.path.read_bytes()

        with patch("semantic_memory.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(RuntimeError):
                memory.remember(["new"], FakeClient(), "nomic")

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".semantic-memory-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
