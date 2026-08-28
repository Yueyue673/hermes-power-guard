# Contributing

Contributions are welcome, especially reproducible safety fixes, lifecycle compatibility updates, Windows power-state diagnostics, and native Hermes Desktop UX improvements.

## Safety rules

1. Tests must never execute a real sleep, shutdown, hibernate, lock, or display-off action.
2. Unknown or unreadable state must fail awake.
3. A model's final prose is not completion evidence.
4. Real power actions must remain restricted to a local Desktop-managed backend.
5. Restart and crash recovery must never replay a claimed action.
6. Any new bypass or relaxed gate requires a specific threat analysis and regression test.

## Setup

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m py_compile power_guard_core.py __init__.py dashboard/plugin_api.py
node --check desktop/plugin.js
```

When developing inside Hermes, also run:

```bash
hermes plugins doctor . --ci
```

## Pull requests

- Keep changes focused.
- Add a regression test for behavior changes.
- Describe safety impact and rollback behavior.
- State exactly which commands were executed.
- Do not include credentials, state databases, logs, or machine-specific paths.
