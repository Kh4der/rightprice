# Sample documents — synthetic development fixtures

The images in `tests/fixtures/documents/` are fully synthetic. They preserve the
visual problems and internally consistent arithmetic needed by the regression
suite without publishing merchant paperwork, employee names, store interiors,
or ticket identifiers. They are not a production acceptance corpus and do not
replace validation with private live merchant data and representative invoices.

Store: **Synthetic Sample Store**, Florida. Lottery retailer **000000-00**.

The fixed example is dated **2026-09-26 around 12:07–12:11 PM EDT** and models a
mid-day rather than a close. The lottery totals are therefore all zeros and the
Square figures model a partial day. This gives the tests a clean "nothing has
happened yet" baseline; the figures are fictional and not representative of a
real closing.

---

## 1. Square "Current drawer" — `square_drawer_screen.jpg`

**This simulates a photo of the iPad screen, not a printout.** That changes the
extraction problem: the failure mode is *glare and reflection*, not thermal
fade. The synthetic screen has a diagonal light streak, and the row labels sit
in low-contrast grey.

| Field on screen | Value |
| --- | --- |
| Started | 9/26/26, 10:12 AM |
| Started by | (employee) |
| Starting cash | $265.00 |
| Paid in/out | $0.00 |
| Cash sales | $34.91 |
| Cash refunds | $0.00 |
| **Expected in drawer** | **$299.91** |

The drawer is still **OPEN** — the "End drawer" button is present and there is
no counted/actual figure yet. A closing submission will have more rows.

### Fields that can be compared with Cash Drawer Shifts

Several screen fields have counterparts in the full shift returned by the
read-only Cash Drawer Shifts API:

| Screen label | SDK field |
| --- | --- |
| Starting cash | `opened_cash_money` |
| Cash sales | `cash_payment_money` |
| Cash refunds | `cash_refunds_money` |
| Paid in/out | `cash_paid_in_money` / `cash_paid_out_money` |
| Expected in drawer | `expected_cash_money` |
| Started by | `opening_team_member_id` |
| Started | `opened_at` |

"Started by (employee)" is a display name while the API returns a team member
ID. An owner may manually store that ID in the matching local user record. It is
not an automatic join and it is not the user's Store Ops login identity.

### Verified identity

```
starting_cash + paid_in_out + cash_sales - cash_refunds = expected_in_drawer
      265.00  +        0.00 +     34.91  -          0.00 =          299.91  ✓
```

Note `Paid in/out` is a **single net row** on this screen, while the API splits
it into `cash_paid_in_money` and `cash_paid_out_money`. The extractor must not
assume it can recover the two from the one; a net $0.00 could be $0 in and $0
out, or $50 in and $50 out.

---

## 2. Square Sales Report — `square_sales_report.jpg`

A genuine thermal printout on white paper. Legible, good contrast.

```
SALES REPORT
September 26, 2026 12:00 AM — September 26, 2026 11:59 PM
Reported on Sep 26, 2026 12:07 PM EDT
All Team Members
All Devices
```

| SALES | |
| --- | --- |
| Gross Sales | $104.41 |
| Returns | $0.00 |
| Discounts & Comps | $0.00 |
| **Net Sales** | **$104.41** |
| Tax | $7.83 |
| Tips | $0.00 |
| Gift Card Sales | $0.00 |
| Refunds by Amount | $0.00 |
| **Total** | **$112.24** |

| PAYMENTS | |
| --- | --- |
| Total Collected | $112.24 |
| Card | $77.33 |
| **Cash** | **$34.91** |
| Fees | −$1.52 |
| Net Total | $110.72 |

| CATEGORY SALES | |
| --- | --- |
| 50 ML MINI × 2 | $5.98 |
| LIQUOR × 5 | $81.45 |
| SODA × 1 | $3.99 |
| Vodka × 1 | $12.99 |

### Verified identities

```
net_sales + tax                = total
   104.41 + 7.83               = 112.24   ✓

card + cash                    = total_collected
77.33 + 34.91                  = 112.24   ✓

total_collected - fees         = net_total
        112.24 - 1.52          = 110.72   ✓

sum(category_sales)            = gross_sales
5.98 + 81.45 + 3.99 + 12.99    = 104.41   ✓
```

Four independent arithmetic checks on one document. This is what makes
verbatim-plus-arithmetic far stronger than a model-reported confidence score: a
misread digit anywhere breaks at least one of these sums.

### The cross-document link

```
Sales Report "Cash"  =  Drawer screen "Cash sales"
             $34.91  =  $34.91                       ✓
```

This is the join between the two Square documents, and it is the anchor the
whole daily reconciliation hangs from.

### Two things this changes

