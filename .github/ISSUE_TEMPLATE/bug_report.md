name: Bug report
about: Report incorrect behavior in loops-skill
title: "[bug] "
labels: ["bug"]
body:
  - type: markdown
    attributes:
      value: |
        Thanks for filing a bug. Please include the loop history JSONL
        if at all possible — that's the single most useful artifact.

  - type: textarea
    id: what-happened
    attributes:
      label: What happened?
    validations:
      required: true

  - type: textarea
    id: repro
    attributes:
      label: Minimal repro (Python snippet or CLI invocation)
    validations:
      required: true

  - type: textarea
    id: env
    attributes:
      label: Environment
      placeholder: |
        - OS: macOS 14.5 / Ubuntu 24.04 / Windows 11
        - Python: 3.11.7 (run `python --version`)
        - loops-skill version: 1.1.1 (run `loops-skill --version` if installed)
    validations:
      required: true

  - type: textarea
    id: history
    attributes:
      label: Loop history JSONL excerpt
      description: |
        Last 5–10 lines from `state/loops/<loop_id>/history.jsonl` —
        it captures every think/execute/check/reflect decision.
      render: shell

  - type: textarea
    id: runtime
    attributes:
      label: Runtime + storage
      description: |
        Which AgentRuntime are you using? Custom, MockAgentRuntime, or
        the Mavis adapter? Where is loop state stored?