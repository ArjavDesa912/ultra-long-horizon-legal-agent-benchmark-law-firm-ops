"""Prime Intellect native environment definition.

Wraps the already-registered law_firm_software OpenEnv image
(prime/praesidiumsystems/law-firm-software:latest, see proj/.build.json)
with verifiers.OpenEnvEnv. Prime's own Sandbox infra creates the container at
rollout time from that manifest -- this package ships no Docker image of its
own and needs none locally to install or run. (For a genuinely local eval
against your own machine with no Prime Sandbox involved, see the sibling
`../../eval_local.py` module instead.)
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import verifiers as vf

HERE = Path(__file__).resolve().parent


def _render_observation(
    obs: Any,
    *,
    context: str,
    action_schema: dict[str, Any] | None = None,
    **_: Any,
) -> list[dict[str, str]]:
    """Turn a StackhouseObservation dict (see server/models.py) into a chat message."""
    if not isinstance(obs, dict):
        return [{"role": "user", "content": str(obs)}]

    parts: list[str] = []
    if context == "reset" and action_schema:
        parts.append(
            "Every turn, respond with exactly one JSON object (no markdown "
            "fences, no other text) matching this action schema:\n"
            + json.dumps(action_schema)
        )
        parts.append(
            "The Stackhouse REST API (endpoint values for the schema above):\n"
            "  GET  /v1/query/<collection>?<field>=<value>&limit=&offset=  "
            "-- filtered/paginated read\n"
            "  GET  /v1/query/<collection>/<id>                            "
            "-- read one row\n"
            "  POST /v1/push/<collection>          payload=<new row JSON>  "
            "-- create\n"
            "  POST /v1/update/<collection>/<id>   payload=<changed fields JSON> "
            "-- partial update\n"
            "  POST /v1/delete/<collection>/<id>                           "
            "-- delete\n"
            "  POST /v1/sql/query                  payload={\"query\": \"SELECT ...\"} "
            "-- read-only SQL\n"
            "<collection> is one of the table names listed below, e.g. "
            "/v1/query/matters -- there is NO app-slug prefix on this app's "
            "table names. There is no PUT/PATCH and no /api/rest or /tables "
            "prefix -- use these paths exactly."
        )
    instruction = obs.get("instruction")
    if instruction:
        parts.append(instruction)
    nonce = obs.get("nonce")
    if nonce:
        parts.append(f"Episode nonce (use it, don't hardcode): {nonce}")
    if context == "step":
        reason = obs.get("reward_reason")
        if reason:
            parts.append(f"Last step result: {reason}")
        status_code = obs.get("status_code")
        response = obs.get("response")
        if status_code or response:
            parts.append(
                f"Last response: HTTP {status_code or '?'} {response or '(empty body)'}"
            )
    tables = obs.get("tables")
    if tables:
        parts.append(f"Available Stackhouse tables: {', '.join(tables)}")
    if not parts:
        parts.append("Continue working the task, then call the verifying step.")
    return [{"role": "user", "content": "\n\n".join(parts)}]


def _restore_build_manifest() -> None:
    """`prime env push` strips dotfiles from the uploaded source archive, so
    `proj/.build.json` never survives the round trip to a hosted eval sandbox.
    `proj/build_manifest.json` is a non-dotfile copy that does survive.
    Verbatim fix from the sibling veterinary_clinic_system module."""
    backup = HERE / "proj" / "build_manifest.json"
    if backup.exists():
        (HERE / "proj" / ".build.json").write_text(backup.read_text())


def _patch_container_sandbox_default() -> None:
    """prime-sandboxes now defaults CreateSandboxRequest to vm=True and
    requires an explicit vm=False for a string start_command (container
    sandbox); bump cpu/memory too -- Postgres + Rust BaaS + Vite frontend
    booting concurrently needs real headroom. Verbatim fix from the sibling
    veterinary_clinic_system module (same underlying platform)."""
    from verifiers.legacy.envs.integrations import openenv_env as _oe

    if getattr(_oe.OpenEnvEnv, "_lfs_vm_patch_applied", False):
        return
    _oe.OpenEnvEnv._lfs_vm_patch_applied = True

    original = _oe.OpenEnvEnv._build_sandbox_request
    real_request_cls = _oe.CreateSandboxRequest

    class _ContainerDefaultRequest(real_request_cls):  # type: ignore[misc,valid-type]
        def __init__(self, **kwargs: Any) -> None:
            kwargs.setdefault("vm", False)
            kwargs["cpu_cores"] = 4
            kwargs["memory_gb"] = 8
            kwargs["disk_size_gb"] = 15
            super().__init__(**kwargs)

    def _patched(self: Any, image: str, start_command: str) -> Any:
        _oe.CreateSandboxRequest = _ContainerDefaultRequest
        try:
            return original(self, image, start_command=start_command)
        finally:
            _oe.CreateSandboxRequest = real_request_cls

    _oe.OpenEnvEnv._build_sandbox_request = _patched


_T = TypeVar("_T")


async def _retry_transient_server_error(
    call: Callable[[], Awaitable[_T]], attempts: int = 3, delay_s: float = 3.0
) -> _T:
    """Retry on the WS-protocol-level "Server error: ..." RuntimeErrors seen
    exactly once, non-deterministically, on the first real request after a
    freshly cold-started sandbox. Verbatim fix from the sibling
    veterinary_clinic_system module."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except RuntimeError as e:
            if "Server error:" not in str(e) or attempt == attempts - 1:
                raise
            last_error = e
            await asyncio.sleep(delay_s)
    raise last_error  # pragma: no cover


