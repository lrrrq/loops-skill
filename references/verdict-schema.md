# Verdict Schema — JSON shapes for every agent and the loop record

This file is the machine-readable contract for the loop. Every agent's output
must be one of the schemas below, and the loop runner must parse every field it
relies on. The schemas are presented as JSON Schema Draft 2020-12 fragments so
they can be plugged into a validator. A reference Python validator lives at
`scripts/validate_verdict.py`.

## ThinkerOutput

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ThinkerOutput",
  "type": "object",
  "required": ["next_action", "rationale", "expected_artifact",
               "expected_success_criteria", "replan", "i_am_stuck"],
  "additionalProperties": false,
  "properties": {
    "next_action": { "type": "string", "minLength": 1 },
    "rationale": { "type": "string", "minLength": 1 },
    "expected_artifact": { "type": "string", "minLength": 1 },
    "expected_success_criteria": {
      "type": "array",
      "minItems": 1,
      "items": { "type": "string", "minLength": 1 }
    },
    "expected_duration_ms": { "type": "integer", "minimum": 0 },
    "replan": { "type": "boolean" },
    "i_am_stuck": { "type": "boolean" },
    "stuck_reason": { "type": "string" }
  }
}
```

## ExecutorOutput

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ExecutorOutput",
  "type": "object",
  "required": ["executed", "command_run", "duration_ms",
               "self_assessment", "i_am_stuck"],
  "additionalProperties": false,
  "properties": {
    "executed": { "type": "boolean" },
    "artifact_path": { "type": ["string", "null"] },
    "artifact_excerpt": { "type": "string", "maxLength": 4096 },
    "command_run": { "type": "string", "minLength": 1 },
    "exit_code": { "type": "integer" },
    "duration_ms": { "type": "integer", "minimum": 0 },
    "raw_output_tail": { "type": "string", "maxLength": 2048 },
    "self_assessment": { "type": "string" },
    "i_am_stuck": { "type": "boolean" },
    "stuck_reason": { "type": "string" }
  }
}
```

## CheckerOutput

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CheckerOutput",
  "type": "object",
  "required": ["verdict", "evidence", "issues", "i_am_stuck"],
  "additionalProperties": false,
  "properties": {
    "verdict": { "enum": ["pass", "fail", "partial"] },
    "evidence": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["criterion", "observed", "result"],
        "properties": {
          "criterion": { "type": "string" },
          "observed": { "type": "string" },
          "result": { "type": "boolean" }
        }
      }
    },
    "issues": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["severity", "detail"],
        "properties": {
          "severity": { "enum": ["blocker", "major", "minor"] },
          "detail": { "type": "string" }
        }
      }
    },
    "i_am_stuck": { "type": "boolean" },
    "stuck_reason": { "type": "string" }
  }
}
```

## ReflectorOutput

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ReflectorOutput",
  "type": "object",
  "required": ["step_verdict", "step_reason",
               "a_check", "b_check", "c_check",
               "macro_status", "consecutive_fails",
               "agent_health", "i_am_stuck"],
  "additionalProperties": false,
  "properties": {
    "step_verdict": { "enum": ["pass", "fail"] },
    "step_reason": { "type": "string", "minLength": 1 },

    "a_check": { "type": "string", "minLength": 1 },
    "b_check": { "type": "string", "minLength": 1 },
    "c_check": { "type": "string", "minLength": 1 },

    "macro_status": { "enum": ["continue", "replan", "blocked"] },
    "macro_reason": { "type": "string" },
    "consecutive_fails": { "type": "integer", "minimum": 0 },
    "agent_health": {
      "type": "object",
      "required": ["thinker", "executor", "checker", "reflector"],
      "properties": {
        "thinker": { "enum": ["ok", "malformed", "hallucinating"] },
        "executor": { "enum": ["ok", "malformed", "hallucinating"] },
        "checker": { "enum": ["ok", "malformed", "hallucinating"] },
        "reflector": { "enum": ["ok", "malformed", "hallucinating"] }
      }
    },
    "i_am_stuck": { "type": "boolean" },
    "stuck_reason": { "type": "string" }
  }
}
```

## LoopStep (one iteration, the row written to history.jsonl)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "LoopStep",
  "type": "object",
  "required": ["iter", "ts", "thinker", "executor",
               "checker", "reflector", "duration_ms"],
  "properties": {
    "iter": { "type": "integer", "minimum": 1 },
    "ts": { "type": "string", "format": "date-time" },
    "thinker": { "$ref": "#/$defs/ThinkerOutput" },
    "executor": { "$ref": "#/$defs/ExecutorOutput" },
    "checker": { "$ref": "#/$defs/CheckerOutput" },
    "reflector": { "$ref": "#/$defs/ReflectorOutput" },
    "duration_ms": { "type": "integer", "minimum": 0 }
  }
}
```

## FinalRecord (terminal state at final.json)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "FinalRecord",
  "type": "object",
  "required": ["loop_id", "status", "goal", "iterations",
               "wall_clock_seconds", "summary"],
  "properties": {
    "loop_id": { "type": "string" },
    "status": {
      "enum": ["done", "blocked", "budget_exhausted", "user_cancelled"]
    },
    "goal": { "type": "string" },
    "iterations": { "type": "integer", "minimum": 0 },
    "wall_clock_seconds": { "type": "integer", "minimum": 0 },
    "summary": {
      "type": "object",
      "required": ["what_happened", "what_remains", "next_action_for_human"],
      "properties": {
        "what_happened": {
          "type": "array",
          "minItems": 1,
          "maxItems": 3,
          "items": { "type": "string" }
        },
        "what_remains": {
          "type": "array",
          "items": { "type": "string" }
        },
        "next_action_for_human": { "type": "string" }
      }
    },
    "block_reason": { "type": "string" }
  }
}
```

## Why these constraints

- **`additionalProperties: false`** on every agent schema — the runtime must
  know exactly what to expect, and an LLM wandering outside the schema is a
  signal worth surfacing.
- **`minLength: 1` on string fields** — empty strings are how LLMs dodge
  decisions. Force them to commit.
- **`verdict` enum over free text** — the loop runner's branching logic needs
  machine-readable values; prose verdicts are not branchable.
- **`agent_health`** is the only place where one agent rates another. Without
  it, a hallucinating Checker can poison the whole loop silently.
- **`consecutive_fails`** is filled by the runtime, not the Reflector — the
  Reflector can use it as context but cannot lie about it.
- **`i_am_stuck` is required on every agent output.** It is the only field that
  short-circuits the loop regardless of any other verdict. If an agent can
  set `i_am_stuck: true` and explain why, the runtime must halt on the next
  Reflector call. Treat it as the loop's panic button — keep it required so a
  sloppy agent cannot omit it by accident.

## Validation

To validate one agent's output:

```python
from jsonschema import validate, Draft202012Validator

validate(instance=output, schema=ThinkerOutput)
# Raises jsonschema.ValidationError on mismatch
```

The validator script at `scripts/validate_verdict.py` accepts a JSON file path
and an agent role and prints the first validation error, if any.
