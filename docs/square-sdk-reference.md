# Square Python SDK — verified reference

The call shapes on this page were read from the installed SDK by introspection
on 2026-09-26. That verifies Python names and type signatures; it does **not**
verify merchant permissions, endpoint behavior, report semantics, or live data.
Those still require the intended account and location.

```
PyPI distribution:  squareup == 46.0.0.20260916
Python import:      square
```

The distribution and the import name differ. `pip install squareup`, then
`import square`. (`square` on **npm** is the Node SDK — unrelated.)

## Client

The pre-v40 `from square.client import Client` shape is gone. Current:

```python
from square import Square, AsyncSquare
from square.environment import SquareEnvironment

client = Square(
    token=settings.SQUARE_ACCESS_TOKEN,
    environment=SquareEnvironment.SANDBOX,  # or .PRODUCTION
    timeout=30.0,
)
```

`SquareEnvironment` has exactly two members:

| Member | Base URL |
| --- | --- |
| `PRODUCTION` | `https://connect.squareup.com` |
| `SANDBOX` | `https://connect.squareupsandbox.com` |

There is a real async client (`AsyncSquare`, httpx-backed). Celery workers are
synchronous, so the sync `Square` client is the right default here.

### API groups on the client

```
apple_pay, bank_accounts, bookings, cards, cash_drawers, catalog, channels,
checkout, customers, devices, disputes, employees, events, gift_cards,
inventory, invoices, labor, locations, loyalty, merchants, o_auth, orders,
payments, payouts, refunds, reporting, sites, snippets, subscriptions, team,
team_members, terminal, transfer_orders, v1transactions, vendors, webhooks
```

`reporting` exposes `get_metadata` and `load`. The repository's read-only
`verify_square` command calls only `get_metadata`; the normal reconciliation
workflow does not currently load report data into the database.

## Reporting API

A semantic-layer query surface that may be useful for reproducing the store's
printed Sales Report. It has not been validated against this merchant, so the
presence of the SDK methods must not be presented as a working reconciliation
integration.

```
Base URL:  https://connect.squareup.com/reporting
  GET  /v1/meta   -> schema discovery   (client.reporting.get_metadata)
  POST /v1/load   -> run a query        (client.reporting.load)
```

### The query shape

`client.reporting.load(query=...)` takes a Cube.js-style query — the response
types (`cubes`, `compiler_id`, `slow_query`, `refresh_key_values`) confirm Cube
underneath:

| `QueryParams` field | Type |
| --- | --- |
| `measures` | `Sequence[str]`, e.g. `Orders.net_sales` |
| `dimensions` | `Sequence[str]`, e.g. `Orders.location_id` |
| `segments` | `Sequence[str]`, e.g. `Orders.closed_checks` |
| `time_dimensions` | `Sequence[TimeDimensionParams]` |
| `filters` | condition / and / or trees |
| `order`, `limit`, `offset`, `ungrouped` | paging and shaping |
| **`timezone`** | **str** |

`TimeDimensionParams` is `{dimension (required), granularity, date_range}`,
where `date_range` accepts a string, a pair of strings, or a dict.

```python
resp = client.reporting.load(
    query={
        "measures": ["Orders.net_sales", "Orders.net_sales_with_tax"],
        "time_dimensions": [
            {
                "dimension": "Orders.sale_timestamp",
                "date_range": ["2026-09-26", "2026-09-26"],
                "granularity": "day",
            }
        ],
        "segments": ["Orders.closed_checks"],
        "timezone": "America/New_York",
    }
)
```

### Why `timezone` matters here

It is a first-class query parameter, so the API can bucket a business day **in
the store's own timezone**. That is exactly the problem
`apps/squareapi/client.py` solves by hand with `zoneinfo` and RFC 3339 UTC
windows, and matching Square's own day boundary is the single hardest part of
making our numbers equal the printed report.

The hand-built window is still needed for keying submissions and for the Cash
Drawer Shifts and Payments APIs, which take plain `begin_time`/`end_time`.
Whether a Reporting API query exactly matches the printed report remains a live
account validation item.

### Not yet verified

