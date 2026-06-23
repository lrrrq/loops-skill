# Changelog

All notable changes to loops-skill are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.1] - 2026-06-23

### Fixed
- **Cross-platform path support**: replaced hardcoded `/tmp/loops-*` and
  `/Applications/...` with `tempfile.gettempdir()` and `Path(__file__)`
  resolution. Works on macOS, Linux, Windows.
- **subprocess Python discovery**: use `sys.executable` instead of `"python3"`
  so the bundled e2e example works on Windows (where `python3` is not on PATH).
- **Subprocess encoding**: force `encoding="utf-8"` on `subprocess.run(text=True)`
  to avoid GBK default encoding on Chinese Windows machines.
- **Mock runtime placeholder**: `MockAgentRuntime` now reports a placeholder
  path under `tempfile.gettempdir()` rather than literal `/tmp`.

### Added
- `tests/test_cross_platform.py` — 8 unit tests covering platform assumptions,
  JsonFileStorage round-trip, mock runtime temp paths, and the
  `macro_status="converged"` → `status=done` short-circuit.
- `.github/workflows/test.yml` — CI matrix over `ubuntu-latest`,
  `macos-latest`, `windows-latest` × Python 3.9-3.12.
- `LICENSE` (MIT) and `README.md` for open-source readiness.

## [1.1.0] - 2026-06-12

### Added
- 4-agent loop runner (`thinker → executor → checker → reflector`)
- `JsonFileStorage` (file-based loop history)
- `MockAgentRuntime` for tests
- Reference docs: agent-interface, storage-interface, mavis-adapter, verdict-schema
- End-to-end example driving `proposal-skill-builder`
- CLI (`run`, `resume`, `status`, `list`)
- `i_am_stuck` protocol for honest failure
- A/B/C vote in reflector

### Known issues (fixed in 1.1.1)
- Runtime did not honour `macro_status="converged"`; loops spun to
  `budget_exhausted` even when the thinker reported "no more work".
- End-to-end example parsed `list-files` text output, which broke on
  filenames containing spaces or CJK characters.
- Checker verdict counted a literal placeholder artifact path as failure.