**The store is in Eastern time.** "Reported on Sep 26, 2026 12:07 PM **EDT**",
and the lottery schedule says all times are Eastern. `STORE_TIMEZONE` must be
`America/New_York`, not the `America/Chicago` placeholder.

**Square reports a calendar day, not a 4am-cutoff day.** The header reads
`12:00 AM — 11:59 PM`. Since the entire point is matching the printout the
employee photographs, our query window has to match Square's, so
`BUSINESS_DAY_CUTOFF` is `00:00` for this store. The cutoff machinery stays —
it is still needed to interpret *when* a late submission belongs — but for this
store it must not shift the window away from Square's own.

> The `11:59 PM` in the header is a display rounding. The window is a full day;
> a sale at 11:59:30 PM is included. Our window stays half-open `[00:00, 00:00)`
> rather than trying to reproduce a literal `23:59` end, which would drop the
> last minute of trading.

---

## 3. Florida Lottery — Ticket Balance Detail — `fl_lottery_ticket_balance_detail.jpg`

Scratch-off **inventory** per book. Pink Florida Lottery thermal stock — the
background graphic prints *over* the text region, which is the hardest
extraction target of the five.

```
TICKET BALANCE DETAIL
09/26/26  12:09:58
RETAILER 000000   REGISTER 1
SHIFT 1  DATE 09/26/26  12:08:58 TO
```

| $ | NAME | SD | GAME BOOK | RANGE | SOLD |
| --- | --- | --- | --- | --- | --- |
| 50.00 | 1642FI | A | 1642-059749 | 014- | 0 |
| 30.00 | 1647FI | A | 1647-058008 | 001- | 0 |
| 10.00 | 1646TW | A | 1646-057254 | 007- | 0 |
| 10.00 | 5063CH | A | 5063-074591 | 008- | 0 |
| 5.00 | 1641PO | A | 1641-052386 | 047- | 0 |
| 5.00 | 1645BL | A | 1645-048373 | 002- | 0 |
| 2.00 | 1644MO | A | 1644-042458 | 006- | 0 |
| 1.00 | 1643WI | A | 1643-034342 | 007- | 0 |

```
PRICE POINT SHIFT TOTAL $ 0.00   SOLD 0
VOID - NOT FOR SALE
```

Structure worth noting:

- `GAME BOOK` is `<game>-<book>`, and the game number repeats in `NAME`
  (`1642FI` ↔ `1642-059749`). That redundancy is a free correctness check: if the
  extracted `NAME` prefix and the `GAME BOOK` game number disagree, one of them
  was misread.
- Price points present: **$1, $2, $5, $10, $30, $50** — a Florida ladder, no $20.
- `RANGE` shows `014-` with the trailing value cut off at the paper edge. The app
  must treat a partially-captured range as *not legible* rather than guessing;
  this is precisely the field where a guess silently invents ticket sales.
- `VOID - NOT FOR SALE` marks this as a report, not a saleable ticket. Harmless,
  but the extractor should not mistake it for a status field.
- The shift began at 12:08:58 and the report ran at 12:09:58, hence all zeros.

---

## 4. Florida Lottery — Daily Scratch-Off Games Sales — `fl_lottery_daily_scratchoff_sales.jpg`

**This is the lottery financial document that matters for reconciliation.**

```
DAILY SCRATCH-OFF GAMES SALES
09/26/26  12:10:37   Synthetic Sample Store  000000-00
FOR SATURDAY 09/26/26
```

| Line | Count | Amount |
| --- | --- | --- |
| BOOKS SETTLED | 0 | 0.00 |
| BOOKS UNSTLD | 0 | 0.00 |
| PARTIAL RETURN | 0 | 0.00 |
| SALES COMM | — | 0.00 |
| **PAYS** | 0 | 0.00 |
| CASHING COMM | — | 0.00 |
| CLAIMS | 0 | **— (none printed)** |
| BOOKS RECEIVED | 0 | — |
| BOOKS ACTIVATED | 0 | — |
| ADJUSTMENTS | 0 | 0.00 |
| **NET TOTAL** | — | 0.00 |

> **Correction.** An earlier version of this page recorded `CLAIMS` as having an
> amount of `0.00`. It does not. Count the amount column on the photograph:
> there are six values, then a **three-row gap** spanning CLAIMS, BOOKS RECEIVED
> and BOOKS ACTIVATED, then two more — eight printed amounts in total, not
> eleven.
>
> This mistake is worth leaving documented rather than quietly fixing, because it
> is exactly the failure the app is built to prevent: a human transcribing the
> raw pink photograph turned an **absent** field into a **zero**. On a busy day
> CLAIMS carries a real figure, so an extractor that does the same thing invents
> a zero that looks like a legitimately quiet day and reconciles perfectly.
>
> It is also the strongest argument for the red-channel preprocessing step. On
> the processed image the gap is unambiguous; on the raw one it is not.

