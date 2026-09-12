import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
import time
from pathlib import Path


_HASH = re.compile(r"^[0-9a-f]{64}$")
_MODEL = re.compile(r"^[A-Za-z0-9._/@:+-]{1,128}$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_-])([\"']?)"
    r"([A-Za-z0-9_-]*(?:password|token|api[_-]?key|secret))\1(\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"(?:-----END [^-\r\n]*PRIVATE KEY-----|$)",
    re.IGNORECASE | re.DOTALL,
)
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{64,}={0,2}(?![A-Za-z0-9+/=])")
_OPAQUE_SECRET = re.compile(
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_RRF_K = 60
_CANDIDATE_CAP = 256
_MAX_FTS_TERMS = 32
_RECORD_OVERHEAD_BYTES = 160
_CACHE_OVERHEAD_BYTES = 96


class SemanticMemory:
    """Bounded single-file SQLite storage for remembered text and reusable embeddings.

    Phase 1 (inspired by memharness/UltraMemory, fully local):
    - one SQLite file: records + FTS5 lexical index + vector cache
    - hybrid recall: vector cosine and BM25 fused with reciprocal-rank fusion (k=60)
    - per-record provenance (source, created_at) and supersession chains
    - the write path stays deterministic: no model calls beyond embedding
    """

    def __init__(
        self,
        path,
        max_bytes=2_000_000,
        max_records=500,
        max_cache_entries=2_000,
        max_text_chars=16_000,
        max_texts=128,
        max_dimensions=8_192,
    ):
        if (
            not isinstance(path, (str, os.PathLike))
            or not os.fspath(path)
            or "\x00" in os.fspath(path)
        ):
            raise ValueError("Memory path must be a non-empty path")
        limits = (
            max_bytes,
            max_records,
            max_cache_entries,
            max_text_chars,
            max_texts,
            max_dimensions,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("Memory limits must be positive integers")
        self.path = Path(path).expanduser().absolute()
        self.max_bytes = max_bytes
        self.max_records = max_records
        self.max_cache_entries = max_cache_entries
        self.max_text_chars = max_text_chars
        self.max_texts = max_texts
        self.max_dimensions = max_dimensions
        self._fts_enabled = True

    @classmethod
    def for_workspace(cls, root, namespace="workspace", **limits):
        """Create stable per-workspace memory below the user's local state directory."""
        if not isinstance(root, (str, os.PathLike)):
            raise ValueError("Workspace must be a directory")
        try:
            workspace = Path(root).expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("Workspace must be a directory") from error
        if not workspace.is_dir():
            raise ValueError("Workspace must be a directory")
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            raise ValueError(
                "Memory namespace may contain only letters, numbers, - and _"
            )
        platform_state = os.getenv("LOCALAPPDATA") or os.getenv("XDG_STATE_HOME")
        state_root = (
            Path(platform_state) if platform_state else Path.home() / ".local" / "state"
        )
        memory_id = hashlib.sha256(
            f"{os.path.normcase(str(workspace))}\0{namespace}".encode("utf-8")
        ).hexdigest()
        return cls(
            state_root / "local-agent" / "memory" / f"{memory_id}.sqlite", **limits
        )

    @staticmethod
    def _hash(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_reparse(path):
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        return path.is_symlink() or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    @staticmethod
    def _strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def _check_path(self):
        for ancestor in reversed(self.path.parents):
            if ancestor.exists() and self._is_reparse(ancestor):
                raise RuntimeError("Semantic memory path is unsafe")
        if self.path.exists() and self._is_reparse(self.path):
            raise RuntimeError("Semantic memory path is unsafe")

    def _validate_model(self, model):
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ValueError("Embedding model name is invalid")
        return model

    def _validate_texts(self, texts, reject_sensitive=False):
        if not isinstance(texts, (list, tuple)) or len(texts) > self.max_texts:
            raise ValueError(
                f"texts must be a sequence of at most {self.max_texts} strings"
            )
        clean = []
        for text in texts:
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > self.max_text_chars
                or "\x00" in text
            ):
                raise ValueError(
                    f"Each text must contain 1 to {self.max_text_chars} characters"
                )
            if reject_sensitive and (
                "base64," in text.casefold()
                or _BASE64.search(text)
                or _OPAQUE_SECRET.search(text)
            ):
                raise ValueError(
                    "Base64 or credential-like content cannot be remembered"
                )
            clean.append(text)
        return clean

    @staticmethod
    def _validate_source(source):
        if source is None:
            return ""
        if not isinstance(source, str) or len(source) > 200:
            raise ValueError("source must be a string of at most 200 characters")
        if "\x00" in source:
            raise ValueError("source must not contain null bytes")
        return source

    def _validate_vector(self, vector, expected_dimension=None):
        if (
            not isinstance(vector, list)
            or not 1 <= len(vector) <= self.max_dimensions
            or expected_dimension is not None
            and len(vector) != expected_dimension
        ):
            raise RuntimeError("Embedding vector is invalid")
        if any(
            type(value) not in {int, float} or not math.isfinite(value)
            for value in vector
        ):
            raise RuntimeError("Embedding vector is invalid")
        return [float(value) for value in vector]

    @staticmethod
    def _redact(text):
        text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
        text = _BEARER.sub("Bearer [REDACTED]", text)
        return _SECRET_ASSIGNMENT.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}{match.group(1)}"
                f"{match.group(3)}[REDACTED]"
            ),
            text,
        )

    @staticmethod
    def _commit(connection):
        try:
            connection.commit()
        except OSError as error:
            raise RuntimeError("Could not save semantic memory") from error

    @staticmethod
    def _encode_vector(vector):
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _decode_vector(blob, dimension):
        return list(struct.unpack(f"<{dimension}f", bytes(blob)))

    # ------------------------------------------------------------------
    # SQLite storage
    # ------------------------------------------------------------------

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS records (
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
    CREATE TABLE IF NOT EXISTS cache (
        rowid_ INTEGER PRIMARY KEY AUTOINCREMENT,
        hash TEXT NOT NULL UNIQUE,
        model TEXT NOT NULL,
        dimension INTEGER NOT NULL,
        embedding BLOB NOT NULL
    );
    CREATE INDEX IF NOT EXISTS cache_model ON cache(model, rowid_);
    """

    def _connect(self):
        self._check_path()
        self._migrate_legacy_json()
        state_dir = self.path.parent
        if state_dir.exists() and not state_dir.is_dir():
            raise RuntimeError("Semantic memory path is unsafe")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(self._SCHEMA)
            self._ensure_fts(connection)
        except sqlite3.Error as error:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise RuntimeError("Semantic memory file is invalid") from error
        return connection

    def _ensure_fts(self, connection):
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING"
                " fts5(text, tokenize=\"unicode61 remove_diacritics 2\")"
            )
            self._fts_enabled = True
        except sqlite3.Error:
            self._fts_enabled = False

    def _migrate_legacy_json(self):
        legacy = self.path.with_suffix(".json")
        if self.path.exists() or not legacy.exists() or legacy == self.path:
            return
        self._check_path()
        try:
            raw = legacy.read_bytes()
            if len(raw) > self.max_bytes:
                raise RuntimeError("Semantic memory file exceeds its size limit")
            state = json.loads(
                raw.decode("utf-8"), object_pairs_hook=self._strict_object
            )
            state = self._validate_legacy_state(state)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("Semantic memory file is invalid") from error
        try:
            connection = sqlite3.connect(self.path)
            connection.executescript(self._SCHEMA)
            self._ensure_fts(connection)
            for entry in reversed(state["cache"]):
                connection.execute(
                    "INSERT OR IGNORE INTO cache(hash, model, dimension, embedding)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        entry["hash"],
                        entry["model"],
                        entry["dimension"],
                        self._encode_vector(entry["embedding"]),
                    ),
                )
            for record in reversed(state["records"]):
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO records"
                    " (hash, model, dimension, embedding, text, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?, '', ?)",
                    (
                        record["metadata"]["hash"],
                        record["metadata"]["model"],
                        record["metadata"]["dimension"],
                        self._encode_vector(record["embedding"]),
                        record["text"],
                        time.time(),
                    ),
                )
                if cursor.rowcount > 0 and self._fts_enabled:
                    connection.execute(
                        "INSERT INTO records_fts(rowid, text) VALUES (?, ?)",
                        (cursor.lastrowid, record["text"]),
                    )
            connection.commit()
            connection.close()
            os.replace(legacy, legacy.with_suffix(".json.migrated"))
        except sqlite3.Error as error:
            raise RuntimeError("Could not migrate semantic memory") from error

    def _validate_legacy_state(self, state):
        if not isinstance(state, dict) or set(state) != {"version", "records", "cache"}:
            raise RuntimeError("Semantic memory file is invalid")
        records = state.get("records")
        cache = state.get("cache")
        if (
            type(state.get("version")) is not int
            or state["version"] != 1
            or not isinstance(records, list)
            or len(records) > self.max_records
            or not isinstance(cache, list)
            or len(cache) > self.max_cache_entries
        ):
            raise RuntimeError("Semantic memory file is invalid")

        clean_records = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "text",
                "metadata",
                "embedding",
            }:
                raise RuntimeError("Semantic memory file is invalid")
            text = self._validate_stored_text(record["text"])
            metadata = record["metadata"]
            metadata_model = (
                metadata.get("model") if isinstance(metadata, dict) else None
            )
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"hash", "model", "dimension"}
                or metadata["hash"] != self._hash(text)
                or not isinstance(metadata_model, str)
                or not _MODEL.fullmatch(metadata_model)
                or type(metadata.get("dimension")) is not int
            ):
                raise RuntimeError("Semantic memory file is invalid")
            vector = self._validate_vector(record["embedding"], metadata["dimension"])
            clean_records.append(
                {"text": text, "metadata": dict(metadata), "embedding": vector}
            )

        clean_cache = []
        seen_hashes = set()
        for entry in cache:
            if not isinstance(entry, dict) or set(entry) != {
                "hash",
                "model",
                "dimension",
                "embedding",
            }:
                raise RuntimeError("Semantic memory file is invalid")
            text_hash = entry.get("hash")
            model = entry.get("model")
            dimension = entry.get("dimension")
            if (
                not isinstance(text_hash, str)
                or not _HASH.fullmatch(text_hash)
                or text_hash in seen_hashes
                or not isinstance(model, str)
                or not _MODEL.fullmatch(model)
                or type(dimension) is not int
            ):
                raise RuntimeError("Semantic memory file is invalid")
            seen_hashes.add(text_hash)
            vector = self._validate_vector(entry["embedding"], dimension)
            clean_cache.append(
                {
                    "hash": text_hash,
                    "model": model,
                    "dimension": dimension,
                    "embedding": vector,
                }
            )
        current_dimensions = {
            entry["model"]: entry["dimension"] for entry in clean_cache
        }
        clean_cache = [
            entry
            for entry in clean_cache
            if entry["dimension"] == current_dimensions[entry["model"]]
        ]
        return {"version": 1, "records": clean_records, "cache": clean_cache}

    def _validate_stored_text(self, text):
        try:
            clean = self._validate_texts([text], reject_sensitive=True)
        except ValueError as error:
            raise RuntimeError("Semantic memory file is invalid") from error
        if self._redact(clean[0]) != clean[0]:
            raise RuntimeError("Semantic memory file is invalid")
        return clean[0]

    def _request_embeddings(self, texts, client, model):
        try:
            response = client.embed(texts, model)
        except (AttributeError, TypeError) as error:
            raise RuntimeError("Embedding client is invalid") from error
        if not isinstance(response, list) or len(response) != len(texts):
            raise RuntimeError("Embedding response is invalid")
        vectors = [self._validate_vector(vector) for vector in response]
        if vectors and any(len(vector) != len(vectors[0]) for vector in vectors):
            raise RuntimeError("Embedding response dimensions do not match")
        return vectors

    def embed_texts(self, texts, client, model):
        """Embed texts once and persist only hashes, model ids, and bounded vectors."""
        clean_texts = self._validate_texts(texts)
        model = self._validate_model(model)
        if not clean_texts:
            # حتى الطلب الفارغ يفتح قاعدة البيانات ليتبع الملف التالف فشلًا صلبًا.
            connection = self._connect()
            connection.close()
            return []
        unique = list(dict.fromkeys(clean_texts))
        hashes = {text: self._hash(text) for text in unique}
        connection = self._connect()
        try:
            # التكافؤ مع المدقق القديم: داخل النموذج الواحد يُقبل بعد المتجهات
            # بأحدث بُعد فقط؛ المدخلات الأقدم تُهمل ويُعاد تضمينها عند الطلب.
            rows = connection.execute(
                "SELECT hash, dimension, embedding FROM cache"
                " WHERE model = ? ORDER BY rowid_ DESC",
                (model,),
            ).fetchall()
            current_dimension = rows[0][1] if rows else None
            cached = {
                row[0]: (row[1], self._decode_vector(row[2], row[1]))
                for row in rows
                if row[1] == current_dimension
            }
            vectors = {}
            missing = [text for text in unique if hashes[text] not in cached]
            fresh = (
                self._request_embeddings(missing, client, model) if missing else []
            )
            vectors.update(zip(missing, fresh, strict=True))

            if fresh:
                dimension = len(fresh[0])
                stale = [
                    text
                    for text in unique
                    if text not in vectors and cached[hashes[text]][0] != dimension
                ]
                replacements = (
                    self._request_embeddings(stale, client, model) if stale else []
                )
                if replacements and len(replacements[0]) != dimension:
                    raise RuntimeError(
                        "Embedding model changed dimensions during one request"
                    )
                vectors.update(zip(stale, replacements, strict=True))
            else:
                dimensions = {cached[hashes[text]][0] for text in unique}
                if len(dimensions) > 1:
                    replacements = self._request_embeddings(unique, client, model)
                    vectors.update(zip(unique, replacements, strict=True))

            for text in unique:
                if text not in vectors:
                    vectors[text] = cached[hashes[text]][1]

            refreshed = [
                text
                for text in unique
                if hashes[text] not in cached
                or (
                    text in vectors
                    and hashes[text] in cached
                    and cached[hashes[text]][0] != len(vectors[text])
                )
            ]
            if refreshed:
                refreshed_hashes = {hashes[text] for text in refreshed}
                connection.execute(
                    "DELETE FROM cache WHERE model = ?"
                    " AND hash IN (%s)" % ",".join("?" * len(refreshed_hashes)),
                    (model, *refreshed_hashes),
                )
                for text in refreshed:
                    connection.execute(
                        "INSERT OR REPLACE INTO cache(hash, model, dimension, embedding)"
                        " VALUES (?, ?, ?, ?)",
                        (
                            hashes[text],
                            model,
                            len(vectors[text]),
                            self._encode_vector(vectors[text]),
                        ),
                    )
                self._prune(connection)
                self._commit(connection)
        finally:
            connection.close()
        return [list(vectors[text]) for text in clean_texts]

    def remember(self, texts, client, model, source=""):
        clean_texts = [
            self._redact(text)
            for text in self._validate_texts(texts, reject_sensitive=True)
        ]
        model = self._validate_model(model)
        source = self._validate_source(source)
        vectors = self.embed_texts(clean_texts, client, model)
        if not clean_texts:
            return 0
        connection = self._connect()
        try:
            now = time.time()
            added = 0
            for text, vector in zip(clean_texts, vectors, strict=True):
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO records"
                    " (hash, model, dimension, embedding, text, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._hash(text),
                        model,
                        len(vector),
                        self._encode_vector(vector),
                        text,
                        source,
                        now,
                    ),
                )
                if cursor.rowcount > 0 and self._fts_enabled:
                    connection.execute(
                        "INSERT INTO records_fts(rowid, text) VALUES (?, ?)",
                        (cursor.lastrowid, text),
                    )
                    added += 1
            self._prune(connection)
            self._commit(connection)
        finally:
            connection.close()
        return added

    def supersede(self, record_id, new_text, client, model, source=""):
        """Close an old record and link it to its replacement; history stays queryable."""
        if type(record_id) is not int or record_id < 1:
            raise ValueError("record_id must be a positive integer")
        clean_text = self._redact(
            self._validate_texts([new_text], reject_sensitive=True)[0]
        )
        model = self._validate_model(model)
        source = self._validate_source(source)
        vector = self.embed_texts([clean_text], client, model)[0]
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, superseded_by FROM records WHERE id = ? AND model = ?",
                (record_id, model),
            ).fetchone()
            if row is None:
                raise ValueError("Record to supersede was not found")
            if row[1] is not None:
                raise ValueError("Record was already superseded")
            cursor = connection.execute(
                "INSERT INTO records"
                " (hash, model, dimension, embedding, text, source, created_at, supersedes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._hash(clean_text),
                    model,
                    len(vector),
                    self._encode_vector(vector),
                    clean_text,
                    source,
                    time.time(),
                    record_id,
                ),
            )
            new_id = cursor.lastrowid
            if self._fts_enabled:
                connection.execute(
                    "INSERT INTO records_fts(rowid, text) VALUES (?, ?)",
                    (new_id, clean_text),
                )
            connection.execute(
                "UPDATE records SET superseded_by = ? WHERE id = ?",
                (new_id, record_id),
            )
            self._prune(connection)
            self._commit(connection)
        finally:
            connection.close()
        return new_id

    def record(self, record_id, client, model):
        """Return one record with its provenance and supersession chain."""
        if type(record_id) is not int or record_id < 1:
            raise ValueError("record_id must be a positive integer")
        model = self._validate_model(model)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, text, source, created_at, supersedes, superseded_by"
                " FROM records WHERE id = ? AND model = ?",
                (record_id, model),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("Record was not found")
        return {
            "id": row[0],
            "text": row[1],
            "source": row[2],
            "created_at": row[3],
            "supersedes": row[4],
            "superseded_by": row[5],
        }

    def _prune(self, connection):
        """Keep record/cache counts and the content byte budget bounded."""
        connection.execute(
            "DELETE FROM records WHERE id NOT IN ("
            " SELECT id FROM records ORDER BY id DESC LIMIT ?)",
            (self.max_records,),
        )
        connection.execute(
            "DELETE FROM cache WHERE rowid_ NOT IN ("
            " SELECT rowid_ FROM cache ORDER BY rowid_ DESC LIMIT ?)",
            (self.max_cache_entries,),
        )

        def content_bytes():
            text_bytes = connection.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM records"
            ).fetchone()[0]
            blob_bytes = connection.execute(
                "SELECT COALESCE(SUM(LENGTH(embedding)), 0) FROM records"
            ).fetchone()[0] + connection.execute(
                "SELECT COALESCE(SUM(LENGTH(embedding)), 0) FROM cache"
            ).fetchone()[0]
            record_count = connection.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            cache_count = connection.execute(
                "SELECT COUNT(*) FROM cache"
            ).fetchone()[0]
            return (
                text_bytes
                + blob_bytes
                + record_count * _RECORD_OVERHEAD_BYTES
                + cache_count * _CACHE_OVERHEAD_BYTES
            )

        while content_bytes() > self.max_bytes:
            cache_oldest = connection.execute(
                "SELECT rowid_ FROM cache ORDER BY rowid_ LIMIT 1"
            ).fetchone()
            if cache_oldest:
                connection.execute(
                    "DELETE FROM cache WHERE rowid_ = ?", (cache_oldest[0],)
                )
                continue
            record_oldest = connection.execute(
                "SELECT id FROM records ORDER BY id LIMIT 1"
            ).fetchone()
            if not record_oldest:
                raise RuntimeError("Semantic memory exceeds its size limit")
            record_count = connection.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            if record_count <= 1:
                raise RuntimeError("Semantic memory exceeds its size limit")
            connection.execute(
                "DELETE FROM records WHERE id = ?", (record_oldest[0],)
            )
        self._sync_fts(connection)

    def _sync_fts(self, connection):
        if not self._fts_enabled:
            return
        connection.execute(
            "DELETE FROM records_fts WHERE rowid NOT IN (SELECT id FROM records)"
        )
        missing = connection.execute(
            "SELECT id, text FROM records"
            " WHERE id NOT IN (SELECT rowid FROM records_fts)"
        ).fetchall()
        for record_id, text in missing:
            connection.execute(
                "INSERT INTO records_fts(rowid, text) VALUES (?, ?)",
                (record_id, text),
            )

    @staticmethod
    def _fts_query(query):
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if not tokens:
            return ""
        return " ".join(
            '"%s"' % token.replace('"', '""') for token in tokens[:_MAX_FTS_TERMS]
        )

    @staticmethod
    def _cosine(left, right):
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
            sum(value * value for value in right)
        )
        return (
            sum(a * b for a, b in zip(left, right, strict=True)) / denominator
            if denominator
            else 0.0
        )

    def recall(self, query, client, model, limit=5):
        """Hybrid recall: vector cosine and FTS5 BM25 fused with RRF (k=60)."""
        query = self._validate_texts([query])[0]
        model = self._validate_model(model)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, dimension, embedding, text, source, created_at,"
                " supersedes, superseded_by FROM records WHERE model = ?",
                (model,),
            ).fetchall()
            fts_ranks = {}
            fts_query = self._fts_query(query)
            if fts_query and self._fts_enabled:
                try:
                    fts_ranks = {
                        row[0]: rank
                        for rank, row in enumerate(
                            connection.execute(
                                "SELECT rowid FROM records_fts WHERE records_fts"
                                " MATCH ? ORDER BY bm25(records_fts) LIMIT ?",
                                (fts_query, _CANDIDATE_CAP),
                            ),
                            start=1,
                        )
                    }
                except sqlite3.OperationalError:
                    self._fts_enabled = False
        finally:
            connection.close()
        active = [row for row in rows if row[7] is None]
        if not active:
            return []
        active_by_id = {row[0]: row for row in active}
        query_vector = self.embed_texts([query], client, model)[0]
        vector_ranked = sorted(
            (row for row in active if row[1] == len(query_vector)),
            key=lambda row: self._cosine(
                query_vector, self._decode_vector(row[2], row[1])
            ),
            reverse=True,
        )
        vector_ranks = {
            row[0]: rank for rank, row in enumerate(vector_ranked, start=1)
        }

        def entry(record, score, cosine):
            return {
                "text": record[3],
                "metadata": {
                    "hash": self._hash(record[3]),
                    "model": model,
                    "dimension": record[1],
                    "source": record[4],
                    "created_at": record[5],
                    "supersedes": record[6],
                },
                "score": score,
                "cosine": cosine,
            }

        fused = {}
        # تسوية RRF إلى مقياس 0..1: أعلى رتبة في كل ساق = 1.0، فتبقى عتبات
        # المتصلين (مثل min_score في التعلّم الذاتي) ذات معنى مقارن بالكوسينوس.
        def fused_score(rank):
            return (_RRF_K + 1) / (_RRF_K + rank)

        for record in vector_ranked:
            fused[record[0]] = entry(
                record,
                fused_score(vector_ranks[record[0]]),
                round(self._cosine(query_vector, self._decode_vector(record[2], record[1])), 4),
            )
        for record_id, fts_rank in fts_ranks.items():
            if record_id in fused:
                fused[record_id]["score"] += fused_score(fts_rank)
            elif record_id in active_by_id:
                fused[record_id] = entry(active_by_id[record_id], fused_score(fts_rank), 0.0)
        ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)
        for item in ranked:
            item["score"] = round(min(item["score"], 2.0), 6)
        return ranked[:limit]