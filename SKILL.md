---
name: loops-skill
description: |
  Run a long, multi-step task against a goal with built-in progress/quality guarantees:
  split work across four independent agents (Thinker / Executor / Checker / Reflector),
  cross-verify each step with an A/B/C vote (artifact / goal-alignment / trajectory),
  and pause for human input only when the loop genuinely cannot advance. Use this skill
  when the user says "drive this project forward", "keep working until done", "run this
  loop until X is achieved", "iterate until converged", or hands you a goal that is
  larger than one session and needs sub-agent ownership plus independent verification.
  Do NOT use for single-shot, well-scoped tasks the orchestrator can finish directly
  (use plain tool calls), for short brainstorming without execution (use plan-mode), or
  for short ad-hoc Q&A. The skill is also not the right home for one-off research
  reports — use deep-research for those.
---

# Loops Skill

Drive a goal forward across many steps using four cooperating agents and a verifier.
The loop pauses only when (a) the goal is satisfied, (b) the loop is genuinely blocked
and needs a human call, or (c) the configured budget is exhausted.

## Quickstart (5 lines, then read the rest)

```
goal      = "process all 24 md files under source_proposals/staging/ through compress-fragments"
storage   = JsonFileStorage("state/loops")
runtime   = MavisAgentRuntime(agents={t:"mavis-t",e:"mavis-e",c:"mavis-c",r:"mavis-r"})
LoopRunner(storage, runtime).run(goal=goal, max_iter=30, max_minutes=60)
```

That is the whole loop in 4 lines. Everything below explains the contract those 4
lines rely on. If you only need to drive a goal forward and you have a Mavis
runtime available, you can stop reading after the **Inputs to collect** section
and come back when something goes wrong.

**Batteries-included runtime.** A stdlib-only Python implementation ships with
this skill at `scripts/loop_runtime.py`, plus a CLI entry point
(`python /path/to/loops-skill/__main__.py run --goal "..."`). The CLI ships
with a `MockAgentRuntime` so you can exercise the loop end-to-end without a
Mavis deployment. To wire a real Mavis adapter, implement the `AgentRuntime`
Protocol in `scripts/loop_runtime.py` and follow `references/mavis-adapter.md`.

A worked end-to-end example (drives the real `proposal-skill-builder` CLI
through the loop) lives at `scripts/example_proposal_skill_builder.py`.

## Inputs to collect

Before starting, gather or decide:

