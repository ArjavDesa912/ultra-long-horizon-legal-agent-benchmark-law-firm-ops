"""HUD environment definition for law_firm_software.

Deployed as `env.py` inside the HUD image (a thin layer over the unified
rl-env container). The agent gets a sandboxed shell in the container via the
`workspace` capability and works the mission against the live Stackhouse BaaS
(curl http://localhost:9090, frontend on http://localhost:3005). Grading runs
the task's standalone fail-closed verifier, exactly like the OpenEnv path.

One template (`vet_task`) covers all 100 tasks; `tasks.py` mints the concrete
rows from the baked task suite.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from hud.environment import Environment

GRADING = Path("/app/env/grading")
RESET_EPISODE = "/app/env/server/reset_episode.py"
STACKHOUSE_API_URL = "http://127.0.0.1:9090"
VERIFIER_TIMEOUT_S = 120
RESET_TIMEOUT_S = 240

ROOT = Path("/hud/workspace")
env = Environment(name="law-firm-software")
# require_isolation=True: without bubblewrap, hud's workspace falls back to
# giving the agent's shell the real container filesystem (including
# /app/env/grading — every task's verifier.py and gold.py). Fail loud instead
# of silently serving an unsandboxed, answer-key-readable shell.
#
# shell_uid/shell_gid: belt-and-suspenders on top of that. The env server
# runs as root, and root bypasses the grading dir's chmod 400/700 (see
# Dockerfile) regardless of the bwrap mount namespace, so the agent's shell
# is dropped to `nobody` — an identity /app/env/grading isn't readable by
# even if it somehow ended up in the same filesystem view.
#
# network=True: hud's Workspace.network flag is inverted from what it reads
# like -- False (the default) gives the sandbox its OWN network namespace,
# severed from the container's. The BaaS/frontend/env-server already listen
# on the container's real loopback (127.0.0.1:9090/3005/8000), so the agent's
# shell needs to share that network to reach them at all.
env.workspace(
    ROOT, require_isolation=True, network=True, shell_uid=65534, shell_gid=65534
)

def _usage_hints(spec: dict) -> str:
    # The old hint told the agent to log in "with the verifier or app_admin
    # credentials from the task" without ever putting those credentials
    # anywhere the agent could see them -- the yielded prompt was just
    # spec["instruction"], which is pure business text. Every task.json bakes
    # the same seed-time verifier account, so interpolate it directly.
    email = spec.get("verifier_email")
    password = spec.get("verifier_password")
    return (
        "\n\n---\n"
        "You are inside the clinic's container. The Stackhouse BaaS REST API is at "
        f"{STACKHOUSE_API_URL}. Log in first: POST /v1/auth/login with JSON body "
        f'{{"email": "{email}", "password": "{password}"}}, then send the returned '
        "access_token as `Authorization: Bearer <token>` on subsequent requests. "
        "The frontend is at http://localhost:3005, and the app's collections are "
        "prefixed `law_firm_software_`. Work the mission via the API (curl "
        "is available). Say `answer: done` when finished."
    )


@env.template(id="vet_task")
async def vet_task(task_id: str):
    spec_path = GRADING / "tasks" / task_id / "task.json"
    spec = __import__("json").loads(spec_path.read_text())

    # Fresh episode: restore the pristine template DB + inject the nonce
    # (runs in a thread so the control channel stays responsive).
    await asyncio.to_thread(_reset_episode, task_id)

    answer = yield spec["instruction"] + _usage_hints(spec)

    reward = await asyncio.to_thread(_grade, task_id)
    yield reward


def _reset_episode(task_id: str) -> None:
    result = subprocess.run(
        [sys.executable, RESET_EPISODE, task_id],
        capture_output=True,
        text=True,
        timeout=RESET_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"episode reset failed: {(result.stderr or '')[-500:]}")


def _grade(task_id: str) -> float:
    verifier = GRADING / "tasks" / task_id / "verifier.py"
    try:
        result = subprocess.run(
            [sys.executable, str(verifier)],
            env=dict(os.environ, STACKHOUSE_API_URL=STACKHOUSE_API_URL),
            capture_output=True,
            text=True,
            timeout=VERIFIER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return 0.0  # fail closed
    return 1.0 if result.returncode == 0 else 0.0
