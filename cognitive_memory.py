"""Project-scoped operational memory built on the existing local hybrid store."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from semantic_memory import SemanticMemory


_SCHEMA_VERSION = 4
_MAX_TEXT = 4_000
_MAX_EXPERIENCES = 1_000
_MAX_CHECKPOINTS = 250
_MAX_TEMPORAL_FACTS = 1_000
_MAX_RELATIONS = 1_000
_MAX_LEARNED_SKILLS = 500
_SENSITIVE_COMMAND_FLAGS = {
    "--password",
    "--passwd",
    "--token",
    "--access-token",
    "--access_token",
    "--auth-token",
    "--auth_token",
    "--api-key",
    "--api_key",
    "--apikey",
    "--secret",
    "--client-secret",
    "--client_secret",
    "-p",
}
_URL_CREDENTIAL = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s]+@"
)
_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    task TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    framework TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    lesson TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS experience_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experience_id INTEGER NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    verification_json TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS experiences_project_status
    ON experiences(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS attempts_experience
    ON experience_attempts(experience_id, id);
CREATE TABLE IF NOT EXISTS checkpoints (
    task_id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    plan_json TEXT NOT NULL,
    completed_json TEXT NOT NULL,
    remaining_json TEXT NOT NULL,
    modified_files_json TEXT NOT NULL DEFAULT '[]',
    tool_outputs_json TEXT NOT NULL DEFAULT '[]',
    last_success_json TEXT,
    last_failure_json TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reflection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    experience_id INTEGER NOT NULL UNIQUE REFERENCES experiences(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'candidate',
    steps_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_used_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, name, version)
);
CREATE TABLE IF NOT EXISTS temporal_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    valid_from REAL NOT NULL,
    valid_until REAL,
    is_current INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS temporal_current_fact
    ON temporal_facts(project_id, subject, predicate) WHERE is_current=1;
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(project_id, name, kind)
);
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, source_id, predicate, target_id)
);
"""


class LocalEmbeddingProvider:
    """Small adapter for an LM Studio-compatible local embedding client."""

    def __init__(self, client, model):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Embedding model is required")
        self.client = client
        self.model = model.strip()

    def embed(self, texts, model=None):
        return self.client.embed(texts, model or self.model)

    def embed_text(self, text):
        return self.embed([text])[0]

    def embed_batch(self, texts):
        return self.embed(list(texts))

    def health_check(self):
        try:
            vector = self.embed_text("local memory health check")
        except (RuntimeError, ValueError, TypeError, AttributeError):
            return False
        return bool(vector)


