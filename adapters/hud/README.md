# HUD adapter — law_firm_software

Wraps the reference `../../env.py` (Gymnasium harness) as a HUD `Environment`.

- `env.py` — the adapter (`StackhouseEnv` -> HUD `Environment`)
- `tasks.py` — task templates wired from each task's `task.json`
- `Dockerfile.hud` — thin HUD layer over the base image
- `pyproject.toml` — package metadata

`hud deploy` requires the platform owner's HUD credentials.
