# Product status and production validation

This page separates what the repository implements from what still needs to be
proved with the store's own systems and documents. It is intentionally a status
record, not a product promise.

## Implemented today

### Capture and extraction

- Authenticated employees can submit daily-close photos, lottery payout
  evidence, and one or more delivery-invoice pages.
- The original files and their identity fields are immutable after upload.
- A Celery task classifies each image, rejects unknown, multi-document, and
  draw-schedule captures, applies document-specific preprocessing, and asks
  OpenAI for schema-validated structured output. A cropped document can still
  be extracted for evidence, but a hard check routes it to review.
- Every extracted value carries its typed value, verbatim visible text,
  presence/legibility state, and visual location. Missing and unreadable fields
  remain null; a printed zero is distinct from an absent amount.
- Deterministic checks cover required evidence, report arithmetic, invoice
  pack/unit math, and cross-photo Square cash agreement.
- AI output remains a draft. Catalog matching, duplicate detection, count
  arithmetic, readiness, and write authorization are deterministic application
  rules; the model never chooses a Square variation or posts inventory.

OpenAI is the only working provider adapter. The default path uses the OpenAI
Responses API with Pydantic Structured Outputs. Anthropic/Google keys, fallback
model settings, multiple-run settings, and second-opinion settings exist as
future configuration placeholders but are not executed by the current
processing code.

### Owner operations

- Owners manage local employee accounts, review submissions, approve or reject
  evidence, count each business day's cash during a weekly collection, confirm
  lottery payout amounts, and mark internal payout records reimbursed.
- Daily report approval is separate from physical collection. Approval snapshots
  Daily cash as all cash counted in the register less the effective drawer
  float, which defaults to $265. The weekly page retains carry-over days, saves short/over counts as visible
  issues, and permits a recount without overwriting the original entry.
- Every Daily cash entry and correction creates an append-only `DailyCashCount`
  revision plus an audit event containing its previous and new values. Summary
  variance includes only business days the owner physically counted.
- Payout approval and reimbursement both recheck duplicate evidence. A reused
  validation reference, ticket reference, or source-photo hash from another
  non-rejected payout blocks the action; rejecting a mistaken duplicate retires
  it without deleting the source evidence.
- Operational decisions and inventory workbook changes create append-only audit
  events.
- An inventory evidence approval and a Square inventory post are distinct
  actions. Approval never causes an implicit Square write.

### Square daily reconciliation

The daily workflow first validates the photographed paperwork and materializes
a provisional reconciliation. From the owner review, a separate action reads
the configured location's cash drawer shifts and completed payments for the
exact store business-day window. It persists the API snapshot in
`DailyReconciliation.square_values` and compares starting cash, paid in/out,
cash sales, cash refunds, expected cash, counted cash, cash/card tenders, and
total collected. The day becomes `MATCHED` only when one closed drawer exists,
all required values are available, and every comparison is within the configured
cent tolerance. Every extracted report date must also match the submission's
store business day. Missing, open, or multiple drawers are not guessed.

The reconciliation also requires the single Square drawer's closing team
member to match the submitting employee's locally mapped Square team member ID.
It calculates unexplained cash as photographed counted cash minus Square
expected cash minus the recorded lottery cash explanation; an unavailable
input makes the comparison incomplete, and a result outside the configured
tolerance makes it a mismatch. Once the daily evidence is approved, its Daily
cash is counted later during the weekly owner collection. A physical count
difference is preserved as a recount issue rather than blocking the initial
entry or rewriting the approved Square evidence.

This is an owner-triggered pull rather than a scheduled sync or webhook. The
`verify_square` command remains a separate read-only diagnostic for locations,
team members, Reporting API metadata, and paper baselines.

### Square inventory

- Read-only catalog refresh caches location-eligible, inventory-tracked item
  variations.
- Matching accepts exact cached UPC/GTIN, reviewed vendor-plus-SKU mappings, or
  exact Square SKU. Exact name equality can suggest a variation but cannot
  approve it. Ambiguous identifiers are refused.
- The revision-bound XLSX review round trip validates workbook identity, row
  identity, formulas, data types, and allowed statuses; it records diffs and
  invalidates previous count snapshots after any edit.
- Read-only count refresh stores count before, delivery delta, and projected
  count after for each matched variation. Multiple invoice lines matched to the
  same variation are aggregated before displaying `current + received =
  projected`.
- A delivery is blocked when another non-rejected delivery has the same
  normalized vendor, invoice number, and invoice date, or when any source-photo
  hash has already been used by another non-rejected delivery. Readiness and
  posting both apply this protection.
- Posting requires an owner action, the server-side write gate, a configured
  location, a locally mapped Square team member ID, fully ready lines, and a
  fresh count projection.
- Before any Square write, a short database transaction commits the `PUSHING`
  state, deterministic batch and line identities, original posting owner,
  Square team member attribution, and audit event. Concurrent attempts are
  refused. Immediately before a new request, the service reads the live counts
  again and releases the unsent claim if the reviewed snapshot is stale.
- A failed request retains that committed identity. A process interrupted while
  `PUSHING` can resume only after `SQUARE_PUSH_STALE_SECONDS` (900 seconds by
  default), using the same keys and original actor attribution rather than
  creating a second receipt.
