# Mavis Adapter — wiring sessions_spawn to the Agent interface

This file is for **Mavis users**. The loop skill's core engine is runtime-agnostic
— it only knows about the four-agent `Agent` interface in
`references/agent-interface.md`. To run on Mavis, you wire each agent to a
`sessions_spawn` call.

## The pattern

> **Pseudocode.** The `mavis.communication.send` shape below is a Mavis-runtime
> abstraction; the production way to invoke it is the `mavis communication send`
> CLI (see the comment block at the end of this section for the CLI equivalent).

```python
from mavis import communication

class MavisAgentRuntime:
    """Implements AgentRuntime by dispatching to Mavis sessions_spawn."""

    def call(self, agent_role: str, payload: dict) -> dict:
        # Map role -> Mavis agent name. User configures this mapping.
        mavis_agent_name = self.role_to_agent[agent_role]
        # Build a strict prompt: goal + context + role-specific input.
        prompt = self._render_prompt(agent_role, payload)
        # Spawn a fresh sub-session. sessions_spawn, NOT sessions_send.
        # Reason: sessions_send requires the target agent to already have an
        # active session; an idle target makes the tool call fail. spawn
        # always works.
        result_json = communication.send(
            from_session=self.owner_session,
            to_session=self.owner_session,  # spawn mode
            command="spawn",
            content=json.dumps({
                "agent": mavis_agent_name,
                "prompt": prompt,
                "expect": "json"  # tell the spawned agent to return JSON only
            }),
        )
        # Parse the sub-session's final reply as JSON.
        return json.loads(result_json)
```

**CLI equivalent** (what a runner script actually shells out to today):

```bash
mavis communication send \
  --from "$OWNER_SESSION" \
  --to "$OWNER_SESSION" \
  --command spawn \
  --content "$(jq -n --arg agent "$mavis_agent_name" --arg prompt "$prompt" \
    '{agent:$agent, prompt:$prompt, expect:"json"}')"
```

The runtime wrapper's job is to hide this from the loop logic; the loop itself
only ever calls `runtime.call(role, payload)`.

## Role → Mavis agent mapping

The user provides this in their loops config (e.g., `loops.yaml`):

```yaml
agents:
  thinker: mavis-agent-thinker
  executor: mavis-agent-executor
  checker: mavis-agent-checker
  reflector: mavis-agent-reflector
```

Each `mavis-agent-*` is an agent the user has created and given a stable system
prompt. The Thinker prompt should bias toward optimism and concrete action. The
Checker prompt should bias toward skepticism and demand evidence. The Reflector
prompt should bias toward brake and consistency. Personality does real work here
— do not use the same default prompt for all four.

## Prompt rendering per role

The runtime must inject the JSON schema **and** the user's role persona into each
spawned session. Skeleton:

```text
You are the {role} in a loop that drives this goal:
{goal}

Current iteration: {iter}
History (last {K} steps): {history_tail}

Your input for this call:
{role_specific_input_json}

You must reply with ONE JSON object matching this schema:
{schema_for_role_json}

Reply with JSON only. No prose, no markdown fences. If you cannot, set
"i_am_stuck": true and explain in a single "stuck_reason" field.
```

The `expect: "json"` flag on the spawn call reinforces this at the Mavis layer.

## Pitfalls (from production use)

These are not theoretical — they were hit on real Mavis deployments:

### 1. `sessions_send` vs `sessions_spawn`

`sessions_send(agentId, message)` requires the target agent to **already have
an active session id**. An idle target makes the call fail. Use `sessions_spawn`
which always creates a fresh session.

### 2. The "I did it" hallucination

The Executor's `self_assessment` field exists **only** so the Checker can ignore
it. If the runtime forwards `self_assessment` into the Checker's prompt
"because it has useful info", the Checker will rubber-stamp the Executor. Strip
it from the Checker prompt.

### 3. Agents role-playing instead of calling tools

