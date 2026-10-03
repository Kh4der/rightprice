from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.accounts.models import Role, User
from apps.capture.models import (
    Document,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from apps.inventory.claude_launcher import (
    ClaimedWork,
    DockerSandboxRunner,
    InventoryLauncherNotConfigured,
    InventorySandboxExecutionError,
    LauncherConfig,
    run_inventory_launcher,
)
from apps.inventory.claude_protocol import (
    MANIFEST_NAME,
    RESULT_JSON_PATH,
    RESULT_XLSX_PATH,
)
from apps.inventory.claude_sandbox import dispatch_inventory_sandbox_job
from apps.inventory.models import (
    Delivery,
    DeliveryStatus,
    InventorySandboxJobStatus,
)
from apps.inventory.tests.test_claude_sandbox import (
    FakeCreateClient,
    _invoice_image_bytes,
    _valid_invoice_result,
    _values_only_workbook,
)


@pytest.fixture
def launcher_owner(db):
    return User.objects.create_user(
        "LAUNCH01",
        "owner-password",
        display_name="Launcher Owner",
        role=Role.OWNER,
    )


@pytest.fixture
def queued_launcher_job(launcher_owner):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=launcher_owner,
        status=SubmissionStatus.PROCESSING,
    )
    evidence = _invoice_image_bytes()
    Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile("invoice.jpg", evidence, content_type="image/jpeg"),
        original_name="invoice.jpg",
        media_type="image/jpeg",
        size_bytes=len(evidence),
        sha256=hashlib.sha256(evidence).hexdigest(),
        requested_type=DocumentType.DELIVERY_INVOICE,
    )
    delivery = Delivery.objects.create(
        submission=submission,
        invoice_number="INV-LAUNCHER",
        status=DeliveryStatus.EXTRACTING,
    )
    with override_settings(
        CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
        CLAUDE_INVENTORY_AGENT_ID="agent_test",
        CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
    ):
        return dispatch_inventory_sandbox_job(
            delivery,
            requested_by=launcher_owner,
            client=FakeCreateClient(),
        )


def _config(tmp_path: Path, *, retain_failed=False) -> LauncherConfig:
    return LauncherConfig(
        environment_id="env_test",
        environment_key="environment-secret",
        workspace_root=tmp_path,
        docker_binary=Path("/usr/bin/docker"),
        docker_image="inventory-worker@sha256:" + "a" * 64,
        docker_network="claude-anthropic-egress",
        docker_user="1000:1000",
        docker_python="/app/.venv/bin/python",
        memory_limit="1g",
        cpu_limit="1.0",
        pids_limit=128,
        run_timeout_seconds=900,
        stop_timeout_seconds=45,
        retain_failed_workspaces=retain_failed,
    )


class FakeQueueClient:
    def __init__(self, job):
        self.job = job
        self.poll_calls = []
        self.stop_calls = []

        class Sessions:
            async def retrieve(inner_self, session_id, **kwargs):
                assert session_id == job.session_id
                return SimpleNamespace(
                    metadata={
                        "inventory_job_id": str(job.id),
                        "workflow_version": "inventory_invoice_v3",
                    }
                )

        outer = self

        class Work:
            def poller(inner_self, **kwargs):
                outer.poll_calls.append(kwargs)

                async def generate():
                    yield SimpleNamespace(
                        id="work_test",
                        data=SimpleNamespace(id=job.session_id),
                        secret="per-session-secret",
                    )

                return generate()

            async def stop(inner_self, work_id, **kwargs):
                outer.stop_calls.append((work_id, kwargs))

        self.beta = SimpleNamespace(
            sessions=Sessions(),
            environments=SimpleNamespace(work=Work()),
        )


class OutputWritingRunner:
    def __init__(self, root):
        self.root = root
        self.calls = []

    async def prepare(self, config):
        return self.root

    async def run(self, config, workspace, job_id, work):
        self.calls.append((config, workspace, job_id, work))
        manifest = json.loads((workspace / MANIFEST_NAME).read_text(encoding="utf-8"))
        (workspace / RESULT_JSON_PATH).write_text(
            json.dumps(
                _valid_invoice_result([source["document_id"] for source in manifest["documents"]])
            ),
            encoding="utf-8",
        )
        (workspace / RESULT_XLSX_PATH).write_bytes(_values_only_workbook())


@pytest.mark.django_db(transaction=True)
def test_launcher_claims_stages_runs_ingests_and_cleans(queued_launcher_job, tmp_path):
    client = FakeQueueClient(queued_launcher_job)
    runner = OutputWritingRunner(tmp_path)

    result = asyncio.run(
        run_inventory_launcher(
            config=_config(tmp_path),
            client=client,
            runner=runner,
        )
    )

    assert result.processed == 1
    assert result.failed == 0
    assert client.poll_calls == [
        {
            "environment_id": "env_test",
            "environment_key": "environment-secret",
            "drain": True,
            "auto_stop": False,
        }
    ]
    assert runner.calls[0][3].secret == "per-session-secret"
    queued_launcher_job.refresh_from_db()
    assert queued_launcher_job.status == InventorySandboxJobStatus.SUCCEEDED
    assert not list(tmp_path.iterdir())