def _patch_env_client_timeouts() -> None:
    """openenv's GenericEnvClient hardcodes message_timeout_s=60.0 -- how long
    it waits for the server's first WS message after connecting. Confirmed
    directly against this image (local docker run): /health and /schema
    respond in ~10s but ensure_stack() (Postgres + Rust BaaS + Vite frontend)
    isn't fully done for longer than that, so the WS reset() call needs more
    headroom -- worse under Prime Sandbox contention. Verbatim fix from the
    sibling veterinary_clinic_system module."""
    from verifiers.legacy.envs.integrations import openenv_env as _oe

    if getattr(_oe, "_lfs_client_timeout_patch_applied", False):
        return
    _oe._lfs_client_timeout_patch_applied = True

    real_cls = _oe.GenericEnvClient
    if real_cls is None:
        return

    class _PatientGenericEnvClient(real_cls):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("connect_timeout_s", 30.0)
            kwargs.setdefault("message_timeout_s", 600.0)
            super().__init__(*args, **kwargs)

        async def _reset_async(self, **kwargs: Any) -> Any:
            return await _retry_transient_server_error(
                lambda: real_cls._reset_async(self, **kwargs)
            )

        async def _step_async(self, action: Any, **kwargs: Any) -> Any:
            try:
                return await _retry_transient_server_error(
                    lambda: real_cls._step_async(self, action, **kwargs)
                )
            except RuntimeError:
                logging.getLogger(__name__).error(
                    "step failed after retries; action=%r kwargs=%r", action, kwargs
                )
                raise

    _oe.GenericEnvClient = _PatientGenericEnvClient


def _patch_graceful_invalid_json_action() -> None:
    """OpenEnvEnv._parse_action raises an uncaught ValueError/RuntimeError on
    a malformed model response, ending the whole rollout over a formatting
    mistake. Turn it into corrective feedback for the next turn instead.
    Verbatim fix from the sibling veterinary_clinic_system module."""
    from verifiers.legacy.envs.integrations import openenv_env as _oe

    if getattr(_oe.OpenEnvEnv, "_lfs_json_patch_applied", False):
        return
    _oe.OpenEnvEnv._lfs_json_patch_applied = True

    original = _oe.OpenEnvEnv._gym_env_response

    async def _patched(self: Any, messages: Any, state: Any) -> Any:
        try:
            return await original(self, messages, state)
        except ValueError as e:
            return [
                {
                    "role": "user",
                    "content": (
                        f"Failed to parse your last response as valid JSON "
                        f"matching the action schema: {e}. Respond with "
                        "exactly one JSON object, no markdown fences, no "
                        "other text."
                    ),
                }
            ]
        except RuntimeError as e:
            if "VALIDATION_ERROR" not in str(e):
                raise
            return [
                {
                    "role": "user",
                    "content": (
                        f"Your last action didn't match the action schema: {e}. "
                        "It must be a JSON object with `method` and `endpoint` "
                        "fields exactly (not e.g. `path`, `url`, `route`). "
                        "Respond with exactly one JSON object, no markdown "
                        "fences, no other text."
                    ),
                }
            ]

    _oe.OpenEnvEnv._gym_env_response = _patched


def load_environment(**kwargs: Any) -> vf.Environment:
    _restore_build_manifest()
    _patch_container_sandbox_default()
    _patch_env_client_timeouts()
    _patch_graceful_invalid_json_action()
    kwargs.setdefault("num_train_examples", 26)
    kwargs.setdefault("num_eval_examples", 0)
    kwargs.setdefault("max_turns", 40)
    kwargs.setdefault("wait_for_creation_max_attempts", 120)
    kwargs.setdefault("startup_timeout_seconds", 420)
    kwargs.setdefault("timeout_seconds", 600)
    return vf.OpenEnvEnv(
        openenv_project=HERE / "proj",
        prompt_renderer=_render_observation,
        **kwargs,
    )
