# Store Ops

Store Ops is a mobile-first Django application for a liquor store that uses
Square. Employees photograph closeout paperwork, lottery payout evidence, and
delivery invoices. A vision provider turns photographs into structured drafts;
inventory can optionally use Anthropic Managed Agents in a self-hosted sandbox.
AI is an extraction assistant, not the inventory decision-maker. The application
preserves the source image and visible text, runs deterministic checks, and
sends uncertain or consequential work to the owner.

This repository is an implementation-stage system, not a production-validated
Square integration. The application and test doubles exercise the workflows,
but production acceptance still requires merchant-scoped Square credentials,
that merchant's real closeout data, and representative distributor invoices.
See [Product status and validation](docs/product-notes.md).

## What is implemented

| Workflow | Current behavior | Important boundary |
| --- | --- | --- |
| Daily close | Captures a Square sales report and ended-drawer photo, optionally captures two lottery reports, extracts typed values with evidence, checks document arithmetic, then lets the owner pull the exact business day's Square drawer and completed-payment totals for a field-by-field comparison. After approval, that day's cash is listed separately for the owner's weekly collection. | The live pull is read-only and requires exactly one closed drawer, the submitting employee's mapped Square ID to match its closer, and counted cash to be explained by Square plus lottery activity. Daily evidence approval does not pretend the owner has already collected the cash. |
| Lottery payout | Captures payout evidence, preserves the employee-entered and extracted amounts separately, requires the owner to confirm the amount and approve the evidence, then lets the owner mark it reimbursed. | This is an app ledger. It cannot create a Square cash-drawer event or Square Payout. Duplicate validation references, ticket references, or source photos block approval and reimbursement. |
| Inventory delivery | Extracts invoice lines into a draft, matches only on safe identifiers, supports owner review, pulls live Square counts, shows `current + received = projected`, and can post explicit inventory adjustments. | AI never chooses or posts a Square item. Duplicate vendor/invoice/date combinations and reused source photos are blocked. Posting is a separate owner action and is disabled by default. |

The original photographs are immutable. Extracted values are stored beside
them, not in place of them. Monetary values are integer cents throughout.

## Daily cash and weekly owner collection

Employees keep each business day's cash separate. Daily Square evidence can be
approved before the owner visits the store. Approval freezes the expected
**Daily cash** as all cash counted in the register minus the effective drawer
float, which defaults to **$265.00**.

The owner workspace groups Daily cash by collection week and carries earlier
uncounted or mismatched days forward. For each business day it shows the
expected amount, the amount physically counted by the owner, and the exact
difference. An incorrect amount is saved as a visible `Short` or `Over` issue
so it cannot disappear. The owner can count again and enter a corrected amount;
the current snapshot updates, while every prior count remains in an append-only
`DailyCashCount` history and the audit log. Weekly variance only compares days
the owner has actually counted, so an uncounted day is never reported as
missing cash.

## Safe Square inventory flow

Inventory writes follow a deliberately gated sequence:

1. **Pull the Square catalog.** A read-only refresh caches item variations that
   are present at the configured location and have inventory tracking enabled.
2. **Extract an invoice draft.** The default provider reads vendor, invoice,
   pack, quantity, cost, and total fields. For the Managed Agents demo, enable
   both `CLAUDE_INVENTORY_SANDBOX_ENABLED` and
   `CLAUDE_INVENTORY_SANDBOX_PRIMARY`; the employee's existing upload is routed
   to the isolated worker and no second owner upload is needed. Extraction never
   authorizes a write or decides which Square variation receives stock.
3. **Match conservatively.** Automatic matches are limited to an exact cached
   UPC/GTIN, a previously reviewed vendor-plus-SKU mapping, or an exact Square
   SKU. A name-only match is only a suggestion and remains blocked until a
   person marks the exact Square variation as matched. Ambiguous identifiers,
   unknown variation IDs, pack ambiguity, fractional sellable units, and unit
   arithmetic disagreements remain review items.
4. **Reject duplicate receiving.** Before a delivery can become ready—and again
   before posting—the app blocks a vendor, invoice number, and invoice date
   already used by another non-rejected delivery. It also blocks a source-photo
   hash already attached to another non-rejected delivery. Rejecting the
   mistaken duplicate retires it without deleting its evidence.
5. **Review the lines in the app.** Only an owner can correct invoice facts or
   choose Square matches. The screen shows invoice cost beside the applicable
   Square vendor/default cost and flags the percentage change. Every edit is
   audited and invalidates old count evidence. After the live Square comparison
   is complete and the owner approves the evidence, the owner can download a
   finalized Excel snapshot; workbook uploads are not accepted. Approval locks
   further edits, so the downloaded file cannot silently become stale.
6. **Pull current counts.** For every reviewed variation, the app reads Square's
   `IN_STOCK` count and stores the comparison explicitly:

   ```text
   current Square count + reviewed delivery units = projected count
   ```

   If several invoice lines point to one variation, their deltas are combined
   for the projection.
