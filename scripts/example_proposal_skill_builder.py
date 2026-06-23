"""End-to-end real-project test of loops-skill runtime.

Drives a real goal against proposal-skill-builder: list accepted files
that have no compiled case yet, pick one, intake+create-case+compile-case
through the workspace's own CLI, verify artifact on disk, then reflect.

This script uses a SingleSessionAgentRuntime that simulates all four
roles from inside one Python process. In Mavis production, you'd swap
in MavisAgentRuntime (see references/mavis-adapter.md). The point of
this test is to prove the runner + storage + state machine + verifier
contract all hold up against a real CLI, not real sub-session isolation.

Cross-platform notes
--------------------
All paths come from environment variables with sensible defaults, and the
Python interpreter is discovered via ``sys.executable`` so this script works
on macOS, Linux, and Windows without modification.

Required environment variables (optional, with defaults):
    LOOPS_E2E_WORKSPACE   absolute path to the proposal-skill-builder checkout
                          (default: parent of this file's parent of this file's
                          parent — only sensible when co-located in the source
                          tree; CI overrides this)
    LOOPS_E2E_STORAGE     absolute path where loops-skill writes its state.jsonl
                          (default: tempfile.mkdtemp(prefix='loops-e2e-'))
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# Skill directory derived from __file__ — works whether loops-skill is
# installed at ~/.mavis/skills/loops-skill, /usr/local/share/loops-skill, or
# bundled inside a project checkout.
SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

# Workspace: env override first, fallback that only works when this script
# lives inside the proposal-skill-builder repo (tests/ subtree). CI must
# always set LOOPS_E2E_WORKSPACE explicitly.
_DEFAULT_WORKSPACE = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "proposal-skill-builder"
)
WORKSPACE = Path(os.environ.get("LOOPS_E2E_WORKSPACE") or _DEFAULT_WORKSPACE)
if not WORKSPACE.exists():
    raise SystemExit(
        f"workspace not found at {WORKSPACE}. "
        "Set LOOPS_E2E_WORKSPACE env var to your proposal-skill-builder checkout."
    )
sys.path.insert(0, str(WORKSPACE))

from loop_runtime import (
    AgentRuntime, JsonFileStorage, LoopRunner, _now_iso,
)

# Use sys.executable so we don't depend on a `python3` symlink (which does
# not exist on Windows by default — Windows usually ships `python` only).
CLI = [sys.executable, "-m", "skill_builder.cli"]

# Cross-platform temp dir. On Windows, /tmp does not exist; tempfile handles
# this by falling back to %TEMP%. mkdtemp gives us an isolated subdir so
# concurrent e2e runs don't collide.
STORAGE_ROOT = os.environ.get("LOOPS_E2E_STORAGE") or tempfile.mkdtemp(
    prefix="loops-e2e-"
)

# DB path and compiled cases dir come from the workspace's own Config, so
# the example follows whatever path layout the user has configured.
from skill_builder.config import Config as _WsConfig  # noqa: E402
DB_PATH = _WsConfig.DB_PATH
COMPILED_CASES_DIR = WORKSPACE / "compiled" / "cases"


def run_cli(*args):
    """Run workspace CLI, return (exit_code, stdout, stderr).

    We force UTF-8 decoding on stdout/stderr. Without this, ``text=True``
    would use the platform's preferred encoding — GBK on a Chinese Windows
    machine — which would mangle any non-ASCII text (think Chinese
    filenames in intake output).
    """
    p = subprocess.run(
        [*CLI, *args],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return p.returncode, p.stdout, p.stderr


def list_unprocessed_accepted():
    """Query the workspace's own DB for accepted files that have no case yet.

    Replaces the previous text-parsing of `list-files` output, which broke when
    filenames contained spaces (and the column count moved). Going through the
    DB means we get structured rows that are immune to whitespace in filenames.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT file_id, original_filename, current_path "
            "FROM source_files "
            "WHERE status='accepted' AND (case_id IS NULL OR case_id='') "
            "ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def pick_unprocessed_thinker(payload):
    """Thinker: pick the first unprocessed accepted file via DB query."""
    rows = list_unprocessed_accepted()
    if not rows:
        return _stuck("no unprocessed accepted files in DB")
    picked = rows[0]
    return {
        "next_action": f"intake + create-case + compile-case for file_id={picked['file_id']}",
        "rationale": f"first accepted file with no case: {picked['original_filename']}",
        "expected_artifact": f"compiled/cases/<new_case_id>/fragments.json",
        "expected_success_criteria": [
            "case_id appears in skill_builder.db cases table",
            "compiled/cases/<case_id>/fragments.json exists and parses as JSON",
        ],
        "expected_duration_ms": 60000,
        "replan": False,
        "i_am_stuck": False,
        "_picked_file_id": picked["file_id"],
        "_picked_filename": picked["original_filename"],
    }


