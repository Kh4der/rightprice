"""
Read-only end-to-end check of the Square connection.

    uv run python manage.py verify_square
    uv run python manage.py verify_square --date 2026-09-26

THIS COMMAND ONLY READS. It lists locations, team members, cash drawer shifts
and payments. It never creates, updates or deletes anything, and in particular
it never touches inventory — so it is safe to point at a production token.

Its real job is to prove the integration against known-good data: when run for a
date whose paperwork has been photographed, it compares what the API returns
against what the paper says. Matching numbers mean the client, the business-day
window, the timezone and the field mapping are all correct at once.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.reconcile.checks import fmt
from apps.squareapi.client import business_day_window, get_client, to_rfc3339

# Transcribed from tests/fixtures/documents/ — see docs/sample-documents.md.
# Used only to check the API against the paper for that one date.
PAPER_BASELINE = {
    dt.date(2026, 9, 26): {
        "drawer_opened_cash_cents": 26_500,
        "drawer_cash_sales_cents": 3_491,
        "drawer_cash_refunds_cents": 0,
        "drawer_expected_cents": 29_991,
        "sales_total_collected_cents": 11_224,
        "sales_card_cents": 7_733,
        "sales_cash_cents": 3_491,
        "sales_gross_cents": 10_441,
        "sales_tax_cents": 783,
    }
}


def money(m) -> int | None:
    """Square Money -> integer cents, or None when the field is absent."""
    if m is None:
        return None
    return m.amount


class Command(BaseCommand):
    help = "Read-only verification of the Square connection and day figures. Writes nothing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            default="2026-09-26",
            help="Business day to pull, YYYY-MM-DD. Defaults to the date of the sample photos.",
        )
        parser.add_argument(
            "--location",
            default=None,
            help="Square location id. Defaults to SQUARE_LOCATION_ID.",
        )

    def handle(self, *args, **opts):
        try:
            day = dt.date.fromisoformat(opts["date"])
        except ValueError as exc:
            raise CommandError(f"--date must be YYYY-MM-DD, got {opts['date']!r}") from exc

        if not settings.SQUARE_ACCESS_TOKEN:
            raise CommandError(
                "SQUARE_ACCESS_TOKEN is empty.\n"
                "Get a token from developer.squareup.com/apps -> your app -> "
                "Sandbox (or Production) -> Access token, and put it in .env."
            )

        client = get_client()
        self._banner(day)

        location_id = opts["location"] or settings.SQUARE_LOCATION_ID
        location_id = self._check_locations(client, location_id)
        self._check_team(client)
        drawers = self._check_drawers(client, location_id, day)
        payments = self._check_payments(client, location_id, day)
        self._check_reporting(client)
        self._compare_to_paper(day, drawers, payments)

    # ---------------------------------------------------------------- output

    def _banner(self, day: dt.date):
        start, end = business_day_window(day)
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Square connection check (read-only)"))
        self.stdout.write(f"  environment    : {settings.SQUARE_ENVIRONMENT}")
        self.stdout.write(f"  business day   : {day}")
        self.stdout.write(f"  store timezone : {settings.STORE_TIMEZONE}")
        self.stdout.write(f"  day cutoff     : {settings.BUSINESS_DAY_CUTOFF}")
        self.stdout.write(f"  query window   : {to_rfc3339(start)} .. {to_rfc3339(end)}")
        self.stdout.write("")

    def _section(self, title: str):
        self.stdout.write(self.style.MIGRATE_HEADING(title))

    def _fail(self, message: str):
        self.stdout.write(self.style.ERROR(f"  ! {message}"))

    def _ok(self, message: str):
        self.stdout.write(self.style.SUCCESS(f"  {message}"))

    # ---------------------------------------------------------------- checks

    def _check_locations(self, client, location_id: str) -> str:
        self._section("Locations")
        try:
            resp = client.locations.list()
        except Exception as exc:
            raise CommandError(
                f"Could not list locations: {exc}\n\n"
                "Most often this means the access token is wrong, or it is a sandbox "
                "token while SQUARE_ENVIRONMENT=production (or the reverse)."
            ) from exc

        locations = resp.locations or []
        if not locations:
            raise CommandError("The token is valid but this account has no locations.")

        for loc in locations:
            marker = "->" if loc.id == location_id else "  "
            self.stdout.write(
                f"  {marker} {loc.id}  {loc.name}  [{loc.status}] tz={loc.timezone} {loc.currency}"
            )

        ids = {loc.id for loc in locations}
        if not location_id:
            chosen = locations[0]
            self._fail(
                f"SQUARE_LOCATION_ID is not set. Using {chosen.id} ({chosen.name}). "
                "Put it in .env to make this deterministic."
            )
            location_id = chosen.id
        elif location_id not in ids:
            raise CommandError(
                f"SQUARE_LOCATION_ID={location_id!r} is not one of this account's locations."
            )
        else:
            self._ok("SQUARE_LOCATION_ID matches a real location")

        # The store timezone is configuration, but Square knows the truth.
        match = next((loc for loc in locations if loc.id == location_id), None)
        if match and match.timezone and match.timezone != settings.STORE_TIMEZONE:
            self._fail(
                f"Square says this location's timezone is {match.timezone}, but "
                f"STORE_TIMEZONE={settings.STORE_TIMEZONE}. Every business-day window "
                "is built from that setting, so fix it in .env."
            )
        self.stdout.write("")
        return location_id

    def _check_team(self, client):
        self._section("Team members")
        try:
            resp = client.team_members.search(limit=50)
        except Exception as exc:
            self._fail(f"Could not search team members: {exc}")
            self.stdout.write("")
            return

        members = resp.team_members or []
        if not members:
            self._fail("No team members returned.")
        for m in members:
            name = " ".join(filter(None, [m.given_name, m.family_name])) or "(no name)"
            self.stdout.write(f"     {m.id}  {name}  [{m.status}]")

        self.stdout.write("")
        self.stdout.write(
            "  These ids are what employee accounts key to. Set one on each login with:"
        )
        self.stdout.write("    uv run python manage.py seed_accounts --employee-square-id <id>")
        self.stdout.write("")

    def _check_drawers(self, client, location_id: str, day: dt.date) -> list:
        self._section("Cash drawer shifts")
        start, end = business_day_window(day)

        try:
            pager = client.cash_drawers.shifts.list(
                location_id=location_id,
                begin_time=to_rfc3339(start),
                end_time=to_rfc3339(end),
            )
            summaries = list(pager)
        except Exception as exc:
            self._fail(f"Could not list cash drawer shifts: {exc}")
            self.stdout.write("")
            return []

        if not summaries:
            self._fail(
                "No cash drawer shifts in this window.\n"
                "    This is expected if the store does not use Square's cash drawer "
                "feature, or if the date is wrong. Without shifts, expected-cash has to "
                "come from payments instead — see docs/sample-documents.md."
            )
            self.stdout.write("")
            return []

        # .list() returns thin summaries without the tender breakdown, so each
        # shift needs its own .get(). Unavoidable N+1; the day-pull job caches it.
        full = []
        for s in summaries:
            try:
                detail = client.cash_drawers.shifts.get(s.id, location_id=location_id)
                shift = detail.cash_drawer_shift
            except Exception as exc:
                self._fail(f"Could not fetch shift {s.id}: {exc}")
                continue
            full.append(shift)

            self.stdout.write(f"  shift {shift.id}  [{shift.state}]")
            self.stdout.write(f"     opened        {shift.opened_at}")
            self.stdout.write(f"     opened_cash   {self._m(shift.opened_cash_money)}")
            self.stdout.write(f"     cash_payment  {self._m(shift.cash_payment_money)}")
            self.stdout.write(f"     cash_refunds  {self._m(shift.cash_refunds_money)}")
            self.stdout.write(f"     paid_in       {self._m(shift.cash_paid_in_money)}")
            self.stdout.write(f"     paid_out      {self._m(shift.cash_paid_out_money)}")
            self.stdout.write(f"     expected      {self._m(shift.expected_cash_money)}")
            self.stdout.write(f"     closed_cash   {self._m(shift.closed_cash_money)}")
            self.stdout.write(f"     opened_by     {shift.opening_team_member_id}")
            self.stdout.write(f"     closed_by     {shift.closing_team_member_id}")

        self.stdout.write("")
        return full

    def _check_payments(self, client, location_id: str, day: dt.date) -> dict:
        self._section("Payments")
        start, end = business_day_window(day)

        try:
            pager = client.payments.list(
                location_id=location_id,
                begin_time=to_rfc3339(start),
                end_time=to_rfc3339(end),
            )
            payments = list(pager)
        except Exception as exc:
            self._fail(f"Could not list payments: {exc}")
            self.stdout.write("")
            return {}

        totals: dict[str, int] = {}
        completed = 0
        for p in payments:
            if p.status != "COMPLETED":
                continue
            completed += 1
            source = p.source_type or "UNKNOWN"
            totals[source] = totals.get(source, 0) + (money(p.total_money) or 0)

        self.stdout.write(f"  {len(payments)} payments, {completed} COMPLETED")
        for source, cents in sorted(totals.items()):
            self.stdout.write(f"     {source:12s} {fmt(cents)}")
        if totals:
            self.stdout.write(f"     {'TOTAL':12s} {fmt(sum(totals.values()))}")
        self.stdout.write("")
        return totals

    def _check_reporting(self, client):
        """
        Discover the Reporting API schema.

        This is the one call that tells us whether the printed Sales Report can be
        reproduced directly, instead of being reassembled by summing payments. It
        takes a query with measures, dimensions and — crucially — a `timezone`, so
        Square can bucket a business day in the store's own timezone rather than us
        hand-building UTC windows and hoping the boundary matches.

        The measure names are not documented in full, so we ask the API and print
        the sales-related ones. See docs/square-sdk-reference.md.
        """
        self._section("Reporting API (schema discovery)")
        try:
            meta = client.reporting.get_metadata()
        except Exception as exc:
            self._fail(
                f"Could not read the reporting schema: {exc}\n"
                "    Not fatal — the Orders/Payments path above is the fallback. It may "
                "mean the Reporting API needs enabling on this account."
            )
            self.stdout.write("")
            return

        cubes = meta.cubes or []
        if not cubes:
            self._fail("No cubes returned; the Reporting API may not be enabled here.")
            self.stdout.write("")
            return

        self.stdout.write(f"  {len(cubes)} cubes available")

        # Surface the measures that plausibly map onto the printed report so the
        # right names can be pinned down rather than guessed.
        wanted = (
            "sale",
            "tax",
            "tender",
            "cash",
            "card",
            "gross",
            "net",
            "total",
            "discount",
            "refund",
        )
        for cube in cubes:
            measures = [m.name for m in (getattr(cube, "measures", None) or []) if m.name]
            hits = [m for m in measures if any(w in m.lower() for w in wanted)]
            if not hits:
                continue
            self.stdout.write(
                f"\n  cube {cube.name}: {len(measures)} measures, {len(hits)} sales-related"
            )
            for name in sorted(hits)[:40]:
                self.stdout.write(f"     {name}")
            if len(hits) > 40:
                self.stdout.write(f"     ... and {len(hits) - 40} more")

        self.stdout.write("")
        self.stdout.write(
            "  If gross sales, tax and a cash/card tender split appear above, the Sales\n"
            "  Report can be reproduced directly and we should stop summing payments."
        )
        self.stdout.write("")

    def _m(self, m) -> str:
        cents = money(m)
        return "(absent)" if cents is None else fmt(cents)

    # ------------------------------------------------------- paper vs the API

    def _compare_to_paper(self, day: dt.date, drawers: list, payments: dict):
        baseline = PAPER_BASELINE.get(day)
        if not baseline:
            self.stdout.write(
                f"  No photographed paperwork on file for {day}, so nothing to compare against. "
                "Run with --date 2026-09-26 to check the API against the sample photos."
            )
            return

        self._section(f"API vs the photographed paperwork for {day}")
        rows: list[tuple[str, int, int | None]] = []

        if drawers:
            d = drawers[0]
            rows += [
                (
                    "drawer opened_cash",
                    baseline["drawer_opened_cash_cents"],
                    money(d.opened_cash_money),
                ),
                (
                    "drawer cash sales",
                    baseline["drawer_cash_sales_cents"],
                    money(d.cash_payment_money),
                ),
                (
                    "drawer cash refunds",
                    baseline["drawer_cash_refunds_cents"],
                    money(d.cash_refunds_money),
                ),
                (
                    "drawer expected",
                    baseline["drawer_expected_cents"],
                    money(d.expected_cash_money),
                ),
            ]
            if len(drawers) > 1:
                self._fail(
                    f"{len(drawers)} drawer shifts in this window; comparing only the first. "
                    "A real multi-shift day needs them summed, which the day-pull job must handle."
                )

        if payments:
            rows.append(("payments CASH", baseline["sales_cash_cents"], payments.get("CASH")))
            rows.append(("payments CARD", baseline["sales_card_cents"], payments.get("CARD")))
            rows.append(
                ("payments TOTAL", baseline["sales_total_collected_cents"], sum(payments.values()))
            )

        if not rows:
            self._fail("Nothing came back from the API to compare.")
            return

        width = max(len(r[0]) for r in rows)
        mismatches = 0
        for label, paper, api in rows:
            if api is None:
                self.stdout.write(f"  {label:<{width}}  paper {fmt(paper):>10}   api (absent)")
                mismatches += 1
                continue
            if api == paper:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  {label:<{width}}  paper {fmt(paper):>10}   api {fmt(api):>10}  match"
                    )
                )
            else:
                mismatches += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"  {label:<{width}}  paper {fmt(paper):>10}   api {fmt(api):>10}  "
                        f"off by {fmt(api - paper)}"
                    )
                )

        self.stdout.write("")
        if mismatches:
            self.stdout.write(
                self.style.WARNING(
                    f"{mismatches} of {len(rows)} did not match.\n"
                    "Before assuming the code is wrong, check the obvious causes: the photos were "
                    "taken MID-DAY (about 12:10pm) so the paper is a partial day and the API has the "
                    "full one; a sandbox token has none of this data at all; and a wrong "
                    "STORE_TIMEZONE shifts the whole window."
                )
            )
        else:
            self._ok(
                "Every figure matches the paperwork. The client, the business-day window, the "
                "timezone and the field mapping are all correct."
            )
