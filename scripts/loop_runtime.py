"""loops-skill runtime.

Stdlib-only Python implementation of the LoopStorage Protocol and the
LoopRunner described in `references/storage-interface.md` and
`references/mavis-adapter.md`. Three layers:

    storage      JsonFileStorage (default) and any user-supplied LoopStorage.
    runtime      AgentRuntime Protocol + a MockAgentRuntime for local dry-runs
                 and a MavisAgentRuntime for live Mavis sessions.
    runner       LoopRunner: one loop = one loop_id, drives iterations until
                 DONE / BLOCKED / BUDGET_EXHAUSTED / user cancel.

Public surface (import from `loop_runtime`):
    JsonFileStorage
    LoopStorage (Protocol)
    AgentRuntime (Protocol)
    MockAgentRuntime
    LoopRunner
    RunResult
    loop_main(argv=None)  -- CLI entry point.

CLI usage:
    python -m loop_runtime run --goal "..." --max-iter 30 --max-minutes 60
    python -m loop_runtime resume --loop-id <id>
    python -m loop_runtime status --loop-id <id>
    python -m loop_runtime list

The CLI is the second of the two entry points the skill promises
("CLI + Mavis skill command"); the Mavis skill command simply re-uses
`loop_main` under the hood.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class LoopStorage(Protocol):
    """The storage contract from `references/storage-interface.md`."""

    def init(self, loop_id: str, goal: str, config: dict) -> None: ...
    def append_step(self, loop_id: str, step: dict) -> None: ...
    def read_history(self, loop_id: str, last_k: int | None = None) -> list[dict]: ...
    def set_meta(self, loop_id: str, key: str, value: Any) -> None: ...
    def get_meta(self, loop_id: str, key: str) -> Any: ...
    def set_final(self, loop_id: str, final_record: dict) -> None: ...
    def read_final(self, loop_id: str) -> dict | None: ...
    def list_loops(self) -> list[str]: ...
    def cleanup(self, loop_id: str) -> None: ...


class JsonFileStorage:
    """Default JSON-file backend. One loop == one directory under root_dir."""

    def __init__(self, root_dir: str | os.PathLike = "state/loops") -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path helpers --------------------------------------------------------
    def _loop_dir(self, loop_id: str) -> Path:
        d = self.root / loop_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _meta_path(self, loop_id: str) -> Path:
        return self._loop_dir(loop_id) / "meta.json"

    def _history_path(self, loop_id: str) -> Path:
        return self._loop_dir(loop_id) / "history.jsonl"

    def _final_path(self, loop_id: str) -> Path:
        return self._loop_dir(loop_id) / "final.json"

    def _errors_dir(self, loop_id: str) -> Path:
        d = self._loop_dir(loop_id) / "errors"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- protocol impl -------------------------------------------------------
    def init(self, loop_id: str, goal: str, config: dict) -> None:
        meta_path = self._meta_path(loop_id)
        if meta_path.exists():
            return  # idempotent
        meta = {
            "loop_id": loop_id,
            "goal": goal,
            "config": config,
            "status": "running",
            "started_at": _now_iso(),
        }
        _atomic_write_json(meta_path, meta)
        # Touch history file so append_step can open it append-mode.
        self._history_path(loop_id).touch()

    def append_step(self, loop_id: str, step: dict) -> None:
        path = self._history_path(loop_id)
        # `newline=""` disables universal newlines mode so a literal "\n"
        # stays "\n" on every OS. Without this, Windows would translate
        # every "\n" we write into "\r\n" and break the JSON Lines format.
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(step, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_history(self, loop_id: str, last_k: int | None = None) -> list[dict]:
        path = self._history_path(loop_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        history = [json.loads(ln) for ln in lines]
        if last_k is not None and last_k > 0:
            history = history[-last_k:]
        return history

    def set_meta(self, loop_id: str, key: str, value: Any) -> None:
        meta_path = self._meta_path(loop_id)
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta[key] = value
        _atomic_write_json(meta_path, meta)

    def get_meta(self, loop_id: str, key: str) -> Any:
        meta_path = self._meta_path(loop_id)
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get(key)

    def set_final(self, loop_id: str, final_record: dict) -> None:
        _atomic_write_json(self._final_path(loop_id), final_record)
        self.set_meta(loop_id, "status", final_record.get("status", "done"))

    def read_final(self, loop_id: str) -> dict | None:
        path = self._final_path(loop_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_loops(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def cleanup(self, loop_id: str) -> None:
        import shutil
        d = self.root / loop_id
        if d.exists():
            shutil.rmtree(d)

    # -- convenience for callers that need to log malformed agent output ----
    def log_error(self, loop_id: str, iter_n: int, agent: str, raw: str) -> None:
        path = self._errors_dir(loop_id) / f"{iter_n:04d}-{agent}.txt"
        path.write_text(raw, encoding="utf-8")


# ---------------------------------------------------------------------------
# AgentRuntime
# ---------------------------------------------------------------------------


class AgentRuntime(Protocol):
    """The four-agent dispatch contract.

    The runtime does not need to know which role it is calling -- it just
    receives the agent name, the role label (thinker / executor / checker /
    reflector), and a payload. The default MockAgentRuntime below plays
    canned agents so the loop is testable end-to-end without a Mavis
    deployment.
    """

    def call(self, role: str, payload: dict) -> dict: ...


class MockAgentRuntime:
    """A canned four-agent runtime for dry-runs and CI.

    It does NOT try to do real work. It produces plausible but trivial
    output so the runner can be exercised: the Thinker emits a "do-nothing"
    next action, the Executor pretends it ran it, the Checker passes
    everything, and the Reflector passes everything. The loop will then
    exit on BUDGET_EXHAUSTED or on the first DONE condition that the
    user's goal can express.

    For a real loop, wire `MavisAgentRuntime` (or any other runtime that
    implements the Protocol).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # Cross-platform placeholder path. On Windows there is no /tmp;
        # tempfile.gettempdir() returns %TEMP% (e.g. C:\Users\xxx\AppData\Local\Temp).
        import tempfile as _tempfile
        self._mock_artifact = str(
            Path(_tempfile.gettempdir()) / "loops-mock-artifact"
        )

    def call(self, role: str, payload: dict) -> dict:
        self.calls.append((role, payload))
        if role == "thinker":
            return {
                "next_action": "noop",
                "rationale": "mock: nothing to do",
                "expected_artifact": self._mock_artifact,
                "expected_success_criteria": ["noop criterion"],
                "replan": False,
                "i_am_stuck": True,
                "stuck_reason": "mock runtime is non-productive; wire a real runtime",
            }
        if role == "executor":
            return {
                "executed": True,
                "artifact_path": self._mock_artifact,
                "artifact_excerpt": "(mock)",
                "command_run": "echo mock",
                "exit_code": 0,
                "duration_ms": 0,
                "raw_output_tail": "(mock)",
                "self_assessment": "mock says ok",
                "i_am_stuck": False,
            }
        if role == "checker":
            return {
                "verdict": "pass",
                "evidence": [{"criterion": "noop criterion", "observed": "mock ok", "result": True}],
                "issues": [],
                "i_am_stuck": False,
            }
        if role == "reflector":
            return {
                "step_verdict": "pass",
                "step_reason": "mock pass",
                "a_check": "mock pass",
                "b_check": "mock pass",
                "c_check": "mock pass",
                "macro_status": "continue",
                "consecutive_fails": 0,
                "agent_health": {"thinker": "ok", "executor": "ok", "checker": "ok", "reflector": "ok"},
                "i_am_stuck": False,
            }
        raise ValueError(f"unknown role: {role}")


