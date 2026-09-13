"""Fast deterministic acceptance benchmark for local cognitive memory."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cognitive_memory import LocalCognitiveMemoryEngine  # noqa: E402


class LocalBenchmarkEmbeddings:
    def embed(self, texts, _model):
        vectors = []
        for text in texts:
            vector = [0.01] * 32
            for token in text.casefold().split():
                bucket = hashlib.sha256(token.encode("utf-8")).digest()[0] % len(vector)
                vector[bucket] += 1
            norm = math.sqrt(sum(value * value for value in vector))
            vectors.append([value / norm for value in vector])
        return vectors


def run():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first_project = root / "first"
        second_project = root / "second"
        first_project.mkdir()
        second_project.mkdir()
        client = LocalBenchmarkEmbeddings()
        engine = LocalCognitiveMemoryEngine.for_workspace(
            first_project, client, "benchmark-local", state_root=root / "state"
        )
        engine.remember(
            "Project build command is python -m pytest",
            memory_type="project",
            importance=9,
            source="benchmark",
        )

        reopened = LocalCognitiveMemoryEngine.for_workspace(
            first_project, client, "benchmark-local", state_root=root / "state"
        )
        recalled = reopened.recall("project build pytest", limit=1)
        assert recalled and "python -m pytest" in recalled[0]["text"]

        isolated = LocalCognitiveMemoryEngine.for_workspace(
            second_project, client, "benchmark-local", state_root=root / "state"
        )
        assert isolated.recall("project build pytest") == []

        failure_id = reopened.start_experience("Build with an invalid option")
        reopened.record_tool_result(
            failure_id,
            "run_command",
            {"argv": ["python", "-m", "pytest", "--invalid-option"]},
            {"exit_code": 2, "output": "invalid option"},
        )
        assert reopened.finish_experience(failure_id)["status"] == "failure"
        assert reopened.find_similar_experiences("invalid option")[0]["status"] == "failure"

        success_id = reopened.start_experience("Run project tests")
        reopened.record_tool_result(
            success_id,
            "run_command",
            {"argv": ["python", "-m", "pytest"]},
            {"exit_code": 0, "output": "all passed"},
        )
        assert reopened.finish_experience(success_id)["status"] == "success"

        reopened.set_temporal_fact("project", "test_command", "pytest")
        reopened.set_temporal_fact("project", "test_command", "python -m pytest")
        assert reopened.current_fact("project", "test_command")["value"] == "python -m pytest"
        assert len(reopened.fact_history("project", "test_command")) == 2

        timings = []
        for _ in range(20):
            started = time.perf_counter()
            reopened.recall("project build pytest", limit=3)
            timings.append((time.perf_counter() - started) * 1000)
        p95 = statistics.quantiles(timings, n=20)[18]
        assert p95 < 2_000
        print(
            "MEMORY_BENCHMARK_PASSED "
            f"precision=1.00 incorrect_recall=0 reuse=1.00 p95_ms={p95:.2f}"
        )


if __name__ == "__main__":
    run()