A Mavis agent whose system prompt says "you coordinate with X agent" will often
**narrate** a fake conversation instead of actually calling `sessions_spawn`.
Mitigation: the spawn call's `prompt` should not say "talk to other agents" —
say "you are a single role, here is your task, here is your output schema".

### 4. The reflector-of-reflector infinite loop

If the Reflector can trigger a replan, and the replan triggers another
Reflector, and that Reflector triggers another replan — you can spiral. Cap
consecutive replans at 2. After 2 with no measurable progress, BLOCKED.

### 5. Sub-session context bleed

If you reuse the same sub-session across iterations (instead of spawning fresh),
the previous iteration's conversation will pollute the next. **Always spawn
fresh**. If the loop is bottlenecked by spawn latency, batch iterations into
chunks of N=5 and only reflect once per chunk.

## Minimal runtime implementation

```python
# Pseudocode — actual implementation is in the user's runtime config.
class LoopRunner:
    def __init__(self, agent_runtime, storage, config):
        self.runtime = agent_runtime
        self.storage = storage
        self.config = config

    def run(self, goal, max_iter=30, max_minutes=60):
        loop_id = new_loop_id()
        self.storage.init(loop_id, goal, self.config)

        deadline = time.time() + max_minutes * 60
        consecutive_fails = 0
        consecutive_replans = 0

        for iter_n in range(1, max_iter + 1):
            if time.time() > deadline:
                self._finalize(loop_id, "BUDGET_EXHAUSTED")
                return

            step = {"iter": iter_n, "ts": now_iso()}

            step["thinker"] = self.runtime.call("thinker", {
                "replan_reason": ...,
                "previous_step_artifact": ...,
                "previous_step_verdict": ...,
            })
            if step["thinker"].get("i_am_stuck"):
                self._finalize(loop_id, "BLOCKED")
                return

            step["executor"] = self.runtime.call("executor", {
                "action": step["thinker"],
            })

            step["checker"] = self.runtime.call("checker", {
                "expected_artifact": step["thinker"]["expected_artifact"],
                "expected_success_criteria": step["thinker"]["expected_success_criteria"],
                "executor_report": step["executor"],
            })

            step["reflector"] = self.runtime.call("reflector", {
                "thinker_output": step["thinker"],
                "executor_output": step["executor"],
                "checker_output": step["checker"],
                "history_tail": self.storage.read_history(loop_id, last_k=5),
                "consecutive_fails": consecutive_fails,
            })

            if step["reflector"].get("i_am_stuck"):
                self._finalize(loop_id, "BLOCKED")
                return

            self.storage.append_step(loop_id, step)

            if step["reflector"]["step_verdict"] == "pass":
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                if consecutive_fails >= 3:
                    self._finalize(loop_id, "BLOCKED", reason="3_consecutive_fails")
                    return

            if step["reflector"]["macro_status"] == "replan":
                consecutive_replans += 1
                if consecutive_replans >= 2:
                    self._finalize(loop_id, "BLOCKED", reason="replan_loop")
                    return
            else:
                consecutive_replans = 0

            if step["reflector"]["macro_status"] == "blocked":
                self._finalize(loop_id, "BLOCKED")
                return

            # success criteria reached: the loop's terminal condition is
            # supplied by the user (e.g., a checker pattern). Until then,
            # keep iterating.
```

## When to NOT use Mavis adapter

- The agent has no Mavis runtime available (plain subprocess shell). Use a
  `SubprocessAgentRuntime` that calls `claude -p <prompt>` or similar.
- You need all four agents on the same machine and want to avoid sub-session
  overhead. Use a `LocalAgentRuntime` that calls one LLM with role-specific
  system prompts (faster, but loses the "independent context" guarantee).
- You're integrating into a non-Mavis platform (LangGraph, CrewAI). Write
  your own adapter using `references/agent-interface.md` as the contract.