- **goal**: A concrete, falsifiable objective the loop can drive toward
  (e.g. "process all 24 markdown cases under `source_proposals/staging/` through the
  compress-fragments pipeline"). A vague goal like "improve the project" will not work.
- **max_iterations**: Cap on loop iterations. Default `30`.
- **max_minutes**: Wall-clock cap. Default `60`. Set higher for tasks that need
  long-running builds, lower for tasks that should fail fast.
- **agents**: Which agents to dispatch. Default is the canonical four
  (Thinker / Executor / Checker / Reflector); can be reduced to two for trivial work.
- **storage**: State persistence backend. Default is JSON file under `state/`;
  swap to SQLite via the Storage interface if concurrent safety matters.
- **agent_runtime**: How to dispatch the agents. Default is the **abstract Agent
  interface** so the skill is portable; Mavis users wire the Mavis adapter
  (`references/mavis-adapter.md`).
- **cross_step_every**: How often the Reflector runs the macro (plan-viability)
  pass. Default `5`. Lower this if a single step is expensive and you cannot
  afford reflection overhead, higher it if a single step is cheap and you want
  the loop to roll forward without stopping to check itself.

If the user gives a goal but no caps, use the defaults above. If the goal is too vague
to verify, ask one focused question: "What concrete artifact or state change means
'done'?" — do not start the loop without that.

## Procedure

The loop has three layers. Each layer answers one question:

1. **Per-step layer** (every iteration): what should we do next, did it land, and did
   it advance the goal?
2. **Cross-step layer** (every N steps, default N=5): are we still on a viable path,
   or has the plan itself gone stale?
3. **Block layer**: has the loop genuinely stopped being able to advance, or is it
   just spinning?

### Per-step flow

For every iteration:

1. **Thinker** reads the goal, the last K steps of history, and the cross-step verdict
   (if any). Outputs a JSON `next_action` with `expected_artifact` and
   `expected_success_criteria`. (See `references/agent-interface.md` for the full
   Thinker output schema.)
2. **Executor** runs `next_action`, captures the actual artifact path or output, and
   returns an `ExecutorReport`. The Executor must not self-evaluate success — that is
   the Checker's job.
3. **Checker** runs an independent verification against
   `expected_success_criteria`. Outputs a verdict with concrete evidence (file
   exists, exit code, parsed value). The Checker must use the artifact on disk, not
   the Executor's report — that is what prevents "I said I did it" hallucinations.
4. **Reflector** aggregates the A/B/C vote:
   - **A** = Checker's verdict (artifact quality)
   - **B** = Goal-alignment (Reflector re-reads the goal and asks "did this step
     actually push us forward, or did we go sideways?")
   - **C** = Trajectory check (are the last K steps repeating, looping, or
     drifting?)
   - Outputs `step_verdict` (pass / fail) and `macro_status` (continue / replan /
     blocked). See `references/verdict-schema.md` for the full JSON shape.

5. **Persist** the iteration: append a record to the storage backend (history,
   artifact path, verdict, timestamp).
6. **Branch** on the verdict:
   - `step_verdict == "pass"` → next iteration.
   - `step_verdict == "fail"` with `macro_status == "continue"` → bounce back to
     the Executor with the Reflector's specific feedback. Track consecutive fails.
   - `step_verdict == "fail"` with `macro_status == "replan"` → go back to the
     Thinker with a "your plan is wrong" prompt, not just "retry the same step".
   - `macro_status == "blocked"` → jump straight to the Block layer.

### Cross-step flow

Every N iterations (default 5, configurable):

1. **Reflector (macro)** looks at the last N iterations and answers three questions:
   - Is the **plan** still viable, or did we hit a structural obstacle?
   - Are we **converging** (artifacts getting closer to goal) or oscillating?
   - Has **the goal itself shifted** because we learned something new?
2. If "plan not viable" → send a `replan` signal. The next Thinker call gets
   explicit "your previous plan failed because X, propose a different angle".
3. If "converging" → keep going.
4. If "oscillating" or "goal shifted" → raise a `human_review` flag in the state
   file but do not stop the loop; the loop pauses at the next natural breakpoint.

### Block layer

The loop pauses and surfaces a **BLOCKED** state when **any** of the following is
true:

- The same step has failed 3+ times in a row (typical death-spiral signal).
- `macro_status == "blocked"` from the Reflector.
- `max_iterations` or `max_minutes` reached.
- An agent explicitly raises `"i_am_stuck": true` in its output.

When BLOCKED fires:

1. Persist a snapshot of state, last N step records, and the agent verdicts that
   triggered the block.
2. Emit a single human-readable report (in this skill's normal runtime, that means
   printing to stdout / current chat — see Output contract).
3. Do **not** retry on your own. Wait for the user to either adjust the goal,
   swap the plan, or terminate.

## Output contract

For every iteration:

- One entry appended to the storage backend under `state/loops/<loop_id>/history.jsonl`,
  one JSON object per line:
  ```json
  {
    "iter": 3,
    "ts": "...",
    "thinker": { ... ThinkerOutput ... },
    "executor": { "artifact_path": "...", "raw_output": "..." },
    "checker": { "verdict": "pass|fail|partial", "evidence": [...] },
    "reflector": { "step_verdict": "...", "macro_status": "...", "a_check": "...", "b_check": "...", "c_check": "..." },
    "duration_ms": 12345
  }
  ```

For a terminal state (done / blocked / budget-exhausted):

- One summary record at `state/loops/<loop_id>/final.json` with `status`, the goal,
  total iterations, wall-clock, and a 3-bullet "what happened" recap.
- One human-readable report printed to stdout. In Mavis, this is the assistant's
  reply to the user; in CLI, this is the exit message.

The full verdict JSON schema lives in `references/verdict-schema.md`. Treat it as
machine-readable — do not let any agent narrate a verdict in prose; force JSON
output and parse it.

## Failure handling

- **Agent returns malformed JSON**: treat as a fail. Log the raw output under
  `state/loops/<loop_id>/errors/`, do not retry more than once on the same agent in
  the same step (that is itself a loop-within-a-loop).
- **Storage write fails**: abort the loop immediately, do not silently lose history.
- **Cross-step replan keeps failing**: after 2 consecutive replans that did not move
  the needle, raise BLOCKED — replan-of-replan is a strong stuck signal.
- **One agent keeps returning nonsense while others are healthy**: surface it in the
  Reflector's verdict (`agent_health` field), do not let one broken agent poison the
  whole loop.
- **Goal was wrong from the start**: the user's first BLOCKED reply is the right
  place to correct this — do not try to retroactively re-interpret the goal inside
  the loop.

## Examples

Input (one canonical case): "Process all 24 markdown files under
`source_proposals/staging/` through the compress-fragments pipeline until each file
has a matching `compiled/cases/<id>/fragments.json`."

The loop runs:

1. Thinker: "List staging dir, pick one unprocessed file, plan to run
   compress-fragments on it, expected artifact: `compiled/cases/<id>/fragments.json`."
2. Executor: runs the command, returns `artifact_path`.
3. Checker: reads the artifact, confirms it is valid JSON with the expected schema.
4. Reflector: A pass / B pass (one more case processed toward 24) / C pass (no
   duplicate work).
5. Repeat. After every 5 cases, Reflector (macro) checks convergence.
6. Either: all 24 done → DONE state; or: a step fails 3x → BLOCKED state and a
   single human-readable report.

For full schema and the storage / runtime contract, see:

- `references/agent-interface.md` — what every agent must accept and return
- `references/storage-interface.md` — how to swap JSON for SQLite / Redis
- `references/mavis-adapter.md` — how to wire the loop into Mavis `sessions_spawn`
- `references/verdict-schema.md` — the JSON schemas for all verdicts

## Anti-patterns (do not do these)

These are the failure modes the loop was built to prevent. If you find yourself
about to do any of them, you do not need the loop — you need a different tool.

- **Skipping the Checker** to save time. The Checker is the only thing standing
  between the Executor's "I did it" and reality. A loop without a Checker is
  just a for-loop that lies to itself.
- **Letting the Thinker read its own previous step's artifact path without the
  Checker having verified it**. That gives the Thinker license to plan against a
  file that may not exist.
- **Embedding prose narrations inside verdict JSON**. The runtime parses JSON;
  if you put "verdict: pass (probably)" in a string field, the loop continues
  but you have lost the audit trail.
- **Treating BLOCKED as a failure**. BLOCKED is the loop doing its job — it is
  the signal that the next move is a human call, not another agent spin.
- **Resizing `max_iterations` to avoid writing a better goal**. If you need 500
  iterations, the goal is probably too vague to verify. Rewrite the goal first.