Everything above is the request/response *shape*, read from the installed SDK
and the published docs. What has **not** been confirmed, because it needs a live
token:

- the exact measure names for gross sales, tax, and the cash-versus-card tender
  split — the docs name only `Orders.net_sales`, `Orders.net_sales_with_tax` and
  `Orders.tips_amount`, and point at a Schema Explorer for the full catalogue
- whether the figures tie out exactly to the printed Sales Report
- whether the API needs any enablement on the account

`client.reporting.get_metadata()` discovers the schema. The owner-side daily
review does not depend on undocumented Reporting measures: it reads the exact
business-day drawer shift and completed payments, stores that snapshot on
`DailyReconciliation`, and compares it with the photographed fields. The
`verify_square` command remains the broader schema/connection diagnostic.

## Inventory

Method names do **not** match the doc endpoint names:

| Docs call it | SDK method |
| --- | --- |
| BatchChangeInventory | `client.inventory.batch_create_changes` |
| BatchRetrieveInventoryCounts | `client.inventory.batch_get_counts` |
| BatchRetrieveInventoryChanges | `client.inventory.batch_get_changes` |

`client.inventory.deprecated_batch_change` also exists — do not use it.

### Change types

`InventoryChangeParams.type` accepts only:

```
PHYSICAL_COUNT | ADJUSTMENT
```

**There is no `TRANSFER` change type** in this version, despite what older
material suggests.

### Receiving a delivery

Use `ADJUSTMENT`, not `PHYSICAL_COUNT`.

- `ADJUSTMENT` with `from_state="NONE"` → `to_state="IN_STOCK"` *adds* the
  received quantity to whatever is on hand.
- `PHYSICAL_COUNT` **overwrites** the on-hand figure. Using it for a delivery
  destroys the existing count, and if the delivery is entered twice it silently
  looks correct rather than doubling — which hides the error instead of
  surfacing it.

`InventoryAdjustmentParams` carries fields tailor-made for this flow:

| Field | Use here |
| --- | --- |
| `from_state` / `to_state` | `"NONE"` → `"IN_STOCK"` |
| `catalog_object_id` | the item **variation** id |
| `quantity` | **a string**, e.g. `"24"` — not an int |
| location | `to_location_id` for the receiving destination |
| `reference_id` | our invoice-line id, for traceability |
| `vendor_id` | the distributor |
| `cost_money` | the line's unit cost, so Square knows COGS |
| `team_member_id` | the locally mapped Square ID of the owner who posts |
| `occurred_at` | the actual delivery time, RFC 3339 |

The Store Ops write builder uses only positive whole-unit quantities. Returns,
negative corrections, transfers, and physical-count workflows are outside its
current scope.

### InventoryState — complete enum

Read from the `states` literal on `batch_get_counts`:

```
CUSTOM, IN_STOCK, SOLD, RETURNED_BY_CUSTOMER, RESERVED_FOR_SALE, SOLD_ONLINE,
ORDERED_FROM_VENDOR, RECEIVED_FROM_VENDOR, IN_TRANSIT_TO, NONE, WASTE,
UNLINKED_RETURN, COMPOSED, DECOMPOSED, SUPPORTED_BY_NEWER_VERSION, IN_TRANSIT,
UNTRACKED
```

`IN_TRANSIT`, `UNTRACKED` and `CUSTOM` are real and are often missing from
third-party lists.

### Reading on-hand counts

```python
pager = client.inventory.batch_get_counts(
    catalog_object_ids=[variation_id],
    location_ids=[location_id],
    states=["IN_STOCK"],
)
for count in pager:  # SyncPager — iterate, do not hand-roll cursors
    print(count.quantity)  # also a string
```

Store Ops reads only `IN_STOCK` counts. It first records
`count before + reviewed delivery delta = projected count`, then reads the count
again after an adjustment and stores any drift.

## Cash drawer shifts

```python
client.cash_drawers.shifts.list(location_id=..., begin_time=..., end_time=...)
client.cash_drawers.shifts.get(shift_id, location_id=...)
client.cash_drawers.shifts.list_events(shift_id, location_id=...)
```

