import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
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


@contextmanager
def open_memory(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        yield connection
        connection.commit()


class SemanticMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.sqlite"

    def tearDown(self):
        self.temporary.cleanup()

    def test_remember_persists_records_and_reuses_duplicate_embeddings(self):
        client = FakeClient({"alpha": [1.0, 0.0], "beta": [0.0, 1.0]})
        memory = SemanticMemory(self.path)

        memory.remember(["alpha", "alpha", "beta"], client, "nomic")
        SemanticMemory(self.path).remember(["alpha"], client, "nomic")

        self.assertEqual(client.calls, [(["alpha", "beta"], "nomic")])
        with open_memory(self.path) as connection:
            rows = connection.execute(
                "SELECT text, model, embedding FROM records ORDER BY id"
            ).fetchall()
        self.assertEqual([row["text"] for row in rows], ["alpha", "beta"])
        self.assertEqual(rows[0]["model"], "nomic")

    def test_old_unique_constraint_is_migrated_to_include_memory_kind(self):
        old_schema = """
        CREATE TABLE records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            supersedes INTEGER,
            superseded_by INTEGER,
            UNIQUE(hash, model)
        );
        """
        with open_memory(self.path) as connection:
            connection.executescript(old_schema)

        client = FakeClient({"shared text": [1.0, 0.0]})
        memory = SemanticMemory(self.path)
        memory.remember(["shared text"], client, "nomic", kind="semantic")
        memory.remember(["shared text"], client, "nomic", kind="project")

        with open_memory(self.path) as connection:
            rows = connection.execute(
                "SELECT kind FROM records ORDER BY id"
            ).fetchall()
        self.assertEqual([row["kind"] for row in rows], ["semantic", "project"])

    def test_forget_source_only_removes_selected_session_memory(self):
        client = FakeClient({"first": [1.0, 0.0], "second": [0.0, 1.0]})
        memory = SemanticMemory(self.path)
        memory.remember(
            ["first"], client, "nomic", source="session:first", kind="conversation"
        )
        memory.remember(
            ["second"], client, "nomic", source="session:second", kind="conversation"
        )

        memory.forget_source("session:first")

        with open_memory(self.path) as connection:
            sources = connection.execute("SELECT source FROM records").fetchall()
        self.assertEqual([row["source"] for row in sources], ["session:second"])

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
        self.assertRegex(first.path.name, r"^[0-9a-f]{64}\.sqlite$")
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
        raw = self.path.read_bytes()
        self.assertNotIn("private workspace passage".encode(), raw)

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
        with open_memory(self.path) as connection:
            connection.executescript(SemanticMemory._SCHEMA)
            connection.execute(
                "INSERT INTO cache(hash, model, dimension, embedding) VALUES (?, ?, ?, ?)",
                (old_hash, "nomic", 2, b"\x00" * 8),
            )
            connection.execute(
                "INSERT OR IGNORE INTO cache(hash, model, dimension, embedding)"
                " VALUES (?, ?, ?, ?)",
                (old_hash, "nomic", 2, b"\x00" * 8),
            )
            connection.execute(
                "INSERT OR REPLACE INTO cache(hash, model, dimension, embedding)"
                " VALUES (?, ?, ?, ?)",
                (new_hash, "nomic", 3, b"\x00" * 12),
            )
            connection.commit()
        client = FakeClient({"old": [1.0, 0.0, 0.0]}, dimension=3)

        self.assertEqual(
            SemanticMemory(self.path).embed_texts(["old"], client, "nomic"),
            [[1.0, 0.0, 0.0]],
        )
        self.assertEqual(client.calls, [(["old"], "nomic")])

    def test_recall_fuses_vector_and_fts_with_rrf(self):
        client = FakeClient(
            {
                "fruit": [1.0, 0.0],
                "apple": [0.9, 0.1],
                "ocean": [0.0, 1.0],
            }
        )
        memory = SemanticMemory(self.path)
        memory.remember(["ocean", "apple"], client, "nomic")

        result = memory.recall("fruit", client, "nomic", limit=5)

        # "fruit" لا تشبه النصين دلاليًا بأكثر من تقارب "apple"؛ الترتيب
        # يتبع التشابه الجيبي المرتّب، والنتيجة تحمل الكوسينوس ومجموع RRF.
        self.assertEqual(result[0]["text"], "apple")
        self.assertEqual(result[0]["metadata"]["dimension"], 2)
        self.assertAlmostEqual(result[0]["cosine"], 0.9939, places=4)
        # أعلى رتبة متجهات → نقاط مسوّاة 1.0 (بدون ساق معجمي مطابق هنا).
        self.assertAlmostEqual(result[0]["score"], 1.0, places=6)
        self.assertIn("source", result[0]["metadata"])
        self.assertIn("created_at", result[0]["metadata"])

    def test_lexical_leg_lifts_exact_text_matches_over_weak_vectors(self):
        client = FakeClient(
            {
                "the deploy target is Fly.io": [0.9, 0.1],
                "database migration notes": [0.8, 0.2],
            }
        )
        memory = SemanticMemory(self.path)
        memory.remember(["database migration notes", "the deploy target is Fly.io"], client, "nomic")

        result = memory.recall("deploy fly", client, "nomic", limit=5)

        # ساق BM25 يضيف رتبة للاستدعاء المطابق نصيًا فيسبق الأعلى كوسينوس.
        self.assertEqual(result[0]["text"], "the deploy target is Fly.io")
        self.assertGreater(result[0]["score"], result[-1]["score"])
        # المطابقة المزدوجة (متجهات + معجم) تساوي 2.0 كحد أقصى.
        self.assertAlmostEqual(result[0]["score"], 2.0, places=6)

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
            lambda: memory.supersede(0, "x", client, "nomic"),
            lambda: memory.supersede(True, "x", client, "nomic"),
            lambda: memory.record(-1, client, "nomic"),
        ]
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

        memory.remember(["one", "two"], client, "nomic")
        memory.remember(["tri"], client, "nomic")
        with open_memory(self.path) as connection:
            rows = connection.execute(
                "SELECT text FROM records ORDER BY id"
            ).fetchall()
            cache_count = connection.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        self.assertEqual([row["text"] for row in rows], ["two", "tri"])
        self.assertLessEqual(cache_count, 2)

    def test_record_budget_rolls_oldest_records_and_keeps_the_latest(self):
        memory = SemanticMemory(self.path, max_bytes=1_100, max_records=10)
        texts = [
            f"{label} " + "detail " * 40 for label in ("oldest", "middle", "newest")
        ]
        client = FakeClient()

        for text in texts:
            memory.remember([text], client, "nomic")

        with open_memory(self.path) as connection:
            rows = connection.execute(
                "SELECT text FROM records ORDER BY id"
            ).fetchall()
        self.assertEqual([row["text"] for row in rows], texts[-2:])

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

        stored = self.path.read_bytes()
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
            self.assertNotIn(secret.encode(), stored)
            self.assertNotIn(secret, embedded)
        self.assertIn("[REDACTED]", stored.decode(encoding="utf-8", errors="ignore"))

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

    def test_corrupt_files_are_rejected(self):
        memory = SemanticMemory(self.path)
        invalid_documents = [
            b"not-a-sqlite-database-at-all" + b"\x00" * 64,
            b"{" + (b" " * 256) + b"}",
        ]
        for document in invalid_documents:
            with self.subTest(document=document[:20]):
                self.path.write_bytes(document)
                with self.assertRaises(RuntimeError):
                    memory.embed_texts([], FakeClient(), "nomic")

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

    def test_failed_commit_preserves_the_previous_state(self):
        memory = SemanticMemory(self.path)
        memory.remember(["old"], FakeClient(), "nomic")
        with open_memory(self.path) as connection:
            before = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]

        original = SemanticMemory._commit
        attempts = {"count": 0}

        def flaky_commit(connection):
            attempts["count"] += 1
            if attempts["count"] >= 2:
                # يحاكي ما تفعله _commit الحقيقية: تتحول OSError إلى RuntimeError.
                raise RuntimeError("Could not save semantic memory")
            original(connection)

        with patch.object(SemanticMemory, "_commit", side_effect=flaky_commit):
            with self.assertRaises(RuntimeError):
                memory.remember(["new"], FakeClient(), "nomic")

        with open_memory(self.path) as connection:
            after = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.assertEqual(before, after)

    def test_provenance_is_stored_and_returned(self):
        client = FakeClient({"fact": [1.0, 0.0]})
        memory = SemanticMemory(self.path)

        memory.remember(["fact"], client, "nomic", source="notes.md:12")

        result = memory.recall("fact", client, "nomic", limit=1)
        self.assertEqual(result[0]["metadata"]["source"], "notes.md:12")
        with self.assertRaises(ValueError):
            memory.remember(["other"], client, "nomic", source="\x00bad")

    def test_supersession_closes_old_record_and_keeps_history(self):
        client = FakeClient({"v1": [1.0, 0.0], "v2": [0.9, 0.1]})
        memory = SemanticMemory(self.path)

        added = memory.remember(["v1"], client, "nomic")
        self.assertEqual(added, 1)
        with open_memory(self.path) as connection:
            old_id = connection.execute(
                "SELECT id FROM records"
            ).fetchone()[0]

        new_id = memory.supersede(old_id, "v2", client, "nomic", source="تصحيح")

        with open_memory(self.path) as connection:
            old_row = connection.execute(
                "SELECT superseded_by, supersedes FROM records WHERE id = ?", (old_id,)
            ).fetchone()
            new_row = connection.execute(
                "SELECT supersedes FROM records WHERE id = ?", (new_id,)
            ).fetchone()
        self.assertEqual(old_row["superseded_by"], new_id)
        self.assertEqual(new_row["supersedes"], old_id)

        result = memory.recall("v2", client, "nomic", limit=5)
        self.assertEqual([item["text"] for item in result], ["v2"])

        info = memory.record(old_id, client, "nomic")
        self.assertEqual(info["superseded_by"], new_id)
        self.assertEqual(info["source"], "")
        with self.assertRaises(ValueError):
            memory.supersede(old_id, "v3", client, "nomic")
        with self.assertRaises(ValueError):
            memory.supersede(999, "v3", client, "nomic")

    def test_legacy_json_is_migrated_and_archived(self):
        legacy = self.path.with_suffix(".json")
        payload = {
            "version": 1,
            "records": [
                {
                    "text": "legacy fact",
                    "metadata": {
                        "hash": hashlib.sha256(b"legacy fact").hexdigest(),
                        "model": "nomic",
                        "dimension": 2,
                    },
                    "embedding": [1.0, 0.0],
                }
            ],
            "cache": [
                {
                    "hash": hashlib.sha256(b"legacy fact").hexdigest(),
                    "model": "nomic",
                    "dimension": 2,
                    "embedding": [1.0, 0.0],
                }
            ],
        }
        legacy.write_text(json.dumps(payload), encoding="utf-8")

        client = FakeClient({"legacy fact": [1.0, 0.0]})
        memory = SemanticMemory(self.path)
        self.assertEqual(
            memory.recall("legacy fact", client, "nomic", limit=5)[0]["text"],
            "legacy fact",
        )
        self.assertEqual(client.calls, [])
        self.assertTrue(legacy.with_suffix(".json.migrated").exists())
        self.assertFalse(legacy.exists())

    def test_invalid_legacy_json_is_rejected(self):
        legacy = self.path.with_suffix(".json")
        legacy.write_bytes(b"{duplicate: key, duplicate: key}")
        with self.assertRaises(RuntimeError):
            SemanticMemory(self.path).embed_texts(["x"], FakeClient(), "nomic")

    def test_fts_query_is_injection_safe(self):
        query = SemanticMemory._fts_query('neo" OR 1=1 --')
        self.assertNotIn("OR", query.replace('"OR"', ""))
        self.assertNotIn("--", query)

    def test_decay_weakens_old_unpinned_memory_but_not_pinned_memory(self):
        client = FakeClient({"old": [1.0, 0.0], "new": [1.0, 0.0], "query": [1.0, 0.0]})
        memory = SemanticMemory(self.path)
        memory.remember(["old", "new"], client, "nomic", kind="project")
        with open_memory(self.path) as connection:
            old_id, new_id = [
                row[0] for row in connection.execute("SELECT id FROM records ORDER BY id")
            ]
            connection.execute(
                "UPDATE records SET created_at=?, last_accessed=? WHERE id=?",
                (1.0, 1.0, old_id),
            )
            connection.execute("UPDATE records SET pinned=1 WHERE id=?", (new_id,))
            connection.commit()

        result = memory.recall("query", client, "nomic", limit=2)

        self.assertEqual(result[0]["id"], new_id)
        self.assertGreater(result[0]["score"], result[1]["score"])

    def test_memory_controls_and_consolidation_preserve_provenance(self):
        client = FakeClient({"same fact": [1.0, 0.0]})
        memory = SemanticMemory(self.path)
        memory.remember(["same fact"], client, "nomic", kind="project")
        memory.remember(["same fact"], client, "nomic", kind="preference")

        consolidated = memory.consolidate()
        with open_memory(self.path) as connection:
            rows = connection.execute(
                "SELECT id, superseded_by, archived FROM records ORDER BY id"
            ).fetchall()

        self.assertEqual(consolidated, 1)
        self.assertEqual(sum(row[2] == 0 for row in rows), 1)
        self.assertEqual(sum(row[1] is not None for row in rows), 1)
        canonical = next(row[0] for row in rows if row[2] == 0)
        memory.set_pinned(canonical, True)
        self.assertTrue(memory.record(canonical, client, "nomic")["pinned"])
        memory.set_archived(canonical, True)
        self.assertTrue(memory.record(canonical, client, "nomic")["archived"])
        memory.forget(canonical)
        with self.assertRaises(ValueError):
            memory.record(canonical, client, "nomic")


if __name__ == "__main__":
    unittest.main()
