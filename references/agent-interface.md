# Agent Interface — the four-agent contract

Every loop iteration calls four agents in this fixed order: **Thinker → Executor →
Checker → Reflector**. They are independent sessions (each with its own context and
trajectory) so a single LLM does not silently "vote with itself". This file is the
portability contract: anyone wiring a new runtime (Mavis, OpenClaw, LangGraph, a
plain subprocess) must implement these four interfaces and obey these schemas.

## Common envelope

Every agent call has the same envelope:

```json
{
  "agent": "thinker" | "executor" | "checker" | "reflector",
  "loop_id": "...",
  "iter": 7,
  "goal": "...",
  "context": {
    "history_tail": [ ... last K LoopStep records ... ],
    "last_macro_verdict": { ... } | null,
    "current_plan": { ... ThinkerOutput from prior replan ... } | null
  },
  "input": { ... agent-specific ... }
}
```

Every agent must return a JSON object — never prose. If the LLM behind an agent
narrates instead of returning JSON, the runtime should retry **at most once**, and
on second failure log the raw output under `state/loops/<loop_id>/errors/` and
treat the step as a fail.

## 1. Thinker

**Persona**: optimistic planner. Asks "what's the next concrete thing that moves us
toward the goal". Bias is toward action, not caution.

**Input**:

```json
{
  "replan_reason": "string | null",
  "previous_step_artifact": "path | null",
  "previous_step_verdict": { ... ReflectorOutput ... } | null
}
```

**Output**:

```json
{
  "next_action": "human-readable action description",
  "rationale": "why this action advances the goal",
  "expected_artifact": "concrete path or output the Executor should produce",
  "expected_success_criteria": [
    "file exists at <path>",
    "file content parses as JSON",
    "exit code == 0",
    "..."
  ],
  "expected_duration_ms": 120000,
  "replan": false,
  "i_am_stuck": false
}
```

**Constraints**:

- `next_action` must name a **single** concrete action, not a batch.
- `expected_artifact` must be **specific**: a file path, a CLI command + expected
  output signature, or a database row id. "Something useful" is not an artifact.
- `expected_success_criteria` must be **observable**: things another agent can
  independently verify without trusting the Executor.
- `replan: true` means the Thinker is proposing a structural change, not just the
  next step. Set this only when the prior plan is no longer viable.
- `i_am_stuck: true` halts the loop with BLOCKED. Use sparingly; do not cry wolf.

## 2. Executor

**Persona**: action-taker. Runs the action, captures the artifact, reports what
actually happened — does **not** claim success or failure.

**Input**:

```json
{
  "action": { ... ThinkerOutput.next_action and friends ... }
}
```

**Output**:

```json
{
  "executed": true,
  "artifact_path": "..." | null,
  "artifact_excerpt": "first 4 KB of produced artifact, or empty if N/A",
  "command_run": "the actual command(s) executed, for reproducibility",
  "exit_code": 0,
  "duration_ms": 12345,
  "raw_output_tail": "last 2 KB of stdout/stderr",
  "self_assessment": "I think this might have worked because X",
  "i_am_stuck": false
}
```

**Constraints**:

- `self_assessment` is **informational only**. The Checker must ignore it.
- If the action failed at the runtime level (command not found, permission
  denied, exception), `executed` may be `false` and the loop treats that as a
  fail.
- `command_run` is mandatory — without it, debugging a stuck loop is impossible.
- `artifact_excerpt` and `raw_output_tail` are bounded to keep history
  manageable; the full artifact stays at `artifact_path`.

## 3. Checker

**Persona**: skeptical verifier. Reads the artifact on disk, runs the success
criteria, produces evidence. Bias is toward fail — "good enough" is not pass.

**Input**:

```json
{
  "expected_artifact": "...",
  "expected_success_criteria": [ "..." ],
  "executor_report": { ... ExecutorOutput ... }
}
```

**Output**:

```json
{
  "verdict": "pass" | "fail" | "partial",
  "evidence": [
    {
      "criterion": "file exists at <path>",
      "observed": "ls -la reports -rw-r--r-- ... <path>",
      "result": true
    },
    ...
  ],
  "issues": [
    {
      "severity": "blocker" | "major" | "minor",
      "detail": "..."
    }
  ],
  "i_am_stuck": false
}
```

**Constraints**:

- The Checker must read `expected_artifact` **on disk itself**. Do not trust the
  Executor's `artifact_path` blindly — verify it exists and matches `excerpt`.
- Each `expected_success_criteria` item must produce one entry in `evidence` with
  an `observed` string. Empty `evidence` on a `pass` verdict is a red flag.
- `partial` is for "some criteria passed, some didn't, but the step still moved
  the goal forward". Reflector decides how to treat `partial`.

## 4. Reflector

**Persona**: brake and lens. Aggregates A/B/C, decides pass/fail on the step, and
every N steps decides whether the **plan** is still viable.

**Input**:

```json
{
  "thinker_output": { ... },
  "executor_output": { ... },
  "checker_output": { ... },
  "history_tail": [ ... last K LoopStep records ... ]
}
```

**Output**:

```json
{
  "step_verdict": "pass" | "fail",
  "step_reason": "one-sentence explanation tied to A/B/C",

  "a_check": "artifact-quality verdict (paraphrasing Checker's evidence)",
  "b_check": "goal-alignment verdict: did this step push toward the goal?",
  "c_check": "trajectory verdict: is the loop repeating / drifting / stuck?",

  "macro_status": "continue" | "replan" | "blocked",
  "macro_reason": "...",
  "consecutive_fails": 0,
  "agent_health": {
    "thinker": "ok" | "malformed" | "hallucinating",
    "executor": "ok" | "malformed" | "hallucinating",
    "checker": "ok" | "malformed" | "hallucinating",
    "reflector": "ok"
  },
  "i_am_stuck": false
}
```

**Constraints**:

- `step_verdict` is the per-step decision (loop continues or bounces).
- `macro_status` is the cross-step decision (when at the N-step boundary).
  Outside of the N-step boundary, default to `continue` unless something
  catastrophic happened.
- `consecutive_fails` is computed from history, not asserted — runtime must
  fill this in before passing to Reflector so the Reflector can use it as
  context, not as a self-report.
- `agent_health` is the Reflector's read on whether the other three agents are
  producing trustworthy output. Surface `hallucinating` only with evidence
  (e.g., "Checker said pass but the file does not actually exist").

## Round-trip guarantees

- A single loop iteration is **one cycle** through Thinker → Executor → Checker →
  Reflector. No agent may be skipped.
- All four calls share `loop_id` and `iter` so the runtime can correlate them in
  storage.
- If any agent's output is unparseable JSON, the runtime retries the agent once
  with the same input but an explicit "respond with JSON only, no prose" prefix.
  A second failure counts as a step fail.
- The Reflector's `i_am_stuck: true` always wins — it short-circuits to
  BLOCKED regardless of other verdicts.