`location_id` is **required** on all three.

Those are the complete cash-drawer shift operations in this installed client.
They are read-only: there is no method to open/end/close a drawer or create a
`PAID_IN`/`PAID_OUT` event. Store Ops cannot write a lottery reimbursement into
Square's drawer history.

### The field names are not what the docs imply

| Commonly written as | Actual SDK field |
| --- | --- |
| `starting_cash_money` | **`opened_cash_money`** |
| `opening_employee_id` | **`opening_team_member_id`** |
| `closing_employee_id` | **`closing_team_member_id`** |
| — | **`ending_team_member_id`** (distinct from closing) |

Full `CashDrawerShift` (returned by `.get()`):

```
id, state (OPEN|ENDED|CLOSED), opened_at, ended_at, closed_at, description,
opened_cash_money, cash_payment_money, cash_refunds_money, cash_paid_in_money,
cash_paid_out_money, expected_cash_money, closed_cash_money, device,
created_at, updated_at, location_id, team_member_ids,
opening_team_member_id, ending_team_member_id, closing_team_member_id
```

### `.list()` returns a summary, not the shift

`CashDrawerShiftSummary` contains only:

```
id, state, opened_at, ended_at, closed_at, description,
opened_cash_money, expected_cash_money, closed_cash_money,
created_at, updated_at, location_id
```

It **omits** `cash_payment_money`, `cash_refunds_money`, `cash_paid_in_money`,
`cash_paid_out_money` and every team member field. The daily reconciliation
therefore performs a `.get()` for the single shift returned by `.list()`. It
persists a read-only snapshot and refuses to choose when a day returns multiple
shifts. The `verify_square` diagnostic uses the same full-detail requirement.

The team member fields can help correlate a Square shift with a person, but
Store Ops logins are keyed by the local `login_code`. `square_team_member_id` is
a manually maintained local mapping, not an authentication key or automatic
directory synchronization.

### Shift event types — complete enum

```
NO_SALE, CASH_TENDER_PAYMENT, OTHER_TENDER_PAYMENT,
CASH_TENDER_CANCELLED_PAYMENT, OTHER_TENDER_CANCELLED_PAYMENT,
CASH_TENDER_REFUND, OTHER_TENDER_REFUND, PAID_IN, PAID_OUT
```

The `..._CANCELLED_PAYMENT` pair is easy to miss and matters: a cancelled cash
tender must not count as a sale, but it does appear in the event stream.

## Payouts are read-only and are not lottery payouts

The installed Payouts client exposes only:

```python
client.payouts.list(...)
client.payouts.get(payout_id)
client.payouts.list_entries(payout_id, ...)
```

These methods read Square's payouts to a merchant's bank account. They do not
create a payout and do not represent cash paid to a lottery winner. Store Ops
therefore keeps lottery evidence and owner reimbursement in its own ledger and
does not call the Square Payouts API for that workflow.

## Pagination

List endpoints return `SyncPager[...]`. Iterate it directly; it fetches pages
transparently. Hand-rolled `cursor` loops are unnecessary and get the
termination condition wrong.

## Team members and vendors

`client.team_members.search()` is used by `verify_square` to display IDs. The
owner must copy the correct ID into the Store Ops user record; the application
does not import or update the Square team directory. Inventory posting refuses
an actor with no local mapping and includes the mapped ID in the adjustment.

`client.vendors` is real (`create`, `batch_create`, `search`, `get`, `update`).
Store Ops does not create or synchronize Square vendors. If a local vendor has
a reviewed `square_vendor_id`, the adjustment includes it; otherwise vendor
identity remains local.

## Still to confirm against live sandbox

- Whether this merchant's hardware produces cash drawer shifts at all. The API
  exists regardless, but a store running Square POS on an iPad without a
  connected drawer may return an empty list — the reconciliation engine needs a
  documented fallback for computing expected cash from Orders/Payments alone.
- Whether sandbox seeds any cash drawer shift data.
- Which Reporting API measures, if any, tie exactly to the printed report.
- The complete inventory path with a real catalog, known before-count, real
  distributor invoice, idempotent retry, and post-write count verification.
