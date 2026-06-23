name: Feature request
about: Suggest a new feature for the loop runtime
title: "[feat] "
labels: ["enhancement"]
body:
  - type: textarea
    id: problem
    attributes:
      label: What problem does this solve?
    validations:
      required: true

  - type: textarea
    id: proposed
    attributes:
      label: Proposed API
      description: |
        Show what the new method / event / verdict / CLI flag would look
        like. Reference existing patterns in `references/`.
      placeholder: |
        ```python
        # New storage method:
        storage.set_final_with_diff(loop_id, final, prev_final)
        ```
    validations:
      required: true

  - type: textarea
    id: tradeoffs
    attributes:
      label: Tradeoffs / alternatives