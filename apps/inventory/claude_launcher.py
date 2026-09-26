"""Trusted host launcher for one isolated Claude inventory sandbox per session.

This module runs on a dedicated Linux application host, not in the web
process.  It is the only component that sees both protected invoice storage
and the Anthropic environment queue.  Each claimed work item gets a new Docker
container with a clean environment and a single bind-mounted workspace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections, transaction

from apps.capture.models import SubmissionStatus

from .claude_protocol import MANAGED_AGENTS_BETA, WORKFLOW_VERSION
from .claude_sandbox import (
    InventorySandboxError,
    fail_inventory_sandbox_job,
    ingest_inventory_sandbox_outputs,
    mark_inventory_sandbox_job_running,
    stage_inventory_sandbox_workspace,
)
from .models import (
    DeliveryStatus,
    InventorySandboxJob,
    InventorySandboxJobStatus,
)

logger = logging.getLogger(__name__)

_IMAGE_DIGEST = re.compile(r"^\S+@sha256:[0-9a-fA-F]{64}$")
_IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_NUMERIC_USER = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:[bkmgBKMG])?$")
_CPU_LIMIT = re.compile(r"^(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$")
_UNSAFE_DOCKER_NETWORKS = {"bridge", "default", "host", "none"}
_DOCKER_CONTAINER_WORKSPACE = "/workspace"
_WORKER_MODULE = "apps.inventory.claude_worker"

_CONTAINER_ENVIRONMENT = (
    "ANTHROPIC_ENVIRONMENT_ID",
    "ANTHROPIC_ENVIRONMENT_KEY",
    "ANTHROPIC_WORK_ID",
    "ANTHROPIC_SESSION_ID",
    "ANTHROPIC_WORK_SECRET",
    "CLAUDE_SANDBOX_WORKDIR",
)


class InventoryLauncherError(InventorySandboxError):
    """Base error for the trusted self-hosted launcher."""


class InventoryLauncherNotConfigured(InventoryLauncherError):
    """The launcher is disabled or missing an isolation prerequisite."""


class InventoryLauncherWorkError(InventoryLauncherError):
    """A claimed queue item cannot safely be mapped to a local job."""


class InventorySandboxExecutionError(InventoryLauncherError):
    """The isolated container did not finish successfully."""


@dataclass(frozen=True)
class LauncherConfig:
    """Validated launcher settings.

    ``environment_key`` is excluded from representations so a command error or
    debugger does not accidentally print it.
    """

    environment_id: str
    environment_key: str = field(repr=False)
    workspace_root: Path
    docker_binary: Path
    docker_image: str
    docker_network: str
    docker_user: str
    docker_python: str
    memory_limit: str
    cpu_limit: str
    pids_limit: int
    run_timeout_seconds: int
    stop_timeout_seconds: int
    retain_failed_workspaces: bool

    @classmethod
    def from_settings(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        system_name: str | None = None,
        docker_lookup=shutil.which,
        host_identity: tuple[int, int] | None = None,
    ) -> LauncherConfig:
        runtime_environment = environment if environment is not None else os.environ
        if not settings.CLAUDE_INVENTORY_SANDBOX_ENABLED:
            raise InventoryLauncherNotConfigured(
                "Claude inventory sandboxes are disabled."
            )
        if not settings.CLAUDE_INVENTORY_LAUNCHER_ENABLED:
            raise InventoryLauncherNotConfigured(
                "The trusted Claude inventory launcher is disabled."
            )
        if (system_name or platform.system()) != "Linux":
            raise InventoryLauncherNotConfigured(
                "The trusted Claude inventory launcher requires a Linux host."
            )

        environment_id = settings.CLAUDE_INVENTORY_ENVIRONMENT_ID.strip()
        environment_key = runtime_environment.get("ANTHROPIC_ENVIRONMENT_KEY", "").strip()
        if not environment_id or not environment_key:
            raise InventoryLauncherNotConfigured(
                "CLAUDE_INVENTORY_ENVIRONMENT_ID and the launcher process's "
                "ANTHROPIC_ENVIRONMENT_KEY are required."
            )

        docker_path = docker_lookup("docker")
        if not docker_path:
            raise InventoryLauncherNotConfigured("Docker is not installed on the launcher host.")
        docker_binary = Path(docker_path)
        if not docker_binary.is_absolute():
            docker_binary = docker_binary.resolve()

        image = settings.CLAUDE_INVENTORY_DOCKER_IMAGE.strip()
        if not _IMAGE_REFERENCE.fullmatch(image):
            raise InventoryLauncherNotConfigured(
                "CLAUDE_INVENTORY_DOCKER_IMAGE must name the prebuilt worker image."
            )
        if settings.CLAUDE_INVENTORY_DOCKER_REQUIRE_DIGEST and not _IMAGE_DIGEST.fullmatch(
            image
        ):
            raise InventoryLauncherNotConfigured(
                "The worker image must be pinned as image@sha256:<digest>."
            )
        if image.endswith(":latest"):
            raise InventoryLauncherNotConfigured("The worker image cannot use the latest tag.")

        network = settings.CLAUDE_INVENTORY_DOCKER_NETWORK.strip()
        if (
            not _NETWORK_NAME.fullmatch(network)
            or network.casefold() in _UNSAFE_DOCKER_NETWORKS
        ):
            raise InventoryLauncherNotConfigured(
                "Configure a dedicated, egress-restricted Docker network for the worker."
            )

        if host_identity is None:
            uid = os.getuid()
            gid = os.getgid()
        else:
            uid, gid = host_identity
        if uid <= 0 or gid <= 0:
            raise InventoryLauncherNotConfigured(
                "Run the trusted launcher as a dedicated non-root Linux user."
            )
        host_user = f"{uid}:{gid}"
        configured_user = settings.CLAUDE_INVENTORY_DOCKER_USER.strip()
        docker_user = configured_user or host_user
        if not _NUMERIC_USER.fullmatch(docker_user):
            raise InventoryLauncherNotConfigured(
                "CLAUDE_INVENTORY_DOCKER_USER must be a non-root numeric uid:gid."
            )
        if docker_user != host_user:
            raise InventoryLauncherNotConfigured(
                "The Docker worker uid:gid must match the non-root launcher so it can access "
                "the private session workspace."
            )

        docker_python = settings.CLAUDE_INVENTORY_DOCKER_PYTHON.strip()
        if not docker_python.startswith("/") or any(
            character.isspace() for character in docker_python
        ):
            raise InventoryLauncherNotConfigured(
                "CLAUDE_INVENTORY_DOCKER_PYTHON must be an absolute path in the image."
            )

        workspace_root = Path(settings.CLAUDE_INVENTORY_WORKSPACE_ROOT)
        if not workspace_root.is_absolute() or "," in str(workspace_root):
            raise InventoryLauncherNotConfigured(
                "CLAUDE_INVENTORY_WORKSPACE_ROOT must be an absolute path without commas."
            )
        if workspace_root == Path(workspace_root.anchor) or len(workspace_root.parts) < 3:
            raise InventoryLauncherNotConfigured(
                "CLAUDE_INVENTORY_WORKSPACE_ROOT must be a dedicated nested directory."
            )

        memory_limit = settings.CLAUDE_INVENTORY_DOCKER_MEMORY.strip()
        cpu_limit = settings.CLAUDE_INVENTORY_DOCKER_CPUS.strip()
        if not _MEMORY_LIMIT.fullmatch(memory_limit):
            raise InventoryLauncherNotConfigured("The Docker memory limit is invalid.")
        if not _CPU_LIMIT.fullmatch(cpu_limit):
            raise InventoryLauncherNotConfigured("The Docker CPU limit is invalid.")

        positive_values = {
            "pids limit": settings.CLAUDE_INVENTORY_DOCKER_PIDS_LIMIT,
            "run timeout": settings.CLAUDE_INVENTORY_RUN_TIMEOUT_SECONDS,
            "stop timeout": settings.CLAUDE_INVENTORY_STOP_TIMEOUT_SECONDS,
        }
        if any(value <= 0 for value in positive_values.values()):
            raise InventoryLauncherNotConfigured(
                "Docker limits and launcher timeouts must be positive integers."
            )
        if settings.CLAUDE_INVENTORY_STOP_TIMEOUT_SECONDS < 30:
            raise InventoryLauncherNotConfigured(
                "The Docker stop timeout must be at least 30 seconds."
            )

        return cls(
            environment_id=environment_id,
            environment_key=environment_key,
            workspace_root=workspace_root,
            docker_binary=docker_binary,
            docker_image=image,
            docker_network=network,
            docker_user=docker_user,
            docker_python=docker_python,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            pids_limit=settings.CLAUDE_INVENTORY_DOCKER_PIDS_LIMIT,
            run_timeout_seconds=settings.CLAUDE_INVENTORY_RUN_TIMEOUT_SECONDS,
            stop_timeout_seconds=settings.CLAUDE_INVENTORY_STOP_TIMEOUT_SECONDS,
            retain_failed_workspaces=settings.CLAUDE_INVENTORY_RETAIN_FAILED_WORKSPACES,
        )


@dataclass(frozen=True)
class ClaimedWork:
    work_id: str
    session_id: str
    secret: str = field(default="", repr=False)


@dataclass(frozen=True)
class LauncherResult:
    processed: int = 0
    failed: int = 0


def _claimed_work(work: Any) -> ClaimedWork:
    work_id = str(getattr(work, "id", "")).strip()
    data = getattr(work, "data", None)
    session_id = str(getattr(data, "id", "")).strip()
    secret = str(getattr(work, "secret", "") or "")
    if not work_id or not session_id:
        raise InventoryLauncherWorkError("The claimed work item has no work or session ID.")
    if len(work_id) > 200 or len(session_id) > 200:
        raise InventoryLauncherWorkError("The claimed work item identifiers are invalid.")
    return ClaimedWork(work_id=work_id, session_id=session_id, secret=secret)


def _prepare_workspace_root(config: LauncherConfig) -> Path:
    root = config.workspace_root
    if root.exists() and root.is_symlink():
        raise InventoryLauncherNotConfigured("The workspace root cannot be a symbolic link.")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    resolved = root.resolve(strict=True)
    if resolved != root.absolute():
        raise InventoryLauncherNotConfigured(
            "The workspace root cannot contain symbolic-link components."
        )
    if resolved.is_symlink() or not resolved.is_dir():
        raise InventoryLauncherNotConfigured("The workspace root is not a safe directory.")
    stat = resolved.stat()
    if stat.st_uid != os.getuid():
        raise InventoryLauncherNotConfigured(
            "The workspace root must be owned by the non-root launcher user."
        )
    if stat.st_mode & 0o077:
        raise InventoryLauncherNotConfigured(
            "The workspace root must not be accessible by group or other users."
        )
    return resolved


def _workspace_for(root: Path, job_id: UUID) -> Path:
    return root / f"{job_id}-{secrets.token_hex(12)}"


def _cleanup_workspace(root: Path, workspace: Path, job_id: UUID) -> None:
    """Delete only a direct, launcher-created child of the configured root."""

    if not workspace.exists() and not workspace.is_symlink():
        return
    expected_prefix = f"{job_id}-"
    if workspace.parent != root or not workspace.name.startswith(expected_prefix):
        raise InventoryLauncherWorkError("Refusing to clean an unexpected workspace path.")
    if workspace.is_symlink():
        raise InventoryLauncherWorkError("Refusing to follow a workspace symbolic link.")
    resolved = workspace.resolve(strict=True)
    if resolved.parent != root or resolved == root:
        raise InventoryLauncherWorkError("Refusing to clean outside the workspace root.")
    shutil.rmtree(resolved)


async def _quiet_process(*argv: str, timeout: int, environment: Mapping[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(environment),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise InventoryLauncherNotConfigured("A Docker prerequisite check timed out.") from None
    if process.returncode:
        raise InventoryLauncherNotConfigured("A Docker prerequisite check failed.")
    return stdout.decode("utf-8", errors="replace").strip()


class DockerSandboxRunner:
    """Launch one hardened, attached Docker container for one claimed session."""

    async def prepare(self, config: LauncherConfig) -> Path:
        root = _prepare_workspace_root(config)
        docker = str(config.docker_binary)
        server_os = await _quiet_process(
            docker,
            "version",
            "--format",
            "{{.Server.Os}}",
            timeout=20,
            environment={},
        )
        if server_os.casefold() != "linux":
            raise InventoryLauncherNotConfigured("The Docker daemon must run Linux containers.")
        await _quiet_process(
            docker,
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            config.docker_image,
            timeout=20,
            environment={},
        )
        inspected_network = await _quiet_process(
            docker,
            "network",
            "inspect",
            "--format",
            "{{.Name}}",
            config.docker_network,
            timeout=20,
            environment={},
        )
        if inspected_network != config.docker_network:
            raise InventoryLauncherNotConfigured("The configured Docker network was not found.")
        return root

    def _command(
        self,
        config: LauncherConfig,
        workspace: Path,
        job_id: UUID,
    ) -> tuple[list[str], str]:
        container_name = f"claude-inventory-{job_id.hex}"
        mount = f"type=bind,src={workspace},dst={_DOCKER_CONTAINER_WORKSPACE},rw"
        command = [
            str(config.docker_binary),
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--init",
            "--stop-signal",
            "SIGTERM",
            "--stop-timeout",
            str(config.stop_timeout_seconds),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(config.pids_limit),
            "--memory",
            config.memory_limit,
            "--cpus",
            config.cpu_limit,
            "--network",
            config.docker_network,
            "--user",
            config.docker_user,
            "--workdir",
            _DOCKER_CONTAINER_WORKSPACE,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs",
            "/mnt/memory:rw,noexec,nosuid,nodev,size=16m",
            "--mount",
            mount,
        ]
        command.extend(argument for name in _CONTAINER_ENVIRONMENT for argument in ("--env", name))
        command.extend(
            [
                "--entrypoint",
                config.docker_python,
                config.docker_image,
                "-m",
                _WORKER_MODULE,
            ]
        )
        return command, container_name

    async def _stop(self, config: LauncherConfig, container_name: str) -> None:
        process = await asyncio.create_subprocess_exec(
            str(config.docker_binary),
            "stop",
            "--time",
            str(config.stop_timeout_seconds),
            container_name,
            env={},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(
                process.wait(), timeout=config.stop_timeout_seconds + 10
            )
        except TimeoutError:
            process.kill()
            await process.wait()

    async def run(
        self,
        config: LauncherConfig,
        workspace: Path,
        job_id: UUID,
        work: ClaimedWork,
    ) -> None:
        command, container_name = self._command(config, workspace, job_id)
        child_environment = {
            "ANTHROPIC_ENVIRONMENT_ID": config.environment_id,
            "ANTHROPIC_ENVIRONMENT_KEY": config.environment_key,
            "ANTHROPIC_WORK_ID": work.work_id,
            "ANTHROPIC_SESSION_ID": work.session_id,
            "CLAUDE_SANDBOX_WORKDIR": _DOCKER_CONTAINER_WORKSPACE,
        }
        if work.secret:
            child_environment["ANTHROPIC_WORK_SECRET"] = work.secret

        process = await asyncio.create_subprocess_exec(
            *command,
            env=child_environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return_code = await asyncio.wait_for(
                process.wait(), timeout=config.run_timeout_seconds
            )
        except TimeoutError:
            await self._stop(config, container_name)
            await process.wait()
            raise InventorySandboxExecutionError(
                "The isolated inventory worker exceeded its time limit."
            ) from None
        except asyncio.CancelledError:
            await self._stop(config, container_name)
            await process.wait()
            raise
        if return_code:
            raise InventorySandboxExecutionError(
                "The isolated inventory worker exited unsuccessfully."
            )


def _load_queued_job(job_id: UUID, session_id: str) -> InventorySandboxJob:
    try:
        job = InventorySandboxJob.objects.select_related("delivery__submission").get(pk=job_id)
    except InventorySandboxJob.DoesNotExist as exc:
        raise InventoryLauncherWorkError("The session does not map to an inventory job.") from exc
    if job.session_id != session_id:
        raise InventoryLauncherWorkError("The session ID does not match its inventory job.")
    if job.workflow_version != WORKFLOW_VERSION:
        raise InventoryLauncherWorkError("The inventory job workflow version is unsupported.")
    if job.status != InventorySandboxJobStatus.QUEUED:
        raise InventoryLauncherWorkError("Only a queued inventory job can be launched.")
    return job


def _fail_active_job(job_id: UUID) -> None:
    with transaction.atomic():
        job = (
            InventorySandboxJob.objects.select_for_update()
            .select_related("delivery__submission")
            .filter(pk=job_id)
            .first()
        )
        if not job or job.status not in {
            InventorySandboxJobStatus.PENDING,
            InventorySandboxJobStatus.QUEUED,
            InventorySandboxJobStatus.STAGED,
            InventorySandboxJobStatus.RUNNING,
        }:
            return
        fail_inventory_sandbox_job(
            job,
            "launcher_failed",
            "The isolated inventory worker did not complete. The saved photos are ready for review or retry.",
        )
        delivery = job.delivery
        delivery.status = DeliveryStatus.NEEDS_REVIEW
        delivery.save(update_fields=["status", "updated_at"])
        submission = delivery.submission
        submission.status = SubmissionStatus.NEEDS_REVIEW
        submission.processing_error = (
            "The isolated invoice reader did not complete. The original photos are saved; "
            "an owner can review them or retry processing."
        )
        submission.save(update_fields=["status", "processing_error", "updated_at"])


async def _stop_claimed_work(client: Any, config: LauncherConfig, work: ClaimedWork) -> None:
    try:
        await client.beta.environments.work.stop(
            work.work_id,
            environment_id=config.environment_id,
            force=True,
            betas=[MANAGED_AGENTS_BETA],
        )
    except Exception:
        # The in-container worker may already have stopped the item.  Never log
        # the response body here because it is outside the app's evidence store.
        logger.warning("Could not force-stop a failed Claude work item.")


async def _process_claimed_work(
    client: Any,
    config: LauncherConfig,
    root: Path,
    runner: DockerSandboxRunner,
    raw_work: Any,
) -> None:
    work = _claimed_work(raw_work)
    job: InventorySandboxJob | None = None
    workspace: Path | None = None
    succeeded = False
    try:
        session = await client.beta.sessions.retrieve(
            work.session_id,
            betas=[MANAGED_AGENTS_BETA],
        )
        metadata = getattr(session, "metadata", None)
        if not isinstance(metadata, dict):
            raise InventoryLauncherWorkError("The session has no inventory metadata.")
        if metadata.get("workflow_version") != WORKFLOW_VERSION:
            raise InventoryLauncherWorkError("The session workflow version is unsupported.")
        try:
            job_id = UUID(str(metadata.get("inventory_job_id", "")))
        except (TypeError, ValueError) as exc:
            raise InventoryLauncherWorkError("The session has no valid inventory job ID.") from exc

        job = await sync_to_async(_load_queued_job, thread_sensitive=True)(
            job_id, work.session_id
        )
        workspace = _workspace_for(root, job.id)
        await sync_to_async(stage_inventory_sandbox_workspace, thread_sensitive=True)(
            job, workspace
        )
        await sync_to_async(mark_inventory_sandbox_job_running, thread_sensitive=True)(job)
        await runner.run(config, workspace, job.id, work)
        await sync_to_async(ingest_inventory_sandbox_outputs, thread_sensitive=True)(
            job, workspace
        )
        succeeded = True
    except Exception:
        if job is not None:
            await sync_to_async(_fail_active_job, thread_sensitive=True)(job.id)
        await _stop_claimed_work(client, config, work)
        raise
    finally:
        if job is not None and workspace is not None and (
            succeeded or not config.retain_failed_workspaces
        ):
            await sync_to_async(_cleanup_workspace, thread_sensitive=True)(
                root, workspace, job.id
            )


async def _run_with_client(
    client: Any,
    config: LauncherConfig,
    runner: DockerSandboxRunner,
    *,
    loop: bool,
) -> LauncherResult:
    root = await runner.prepare(config)
    processed = 0
    failed = 0
    poller = client.beta.environments.work.poller(
        environment_id=config.environment_id,
        environment_key=config.environment_key,
        drain=not loop,
        auto_stop=False,
    )
    async for work in poller:
        try:
            await _process_claimed_work(client, config, root, runner, work)
            processed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            logger.error(
                "Claude inventory work item failed (%s).",
                type(exc).__name__,
            )
            if not loop:
                raise
        if not loop:
            break
    return LauncherResult(processed=processed, failed=failed)


async def run_inventory_launcher(
    *,
    config: LauncherConfig | None = None,
    client: Any | None = None,
    runner: DockerSandboxRunner | None = None,
    loop: bool = False,
) -> LauncherResult:
    """Claim and process one job, or continuously poll when ``loop`` is true."""

    launcher_config = config or LauncherConfig.from_settings()
    sandbox_runner = runner or DockerSandboxRunner()
    try:
        if client is not None:
            return await _run_with_client(
                client,
                launcher_config,
                sandbox_runner,
                loop=loop,
            )
        async with AsyncAnthropic(auth_token=launcher_config.environment_key) as owned_client:
            return await _run_with_client(
                owned_client,
                launcher_config,
                sandbox_runner,
                loop=loop,
            )
    finally:
        # ORM work runs in asgiref's thread-sensitive executor. Close that
        # thread's connections when a one-shot exits or a long-running poller
        # shuts down, rather than leaving an idle database session behind.
        await sync_to_async(connections.close_all, thread_sensitive=True)()