`PAYS` is the cash paid out to winners — the payouts the owner reimburses.
`SALES COMM` and `CASHING COMM` are the retailer's two commissions. `NET TOTAL`
is what settles with the lottery.

Note the two-column layout: several lines carry **both a count and an amount**,
and some carry only one. An extractor that assumes one value per line will
silently shift `BOOKS RECEIVED`'s count into an amount column.

The date line reads `FOR SATURDAY 09/26/26` — the weekday is spelled out, giving
another free cross-check: 2026-09-26 **is** a Saturday. A mismatch means the
date was misread.

---

## 5. Florida Lottery — Draw Games Schedule — `fl_lottery_draw_schedule.jpg`

```
DRAW GAMES SCHEDULE / HORARIO DE LOS SORTEOS
09/26/26  12:11:19  000000-00
Synthetic Sample Store
```

A table of games (Pick 2/3/4/5 midday and evening, Fantasy 5, Mega Millions,
JP Triple Play, Powerball, Florida Lotto, Cash Pop ×5) with cutoff and draw
times.

**This document contains no money and is not part of any reconciliation.**

It is worth calling out precisely *because* it will get photographed by mistake
— it prints from the same terminal, on the same pink stock, at the same moment
as the two that matter. The implemented classifier recognizes the schedule as a
negative type and rejects it rather than extracting plausible zeros.

---

## What the full picture implies

### The lottery/POS relationship still needs a non-zero day

This partial-day fixture shows liquor, soda, and minis in Square and zero lottery
activity on the lottery report. It therefore cannot prove whether lottery sales
ring through Square on an active day. If they do not, the intended equation is:

```
counted_cash - square_expected_cash  =  lottery_cash_in - lottery_payouts ± error
```

That relationship is why `LOTTERY_RINGS_THROUGH_POS` is explicit configuration
rather than an inference. It is currently configured `false`, but that choice
must be verified against a real non-zero day before the resulting cash variance
is trusted.

### Five documents, four that matter

| Document | Type | Used for |
| --- | --- | --- |
| Square Current drawer (screen) | `SQUARE_DRAWER_SCREEN` | expected cash, opening float, attribution |
| Square Sales Report (print) | `SQUARE_SALES_REPORT` | gross/net sales, tax, card vs cash split |
| FL Ticket Balance Detail | `LOTTERY_TICKET_BALANCE` | scratch-off book inventory and ranges |
| FL Daily Scratch-Off Sales | `LOTTERY_DAILY_SALES` | pays, commissions, net due |
| FL Draw Games Schedule | `LOTTERY_DRAW_SCHEDULE` | **nothing** — detect and reject |

The earlier assumption of a single combined "Z-report" plus a handwritten drawer
sheet and a handwritten lottery sheet was wrong for this store. Nothing here is
handwritten, which is good news for extraction accuracy — but two of the four
are the harder cases: a glare-prone screen photo and pink-on-pink thermal stock.

### Two more things the photographs show

**A second document is in frame.** The Square sales report is clearly visible
down the right-hand third of `fl_lottery_daily_scratchoff_sales.jpg`. Text from
document B can bleed into document A's extraction — and here the intruder is
another money document, so the bleed would be plausible numbers rather than
obvious noise. The implemented classifier counts physical documents and refuses
more than one instead of trusting that the intended document wins.

**On the drawer screen, the values sit about 18px _above_ their labels.**
Measured on `square_drawer_screen.jpg`: the "Started" label is at y≈458 while
its value is at y≈440; "Starting cash" is at y≈531 with `$265.00` at y≈512. That
is keystone from shooting the iPad from below and to the left, not skew, so
deskewing does not remove it.

This is why the implementation uses a schema-guided vision model rather than a
simple horizontal-band parser. It is not proof that every vision model will read
the field correctly; the result still needs evidence and deterministic checks.

### Still unknown

- What the drawer screen looks like **after** "End drawer" — that closing view
  carries the counted total and the over/short figure, which is the number the
  owner actually cares about. Needed before the reconciliation can be finished.
- A day with real lottery activity: non-zero `PAYS`, settled books, and a
  `RANGE` with both endpoints, to pin down the serial-direction question.
- Whether draw-game (online) sales appear on a separate terminal report.
- A representative distributor delivery invoice, including pack and charge
  lines, for the inventory flow.
- A real lottery payout document for the payout flow.
- Results from the configured OpenAI model and the intended Square account;
  automated tests use fakes and do not supply live credentials.