class LocalCognitiveMemoryEngine:
    """Local memory facade whose durable learning is gated by real evidence."""

    def __init__(
        self,
        workspace,
        path,
        client,
        embed_model,
        *,
        experience_learning=True,
        reflection=True,
        skill_learning=True,
        knowledge_graph=True,
        memory_consolidation=True,
    ):
        features = (
            experience_learning,
            reflection,
            skill_learning,
            knowledge_graph,
            memory_consolidation,
        )
        if any(type(value) is not bool for value in features):
            raise ValueError("Memory feature flags must be booleans")
        try:
            self.workspace = Path(workspace).expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("Workspace must be a directory") from error
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be a directory")
        self.path = Path(path).expanduser().absolute()
        self.provider = LocalEmbeddingProvider(client, embed_model)
        self.memory = SemanticMemory(self.path)
        self.experience_learning_enabled = experience_learning
        self.reflection_enabled = reflection
        self.skill_learning_enabled = skill_learning
        self.knowledge_graph_enabled = knowledge_graph
        self.memory_consolidation_enabled = memory_consolidation
        self.project_key = hashlib.sha256(
            os.path.normcase(str(self.workspace)).encode("utf-8")
        ).hexdigest()
        self._initialize()

    @classmethod
    def for_workspace(
        cls,
        workspace,
        client,
        embed_model,
        *,
        state_root=None,
        experience_learning=True,
        reflection=True,
        skill_learning=True,
        knowledge_graph=True,
        memory_consolidation=True,
    ):
        try:
            resolved = Path(workspace).expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("Workspace must be a directory") from error
        if not resolved.is_dir():
            raise ValueError("Workspace must be a directory")
        if state_root is None:
            platform_state = os.getenv("LOCALAPPDATA") or os.getenv("XDG_STATE_HOME")
            state_root = (
                Path(platform_state)
                if platform_state
                else Path.home() / ".local" / "state"
            )
        identifier = hashlib.sha256(
            f"{os.path.normcase(str(resolved))}\0cognitive".encode("utf-8")
        ).hexdigest()
        path = Path(state_root) / "local-agent" / "memory" / f"{identifier}.sqlite"
        return cls(
            resolved,
            path,
            client,
            embed_model,
            experience_learning=experience_learning,
            reflection=reflection,
            skill_learning=skill_learning,
            knowledge_graph=knowledge_graph,
            memory_consolidation=memory_consolidation,
        )

    def _connect(self):
        connection = self.memory._connect()
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_CORE_SCHEMA)
        skill_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(learned_skills)")
        }
        if "enabled" not in skill_columns:
            connection.execute(
                "ALTER TABLE learned_skills ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        if "last_used_at" not in skill_columns:
            connection.execute("ALTER TABLE learned_skills ADD COLUMN last_used_at REAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            connection.close()
            raise RuntimeError("Cognitive memory schema is newer than this application")
        if version < _SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            now = time.time()
            connection.execute(
                "INSERT INTO projects(id, project_key, path, created_at, updated_at)"
                " VALUES (1, ?, ?, ?, ?)"
                " ON CONFLICT(project_key) DO UPDATE SET path=excluded.path,"
                " updated_at=excluded.updated_at",
                (self.project_key, str(self.workspace), now, now),
            )
            connection.commit()
        finally:
            connection.close()

    def _safe_text(self, value, limit=_MAX_TEXT):
        if not isinstance(value, str):
            value = str(value)
        value = " ".join(value.split())[:limit]
        if not value:
            return ""
        try:
            return self.memory._redact(
                self.memory._validate_texts([value], reject_sensitive=True)[0]
            )
        except ValueError:
            return "[sensitive content omitted]"

    def _safe_json(self, value, limit=_MAX_TEXT, *, trusted=False):
        try:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(value)
        return rendered[:limit] if trusted else self._safe_text(rendered, limit)

    def _sanitize_tool_arguments(self, tool_name, arguments):
        if tool_name != "run_command" or not isinstance(arguments, dict):
            return arguments
        argv = arguments.get("argv")
        if not isinstance(argv, list):
            return arguments
        clean = []
        redact_next = False
        for part in argv:
            if not isinstance(part, str):
                clean.append(part)
                redact_next = False
                continue
            if redact_next:
                clean.append("[REDACTED]")
                redact_next = False
                continue
            lowered = part.casefold()
            if lowered in _SENSITIVE_COMMAND_FLAGS:
                clean.append(part)
                redact_next = True
                continue
            if lowered.startswith("-p") and len(part) > 2:
                clean.append(f"{part[:2]}[REDACTED]")
                continue
            key, separator, _value = part.partition("=")
            if separator and key.casefold() in _SENSITIVE_COMMAND_FLAGS:
                clean.append(f"{key}=[REDACTED]")
                continue
            clean.append(
                self.memory._redact(
                    _URL_CREDENTIAL.sub(r"\1[REDACTED]@", part)
                )
            )
        return {**arguments, "argv": clean}

    @staticmethod
    def _is_verification_command(arguments):
        if not isinstance(arguments, dict):
            return False
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(part, str) for part in argv)
        ):
            return False
        tokens = [Path(part).name.casefold() for part in argv]
        executable = tokens[0].removesuffix(".exe")
        arguments_only = tokens[1:]
        if any(token in {"--help", "--version", "-h"} for token in arguments_only):
            return False
        if executable == "pytest":
            return True
        if executable == "ruff":
            return bool(arguments_only) and arguments_only[0] == "check"
        if executable in {"mypy", "eslint", "jest", "vitest"}:
            return bool(arguments_only)
        if executable == "tsc":
            return not any(token == "--init" for token in arguments_only)
        if executable in {"python", "python3", "py"}:
            try:
                module_index = arguments_only.index("-m") + 1
                module = arguments_only[module_index]
            except (ValueError, IndexError):
                return False
            if module == "ruff":
                return len(arguments_only) > module_index + 1 and (
                    arguments_only[module_index + 1] == "check"
                )
            return module in {"pytest", "unittest", "mypy"}
        if executable in {"npm", "pnpm", "yarn", "bun"}:
            script = ""
            if arguments_only and arguments_only[0] in {"test", "build", "lint", "check"}:
                script = arguments_only[0]
            elif len(arguments_only) >= 2 and arguments_only[0] in {"run", "run-script"}:
                script = arguments_only[1]
            return bool(
                re.fullmatch(
                    r"(?:test|build|lint|check|typecheck|verify)(?:[:_-].+)?",
                    script,
                )
            )
        allowed = {
            "cargo": {"test", "build", "check", "clippy"},
            "dotnet": {"test", "build"},
            "mvn": {"test", "verify", "package"},
            "gradle": {"test", "build", "check"},
            "gradlew": {"test", "build", "check"},
        }
        first_argument = next(
            (token for token in arguments_only if not token.startswith("-")), ""
        )
        return executable in allowed and first_argument in allowed[executable]

    @staticmethod
    def _prune_cognitive(connection):
        connection.execute(
            "DELETE FROM experiences WHERE id NOT IN ("
            " SELECT id FROM experiences ORDER BY id DESC LIMIT ?)",
            (_MAX_EXPERIENCES,),
        )
        connection.execute(
            "DELETE FROM checkpoints WHERE status!='active' AND task_id NOT IN ("
            " SELECT task_id FROM checkpoints ORDER BY updated_at DESC LIMIT ?)",
            (_MAX_CHECKPOINTS,),
        )
        connection.execute(
            "DELETE FROM temporal_facts WHERE is_current=0 AND id NOT IN ("
            " SELECT id FROM temporal_facts ORDER BY id DESC LIMIT ?)",
            (_MAX_TEMPORAL_FACTS,),
        )
        connection.execute(
            "DELETE FROM relations WHERE id NOT IN ("
            " SELECT id FROM relations ORDER BY id DESC LIMIT ?)",
            (_MAX_RELATIONS,),
        )
        connection.execute(
            "DELETE FROM learned_skills WHERE status!='promoted' AND id NOT IN ("
            " SELECT id FROM learned_skills ORDER BY updated_at DESC LIMIT ?)",
            (_MAX_LEARNED_SKILLS,),
        )
        connection.execute(
            "DELETE FROM entities WHERE id NOT IN ("
            " SELECT source_id FROM relations UNION SELECT target_id FROM relations)"
        )

    def remember(self, content, *, memory_type="project", importance=5, source=""):
        return self.memory.remember(
            [content],
            self.provider,
            self.provider.model,
            source=source,
            kind=memory_type,
            importance=importance,
        )

    def list_memories(self, *, limit=100, query="", include_archived=False):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if type(include_archived) is not bool:
            raise ValueError("include_archived must be boolean")
        query = self._safe_text(query)
        connection = self._connect()
        try:
            parameters = []
            where = " WHERE superseded_by IS NULL"
            if not include_archived:
                where += " AND archived=0"
            if query:
                where += " AND text LIKE ? ESCAPE '\\'"
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                parameters.append(f"%{escaped}%")
            rows = connection.execute(
                "SELECT id, text, source, created_at, kind, importance, feedback,"
                " access_count, success_count, failure_count, pinned, archived"
                " FROM records"
                + where
                + " ORDER BY pinned DESC, importance DESC, created_at DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "id": row[0],
                "text": row[1],
                "source": row[2],
                "created_at": row[3],
                "kind": row[4],
                "importance": row[5],
                "feedback": row[6],
                "access_count": row[7],
                "success_count": row[8],
                "failure_count": row[9],
                "pinned": bool(row[10]),
                "archived": bool(row[11]),
            }
            for row in rows
        ]

    def pin(self, memory_id):
        self.memory.set_pinned(memory_id, True)

    def unpin(self, memory_id):
        self.memory.set_pinned(memory_id, False)

    def archive(self, memory_id):
        self.memory.set_archived(memory_id, True)

    def unarchive(self, memory_id):
        self.memory.set_archived(memory_id, False)

    def forget(self, memory_id):
        self.memory.forget(memory_id)

    def edit_memory(self, memory_id, content):
        new_id = self.memory.supersede(
            memory_id,
            content,
            self.provider,
            self.provider.model,
            source=f"edited:{memory_id}",
        )
        return self.memory.record(new_id, self.provider, self.provider.model)

    def consolidate(self):
        if not self.memory_consolidation_enabled:
            return 0
        return self.memory.consolidate()

    def recall(self, query, *, limit=5, kinds=None):
        requested = set(kinds or ())
        if requested:
            for kind in requested:
                self.memory._validate_kind(kind)
        fetch_limit = min(100, max(limit, limit * 4 if requested else limit))
        try:
            rows = self.memory.recall(
                query, self.provider, self.provider.model, fetch_limit
            )
            retrieval = "hybrid"
        except (RuntimeError, ValueError, TypeError, AttributeError):
            rows = self.memory.keyword_search(query, fetch_limit)
            retrieval = "fts_fallback"
        if requested:
            rows = [row for row in rows if row["metadata"].get("kind") in requested]
        return [{**row, "retrieval": retrieval} for row in rows[:limit]]

    def start_experience(self, task, *, category="", framework="", metadata=None):
        if not self.experience_learning_enabled:
            raise RuntimeError("Experience learning is disabled")
        task = self._safe_text(task)
        if not task:
            raise ValueError("Experience task is required")
        connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO experiences(project_id, task, category, framework,"
                " metadata_json, created_at) VALUES (1, ?, ?, ?, ?, ?)",
                (
                    task,
                    self._safe_text(category, 120),
                    self._safe_text(framework, 120),
                    self._safe_json(metadata or {}),
                    time.time(),
                ),
            )
            self._prune_cognitive(connection)
            connection.commit()
            return cursor.lastrowid
        finally:
            connection.close()

    def record_tool_result(
        self,
        experience_id,
        tool_name,
        arguments,
        result,
        *,
        error="",
    ):
        if type(experience_id) is not int or experience_id < 1:
            raise ValueError("Experience id must be a positive integer")
        tool = self._safe_text(tool_name, 120)
        safe_arguments = self._sanitize_tool_arguments(tool_name, arguments)
        action = self._safe_json(
            safe_arguments, trusted=tool_name == "run_command"
        )
        verified = False
        outcome = "failure" if error else "unknown"
        verification = {}
        if (
            tool_name == "run_command"
            and isinstance(result, dict)
            and self._is_verification_command(arguments)
        ):
            exit_code = result.get("exit_code")
            if type(exit_code) is int:
                verified = True
                outcome = "success" if exit_code == 0 else "failure"
                output = result.get("output", "")
                verification = {
                    "kind": "process_exit",
                    "exit_code": exit_code,
                    "output_sha256": hashlib.sha256(
                        str(output).encode("utf-8", errors="replace")
                    ).hexdigest(),
                }
        connection = self._connect()
        try:
            if connection.execute(
                "SELECT 1 FROM experiences WHERE id=? AND project_id=1",
                (experience_id,),
            ).fetchone() is None:
                raise ValueError("Experience was not found")
            cursor = connection.execute(
                "INSERT INTO experience_attempts(experience_id, tool, action, outcome,"
                " error, verification_json, verified, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experience_id,
                    tool,
                    action,
                    outcome,
                    self._safe_text(error),
                    self._safe_json(verification, trusted=True),
                    int(verified),
                    time.time(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return {
            "id": cursor.lastrowid,
            "outcome": outcome,
            "verified": verified,
            "verification": verification,
        }

    def finish_experience(self, experience_id):
        connection = self._connect()
        try:
            experience = connection.execute(
                "SELECT task FROM experiences WHERE id=? AND project_id=1",
                (experience_id,),
            ).fetchone()
            if experience is None:
                raise ValueError("Experience was not found")
            attempts = connection.execute(
                "SELECT tool, action, outcome, verification_json, verified"
                " FROM experience_attempts WHERE experience_id=? ORDER BY id",
                (experience_id,),
            ).fetchall()
            verified_attempts = [row for row in attempts if row[4]]
            status = verified_attempts[-1][2] if verified_attempts else "unverified"
            verified = status != "unverified"
            lesson = ""
            if verified_attempts:
                decisive = verified_attempts[-1]
                evidence = json.loads(decisive[3])
                prefix = "نجح" if status == "success" else "فشل"
                suffix = (
                    "."
                    if status == "success"
                    else "; غيّر النهج قبل إعادة المحاولة."
                )
                lesson = (
                    f"{prefix} {decisive[0]} {decisive[1]} "
                    f"(exit_code={evidence['exit_code']}){suffix}"
                )
            confidence = 0.95 if verified else 0.5
            connection.execute(
                "UPDATE experiences SET status=?, lesson=?, verified=?, confidence=?,"
                " completed_at=? WHERE id=?",
                (status, lesson, int(verified), confidence, time.time(), experience_id),
            )
            self._prune_cognitive(connection)
            connection.commit()
        finally:
            connection.close()
        if verified:
            try:
                self.remember(
                    "خبرة موثقة "
                    f"{hashlib.sha256(experience[0].encode('utf-8')).hexdigest()[:16]}: "
                    f"{lesson}",
                    memory_type="experience",
                    importance=9 if status == "failure" else 8,
                    source=f"experience:{experience_id}",
                )
            except (RuntimeError, ValueError, TypeError, AttributeError):
                pass
        result = {
            "id": experience_id,
            "status": status,
            "verified": verified,
            "lesson": lesson,
            "confidence": confidence,
        }
        result["reflection"] = (
            self.reflect(experience_id) if self.reflection_enabled else None
        )
        if self.skill_learning_enabled:
            self._learn_verified_commands(experience_id, attempts)
        return result

    def _learn_verified_commands(self, experience_id, attempts):
        latest = {}
        for tool, action, outcome, verification_json, verified in attempts:
            if tool != "run_command" or not verified:
                continue
            try:
                arguments = json.loads(action)
                argv = arguments.get("argv")
                if (
                    not isinstance(argv, list)
                    or not argv
                    or any(not isinstance(part, str) for part in argv)
                ):
                    continue
                latest[self._safe_json(argv, trusted=True)] = (
                    argv,
                    outcome,
                    verification_json,
                )
            except (json.JSONDecodeError, RuntimeError, ValueError, TypeError):
                continue
        for argv, outcome, verification_json in latest.values():
            try:
                tokens = [
                    re.sub(r"[^a-z0-9]+", "-", part.casefold()).strip("-")
                    for part in argv[:3]
                ]
                name = "run-" + "-".join(token for token in tokens if token)
                self.observe_skill_pattern(
                    name[:120],
                    f"Verified command: {' '.join(argv)}",
                    argv,
                    json.loads(verification_json),
                    experience_id=experience_id,
                    success=outcome == "success",
                    verified=True,
                )
            except (json.JSONDecodeError, RuntimeError, ValueError, TypeError):
                continue

    @staticmethod
    def _checkpoint_id(task_id):
        if (
            not isinstance(task_id, str)
            or not 1 <= len(task_id) <= 128
            or "\x00" in task_id
        ):
            raise ValueError("Checkpoint task id is invalid")
        return task_id

    @staticmethod
    def _json_list(value, name):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list")
        return list(value)

    def create_checkpoint(self, task_id, plan, *, completed=(), remaining=None):
        task_id = self._checkpoint_id(task_id)
        plan = self._json_list(plan, "plan")
        completed = self._json_list(completed, "completed")
        remaining = self._json_list(plan if remaining is None else remaining, "remaining")
        now = time.time()
        connection = self._connect()
        try:
            self._prune_cognitive(connection)
            active_count = connection.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE status='active'"
            ).fetchone()[0]
            if active_count >= _MAX_CHECKPOINTS:
                raise RuntimeError("Active checkpoint retention limit reached")
            connection.execute(
                "INSERT INTO checkpoints(task_id, project_id, plan_json, completed_json,"
                " remaining_json, created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    self._safe_json(plan),
                    self._safe_json(completed),
                    self._safe_json(remaining),
                    now,
                    now,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("Checkpoint already exists") from error
        finally:
            connection.close()
        return self.load_checkpoint(task_id)

    def update_checkpoint(
        self,
        task_id,
        *,
        completed=None,
        remaining=None,
        modified_files=None,
        tool_output=None,
        last_success=None,
        last_failure=None,
    ):
        task_id = self._checkpoint_id(task_id)
        current = self.load_checkpoint(task_id)
        if current is None or current["status"] != "active":
            raise ValueError("Active checkpoint was not found")
        outputs = list(current["tool_outputs"])
        if tool_output is not None:
            outputs = [*outputs[-19:], tool_output]
        values = {
            "completed_json": current["completed"] if completed is None else self._json_list(completed, "completed"),
            "remaining_json": current["remaining"] if remaining is None else self._json_list(remaining, "remaining"),
            "modified_files_json": current["modified_files"] if modified_files is None else self._json_list(modified_files, "modified_files"),
            "tool_outputs_json": outputs,
            "last_success_json": current["last_success"] if last_success is None else last_success,
            "last_failure_json": current["last_failure"] if last_failure is None else last_failure,
        }
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE checkpoints SET completed_json=?, remaining_json=?,"
                " modified_files_json=?, tool_outputs_json=?, last_success_json=?,"
                " last_failure_json=?, updated_at=? WHERE task_id=? AND project_id=1",
                (
                    self._safe_json(values["completed_json"]),
                    self._safe_json(values["remaining_json"]),
                    self._safe_json(values["modified_files_json"]),
                    self._safe_json(values["tool_outputs_json"]),
                    self._safe_json(values["last_success_json"]) if values["last_success_json"] is not None else None,
                    self._safe_json(values["last_failure_json"]) if values["last_failure_json"] is not None else None,
                    time.time(),
                    task_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return self.load_checkpoint(task_id)

    def load_checkpoint(self, task_id):
        task_id = self._checkpoint_id(task_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT task_id, plan_json, completed_json, remaining_json,"
                " modified_files_json, tool_outputs_json, last_success_json,"
                " last_failure_json, status, created_at, updated_at"
                " FROM checkpoints WHERE task_id=? AND project_id=1",
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None

        def decode(value, fallback):
            return json.loads(value) if value is not None else fallback

        return {
            "task_id": row[0],
            "plan": decode(row[1], []),
            "completed": decode(row[2], []),
            "remaining": decode(row[3], []),
            "modified_files": decode(row[4], []),
            "tool_outputs": decode(row[5], []),
            "last_success": decode(row[6], None),
            "last_failure": decode(row[7], None),
            "status": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }

    def resume_checkpoint(self, task_id):
        checkpoint = self.load_checkpoint(task_id)
        return checkpoint if checkpoint and checkpoint["status"] == "active" else None

    def _set_checkpoint_status(self, task_id, status):
        task_id = self._checkpoint_id(task_id)
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE checkpoints SET status=?, updated_at=?"
                " WHERE task_id=? AND project_id=1 AND status='active'",
                (status, time.time(), task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Active checkpoint was not found")
            self._prune_cognitive(connection)
            connection.commit()
        finally:
            connection.close()

    def complete_checkpoint(self, task_id):
        self._set_checkpoint_status(task_id, "completed")

    def abort_checkpoint(self, task_id):
        self._set_checkpoint_status(task_id, "aborted")

    def reflect(self, experience_id):
        if type(experience_id) is not int or experience_id < 1:
            raise ValueError("Experience id must be a positive integer")
        connection = self._connect()
        try:
            experience = connection.execute(
                "SELECT task, status, lesson, verified FROM experiences"
                " WHERE id=? AND project_id=1",
                (experience_id,),
            ).fetchone()
            if experience is None:
                raise ValueError("Experience was not found")
            attempts = connection.execute(
                "SELECT tool, action, outcome, verified FROM experience_attempts"
                " WHERE experience_id=? ORDER BY id",
                (experience_id,),
            ).fetchall()
            payload = {
                "task": experience[0],
                "worked": [f"{row[0]} {row[1]}" for row in attempts if row[2] == "success" and row[3]],
                "failed": [f"{row[0]} {row[1]}" for row in attempts if row[2] == "failure" and row[3]],
                "lesson": experience[2],
                "verified": bool(experience[3]),
                "reusable": bool(experience[3] and experience[2]),
                "scope": "project",
            }
            connection.execute(
                "INSERT INTO reflection_events(project_id, experience_id, payload_json, created_at)"
                " VALUES (1, ?, ?, ?) ON CONFLICT(experience_id) DO UPDATE SET"
                " payload_json=excluded.payload_json, created_at=excluded.created_at",
                (experience_id, self._safe_json(payload), time.time()),
            )
            connection.commit()
        finally:
            connection.close()
        return payload

    def reinforce(self, memory_id):
        return self.memory.reinforce(memory_id)

    def penalize(self, memory_id):
        return self.memory.penalize(memory_id)

    def observe_skill_pattern(
        self,
        name,
        description,
        steps,
        verification,
        *,
        experience_id,
        success,
        verified,
    ):
        if not self.skill_learning_enabled or not verified:
            return None
        name = self._safe_text(name, 120)
        if not name:
            raise ValueError("Skill name is required")
        steps = self._json_list(steps, "steps")
        steps = self._sanitize_tool_arguments("run_command", {"argv": steps})["argv"]
        if not isinstance(verification, dict):
            raise ValueError("verification must be an object")
        pattern_hash = hashlib.sha256(
            self._safe_json(steps, trusted=True).encode("utf-8")
        ).hexdigest()
        now = time.time()
        connection = self._connect()
        try:
            self._prune_cognitive(connection)
            row = connection.execute(
                "SELECT id, pattern_hash, version, status, evidence_json,"
                " success_count, failure_count FROM learned_skills"
                " WHERE project_id=1 AND name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row is None or row[1] != pattern_hash:
                skill_count = connection.execute(
                    "SELECT COUNT(*) FROM learned_skills"
                ).fetchone()[0]
                if skill_count >= _MAX_LEARNED_SKILLS:
                    raise RuntimeError("Learned skill retention limit reached")
                version = 1 if row is None else row[2] + 1
                cursor = connection.execute(
                    "INSERT INTO learned_skills(project_id, name, description,"
                    " pattern_hash, version, steps_json, verification_json,"
                    " evidence_json, success_count, failure_count, last_used_at,"
                    " created_at, updated_at)"
                    " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        self._safe_text(description),
                        pattern_hash,
                        version,
                        self._safe_json(steps, trusted=True),
                        self._safe_json(verification, trusted=True),
                        self._safe_json([experience_id], trusted=True),
                        int(bool(success)),
                        int(not success),
                        now,
                        now,
                        now,
                    ),
                )
                skill_id = cursor.lastrowid
                old_status = "candidate"
            else:
                skill_id = row[0]
                old_status = row[3]
                evidence = json.loads(row[4])
                if experience_id not in evidence:
                    evidence.append(experience_id)
                    connection.execute(
                        "UPDATE learned_skills SET evidence_json=?, success_count=?,"
                        " failure_count=?, verification_json=?, last_used_at=?,"
                        " updated_at=? WHERE id=?",
                        (
                            self._safe_json(evidence[-50:], trusted=True),
                            row[5] + int(bool(success)),
                            row[6] + int(not success),
                            self._safe_json(verification, trusted=True),
                            now,
                            now,
                            skill_id,
                        ),
                    )
            counts = connection.execute(
                "SELECT success_count, failure_count FROM learned_skills WHERE id=?",
                (skill_id,),
            ).fetchone()
            total = counts[0] + counts[1]
            status = (
                "promoted"
                if counts[0] >= 3 and counts[0] / total >= 0.8
                else "candidate"
            )
            connection.execute(
                "UPDATE learned_skills SET status=?, updated_at=? WHERE id=?",
                (status, now, skill_id),
            )
            connection.commit()
        finally:
            connection.close()
        skill = self._skill(skill_id)
        if status == "promoted" and old_status != "promoted":
            try:
                self.remember(
                    f"مهارة موثقة: {skill['name']}. {skill['description']}",
                    memory_type="skill",
                    importance=8,
                    source=f"skill:{skill_id}:v{skill['version']}",
                )
            except (RuntimeError, ValueError, TypeError, AttributeError):
                pass
        return skill

    def _skill(self, skill_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, name, description, version, status, steps_json,"
                " verification_json, success_count, failure_count, enabled,"
                " last_used_at, updated_at"
                " FROM learned_skills WHERE id=? AND project_id=1",
                (skill_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "version": row[3],
            "status": row[4],
            "steps": json.loads(row[5]),
            "verification": json.loads(row[6]),
            "success_count": row[7],
            "failure_count": row[8],
            "enabled": bool(row[9]),
            "last_used_at": row[10],
            "updated_at": row[11],
        }

    def list_learned_skills(self, *, promoted_only=False):
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id FROM learned_skills WHERE project_id=1"
                + (" AND status='promoted'" if promoted_only else "")
                + " ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            connection.close()
        return [self._skill(row[0]) for row in rows]

    def set_skill_enabled(self, skill_id, enabled):
        if type(skill_id) is not int or skill_id < 1 or type(enabled) is not bool:
            raise ValueError("Invalid skill update")
        connection = self._connect()
        try:
            skill = connection.execute(
                "SELECT version FROM learned_skills WHERE id=? AND project_id=1",
                (skill_id,),
            ).fetchone()
            if skill is None:
                raise ValueError("Skill was not found")
            connection.execute(
                "UPDATE learned_skills SET enabled=?, updated_at=?"
                " WHERE id=? AND project_id=1",
                (int(enabled), time.time(), skill_id),
            )
            connection.execute(
                "UPDATE records SET archived=? WHERE source=?",
                (int(not enabled), f"skill:{skill_id}:v{skill[0]}"),
            )
            connection.commit()
        finally:
            connection.close()

    def delete_skill(self, skill_id):
        if type(skill_id) is not int or skill_id < 1:
            raise ValueError("Invalid skill id")
        connection = self._connect()
        try:
            skill = connection.execute(
                "SELECT version FROM learned_skills WHERE id=? AND project_id=1",
                (skill_id,),
            ).fetchone()
            if skill is None:
                raise ValueError("Skill was not found")
            connection.execute(
                "DELETE FROM learned_skills WHERE id=? AND project_id=1", (skill_id,)
            )
            connection.execute(
                "DELETE FROM records WHERE source=?",
                (f"skill:{skill_id}:v{skill[0]}",),
            )
            self.memory._sync_fts(connection)
            connection.commit()
        finally:
            connection.close()

    def clear_project_memory(self):
        connection = self._connect()
        try:
            connection.execute("PRAGMA secure_delete=ON")
            for table in (
                "relations",
                "entities",
                "temporal_facts",
                "learned_skills",
                "reflection_events",
                "experience_attempts",
                "checkpoints",
                "experiences",
                "records",
                "cache",
            ):
                connection.execute(f"DELETE FROM {table}")
            self.memory._sync_fts(connection)
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()

    def set_temporal_fact(
        self, subject, predicate, value, *, source="", confidence=0.5
    ):
        subject = self._safe_text(subject, 240)
        predicate = self._safe_text(predicate, 120)
        value = self._safe_text(value)
        if not subject or not predicate or not value:
            raise ValueError("Temporal fact fields are required")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        now = time.time()
        connection = self._connect()
        try:
            current = connection.execute(
                "SELECT id, value FROM temporal_facts WHERE project_id=1"
                " AND subject=? AND predicate=? AND is_current=1",
                (subject, predicate),
            ).fetchone()
            if current and current[1] == value:
                connection.execute(
                    "UPDATE temporal_facts SET source=?, confidence=? WHERE id=?",
                    (self._safe_text(source, 500), float(confidence), current[0]),
                )
                fact_id = current[0]
            else:
                if current:
                    connection.execute(
                        "UPDATE temporal_facts SET valid_until=?, is_current=0 WHERE id=?",
                        (now, current[0]),
                    )
                cursor = connection.execute(
                    "INSERT INTO temporal_facts(project_id, subject, predicate, value,"
                    " source, confidence, valid_from) VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (
                        subject,
                        predicate,
                        value,
                        self._safe_text(source, 500),
                        float(confidence),
                        now,
                    ),
                )
                fact_id = cursor.lastrowid
            self._prune_cognitive(connection)
            connection.commit()
        finally:
            connection.close()
        return self._temporal_fact(fact_id)

    @staticmethod
    def _fact_from_row(row):
        if row is None:
            return None
        return {
            "id": row[0],
            "subject": row[1],
            "predicate": row[2],
            "value": row[3],
            "source": row[4],
            "confidence": row[5],
            "valid_from": row[6],
            "valid_until": row[7],
            "is_current": bool(row[8]),
        }

    def _temporal_fact(self, fact_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, subject, predicate, value, source, confidence,"
                " valid_from, valid_until, is_current FROM temporal_facts"
                " WHERE id=? AND project_id=1",
                (fact_id,),
            ).fetchone()
        finally:
            connection.close()
        return self._fact_from_row(row)

    def current_fact(self, subject, predicate):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, subject, predicate, value, source, confidence,"
                " valid_from, valid_until, is_current FROM temporal_facts"
                " WHERE project_id=1 AND subject=? AND predicate=? AND is_current=1",
                (self._safe_text(subject, 240), self._safe_text(predicate, 120)),
            ).fetchone()
        finally:
            connection.close()
        return self._fact_from_row(row)

    def fact_history(self, subject, predicate):
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, subject, predicate, value, source, confidence,"
                " valid_from, valid_until, is_current FROM temporal_facts"
                " WHERE project_id=1 AND subject=? AND predicate=?"
                " ORDER BY valid_from DESC, id DESC",
                (self._safe_text(subject, 240), self._safe_text(predicate, 120)),
            ).fetchall()
        finally:
            connection.close()
        return [self._fact_from_row(row) for row in rows]

    def _entity_id(self, connection, name, kind=""):
        name = self._safe_text(name, 240)
        kind = self._safe_text(kind, 120)
        if not name:
            raise ValueError("Entity name is required")
        connection.execute(
            "INSERT OR IGNORE INTO entities(project_id, name, kind, created_at)"
            " VALUES (1, ?, ?, ?)",
            (name, kind, time.time()),
        )
        return connection.execute(
            "SELECT id FROM entities WHERE project_id=1 AND name=? AND kind=?",
            (name, kind),
        ).fetchone()[0]

    def link_entities(
        self,
        source_entity,
        relation,
        target_entity,
        *,
        source_kind="",
        target_kind="",
        confidence=0.5,
        source="",
    ):
        if not self.knowledge_graph_enabled:
            raise RuntimeError("Knowledge graph is disabled")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        relation = self._safe_text(relation, 120)
        if not relation:
            raise ValueError("Relation is required")
        connection = self._connect()
        try:
            source_id = self._entity_id(connection, source_entity, source_kind)
            target_id = self._entity_id(connection, target_entity, target_kind)
            now = time.time()
            connection.execute(
                "INSERT INTO relations(project_id, source_id, predicate, target_id,"
                " confidence, source, created_at, updated_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(project_id, source_id, predicate, target_id) DO UPDATE SET"
                " confidence=excluded.confidence, source=excluded.source,"
                " updated_at=excluded.updated_at",
                (
                    source_id,
                    relation,
                    target_id,
                    float(confidence),
                    self._safe_text(source, 500),
                    now,
                    now,
                ),
            )
            self._prune_cognitive(connection)
            connection.commit()
        finally:
            connection.close()

    def graph_neighbors(self, entity):
        entity = self._safe_text(entity, 240)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT target.name, relations.predicate, relations.confidence,"
                " relations.source FROM relations"
                " JOIN entities origin ON origin.id=relations.source_id"
                " JOIN entities target ON target.id=relations.target_id"
                " WHERE relations.project_id=1 AND origin.name=?"
                " ORDER BY relations.confidence DESC, relations.id",
                (entity,),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "target": row[0],
                "relation": row[1],
                "confidence": row[2],
                "source": row[3],
            }
            for row in rows
        ]

    def find_similar_experiences(self, query, limit=5):
        matches = self.recall(query, limit=limit, kinds=("experience",))
        identifiers = []
        for match in matches:
            source = match["metadata"].get("source", "")
            if source.startswith("experience:"):
                try:
                    identifiers.append(int(source.split(":", 1)[1]))
                except ValueError:
                    continue
        if not identifiers:
            return []
        connection = self._connect()
        try:
            rows = {
                row[0]: row
                for row in connection.execute(
                    "SELECT id, task, status, lesson, verified, confidence, created_at"
                    f" FROM experiences WHERE id IN ({','.join('?' for _ in identifiers)})",
                    identifiers,
                )
            }
        finally:
            connection.close()
        return [
            {
                "id": rows[item][0],
                "task": rows[item][1],
                "status": rows[item][2],
                "lesson": rows[item][3],
                "verified": bool(rows[item][4]),
                "confidence": rows[item][5],
                "created_at": rows[item][6],
            }
            for item in identifiers
            if item in rows
        ]

    def list_experiences(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, task, category, framework, status, lesson, verified,"
                " confidence, created_at, completed_at FROM experiences"
                " WHERE project_id=1 ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "id": row[0],
                "task": row[1],
                "category": row[2],
                "framework": row[3],
                "status": row[4],
                "lesson": row[5],
                "verified": bool(row[6]),
                "confidence": row[7],
                "created_at": row[8],
                "completed_at": row[9],
            }
            for row in rows
        ]

    def stats(self):
        connection = self._connect()
        try:
            experiences = connection.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN status='success' AND verified=1 THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN status='failure' AND verified=1 THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN status='unverified' THEN 1 ELSE 0 END)"
                " FROM experiences WHERE project_id=1"
            ).fetchone()
            memories = connection.execute(
                "SELECT COUNT(*) FROM records WHERE superseded_by IS NULL AND archived=0"
            ).fetchone()[0]
            skills = connection.execute(
                "SELECT COUNT(*) FROM learned_skills"
                " WHERE project_id=1 AND status='promoted' AND enabled=1"
            ).fetchone()[0]
        finally:
            connection.close()
        return {
            "memories": memories,
            "experiences": experiences[0],
            "verified_successes": experiences[1] or 0,
            "verified_failures": experiences[2] or 0,
            "unverified_experiences": experiences[3] or 0,
            "learned_skills": skills,
        }
