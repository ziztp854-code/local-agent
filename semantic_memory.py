import hashlib
import json
import math
import os
import re
import stat
import tempfile
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


class SemanticMemory:
    """Bounded JSON storage for remembered text and reusable local embeddings."""

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
            state_root / "local-agent" / "memory" / f"{memory_id}.json", **limits
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
    def _empty():
        return {"version": 1, "records": [], "cache": []}

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

    def _validate_state(self, state):
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

    def _load(self):
        self._check_path()
        if not self.path.exists():
            return self._empty()
        try:
            with self.path.open("rb") as source:
                data = source.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise RuntimeError("Semantic memory file exceeds its size limit")
            state = json.loads(
                data.decode("utf-8"), object_pairs_hook=self._strict_object
            )
        except RuntimeError:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("Semantic memory file is invalid") from error
        return self._validate_state(state)

    def _encode_bounded(self, state):
        bounded = {
            **state,
            "records": list(state["records"]),
            "cache": list(state["cache"][-self.max_cache_entries :]),
        }
        while True:
            data = json.dumps(
                bounded, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(data) <= self.max_bytes:
                return bounded, data
            if bounded["cache"]:
                bounded["cache"].pop(0)
            elif len(bounded["records"]) > 1:
                bounded["records"].pop(0)
            else:
                raise RuntimeError("Semantic memory exceeds its size limit")

    def _save(self, state):
        candidate = {**state, "cache": list(state["cache"][-self.max_cache_entries :])}
        state, data = self._encode_bounded(self._validate_state(candidate))
        state_dir = self.path.parent
        self._check_path()
        if state_dir.exists() and not state_dir.is_dir():
            raise RuntimeError("Semantic memory path is unsafe")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=state_dir,
                prefix=".semantic-memory-",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as error:
            raise RuntimeError("Could not save semantic memory") from error
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return state

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
        state = self._load()
        if not clean_texts:
            return []

        unique = list(dict.fromkeys(clean_texts))
        hashes = {text: self._hash(text) for text in unique}
        cached = {
            entry["hash"]: entry for entry in state["cache"] if entry["model"] == model
        }
        vectors = {}
        missing = [text for text in unique if hashes[text] not in cached]
        fresh = self._request_embeddings(missing, client, model) if missing else []
        vectors.update(zip(missing, fresh, strict=True))

        if fresh:
            dimension = len(fresh[0])
            stale = [
                text
                for text in unique
                if text not in vectors
                and cached[hashes[text]]["dimension"] != dimension
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
            dimensions = {cached[hashes[text]]["dimension"] for text in unique}
            if len(dimensions) > 1:
                replacements = self._request_embeddings(unique, client, model)
                vectors.update(zip(unique, replacements, strict=True))

        for text in unique:
            if text not in vectors:
                vectors[text] = cached[hashes[text]]["embedding"]

        refreshed = [
            text
            for text in unique
            if hashes[text] not in cached
            or vectors[text] is not cached[hashes[text]]["embedding"]
        ]
        if refreshed:
            refreshed_hashes = {hashes[text] for text in refreshed}
            state["cache"] = [
                entry
                for entry in state["cache"]
                if entry["hash"] not in refreshed_hashes
            ]
            state["cache"].extend(
                {
                    "hash": hashes[text],
                    "model": model,
                    "dimension": len(vectors[text]),
                    "embedding": vectors[text],
                }
                for text in refreshed
            )
            self._save(state)
        return [list(vectors[text]) for text in clean_texts]

    def remember(self, texts, client, model):
        clean_texts = [
            self._redact(text)
            for text in self._validate_texts(texts, reject_sensitive=True)
        ]
        model = self._validate_model(model)
        vectors = self.embed_texts(clean_texts, client, model)
        if not clean_texts:
            return 0
        state = self._load()
        existing = {
            (record["metadata"]["hash"], record["metadata"]["model"])
            for record in state["records"]
        }
        added = []
        seen = set(existing)
        for text, vector in zip(clean_texts, vectors, strict=True):
            key = (self._hash(text), model)
            if key in seen:
                continue
            seen.add(key)
            added.append(
                {
                    "text": text,
                    "metadata": {
                        "hash": key[0],
                        "model": model,
                        "dimension": len(vector),
                    },
                    "embedding": vector,
                }
            )
        if added:
            state["records"] = (state["records"] + added)[-self.max_records :]
            self._save(state)
        return len(added)

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
        query = self._validate_texts([query])[0]
        model = self._validate_model(model)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        state = self._load()
        candidates = [
            record
            for record in state["records"]
            if record["metadata"]["model"] == model
        ]
        if not candidates:
            return []
        query_vector = self.embed_texts([query], client, model)[0]
        ranked = sorted(
            (
                {
                    "text": record["text"],
                    "metadata": dict(record["metadata"]),
                    "score": round(self._cosine(query_vector, record["embedding"]), 4),
                }
                for record in candidates
                if record["metadata"]["dimension"] == len(query_vector)
            ),
            key=lambda record: record["score"],
            reverse=True,
        )
        return ranked[:limit]
