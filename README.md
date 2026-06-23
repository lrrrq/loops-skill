# loops-skill

> 4-agent multi-step loop runner for Mavis / Claude Code / standalone use

让单个 goal 跑多步 think → execute → check → reflect 循环，
直到目标收敛、被阻塞、或预算耗尽。stdlib-only，零依赖。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)](https://github.com/)

## 为什么做这个

agent 在多步任务中容易出现：

- **幻觉完成**：说"我做完了"，实际没落盘
- **静默失败**：调工具报错但不暴露
- **循环震荡**：永远不停
- **目标漂移**：做着做着忘记初衷

`loops-skill` 通过四角色分离 + 独立验证 + 反射投票，
强制每一步都过一遍 verifier，不靠 agent 自己说 OK。

## 安装

需要 **Python 3.9+**，纯标准库。

### 方式 A：装到 Mavis skill 目录

```bash
git clone https://github.com/lrrrq/loops-skill.git \
  ~/.mavis/skills/loops-skill
```

重启 Mavis daemon 后，`loops-skill` 会被自动发现。

### 方式 B：pip editable install

```bash
git clone https://github.com/lrrrq/loops-skill.git
cd loops-skill
pip install -e .
# 现在有 `loops-runtime` console script
loops-runtime run --goal "..." --max-iter 30
```

### 方式 C：直接 import

```python
from loop_runtime import LoopRunner, JsonFileStorage, MockAgentRuntime
# ...在你的代码里 wiring
```

## 快速上手（CLI）

```bash
python3 __main__.py run \
  --goal "Process all accepted files in /path/to/proposal-skill-builder that have no case yet" \
  --max-iter 30 \
  --max-minutes 60
```

或在 Mavis session 里：

```bash
# 让 agent 用 loops-skill 跑
mavis session new --skill loops-skill --goal "..."
```

## 4-agent 协议

每步循环调 4 个角色：

```
   ┌──────────────────────────────────────────────┐
   │  1. THINKER                                  │
   │     输入：goal + history tail                 │
   │     输出：next_action, expected_artifact,     │
   │           expected_success_criteria,         │
   │           i_am_stuck (bool)                   │
   └──────────────────────────────────────────────┘
                       │
                       ▼
   ┌──────────────────────────────────────────────┐
   │  2. EXECUTOR                                 │
   │     输入：thinker.next_action                │
   │     输出：executed (bool), artifact_path,    │
   │           exit_code, raw_output_tail         │
   └──────────────────────────────────────────────┘
                       │
                       ▼
   ┌──────────────────────────────────────────────┐
   │  3. CHECKER（独立验证）                      │
   │     输入：expected_artifact + executor_report│
   │     输出：verdict (pass/fail), evidence[]    │
   │     **关键**：拿不到 executor 的 self_assessment│
   │             （防"我说我做完了"幻觉）          │
   └──────────────────────────────────────────────┘
                       │
                       ▼
   ┌──────────────────────────────────────────────┐
   │  4. REFLECTOR                                │
   │     输入：thinker + executor + checker       │
   │     输出：step_verdict, macro_status         │
   │           (continue/converged/replan/blocked)│
   │           A/B/C 三视角投票                   │
   └──────────────────────────────────────────────┘
```

详细 schema 见 `references/agent-interface.md` + `references/verdict-schema.md`。

## Runtime 状态

- **`continue`** — 正常，继续下一步
- **`converged`** — 目标已达成，停止循环（status=`done`）
- **`replan`** — 需要重新规划（连续 2 次触发则 blocked）
- **`blocked`** — 不可恢复，停止循环（status=`blocked`）
- **`budget_exhausted`** — 达到 max_iter / max_minutes，停止

## 跨平台支持

- **macOS / Linux / Windows** 全平台（CI matrix 已配）
- 路径用 `pathlib.Path`
- 临时目录用 `tempfile.gettempdir()`
- Mock runtime placeholder 用 `tempfile.gettempdir()` 而非 `/tmp`
- 文件 IO 全部 `encoding="utf-8"`
- CLI 例子用 `sys.executable` 找 Python，Windows 上无 `python3` 也不挂

## 运行测试

```bash
# 跨平台 smoke test
python3 tests/test_cross_platform.py -v

# 或用 pytest
python3 -m pytest tests/ -v
```

8 个测试覆盖：
- 平台假设（tempdir / pathlib / UTF-8 JSON）
- JsonFileStorage 在 sandbox 目录里 init/append/read
- Mock runtime placeholder 路径指向 `tempfile.gettempdir()`
- LoopRunner 收敛路径 → status=done, block_reason=reflector_converged

## 项目结构

```
loops-skill/
├── SKILL.md                       # Mavis skill frontmatter
├── __main__.py                    # CLI 入口（run/resume/status/list）
├── references/                    # 协议文档
│   ├── agent-interface.md
│   ├── storage-interface.md
│   ├── mavis-adapter.md
│   └── verdict-schema.md
├── scripts/
│   ├── loop_runtime.py            # 核心 runtime（JsonFileStorage + LoopRunner）
│   └── example_proposal_skill_builder.py  # 真实 workspace e2e
├── tests/
│   └── test_cross_platform.py     # 跨平台 smoke test
├── .github/workflows/
│   └── test.yml                   # CI matrix
└── LICENSE
```

## e2e 例子

`scripts/example_proposal_skill_builder.py` 是一个真实 workspace 端到端测试：
驱动 `proposal-skill-builder` 跑完 1 个 unprocessed accepted 文件的全套流程
（intake → create-case → compile-case → 验证 fragments.json）。

```bash
LOOPS_E2E_WORKSPACE=/path/to/proposal-skill-builder \
  python3 scripts/example_proposal_skill_builder.py
```

依赖：
- `proposal-skill-builder` 的 SQLite db 已 init
- 至少 1 个 `status='accepted' AND case_id IS NULL` 的 source_file

## 许可

MIT License — 见 [LICENSE](LICENSE)。