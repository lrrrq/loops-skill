"""Cross-platform smoke test for loops-skill.

Run with:

    python3 -m pytest tests/test_cross_platform.py -v
or
    python3 tests/test_cross_platform.py

Why this exists
---------------
The runtime ships hardcoded platform assumptions into a few corners
(/tmp paths, locale-dependent decoding, ``python3`` shebang). This test
exercises the runtime end-to-end on whatever platform pytest is running,
so a CI matrix (ubuntu / macOS / windows) immediately surfaces any
regression. It uses only the standard library and the bundled MockAgentRuntime
— no external workspace, no network, no AI calls.
"""
from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Make the bundled runtime importable regardless of where pytest is invoked.
HERE = Path(__file__).resolve().parent
RUNTIME_DIR = HERE.parent / "scripts"
sys.path.insert(0, str(RUNTIME_DIR))

from loop_runtime import (  # noqa: E402
    JsonFileStorage,
    LoopRunner,
    MockAgentRuntime,
    _now_iso,
)


class PlatformAssumptionsTest(unittest.TestCase):
    """Tiny assertions that catch regressions of platform-unsafe code."""

    def test_tempfile_gettempdir_exists(self) -> None:
        """gettempdir() must return a writable directory on every platform."""
        tmp = tempfile.gettempdir()
        self.assertTrue(os.path.isdir(tmp), f"tempdir missing: {tmp}")
        # And we can actually write into it
        probe = Path(tmp) / f"loops-skill-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        try:
            self.assertEqual(probe.read_text(encoding="utf-8"), "ok")
        finally:
            probe.unlink(missing_ok=True)

    def test_pathlib_path_handles_current_platform(self) -> None:
        """Path() round-trips through current filesystem without losing data."""
        if platform.system() == "Windows":
            sample = Path("C:/Users/Example/AppData/Local/Temp/loops test")
        elif platform.system() == "Darwin":
            sample = Path("/Users/example/Library/Application Support/loops test")
        else:
            sample = Path("/tmp/loops-skill-test with spaces")
        self.assertEqual(str(sample), str(Path(str(sample))))

    def test_json_unicode_preserved(self) -> None:
        """ensure_ascii=False + utf-8 encoding preserves CJK and emoji."""
        payload = {"name": "情绪短片", "emoji": "🚀", "latin": "ALSO 「兴趣填空」"}
        raw = json.dumps(payload, ensure_ascii=False, encoding="utf-8") \
            if False else json.dumps(payload, ensure_ascii=False)
        # bytes round-trip
        encoded = raw.encode("utf-8")
        decoded = json.loads(encoded.decode("utf-8"))
        self.assertEqual(decoded, payload)


class JsonFileStorageTest(unittest.TestCase):
    """JsonFileStorage must work no matter where we put the storage root."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="loops-storage-"))
        self.storage = JsonFileStorage(self.tmpdir)

    def tearDown(self) -> None:
        # Best-effort cleanup; we used a tempdir we own.
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_writes_meta_and_history(self) -> None:
        loop_id = "loop_test_001"
        self.storage.init(loop_id, "test goal", {"max_iter": 1})
        meta_path = self.tmpdir / loop_id / "meta.json"
        hist_path = self.tmpdir / loop_id / "history.jsonl"
        self.assertTrue(meta_path.exists())
        self.assertTrue(hist_path.exists())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["goal"], "test goal")

    def test_append_step_and_read_history(self) -> None:
        loop_id = "loop_test_002"
        self.storage.init(loop_id, "g", {})
        step = {
            "iter": 1,
            "ts": _now_iso(),
            "thinker": {"next_action": "do thing"},
            "executor": {"executed": True, "exit_code": 0},
            "checker": {"verdict": "pass"},
            "reflector": {"step_verdict": "pass", "macro_status": "converged"},
        }
        self.storage.append_step(loop_id, step)
        history = self.storage.read_history(loop_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["iter"], 1)

    def test_history_appends_with_utf8_newlines(self) -> None:
        """history.jsonl must be valid JSON Lines with UTF-8 newlines."""
        loop_id = "loop_test_003"
        self.storage.init(loop_id, "unicode goal: 情绪短片", {})
        # Force a write so we can inspect raw bytes
        self.storage.append_step(loop_id, {"iter": 1, "note": "中文"})
        raw = (self.tmpdir / loop_id / "history.jsonl").read_bytes()
        # Each line ends with \n (not \r\n) so other tools can tail it cleanly
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r\n", raw)


class MockRuntimeTest(unittest.TestCase):
    """MockAgentRuntime advertises non-productive placeholders."""

    def test_mock_artifact_uses_gettempdir(self) -> None:
        rt = MockAgentRuntime()
        thinker = rt.call("thinker", {})
        executor = rt.call("executor", {})
        # Placeholder paths must point at the OS tempdir, not /tmp literally,
        # otherwise Windows users see 'No such file or directory'.
        self.assertIn(tempfile.gettempdir(), thinker["expected_artifact"])
        self.assertIn(tempfile.gettempdir(), executor["artifact_path"])
        self.assertTrue(thinker["i_am_stuck"])


class LoopRunnerConvergenceTest(unittest.TestCase):
    """End-to-end: the loop must honor macro_status=converged on every OS."""

    def _runtime(self) -> MockAgentRuntime:
        return MockAgentRuntime()

    def _storage(self) -> JsonFileStorage:
        return JsonFileStorage(Path(tempfile.mkdtemp(prefix="loops-runner-")))

    def test_converged_short_circuits_to_done(self) -> None:
        """Reflector says converged -> loop ends with status=done, not budget."""
        storage = self._storage()
        runtime = self._runtime()
        # Patch in a one-shot reflector that emits converged on iter 1
        calls = {"n": 0}

        def fake_call(role, payload):
            calls["n"] += 1
            if role == "reflector":
                return {
                    "step_verdict": "pass",
                    "macro_status": "converged",
                    "consecutive_fails": 0,
                    "agent_health": {"thinker": "ok", "executor": "ok",
                                     "checker": "ok", "reflector": "ok"},
                    "i_am_stuck": False,
                }
            return runtime.call(role, payload)

        runtime.call = fake_call  # type: ignore[assignment]
        runner = LoopRunner(storage, runtime, cross_step_every=2)
        result = runner.run(goal="g", max_iterations=20, max_minutes=1)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.final.get("block_reason"), "reflector_converged")
        self.assertGreaterEqual(calls["n"], 4)  # 4 agents per iter


if __name__ == "__main__":
    unittest.main()