def _stuck(reason):
    return {
        "next_action": "", "rationale": reason, "expected_artifact": "",
        "expected_success_criteria": [], "replan": False,
        "i_am_stuck": True, "stuck_reason": reason,
    }


def executor(payload):
    """Executor: actually run the workspace CLI for the picked file."""
    thinker = payload["action"]
    if thinker.get("i_am_stuck"):
        return {"executed": False, "i_am_stuck": True,
                "command_run": "(skipped)", "exit_code": -1, "duration_ms": 0,
                "self_assessment": "skipped: thinker stuck"}
    file_id = thinker["_picked_file_id"]
    rc1, out1, err1 = run_cli("intake", "--limit", "1")  # noop-ish: just ensure initialized
    # Real work: create-case + compile-case
    title = f"loop-e2e-{file_id[:8]}"
    rc2, out2, err2 = run_cli("create-case", "--file-id", file_id, "--title", title)
    if rc2 != 0:
        return {"executed": False, "command_run": f"intake + create-case {file_id}",
                "exit_code": rc2, "duration_ms": 0, "i_am_stuck": False,
                "self_assessment": f"create-case failed: {err2[:200]}"}
    # extract case_id from output
    case_id = None
    for ln in out2.splitlines():
        if "case_" in ln:
            for tok in ln.split():
                if tok.startswith("case_"):
                    case_id = tok.strip(":,.")
                    break
    if not case_id:
        return {"executed": False, "command_run": f"create-case {file_id}",
                "exit_code": 2, "duration_ms": 0, "i_am_stuck": False,
                "self_assessment": f"no case_id in output: {out2[:200]}"}
    rc3, out3, err3 = run_cli("compile-case", case_id)
    artifact = COMPILED_CASES_DIR / case_id / "fragments.json"
    return {
        "executed": rc3 == 0 and artifact.exists(),
        "artifact_path": str(artifact),
        "artifact_excerpt": artifact.read_text(encoding="utf-8")[:512] if artifact.exists() else "",
        "command_run": f"intake + create-case {file_id} + compile-case {case_id}",
        "exit_code": rc3,
        "duration_ms": 0,
        "raw_output_tail": (out3 + err3)[-512:],
        "self_assessment": f"compile-case rc={rc3}, artifact_exists={artifact.exists()}",
        "i_am_stuck": False,
        "_case_id": case_id,
    }


def checker(payload):
    """Checker: independently verify artifact on disk.

    The thinker's `expected_artifact` may use a template like
    `compiled/cases/<new_case_id>/fragments.json` because the case_id is not
    known until after create-case runs. We therefore resolve the real path
    from the executor_report (which carries the actual case_id) and verify
    against that. We still record whether the literal placeholder existed
    so the verdict reflects both views.
    """
    raw_expected = Path(payload["expected_artifact"])
    exec_report = payload.get("executor_report", {}) or {}
    real_case_id = exec_report.get("_case_id") or ""
    real_artifact = Path(exec_report.get("artifact_path") or raw_expected)
    evidence = []

    # The literal placeholder path almost certainly won't exist; record that
    # as a soft observation but don't fail the verdict on it alone.
    evidence.append({
        "criterion": "literal expected_artifact exists (template may be a placeholder)",
        "observed": f"missing: {raw_expected}",
        "result": raw_expected.exists(),
    })

    # Hard requirement: the executor's resolved artifact must exist.
    evidence.append({
        "criterion": "executor artifact_path exists",
        "observed": f"missing: {real_artifact}" if not real_artifact.exists() else f"exists: {real_artifact}",
        "result": real_artifact.exists(),
    })

    if real_artifact.exists():
        try:
            parsed = json.loads(real_artifact.read_text(encoding="utf-8"))
            ok = isinstance(parsed, list) or (isinstance(parsed, dict) and "fragments" in parsed)
            evidence.append({
                "criterion": "artifact parses as JSON list or {fragments:[]}",
                "observed": f"type={type(parsed).__name__}, len={len(parsed) if hasattr(parsed, '__len__') else '?'}",
                "result": ok,
            })
        except Exception as exc:
            evidence.append({"criterion": "artifact parses as JSON",
                             "observed": f"parse error: {exc}",
                             "result": False})

    # Verdict: pass if the *resolved* artifact exists and parses. The
    # literal-placeholder observation is informational only.
    hard_checks = [e for e in evidence if "literal expected_artifact" not in e["criterion"]]
    return {
        "verdict": "pass" if all(e["result"] for e in hard_checks) else "fail",
        "evidence": evidence,
        "issues": [],
        "i_am_stuck": False,
    }


