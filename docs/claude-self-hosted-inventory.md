# Claude self-hosted inventory sandbox

This integration is an opt-in processing path for delivery-invoice photos. It
does not replace the owner review or the application's guarded Square update.
Claude extracts invoice rows and can create a values-only processing artifact;
the Django application validates and stores those results. Owners correct any
unread fields inline in the web app, where the rows are compared with live
Square catalog, stock, and cost data. The owner-facing Excel file is generated
only after that review as a download-only final snapshot. Square is read and
updated only by the existing server-side Square integration after an owner
confirms the corrected lines.

The feature is disabled by default. It has mock coverage, but it has not been
run against this store's live Anthropic account, agent, or environment.

## Trust boundary

The design follows Anthropic's self-hosted Managed Agents model:

- Django creates a session with an opaque inventory-job UUID in `metadata`.
  It does **not** attach a file or GitHub resource; those are unsupported for a
  self-hosted environment.
- A trusted launcher outside the sandbox resolves that UUID, reads the
  protected Django storage, and copies immutable invoice files to a new
  per-session workspace.
- The isolated worker receives only the workspace, environment/work/session
  identifiers, and the Anthropic environment key. It refuses to run when it
  sees a database, object-storage, OpenAI, Django, or Square credential.
- Before the first tool runs, the worker removes the environment key from the
  process environment inherited by `bash`. The SDK remains authenticated with
  the explicit in-memory key.
- The worker registers Anthropic's built-in agent toolset only. There is no
  Square custom tool, MCP server, vault, or application API in this sandbox.
- The launcher must enforce one fresh Linux container per session, resource
  limits, and an egress policy that permits the Anthropic control plane only.
  A worker work directory is a file-tool guardrail; it is not a `bash`
  sandbox by itself.
- Tool inputs and results still pass through Anthropic's control plane so the
  model can reason over them. Self-hosting controls tool execution and file
  storage; it does not mean the model never sees invoice content.

Official references:

- <https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes>
- <https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-security>
- <https://platform.claude.com/docs/en/managed-agents/files>
- <https://platform.claude.com/docs/en/managed-agents/sessions>

## Anthropic setup

1. Create a Managed Agents agent for invoice extraction. Give it the built-in
   `agent_toolset_20260401` tools and Anthropic's `xlsx` skill. Pin the exact
   skill version tested in production rather than following `latest` forever.
2. Do not attach a vault, Square MCP server, Square token, or any tool capable
   of changing inventory.
3. Create a `self_hosted` environment and generate its environment key in the
   Anthropic Console. The web application uses the normal organization API
   key; only the isolated worker receives the environment key.
4. Build the worker image on Linux with `/bin/bash` at that exact path and the
   Python dependencies from this project. The normal project image contains
   the worker module; the launcher overrides its entrypoint with:

   ```text
   python -m apps.inventory.claude_worker
   ```

   The implementation is pinned to the `managed-agents-2026-04-01` beta API.
   Upgrade it deliberately with SDK and integration tests when Anthropic
   publishes a later contract.

## Web application settings

Configure these values on the Django/Celery hosts:

```text
ANTHROPIC_API_KEY=<organization API key>
CLAUDE_INVENTORY_SANDBOX_ENABLED=false
CLAUDE_INVENTORY_SANDBOX_PRIMARY=false
CLAUDE_INVENTORY_AGENT_ID=agent_...
CLAUDE_INVENTORY_ENVIRONMENT_ID=env_...
CLAUDE_INVENTORY_MAX_COST_CENTS=250
CLAUDE_INVENTORY_MAX_RESULT_JSON_BYTES=2097152
CLAUDE_INVENTORY_MAX_WORKBOOK_BYTES=26214400
CLAUDE_INVENTORY_LAUNCHER_ENABLED=false
CLAUDE_INVENTORY_WORKSPACE_ROOT=/var/lib/store-ops/claude-inventory
CLAUDE_INVENTORY_DOCKER_IMAGE=<registry>/store-ops@sha256:<digest>
CLAUDE_INVENTORY_DOCKER_NETWORK=claude-anthropic-egress
CLAUDE_INVENTORY_DOCKER_USER=
CLAUDE_INVENTORY_DOCKER_PYTHON=/app/.venv/bin/python
CLAUDE_INVENTORY_DOCKER_REQUIRE_DIGEST=true
CLAUDE_INVENTORY_DOCKER_MEMORY=1g
CLAUDE_INVENTORY_DOCKER_CPUS=1.0
CLAUDE_INVENTORY_DOCKER_PIDS_LIMIT=128
CLAUDE_INVENTORY_RUN_TIMEOUT_SECONDS=900
CLAUDE_INVENTORY_STOP_TIMEOUT_SECONDS=45
CLAUDE_INVENTORY_RETAIN_FAILED_WORKSPACES=false
```