7. **Approve, then post explicitly.** Evidence approval does not post inventory.
   An owner must separately choose the Square post action. The server also
   refuses every write unless `SQUARE_INVENTORY_WRITES_ENABLED=true` and the
   posting owner has a locally configured Square team member ID. Immediately
   before a first post, it re-reads Square and blocks if the live count no
   longer equals the reviewed before-count; the owner must refresh and review a
   new projection.
8. **Commit the request identity, then send.** In one database transaction, the
   app marks the delivery as posting and stores its deterministic batch keys,
   deterministic line change IDs, posting owner, Square team member attribution,
   and audit event before any Square write is attempted. Concurrent posts are
   refused. If the process stops after that durable claim, the request can be
   resumed only after `SQUARE_PUSH_STALE_SECONDS` (15 minutes by default), with
   the same keys and the original posting owner's Square attribution. Batches
   contain at most 100 `ADJUSTMENT` changes. A terminal posted delivery is never
   posted again.
9. **Verify the result.** The app immediately re-reads the counts. It records
   `PUSHED` when they equal the projection, `PUSHED_WITH_DRIFT` with per-line
   differences when they do not, and `PUSHED_UNVERIFIED` when the write
   succeeded but the verification read was unavailable.

The write targets a Square **item variation ID**, not an item ID. Receiving uses
an `ADJUSTMENT` from `NONE` to `IN_STOCK`, so the reviewed quantity is a delta
added to stock. The request quantity is sent as a string, as required by the
Square SDK. The app does not use `PHYSICAL_COUNT`: that operation sets an
absolute on-hand count and would be unsafe for a delivery. Only positive,
whole-unit receipts are currently supported; returns and correction workflows
are not implemented.

### Square team member mapping

`login_code` plus the hashed PIN/password is the application's login identity.
`square_team_member_id` is an optional, manually maintained local link to a
Square team member. The app does not provision or continuously synchronize
Square team members, and the field alone does not prove that the signed-in user
controls that Square account. It becomes mandatory for an inventory post so the
adjustment payload can include the selected Square team member ID.

Run the read-only `verify_square` command to list the merchant's team member IDs,
then copy the appropriate ID into the employee record through the owner UI or
the seed command.

## Square read-only boundaries

- The Cash Drawer Shifts SDK surface used here only lists shifts, retrieves a
  shift, and lists its events. It cannot open/close a drawer or create a
  `PAID_OUT` event. The application therefore treats photographed drawer data as
  evidence and does not claim to update a Square drawer.
- Square's Payouts API in the installed SDK only lists/gets Square-to-bank
  payouts and their entries. Those payouts are unrelated to a lottery winner
  paid from the till, and the API has no create method. Lottery reimbursement
  stays in the Store Ops ledger.
- `verify_square` reads locations, team members, drawer shifts, payments, and
  Reporting API metadata. It never writes inventory or other Square data.
- The owner-side daily review also performs a read-only pull of one business
  day. It persists the returned drawer/payment snapshot and will not mark the
  day matched when the drawer is open, missing, ambiguous, or disagrees with
  the photographed reports. Extracted report dates must match the submission's
  store business day. The Square drawer's closing team member must match the
  submitting employee's mapped Square ID, and counted cash must equal Square
  expected cash plus the recorded lottery explanation within the configured
  tolerance.

See [Square SDK reference](docs/square-sdk-reference.md) for the installed SDK
shapes and [Sample documents](docs/sample-documents.md) for the fixture limits.

## Technology

- Python 3.13, Django 6.1, server-rendered templates, and HTMX
- PostgreSQL 17 with psycopg 3 pooling
- Celery and Redis for background extraction
- OpenAI Responses API with Pydantic Structured Outputs for image extraction
- private local, Vercel Blob, or S3-compatible photo storage; Cloudflare R2 is
  supported for non-Vercel production deployments
- Square Python SDK (`squareup` distribution, `square` import)
- Vercel Functions and Celery subscribers for the hosted deployment, plus
  Docker Compose and Caddy for a VPS deployment

OpenAI is the only extraction provider implemented today. The provider boundary
and placeholder Anthropic/Google settings are future-facing; selecting a value
other than `openai` currently fails fast.

## Vercel deployment

The repository is ready for Vercel's native Django runtime. `vercel.json`
configures the web and background functions, while `pyproject.toml` registers
the WSGI entry point and Celery subscriber. Production needs a Marketplace
PostgreSQL database and a private Vercel Blob store connected to the project.

Set the application values documented in `.env.example`, use
`DJANGO_SETTINGS_MODULE=config.settings.prod`, `STORAGE_DRIVER=vercel_blob`,
and keep all Square write gates disabled until merchant credentials have been
validated. Run Django migrations against the hosted database before the first
deployment. On Vercel, browser photos are compressed when needed and uploaded
one at a time before the lightweight final form is submitted, avoiding the
platform's aggregate request-body limit.

Square synchronization and AI extraction are intentionally unavailable until
their real credentials are configured. The rest of the application—including
login, database-backed review, manual corrections, Daily cash, and private
evidence storage—can be deployed and verified independently.

## Local setup

Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/), and Docker with
Compose.