class FailingRunner(OutputWritingRunner):
    async def run(self, config, workspace, job_id, work):
        raise InventorySandboxExecutionError("simulated container failure")


@pytest.mark.django_db(transaction=True)
def test_launcher_failure_releases_job_to_owner_review_and_cleans(
    queued_launcher_job,
    tmp_path,
):
    client = FakeQueueClient(queued_launcher_job)

    with pytest.raises(InventorySandboxExecutionError):
        asyncio.run(
            run_inventory_launcher(
                config=_config(tmp_path),
                client=client,
                runner=FailingRunner(tmp_path),
            )
        )

    queued_launcher_job.refresh_from_db()
    queued_launcher_job.delivery.refresh_from_db()
    queued_launcher_job.delivery.submission.refresh_from_db()
    assert queued_launcher_job.status == InventorySandboxJobStatus.FAILED
    assert queued_launcher_job.delivery.status == DeliveryStatus.NEEDS_REVIEW
    assert queued_launcher_job.delivery.submission.status == SubmissionStatus.NEEDS_REVIEW
    assert "original photos are saved" in (queued_launcher_job.delivery.submission.processing_error)
    assert client.stop_calls[0][0] == "work_test"
    assert client.stop_calls[0][1]["force"] is True
    assert not list(tmp_path.iterdir())


def test_docker_runner_passes_only_allowlisted_environment(monkeypatch, tmp_path):
    captured = {}

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_subprocess(*argv, **kwargs):
        captured["argv"] = argv
        captured["environment"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    config = _config(tmp_path)
    work = ClaimedWork(
        work_id="work_test",
        session_id="session_test",
        secret="work-secret",
    )
    queued_id = uuid4()

    asyncio.run(
        DockerSandboxRunner().run(
            config,
            tmp_path,
            queued_id,
            work,
        )
    )

    assert captured["environment"] == {
        "ANTHROPIC_ENVIRONMENT_ID": "env_test",
        "ANTHROPIC_ENVIRONMENT_KEY": "environment-secret",
        "ANTHROPIC_WORK_ID": "work_test",
        "ANTHROPIC_SESSION_ID": "session_test",
        "ANTHROPIC_WORK_SECRET": "work-secret",
        "CLAUDE_SANDBOX_WORKDIR": "/workspace",
    }
    serialized_argv = " ".join(captured["argv"])
    assert "environment-secret" not in serialized_argv
    assert "work-secret" not in serialized_argv
    assert "--read-only" in captured["argv"]
    assert (
        captured["argv"][captured["argv"].index("--cap-drop")],
        captured["argv"][captured["argv"].index("--cap-drop") + 1],
    ) == ("--cap-drop", "ALL")
    assert f"claude-inventory-{queued_id.hex}" in captured["argv"]


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_LAUNCHER_ENABLED=True,
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
    CLAUDE_INVENTORY_DOCKER_IMAGE="inventory-worker:demo",
    CLAUDE_INVENTORY_DOCKER_NETWORK="claude-anthropic-egress",
    CLAUDE_INVENTORY_DOCKER_USER="1000:1000",
    CLAUDE_INVENTORY_DOCKER_REQUIRE_DIGEST=True,
)
def test_launcher_requires_digest_pinned_image(tmp_path):
    with pytest.raises(InventoryLauncherNotConfigured, match="pinned"):
        LauncherConfig.from_settings(
            environment={"ANTHROPIC_ENVIRONMENT_KEY": "environment-secret"},
            system_name="Linux",
            docker_lookup=lambda _name: str(tmp_path / "docker"),
            host_identity=(1000, 1000),
        )


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_LAUNCHER_ENABLED=True,
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
)
def test_launcher_rejects_non_linux_host():
    with pytest.raises(InventoryLauncherNotConfigured, match="Linux"):
        LauncherConfig.from_settings(
            environment={"ANTHROPIC_ENVIRONMENT_KEY": "environment-secret"},
            system_name="Windows",
            docker_lookup=lambda _name: "C:/docker.exe",
        )


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_LAUNCHER_ENABLED=True,
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
    CLAUDE_INVENTORY_DOCKER_IMAGE="inventory-worker@sha256:" + "a" * 64,
    CLAUDE_INVENTORY_DOCKER_NETWORK="claude-anthropic-egress",
    CLAUDE_INVENTORY_DOCKER_USER="1000:1000",
)
def test_launcher_rejects_root_host_user(tmp_path):
    with pytest.raises(InventoryLauncherNotConfigured, match="non-root"):
        LauncherConfig.from_settings(
            environment={"ANTHROPIC_ENVIRONMENT_KEY": "environment-secret"},
            system_name="Linux",
            docker_lookup=lambda _name: str(tmp_path / "docker"),
            host_identity=(0, 0),
        )