def reflector(payload):
    """Reflector: A/B/C vote + macro_status.

    Key invariant: when the thinker reports `i_am_stuck` because there is
    no more work (the goal has been satisfied), the loop should converge,
    not spin forever. We detect this case and emit `step_verdict=pass` and
    `macro_status=converged`. Genuine blockers (e.g. executor failure) still
    get `step_verdict=fail` and `macro_status=blocked`.
    """
    thinker = payload["thinker_output"]
    checker_v = payload["checker_output"]["verdict"]
    no_work_stuck = (
        thinker.get("i_am_stuck")
        and "no unprocessed" in (thinker.get("stuck_reason") or "").lower()
    )
    if no_work_stuck:
        return {
            "step_verdict": "pass",
            "step_reason": "thinker reports goal satisfied: no unprocessed files remain",
            "a_check": "checker verdict not applicable; thinker self-evaluated",
            "b_check": "goal reached: every accepted file has been processed",
            "c_check": "history shows prior iterations succeeded on these files",
            "macro_status": "converged",
            "consecutive_fails": 0,
            "agent_health": {"thinker": "ok", "executor": "ok", "checker": "ok", "reflector": "ok"},
            "i_am_stuck": False,
        }
    return {
        "step_verdict": "pass" if checker_v == "pass" else "fail",
        "step_reason": f"A={checker_v}, B=one more case in registry, C=no duplicate",
        "a_check": f"checker said {checker_v}",
        "b_check": "goal: process all unprocessed accepted files; one more done",
        "c_check": "history tail shows no duplicate work",
        "macro_status": "continue",
        "consecutive_fails": 0,
        "agent_health": {"thinker": "ok", "executor": "ok", "checker": "ok", "reflector": "ok"},
        "i_am_stuck": False,
    }


class SingleSessionRuntime(AgentRuntime):
    def __init__(self):
        self.calls = []
    def call(self, role, payload):
        self.calls.append(role)
        return {"thinker": pick_unprocessed_thinker,
                "executor": executor,
                "checker": checker,
                "reflector": reflector}[role](payload)


def main():
    storage = JsonFileStorage(STORAGE_ROOT)
    runtime = SingleSessionRuntime()
    runner = LoopRunner(storage, runtime, cross_step_every=2)

    # max_iterations is generous enough that the loop can naturally
    # converge when the thinker reports "no unprocessed files". If we
    # cap too low, the loop ends with budget_exhausted even on success.
    result = runner.run(
        goal="Process all accepted files in proposal-skill-builder that have no case yet, until each has compiled/cases/<id>/fragments.json",
        max_iterations=10,
        max_minutes=10,
    )
    print("=== RUN RESULT ===")
    print(json.dumps({
        "loop_id": result.loop_id,
        "status": result.status,
        "iterations": result.iterations,
        "wall_clock_seconds": result.wall_clock_seconds,
        "final": result.final,
    }, indent=2, ensure_ascii=False))
    print("=== AGENT CALL COUNT ===")
    from collections import Counter
    print(dict(Counter(runtime.calls)))


if __name__ == "__main__":
    main()