1. Start PostgreSQL, Redis, and the local S3-compatible service:

   ```bash
   docker compose up -d
   ```

2. Install the locked dependencies:

   ```bash
   uv sync --frozen
   ```

3. Copy `.env.example` to `.env`, generate a Django secret, and fill the local
   settings. On PowerShell:

   ```powershell
   Copy-Item .env.example .env
   uv run python -c "import secrets; print(secrets.token_urlsafe(64))"
   ```

   On a POSIX shell:

   ```bash
   cp .env.example .env
   uv run python -c 'import secrets; print(secrets.token_urlsafe(64))'
   ```

   Paste the generated value into `DJANGO_SECRET_KEY`. For real extraction,
   keep `EXTRACTION_PROVIDER=openai` and set `OPENAI_API_KEY`. Comments in
   `.env` must be on their own lines.

4. Create and test the development photo bucket, then provision the database:

   ```bash
   uv run python manage.py ensure_storage --create
   uv run python manage.py migrate
   ```

5. Create accounts. Either use Django's owner flow:

   ```bash
   uv run python manage.py createsuperuser
   ```

   or set a strong `SEED_OWNER_PASSWORD` and a numeric `SEED_EMPLOYEE_PIN` in `.env`, then run:

   ```bash
   uv run python manage.py seed_accounts
   ```

6. Start the web process and, in another terminal, the extraction worker:

   ```bash
   uv run python manage.py runserver
   uv run celery -A config worker --loglevel=info
   ```

For a UI-only local walkthrough without Redis/Celery dispatch, set
`CELERY_TASK_ALWAYS_EAGER=true`. OpenAI calls are still real unless tests inject
the fake provider.

### Local ports

| Service | Host port |
| --- | --- |
| Django development server | 8000 |
| PostgreSQL | 5433 |
| Redis | 6379 |
| S3Mock API | 9090 |

PostgreSQL deliberately uses 5433 on the host; the container still uses 5432.

## Square sandbox validation

Create a Square developer application and put its sandbox access token and
location ID in `.env`; keep the application ID with the app configuration as
well, although the current server-side flows do not consume it. The webhook
signature key is also unused until a webhook endpoint exists. Leave both of
these safety settings in place at first:

```dotenv
SQUARE_ENVIRONMENT=sandbox
SQUARE_INVENTORY_WRITES_ENABLED=false
```

Check the connection without writing anything:

```bash
uv run python manage.py verify_square --date 2026-09-26
```

The bundled 2026-09-26 photos are partial-day development fixtures. A sandbox
account normally does not contain the corresponding merchant data, so a
mismatch is expected. Use a date for which the selected Square account has
known data when validating time windows and tender totals.

Before enabling sandbox inventory writes, use a real representative invoice
and confirm every item variation, pack conversion, count-before value, projected
count, idempotent retry, and post-write verification result. Only then set:

```dotenv
SQUARE_INVENTORY_WRITES_ENABLED=true
```

Do not switch to `SQUARE_ENVIRONMENT=production` until the production checklist
in [Product status and validation](docs/product-notes.md) has been completed.

## Tests and checks

The automated suite uses in-memory/fake clients and must not contact OpenAI or
Square:

```bash
uv run pytest
uv run ruff check .
uv run python manage.py check
```

For production settings, also run:

```bash
uv run python manage.py check --deploy --settings=config.settings.prod
```

Passing tests demonstrates application behavior against fixtures and fakes. It
does not validate a merchant's Square permissions, catalog data, device cash
drawer behavior, model accuracy on new invoice layouts, or live inventory
writes.

## Deployment outline

The repository includes `Dockerfile`, `compose.prod.yml`, and `Caddyfile`. Fill
a production `.env` with explicit hosts/origins, a strong database password,
private object-storage credentials, an OpenAI key, and the Square settings, then
run:

```bash
docker compose -f compose.prod.yml up -d --build
docker compose -f compose.prod.yml exec web python manage.py check --deploy
```

The production web container runs the deploy checks, database migrations, and
`collectstatic` before starting Gunicorn. WhiteNoise serves those collected
assets through Django behind Caddy. The worker and Caddy wait for the web health
check, so queued extraction work cannot start before migrations finish. The
bare `Dockerfile` command starts only Gunicorn; deployments that do not use
`compose.prod.yml` must run the checks, migrations, and `collectstatic` as
explicit release steps.

PostgreSQL and Redis have no published production ports; Caddy is the public
entry point. Keep inventory writes disabled during deployment smoke tests.

## Repository map

```text
apps/accounts/     local users, roles, PIN/password login, lockout
apps/capture/      submissions, immutable photos, capture screens
apps/extraction/   vision adapters, schemas, preprocessing, validation
apps/inventory/    catalog cache, matching, final workbook, Square writes
apps/reconcile/    paper/API comparisons, owner decisions, payout/cash ledgers
apps/squareapi/    Square client, business-day windows, diagnostics
apps/audit/        append-only operational events
config/            settings, URLs, Celery, deployment checks
docs/              implementation notes, fixture notes, validation status
```