# ---------------------------------------------------------------------------
# LoopRunner
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RunResult:
    loop_id: str
    status: str  # done | blocked | budget_exhausted | user_cancelled
    iterations: int
    wall_clock_seconds: int
    final: dict


class LoopRunner:
    """Drive a goal forward with the four-agent loop.

    The runner is the single source of truth for loop lifecycle: it owns
    the iteration counter, the consecutive-fails / consecutive-replans
    budgets, and the terminal-state machine. It does NOT know how each
    agent works -- that is the runtime's job.
    """

    def __init__(
        self,
        storage: LoopStorage,
        runtime: AgentRuntime,
        *,
        cross_step_every: int = 5,
        on_step: Callable[[dict], None] | None = None,
    ) -> None:
        self.storage = storage
        self.runtime = runtime
        self.cross_step_every = cross_step_every
        self.on_step = on_step or (lambda _step: None)

    def run(
        self,
        goal: str,
        *,
        max_iterations: int = 30,
        max_minutes: float = 60.0,
        loop_id: str | None = None,
        is_done: Callable[[dict], bool] | None = None,
    ) -> RunResult:
        loop_id = loop_id or new_loop_id()
        config = {
            "max_iterations": max_iterations,
            "max_minutes": max_minutes,
            "cross_step_every": self.cross_step_every,
        }
        self.storage.init(loop_id, goal, config)

        deadline = time.time() + max_minutes * 60
        consecutive_fails = 0
        consecutive_replans = 0
        iter_n = 0  # ensure bound even if max_iterations < 1
        start = time.time()

        for iter_n in range(1, max_iterations + 1):
            if time.time() > deadline:
                return self._finalize(loop_id, goal, iter_n - 1, start, "budget_exhausted")

            step = self._run_one_iter(loop_id, iter_n, goal, consecutive_fails)
            self.storage.append_step(loop_id, step)
            self.on_step(step)

            reflector = step["reflector"]
            if reflector.get("i_am_stuck"):
                return self._finalize(loop_id, goal, iter_n, start, "blocked", reason="reflector_stuck")

            if reflector["step_verdict"] == "pass":
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                if consecutive_fails >= 3:
                    return self._finalize(
                        loop_id, goal, iter_n, start, "blocked", reason="3_consecutive_fails"
                    )

            if reflector["macro_status"] == "replan":
                consecutive_replans += 1
                if consecutive_replans >= 2:
                    return self._finalize(
                        loop_id, goal, iter_n, start, "blocked", reason="replan_loop"
                    )
            else:
                consecutive_replans = 0

            if reflector["macro_status"] == "blocked":
                return self._finalize(loop_id, goal, iter_n, start, "blocked", reason="macro_blocked")

            # `converged` means the reflector (or thinker) has determined the
            # goal is satisfied — there is no more work. Honor it and stop
            # the loop instead of charging into budget_exhausted.
            if reflector["macro_status"] == "converged":
                return self._finalize(loop_id, goal, iter_n, start, "done", reason="reflector_converged")

            if is_done and is_done(step):
                return self._finalize(loop_id, goal, iter_n, start, "done")

        return self._finalize(loop_id, goal, iter_n, start, "budget_exhausted")

    def resume(self, loop_id: str, *, is_done: Callable[[dict], bool] | None = None) -> RunResult:
        """Resume a loop that was interrupted before writing final.json.

        Reads meta + history, restarts from the next iteration. The user
        is expected to have wired the same runtime as the original run.
        """
        goal = self.storage.get_meta(loop_id, "goal")
        if goal is None:
            raise ValueError(f"loop {loop_id!r} not found")
        config = self.storage.get_meta(loop_id, "config") or {}
        existing = self.storage.read_history(loop_id)
        max_iter = int(config.get("max_iterations", 30))
        max_min = float(config.get("max_minutes", 60))
        start_iter = len(existing) + 1

        deadline = time.time() + max_min * 60
        consecutive_fails = 0
        consecutive_replans = 0
        start = time.time()
        iter_n = start_iter - 1  # ensure bound even if max_iter < start_iter

        for iter_n in range(start_iter, max_iter + 1):
            if time.time() > deadline:
                return self._finalize(loop_id, goal, iter_n - 1, start, "budget_exhausted")
            step = self._run_one_iter(loop_id, iter_n, goal, consecutive_fails)
            self.storage.append_step(loop_id, step)
            self.on_step(step)

            reflector = step["reflector"]
            if reflector.get("i_am_stuck"):
                return self._finalize(loop_id, goal, iter_n, start, "blocked", reason="reflector_stuck")

            if reflector["step_verdict"] == "pass":
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                if consecutive_fails >= 3:
                    return self._finalize(
                        loop_id, goal, iter_n, start, "blocked", reason="3_consecutive_fails"
                    )

            if reflector["macro_status"] == "replan":
                consecutive_replans += 1
                if consecutive_replans >= 2:
                    return self._finalize(loop_id, goal, iter_n, start, "blocked", reason="replan_loop")
            else:
                consecutive_replans = 0

            if reflector["macro_status"] == "blocked":
                return self._finalize(loop_id, goal, iter_n, start, "blocked", reason="macro_blocked")

            # Mirror the run() loop: honor `converged` and stop the loop.
            if reflector["macro_status"] == "converged":
                return self._finalize(loop_id, goal, iter_n, start, "done", reason="reflector_converged")

            if is_done and is_done(step):
                return self._finalize(loop_id, goal, iter_n, start, "done")

        return self._finalize(loop_id, goal, iter_n, start, "budget_exhausted")

    # -- internals -----------------------------------------------------------
    def _run_one_iter(self, loop_id: str, iter_n: int, goal: str, consecutive_fails: int) -> dict:
        t0 = time.time()
        history_tail = self.storage.read_history(loop_id, last_k=5)
        last_macro = self.storage.get_meta(loop_id, "last_macro_verdict")
        prev_artifact = history_tail[-1]["executor"].get("artifact_path") if history_tail else None
        prev_verdict = history_tail[-1]["reflector"] if history_tail else None

        # 1. Thinker
        thinker = self._safe_call(
            "thinker",
            loop_id,
            iter_n,
            {
                "replan_reason": last_macro if last_macro and last_macro.get("macro_status") == "replan" else None,
                "previous_step_artifact": prev_artifact,
                "previous_step_verdict": prev_verdict,
            },
        )
        if thinker.get("i_am_stuck"):
            thinker.setdefault("stuck_reason", "thinker self-reported stuck")

        # 2. Executor (skip if Thinker is stuck)
        if thinker.get("i_am_stuck"):
            executor = _synthetic_executor_skip()
        else:
            executor = self._safe_call("executor", loop_id, iter_n, {"action": thinker})

        # 3. Checker (skip if Executor did not run)
        if executor.get("i_am_stuck") or not executor.get("executed"):
            checker = _synthetic_checker_skip()
        else:
            # Strip self_assessment from the prompt to the Checker to prevent
            # the "I did it" hallucination (mavis-adapter pitfall #2).
            scrubbed = {k: v for k, v in executor.items() if k != "self_assessment"}
            checker = self._safe_call(
                "checker",
                loop_id,
                iter_n,
                {
                    "expected_artifact": thinker.get("expected_artifact"),
                    "expected_success_criteria": thinker.get("expected_success_criteria", []),
                    "executor_report": scrubbed,
                },
            )

        # 4. Reflector
        reflector = self._safe_call(
            "reflector",
            loop_id,
            iter_n,
            {
                "thinker_output": thinker,
                "executor_output": executor,
                "checker_output": checker,
                "history_tail": history_tail,
                "consecutive_fails": consecutive_fails,
            },
        )
        # Reflector fills in macro_status, and at N-step boundaries the
        # runtime is responsible for remembering the last macro verdict.
        if iter_n % self.cross_step_every == 0:
            self.storage.set_meta(loop_id, "last_macro_verdict", reflector)

        return {
            "iter": iter_n,
            "ts": _now_iso(),
            "thinker": thinker,
            "executor": executor,
            "checker": checker,
            "reflector": reflector,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    def _safe_call(self, role: str, loop_id: str, iter_n: int, payload: dict) -> dict:
        try:
            raw = self.runtime.call(role, payload)
            if not isinstance(raw, dict):
                raise ValueError(f"{role} returned non-dict: {type(raw).__name__}")
            return raw
        except Exception as exc:  # noqa: BLE001
            self.storage.log_error(loop_id, iter_n, role, f"{type(exc).__name__}: {exc}\npayload={payload!r}")
            return _synthetic_malformed(role, str(exc))

    def _finalize(
        self,
        loop_id: str,
        goal: str,
        iterations: int,
        start_ts: float,
        status: str,
        *,
        reason: str | None = None,
    ) -> RunResult:
        history = self.storage.read_history(loop_id)
        final = {
            "loop_id": loop_id,
            "status": status,
            "goal": goal,
            "iterations": iterations,
            "wall_clock_seconds": int(time.time() - start_ts),
            "summary": {
                "what_happened": _summarize(history, status),
                "what_remains": [] if status == "done" else [f"loop ended with status={status}"],
                "next_action_for_human": (
                    "inspect state/loops/<loop_id>/history.jsonl"
                    if status in ("blocked", "budget_exhausted")
                    else "loop reached done"
                ),
            },
        }
        if reason:
            final["block_reason"] = reason
        self.storage.set_final(loop_id, final)
        return RunResult(
            loop_id=loop_id,
            status=status,
            iterations=iterations,
            wall_clock_seconds=final["wall_clock_seconds"],
            final=final,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop-runtime", description="loops-skill runtime CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Start a new loop")
    p_run.add_argument("--goal", required=True)
    p_run.add_argument("--max-iter", type=int, default=30)
    p_run.add_argument("--max-minutes", type=float, default=60.0)
    p_run.add_argument("--loop-id", default=None)
    p_run.add_argument("--storage-root", default="state/loops")
    p_run.add_argument("--runtime", choices=["mock"], default="mock",
                       help="which AgentRuntime to use (only 'mock' is bundled)")
    p_run.add_argument("--cross-step-every", type=int, default=5)

    p_resume = sub.add_parser("resume", help="Resume an interrupted loop")
    p_resume.add_argument("--loop-id", required=True)
    p_resume.add_argument("--storage-root", default="state/loops")
    p_resume.add_argument("--runtime", choices=["mock"], default="mock")

    p_status = sub.add_parser("status", help="Show a loop's terminal state")
    p_status.add_argument("--loop-id", required=True)
    p_status.add_argument("--storage-root", default="state/loops")

    p_list = sub.add_parser("list", help="List known loop ids")
    p_list.add_argument("--storage-root", default="state/loops")

    return p


def loop_main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code (0 success, 1 fail)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "list":
        storage = JsonFileStorage(args.storage_root)
        for lid in storage.list_loops():
            print(lid)
        return 0

    if args.cmd == "status":
        storage = JsonFileStorage(args.storage_root)
        final = storage.read_final(args.loop_id)
        if final is None:
            meta = storage.get_meta(args.loop_id, "status") or "unknown"
            print(json.dumps({"loop_id": args.loop_id, "status": meta, "terminal": False}, indent=2))
            return 0
        print(json.dumps(final, indent=2, ensure_ascii=False))
        return 0

    storage = JsonFileStorage(args.storage_root)
    runtime = MockAgentRuntime()  # only mock is bundled; see mavis-adapter.md for live wiring

    if args.cmd == "run":
        runner = LoopRunner(storage, runtime, cross_step_every=args.cross_step_every)
        result = runner.run(
            args.goal,
            max_iterations=args.max_iter,
            max_minutes=args.max_minutes,
            loop_id=args.loop_id,
        )
    elif args.cmd == "resume":
        runner = LoopRunner(storage, runtime)
        result = runner.resume(args.loop_id)
    else:  # pragma: no cover
        parser.error(f"unknown cmd: {args.cmd}")
        return 2

    print(json.dumps({
        "loop_id": result.loop_id,
        "status": result.status,
        "iterations": result.iterations,
        "wall_clock_seconds": result.wall_clock_seconds,
    }, indent=2))
    return 0 if result.status in ("done", "budget_exhausted") else 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_loop_id() -> str:
    return f"loop_{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _summarize(history: list[dict], status: str) -> list[str]:
    if not history:
        return [f"loop ended with status={status} and no iterations ran"]
    passes = sum(1 for s in history if s.get("reflector", {}).get("step_verdict") == "pass")
    fails = len(history) - passes
    return [
        f"ran {len(history)} iterations ({passes} pass / {fails} fail)",
        f"ended with status={status}",
    ]


def _synthetic_executor_skip() -> dict:
    return {
        "executed": False,
        "artifact_path": None,
        "artifact_excerpt": "",
        "command_run": "(skipped: thinker reported i_am_stuck)",
        "exit_code": -1,
        "duration_ms": 0,
        "raw_output_tail": "",
        "self_assessment": "skipped",
        "i_am_stuck": True,
        "stuck_reason": "thinker reported i_am_stuck; executor skipped",
    }


def _synthetic_checker_skip() -> dict:
    return {
        "verdict": "fail",
        "evidence": [],
        "issues": [{"severity": "blocker", "detail": "executor did not run; checker skipped"}],
        "i_am_stuck": True,
        "stuck_reason": "executor did not run",
    }


def _synthetic_malformed(role: str, exc_msg: str) -> dict:
    if role == "reflector":
        # Reflector malformed -> hard fail the step.
        return {
            "step_verdict": "fail",
            "step_reason": f"reflector raised: {exc_msg}",
            "a_check": "n/a",
            "b_check": "n/a",
            "c_check": "n/a",
            "macro_status": "continue",
            "consecutive_fails": 0,
            "agent_health": {"thinker": "ok", "executor": "ok", "checker": "ok", "reflector": "malformed"},
            "i_am_stuck": False,
        }
    return {
        "executed": role == "executor",
        "artifact_path": None,
        "artifact_excerpt": "",
        "command_run": f"(synthetic: {role} raised)",
        "exit_code": -1,
        "duration_ms": 0,
        "raw_output_tail": "",
        "self_assessment": f"synthetic-malformed: {exc_msg}",
        "i_am_stuck": True,
        "stuck_reason": exc_msg,
    }


if __name__ == "__main__":  # pragma: no cover
    sys.exit(loop_main())
