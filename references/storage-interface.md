# Storage Interface — persistent state across iterations and sessions

The loop needs to remember every iteration's record, the goal, the current plan,
and the final outcome. Anything that can read/write a key-value-ish record will
work — the skill deliberately avoids hard-coding SQLite so it stays portable to
JSON, Redis, or in-memory dicts.

## Interface

```python
from typing import Protocol, Iterator, Any

class LoopStorage(Protocol):
    def init(self, loop_id: str, goal: str, config: dict) -> None:
        """Create the loop's state container. Idempotent."""

    def append_step(self, loop_id: str, step: dict) -> None:
        """Append one iteration record. Must persist atomically."""

    def read_history(self, loop_id: str, last_k: int | None = None) -> list[dict]:
        """Read the iteration history (most recent last)."""

    def set_meta(self, loop_id: str, key: str, value: Any) -> None:
        """Set a metadata key on the loop (e.g., current plan, status)."""

    def get_meta(self, loop_id: str, key: str) -> Any:
        """Read a metadata key."""

    def set_final(self, loop_id: str, final_record: dict) -> None:
        """Write the terminal record (DONE / BLOCKED / BUDGET_EXHAUSTED)."""

    def read_final(self, loop_id: str) -> dict | None:
        """Read the terminal record, or None if the loop is still running."""

    def list_loops(self) -> list[str]:
        """List all loop ids this storage has ever seen."""

    def cleanup(self, loop_id: str) -> None:
        """Delete the loop's state. Used by the user, not by the loop itself."""
```

## Default: JSON file backend

The default implementation writes under `state/loops/<loop_id>/`:

```
state/loops/<loop_id>/
├── meta.json          # loop_id, goal, config, status, started_at
├── history.jsonl      # one JSON object per line, append-only
├── final.json         # terminal record, written once at DONE/BLOCKED
└── errors/
    └── <iter>-<agent>.txt   # raw agent output when JSON parsing failed
```

`append_step` opens `history.jsonl` in append mode and writes one JSON line. Each
write is flushed and fsynced so a crash mid-iteration does not lose history.

`read_history` reads the file line by line. `last_k` slices the tail in memory.

This backend is **single-writer-safe**. If you need concurrency (e.g., multiple
loops in parallel from the same orchestrator), use the SQLite backend.

## Optional: SQLite backend

A reference SQLite implementation is **not shipped with this skill** by default
to avoid pulling in a dependency. The contract above is the only thing you need
to implement it: a single SQLite database with a `loops` table and a `loop_steps`
table, both keyed by `loop_id`. Keep `append_step` atomic (single transaction
per row) and `read_history` cheap (index on `loop_id, iter`).

To swap, the user provides a `LoopStorage` instance to the loop runner:

```python
storage = JsonFileStorage(root_dir="state/loops")
# storage = SqliteStorage(db_path="state/loops.db")
runner = LoopRunner(storage=storage, agent_runtime=mavis_runtime)
runner.run(goal=..., max_iterations=30, max_minutes=60)
```

> Note: previous versions of this file pointed at `scripts/sqlite_storage.py`
> and `scripts/validate_verdict.py`. Those scripts are intentionally not part
> of the v1.1 skill package — the storage and verdict contracts are the source
> of truth, and any implementation must follow them, not a bundled script. If
> you need a starter implementation, port the JSON backend in your own repo
> and follow the Protocol.

## What is stored — what is not

**Stored**:

- `goal`, config, `loop_id`, `started_at`
- Every iteration's full record (Thinker / Executor / Checker / Reflector
  outputs)
- `final.json` with terminal status

**Not stored** (kept in agent runtime memory):

- The actual conversation history of each agent (those belong to the agent
  runtime, not the loop). The loop stores the **verdicts**, not the
  conversations.
- Tool-call traces from inside agents (same reason — agent runtime's problem).

This split is intentional: the loop's job is to remember **decisions and
artifacts**, not to second-guess how each agent decided. If an agent's
conversation is needed for debugging, retrieve it from the agent runtime, not
the loop storage.

## Crash recovery

If the loop is interrupted (kill -9, OOM, machine reboot):

1. `read_final(loop_id)` returns `None` → loop was mid-flight.
2. `read_meta(loop_id, "status")` may say `"running"`.
3. Resume: re-instantiate the loop with the same `loop_id`, the runner reads
   history, and continues from the next iteration. The Reflector's
   `history_tail` will include the pre-crash iterations and can detect any
   half-finished state.

If `final.json` exists, the loop is **already done** — resume is a no-op.