Leave the feature false until the isolated worker and launcher have passed a
complete sandbox test. Never place `ANTHROPIC_ENVIRONMENT_KEY` in Django's
`.env`.

The launcher must run directly on a Linux Docker host as a dedicated non-root
user. Its container uid:gid must match that host user so the mode-`0700`
workspace remains private and usable. Leave `CLAUDE_INVENTORY_DOCKER_USER`
blank to select the launcher's uid:gid automatically. A root launcher is
rejected.

Build and push the image through the normal trusted release process, then use
its immutable registry digest. For a local-only demo, a locally built tag can
be used by explicitly setting `CLAUDE_INVENTORY_DOCKER_REQUIRE_DIGEST=false`;
do not carry that exception into production. The configured Docker network
must be a dedicated network whose host firewall or proxy permits the Anthropic
control plane and blocks arbitrary egress. The launcher refuses Docker's
standard broad networks, but it cannot inspect host firewall rules.

## Session lifecycle

Set both flags to `true` only for the inventory demo after the worker is ready.
The employee's original invoice upload is then dispatched to this path in the
background instead of also running the ordinary extraction provider. The owner
never uploads a replacement workbook or image.

`apps.inventory.claude_sandbox.dispatch_inventory_sandbox_job` creates the
database job and Anthropic session. It applies a hard per-session cost ceiling,
passes only the job UUID and workflow version as metadata, and leaves the job
in `QUEUED`.

The trusted launcher performs this sequence automatically:

1. Claim a work item from the self-hosted environment queue. Retrieve the
   session and read its `inventory_job_id` metadata. Do not log the work-item
   secret or environment key.
2. Allocate a brand-new mode-`0700` host directory, re-read the protected
   evidence, and verify every immutable size and SHA-256 before launch. Image
   evidence—including iPhone HEIC/HEIF—is EXIF-oriented, resized, and written
   as a tool-readable JPEG derivative in that directory; the original upload
   remains unchanged.
3. Mark the job running, then launch a fresh container with only that directory
   bind-mounted as `/workspace`. The child receives only
   `ANTHROPIC_ENVIRONMENT_ID`, `ANTHROPIC_ENVIRONMENT_KEY`,
   `ANTHROPIC_WORK_ID`, `ANTHROPIC_SESSION_ID`, the claimed item's optional
   `ANTHROPIC_WORK_SECRET`, and `CLAUDE_SANDBOX_WORKDIR`. It never inherits the
   launcher's Django, database, storage, OpenAI, or Square environment. The
   environment key and per-session secret are removed before model-run shell
   commands begin.
4. Let the one-shot worker exit. The trusted launcher then validates and
   ingests the fixed-path results automatically. Numeric values and line counts
   are bounded by the delivery database schema before any draft rows are saved.

5. Remove only that exact per-session workspace after successful ingestion or
   after retaining it according to the store's evidence/error policy.

The container uses a read-only root filesystem, a non-root uid, no Linux
capabilities, `no-new-privileges`, bounded CPU/memory/process counts, a fixed
runtime timeout, and at least 30 seconds for graceful SDK shutdown. The Docker
socket, host source checkout, `.env`, and cloud credential directories are
never mounted.

Run one queued job for a controlled demo:

```text
python manage.py run_claude_inventory_launcher
```

Run the always-on poller under the host service manager:

```text
python manage.py run_claude_inventory_launcher --loop
```

Inject `ANTHROPIC_ENVIRONMENT_KEY` into only that command from the host secrets
manager. The one-shot command cleanly reports when no job is waiting. A stage,
container, timeout, or ingest failure marks the sandbox job failed, returns the
saved submission to owner review/retry, force-stops the claimed queue item, and
removes the exact workspace by default. The three manual stage/mark/ingest
management commands remain available for diagnosis, but the normal launcher
does not require them.

## Fixed workspace contract

The host creates these inputs:

```text
/workspace/job-manifest.json
/workspace/input/001-<document-uuid>.<safe-extension>
/workspace/schema/delivery-invoice.schema.json
/workspace/output/
```

Only these outputs are accepted:

```text
/workspace/output/inventory-result.json
/workspace/output/corrected-inventory.xlsx
```

The JSON must validate against the strict `DeliveryInvoice` evidence schema.
The XLSX must be a real workbook with bounded expanded size and no formulas,
macros, embedded objects, or external links. It is copied to the app's private
storage path `inventory-sandbox/<job-uuid>/corrected-inventory.xlsx`; there is
no public media URL.

The workbook is an auditable download, not an authority to update Square.
Owner corrections remain in the web application, and a fresh Square read plus
the existing explicit write confirmation is still required before inventory
changes.