- Writes use `ADJUSTMENT` changes from `NONE` to `IN_STOCK`, item variation IDs,
  string quantities, batches of at most 100, deterministic idempotency keys,
  and deterministic change IDs.
- A follow-up count read records an exact result, per-line drift, or a distinct
  "posted but unverified" state. A terminal posted delivery does not post again.

## Boundaries and non-features

### Cash Drawer and Payout APIs do not provide the desired writes

The installed Cash Drawer Shifts client exposes list, get, and list-events
operations. It does not expose an operation to open or close a drawer or create
a paid-out event. Store Ops therefore cannot represent lottery reimbursement by
writing a Square cash drawer event.

The installed Square Payouts client exposes list, get, and list-entries. It is a
read-only view of Square's transfers to the merchant's bank account, not a way
to create a cash payment to a lottery winner. The Store Ops lottery payout and
reimbursement states are local records only.

### A Square team member ID is a local mapping

Store Ops authenticates a user with its own login code and hashed PIN/password.
An owner manually copies the corresponding Square team member ID into that
local account. The app can include that ID in an inventory adjustment, but it
does not synchronize the team directory, authenticate through Square, or prove
account ownership from that value.

### Inventory scope is intentionally narrow

The current write path receives positive, whole sellable units. It does not
implement returns to a vendor, damaged stock, stock transfers, negative
corrections, or an absolute physical count workflow. A `PHYSICAL_COUNT` would
set an absolute count; it is not a synonym for receiving and is never used by
this application.

### No webhook-driven synchronization

The settings include a Square webhook signature key, but this repository does
not currently expose a webhook route or consume Square events. Catalog and
inventory reads occur only when explicitly requested by the inventory workflow
or diagnostic command.

## What the bundled fixtures prove

The repository contains five photographs dated 2026-09-26:

- a partial-day Square sales report;
- an open/current Square drawer screen;
- a Florida Lottery ticket-balance report;
- a zero-activity daily scratch-off sales report; and
- a draw-games schedule used as a negative classification case.

They are useful for preprocessing, schema, and arithmetic regression tests.
They are not a complete closeout acceptance set: the drawer is not ended, the
lottery values are mostly zero, and one image contains part of a second document.

There is no representative distributor invoice photo or winning-ticket payout
photo in the fixture directory. Inventory tests construct structured invoice
data and fake Square responses; they do not measure extraction accuracy on a
real vendor layout. No automated test uses a live OpenAI or Square account.

## Required production validation

Keep `SQUARE_ENVIRONMENT=sandbox` and
`SQUARE_INVENTORY_WRITES_ENABLED=false` until these checks have recorded owners
and expected results.

### 1. Merchant read-only validation

- Use a least-privilege token for the intended merchant and location.
- Run `verify_square` against dates with known closeout paperwork.
- Confirm location timezone, the app's business-day window, tender totals, and
  the availability and meaning of cash drawer shifts on the store's hardware.
- Confirm which Reporting API measures, if any, reproduce the printed Square
  report. Do not infer measure names from SDK types alone.
- Copy and independently verify each posting owner's Square team member ID.

### 2. Real document acceptance set

Collect consented, redacted where appropriate, representative examples of:

- an ended Square drawer with counted cash and over/short;
- non-zero lottery daily activity and complete ticket ranges;
- the store's real payout evidence;
- every major distributor's invoice layout, including multiple pages, credits,
  deposits, taxes, fees, duplicate product descriptions, and ambiguous packs;
  and
- poor but acceptable phone captures, plus known retake cases.

Have two people transcribe ground truth independently. Measure field-level and
line-level accuracy, and verify that every ambiguity blocks rather than silently
selects a value or catalog variation.

### 3. Sandbox inventory acceptance

- Refresh the sandbox catalog and confirm only variations tracked at the chosen
  location are eligible.
- Test exact UPC, vendor mapping, exact SKU, name-only suggestion, duplicate
  identifier, unknown variation, non-stock charge, fractional unit, and pack
  mismatch cases.
- For a known count, verify the displayed `before + delta = projected` equation.
- Enable the write gate only for the controlled test, post once, and verify the
  Square count and stored response.
- Repeat the same request/revision and prove Square applies no duplicate delta.
- Simulate interruption after the pre-write claim and verify that an immediate
  concurrent retry is refused, while a stale retry reuses the stored keys and
  original Square team member attribution.
- Submit the same vendor/invoice/date and the same source photo twice; confirm
  both duplicate paths block posting until the mistaken record is rejected.
- Cause a concurrent count change and confirm the app surfaces drift instead of
  claiming an exact result.
- Simulate a failed follow-up read and confirm the owner sees "posted but
  unverified" and does not retry blindly.
- Submit payouts with duplicate validation references, ticket references, and
  source photos; confirm approval and reimbursement remain blocked until the
  mistaken duplicate is explicitly rejected.

### 4. Operational readiness

- Verify private object storage, short-lived signed URLs, database backups and
  restore, worker restart behavior, audit-log retention, monitoring, and an
  incident procedure for ambiguous post results.
- Run migrations, the complete test suite, lint, Django system checks, and
  production deploy checks from the exact release artifact.
- Start production with inventory writes off, complete read-only smoke tests,
  and use a small supervised canary before normal operation.

Until the live credential checks and real invoice acceptance set are complete,
describe the system as implemented and sandbox-testable—not production-validated.
