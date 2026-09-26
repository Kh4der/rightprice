"""One-shot worker entrypoint for an isolated Managed Agents sandbox.

Run this module *inside* a fresh Linux sandbox whose only mounted data is the
workspace prepared by :func:`stage_inventory_sandbox_workspace`.  It imports no
Django settings and refuses to start when application or Square credentials are
present in its environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.lib.environments import EnvironmentWorker
from anthropic.lib.tools.agent_toolset import beta_agent_toolset_20260401

from .claude_protocol import (
    INPUT_DIRECTORY,
    MANAGED_AGENTS_BETA,
    MANIFEST_NAME,
    RESULT_JSON_PATH,
    RESULT_XLSX_PATH,
    SCHEMA_PATH,
    WORKFLOW_VERSION,
)

_REQUIRED_ENVIRONMENT = (
    "ANTHROPIC_ENVIRONMENT_ID",
    "ANTHROPIC_ENVIRONMENT_KEY",
    "ANTHROPIC_WORK_ID",
    "ANTHROPIC_SESSION_ID",
)

_OPTIONAL_ENVIRONMENT = (
    "ANTHROPIC_WORK_SECRET",
)

# None of these belongs inside the tool-execution sandbox.  In particular, the
# worker has no Square write tool and cannot reach the Django database or object
# store to turn invoice text into an instruction that mutates production data.
_FORBIDDEN_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_URL",
    "DJANGO_SECRET_KEY",
    "GOOGLE_GENAI_API_KEY",
    "OPENAI_API_KEY",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "SQUARE_ACCESS_TOKEN",
    "SQUARE_WEBHOOK_SIGNATURE_KEY",
)

_SCRUB_BEFORE_TOOLS = (
    "ANTHROPIC_ENVIRONMENT_KEY",
    "ANTHROPIC_WORK_SECRET",
)


class WorkerConfigurationError(RuntimeError):
    """The per-session worker was launched outside its safe boundary."""


def _read_environment(environment: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in _REQUIRED_ENVIRONMENT if not environment.get(name, "").strip()]
    if missing:
        raise WorkerConfigurationError(
            "The worker is missing required environment values: " + ", ".join(missing)
        )
    forbidden = [name for name in _FORBIDDEN_CREDENTIALS if environment.get(name, "").strip()]
    if forbidden:
        raise WorkerConfigurationError(
            "The isolated worker received forbidden application credentials: "
            + ", ".join(forbidden)
        )
    values = {name: environment[name] for name in _REQUIRED_ENVIRONMENT}
    values.update(
        {
            name: environment[name]
            for name in _OPTIONAL_ENVIRONMENT
            if environment.get(name, "").strip()
        }
    )
    return values


def _safe_workspace_file(workspace: Path, relative_name: str) -> Path:
    candidate = workspace / relative_name
    if candidate.is_symlink() or not candidate.is_file():
        raise WorkerConfigurationError(f"Required workspace file {relative_name} is missing.")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(workspace):
        raise WorkerConfigurationError("A staged input escaped the session workspace.")
    return resolved


def validate_staged_workspace(workspace_path: str | Path, expected_job_id: str) -> dict[str, Any]:
    """Verify manifest identity and evidence hashes before any model tool runs."""

    workspace = Path(workspace_path)
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
        raise WorkerConfigurationError("The worker needs an existing absolute workspace path.")
    workspace = workspace.resolve(strict=True)
    manifest_path = _safe_workspace_file(workspace, MANIFEST_NAME)
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise WorkerConfigurationError("The workspace manifest is invalid.") from exc
    if not isinstance(manifest, dict):
        raise WorkerConfigurationError("The workspace manifest must be an object.")
    if manifest.get("job_id") != expected_job_id:
        raise WorkerConfigurationError("The workspace belongs to another inventory job.")
    if manifest.get("workflow_version") != WORKFLOW_VERSION:
        raise WorkerConfigurationError("The workspace workflow version is unsupported.")
    if (
        manifest.get("schema_path") != SCHEMA_PATH
        or manifest.get("result_json_path") != RESULT_JSON_PATH
        or manifest.get("result_xlsx_path") != RESULT_XLSX_PATH
    ):
        raise WorkerConfigurationError("The workspace requests an unsupported output path.")
    _safe_workspace_file(workspace, SCHEMA_PATH)

    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise WorkerConfigurationError("The workspace contains no invoice evidence.")
    for document in documents:
        if not isinstance(document, dict):
            raise WorkerConfigurationError("The workspace document manifest is invalid.")
        relative_path = document.get("path")
        path_parts = Path(relative_path).parts if isinstance(relative_path, str) else ()
        if not path_parts or path_parts[0] != INPUT_DIRECTORY:
            raise WorkerConfigurationError("A workspace document path is invalid.")
        staged_file = _safe_workspace_file(workspace, relative_path)
        content = staged_file.read_bytes()
        if len(content) != document.get("size_bytes"):
            raise WorkerConfigurationError("A staged invoice size does not match its manifest.")
        if hashlib.sha256(content).hexdigest() != document.get("sha256"):
            raise WorkerConfigurationError("A staged invoice hash does not match its manifest.")
    return manifest


async def _run_with_client(
    client: Any,
    values: dict[str, str],
    workspace: Path,
    *,
    worker_factory,
) -> None:
    session = await client.beta.sessions.retrieve(
        values["ANTHROPIC_SESSION_ID"],
        betas=[MANAGED_AGENTS_BETA],
    )
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        raise WorkerConfigurationError("The Managed Agents session has no job metadata.")
    job_id = metadata.get("inventory_job_id", "")
    if metadata.get("workflow_version") != WORKFLOW_VERSION or not job_id:
        raise WorkerConfigurationError("The Managed Agents session is not an inventory job.")
    validate_staged_workspace(workspace, job_id)

    max_file_bytes = int(os.environ.get("CLAUDE_SANDBOX_TOOL_MAX_FILE_BYTES", 50 * 1024 * 1024))
    worker = worker_factory(
        client,
        environment_id=values["ANTHROPIC_ENVIRONMENT_ID"],
        environment_key=values["ANTHROPIC_ENVIRONMENT_KEY"],
        workdir=workspace,
        unrestricted_paths=False,
        max_file_bytes=max_file_bytes,
        memory_sync_interval=None,
        tools=lambda context: beta_agent_toolset_20260401(context),
    )
    await worker.handle_item(
        work_id=values["ANTHROPIC_WORK_ID"],
        environment_id=values["ANTHROPIC_ENVIRONMENT_ID"],
        session_id=values["ANTHROPIC_SESSION_ID"],
        environment_key=values["ANTHROPIC_ENVIRONMENT_KEY"],
        work_secret=values.get("ANTHROPIC_WORK_SECRET"),
    )


async def run_worker_once(
    *,
    environment: MutableMapping[str, str] | None = None,
    client: Any | None = None,
    worker_factory=EnvironmentWorker,
) -> None:
    """Validate and execute exactly one claimed session in the mounted workspace."""

    runtime_environment = environment if environment is not None else os.environ
    values = _read_environment(runtime_environment)
    workspace = Path(runtime_environment.get("CLAUDE_SANDBOX_WORKDIR", "/workspace"))

    # Construct the authenticated client first, then remove credentials from the
    # process environment inherited by bash.  Explicit arguments keep the SDK
    # worker authenticated without making its key visible to model-run commands.
    owned_client = client or AsyncAnthropic(auth_token=values["ANTHROPIC_ENVIRONMENT_KEY"])
    for name in _SCRUB_BEFORE_TOOLS:
        runtime_environment.pop(name, None)

    if client is not None:
        await _run_with_client(owned_client, values, workspace, worker_factory=worker_factory)
        return
    async with owned_client:
        await _run_with_client(owned_client, values, workspace, worker_factory=worker_factory)


async def _run_until_signal() -> None:
    """Cancel gracefully so the SDK can stop its claimed work item."""

    task = asyncio.create_task(run_worker_once())
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, task.cancel)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX fallback
            continue
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def main() -> None:
    asyncio.run(_run_until_signal())


if __name__ == "__main__":  # pragma: no cover - exercised by the container entrypoint
    main()
