import datetime as dt
from types import SimpleNamespace

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.capture.models import DocumentType, Submission, SubmissionKind
from apps.reconcile.models import DailyReconciliation, ReconciliationStatus
from apps.reconcile.square_sync import sync_daily_reconciliation
from apps.squareapi.client import business_day_window, to_rfc3339


def evidence(value):
    return {
        "value": value,
        "verbatim": str(value),
        "present": True,
        "legible": True,
        "location": "test fixture",
    }


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "SQUARESYNC",
        "1234",
        display_name="Square Sync Employee",
        square_team_member_id="TM-sync",
    )


def make_reconciliation(employee, *, paper_overrides=None):
    drawer = {
        "starting_cash_cents": evidence(10_000),
        "paid_in_out_cents": evidence(-100),
        "cash_sales_cents": evidence(2_500),
        "cash_refunds_cents": evidence(100),
        "expected_in_drawer_cents": evidence(12_300),
        "counted_cash_cents": evidence(12_300),
    }
    sales = {
        "cash_cents": evidence(2_500),
        "card_cents": evidence(7_750),
        "total_collected_cents": evidence(10_250),
    }
    if paper_overrides:
        drawer.update(paper_overrides.get("drawer", {}))
        sales.update(paper_overrides.get("sales", {}))
    counted_cash_cents = drawer["counted_cash_cents"]["value"]
    submission = Submission.objects.create(
        kind=SubmissionKind.DAILY_REPORT,
        business_day=dt.date(2026, 9, 26),
        submitted_by=employee,
    )
    return DailyReconciliation.objects.create(
        submission=submission,
        status=ReconciliationStatus.PROVISIONAL,
        paper_values={
            DocumentType.SQUARE_DRAWER_SCREEN: drawer,
            DocumentType.SQUARE_SALES_REPORT: sales,
        },
        check_results=[
            {
                "name": "paper arithmetic",
                "passed": True,
                "severity": "hard",
            }
        ],
        counted_cash_cents=counted_cash_cents,
        explained_cash_cents=0,
    )


def money(cents):
    return SimpleNamespace(amount=cents, currency="USD") if cents is not None else None


def shift(shift_id="SHIFT-1", **overrides):
    values = {
        "id": shift_id,
        "state": "CLOSED",
        "opened_at": "2026-09-26T13:00:00Z",
        "ended_at": "2026-09-27T02:00:00Z",
        "closed_at": "2026-09-27T02:01:00Z",
        "opened_cash_money": money(10_000),
        "cash_payment_money": money(2_500),
        "cash_refunds_money": money(-100),
        "cash_paid_in_money": money(500),
        "cash_paid_out_money": money(-600),
        "expected_cash_money": money(12_300),
        "closed_cash_money": money(12_300),
        "opening_team_member_id": "TM-open",
        "closing_team_member_id": "TM-sync",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def payment(payment_id, source, cents, *, status="COMPLETED"):
    return SimpleNamespace(
        id=payment_id,
        source_type=source,
        total_money=money(cents),
        status=status,
    )


class FakeShiftEndpoint:
    def __init__(self, shifts):
        self._shifts = shifts
        self.list_calls = []
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return [SimpleNamespace(id=item.id) for item in self._shifts]

    def get(self, shift_id, **kwargs):
        self.get_calls.append((shift_id, kwargs))
        found = next(item for item in self._shifts if item.id == shift_id)
        return SimpleNamespace(cash_drawer_shift=found)


class FakePaymentsEndpoint:
    def __init__(self, payments):
        self._payments = payments
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return list(self._payments)


def fake_client(*, shifts=None, payments=None):
    shift_endpoint = FakeShiftEndpoint([shift()] if shifts is None else shifts)
    payment_endpoint = FakePaymentsEndpoint(
        [payment("PAY-CASH", "CASH", 2_500), payment("PAY-CARD", "CARD", 7_750)]
        if payments is None
        else payments
    )
    client = SimpleNamespace(
        cash_drawers=SimpleNamespace(shifts=shift_endpoint),
        payments=payment_endpoint,
    )
    return client, shift_endpoint, payment_endpoint


@pytest.mark.django_db
def test_matching_closed_drawer_and_completed_payments_are_persisted(employee):
    reconciliation = make_reconciliation(employee)
    client, shifts, payments = fake_client(
        payments=[
            payment("PAY-CASH", "CASH", 2_500),
            payment("PAY-CARD-1", "CARD", 7_000),
            payment("PAY-CARD-2", "CARD", 750),
            payment("PAY-PENDING", "CARD", 99_999, status="PENDING"),
        ]
    )

    result = sync_daily_reconciliation(reconciliation, client=client)

    reconciliation.refresh_from_db()
    assert result.status == ReconciliationStatus.MATCHED
    assert reconciliation.status == ReconciliationStatus.MATCHED
    assert reconciliation.square_synced_at is not None
    assert reconciliation.square_expected_cash_cents == 12_300
    assert reconciliation.unexplained_variance_cents == 0
    assert reconciliation.square_values["drawer"]["shifts"][0]["closed_cash_cents"] == 12_300
    assert reconciliation.square_values["drawer"]["shifts"][0]["cash_refunds_cents"] == -100
    assert reconciliation.square_values["payments"] == {
        "status": "ok",
        "returned_count": 4,
        "completed_count": 3,
        "totals_by_source_cents": {"CARD": 7_750, "CASH": 2_500},
        "total_cents": 10_250,
    }
    assert len(result.comparisons) == 11
    assert all(item["available"] and item["passed"] for item in result.comparisons)
    assert reconciliation.check_results[0]["name"] == "paper arithmetic"

    start, end = business_day_window(reconciliation.submission.business_day)
    expected_query = {
        "location_id": "TEST_LOCATION",
        "begin_time": to_rfc3339(start),
        "end_time": to_rfc3339(end),
    }
    assert shifts.list_calls == [expected_query]
    assert shifts.get_calls == [("SHIFT-1", {"location_id": "TEST_LOCATION"})]
    assert payments.list_calls == [expected_query]


@pytest.mark.django_db
def test_api_difference_sets_mismatch_and_preserves_integer_cents(employee):
    reconciliation = make_reconciliation(
        employee,
        paper_overrides={
            "sales": {
                "cash_cents": evidence(1),
                "card_cents": evidence(12_344),
                "total_collected_cents": evidence(12_345),
            }
        },
    )
    client, _, _ = fake_client(
        payments=[payment("PAY-CASH", "CASH", 1), payment("PAY-CARD", "CARD", 12_343)]
    )

    result = sync_daily_reconciliation(reconciliation, client=client)

    by_name = {item["name"]: item for item in result.comparisons}
    assert result.status == ReconciliationStatus.MISMATCH
    assert by_name["square.payments.cash"]["square_cents"] == 1
    assert by_name["square.payments.card"]["paper_cents"] == 12_344
    assert by_name["square.payments.card"]["square_cents"] == 12_343
    assert by_name["square.payments.card"]["delta_cents"] == -1
    assert by_name["square.payments.card"]["passed"] is False


@pytest.mark.django_db
def test_no_drawer_is_incomplete_without_turning_zero_payments_into_missing(employee):
    reconciliation = make_reconciliation(
        employee,
        paper_overrides={
            "sales": {
                "cash_cents": evidence(0),
                "card_cents": evidence(0),
                "total_collected_cents": evidence(0),
            }
        },
    )
    client, _, _ = fake_client(shifts=[], payments=[])

    result = sync_daily_reconciliation(reconciliation, client=client)

    assert result.status == ReconciliationStatus.INCOMPLETE
    assert result.square_values["drawer"]["status"] == "missing"
    assert result.square_values["payments"]["totals_by_source_cents"] == {
        "CARD": 0,
        "CASH": 0,
    }
    payment_checks = [
        item for item in result.comparisons if item["name"].startswith("square.payments")
    ]
    assert all(item["available"] and item["passed"] for item in payment_checks)
    assert reconciliation.square_expected_cash_cents is None


@pytest.mark.django_db
def test_multiple_drawers_are_ambiguous_and_never_choose_the_first(employee):
    reconciliation = make_reconciliation(employee)
    client, shifts, _ = fake_client(shifts=[shift("SHIFT-A"), shift("SHIFT-B")])

    result = sync_daily_reconciliation(reconciliation, client=client)

    assert result.status == ReconciliationStatus.INCOMPLETE
    assert result.square_values["drawer"]["status"] == "ambiguous"
    assert result.square_values["drawer"]["shift_count"] == 2
    assert [item["id"] for item in result.square_values["drawer"]["shifts"]] == [
        "SHIFT-A",
        "SHIFT-B",
    ]
    drawer_checks = [
        item for item in result.comparisons if item["name"].startswith("square.drawer")
    ]
    assert all(item["square_cents"] is None for item in drawer_checks)
    assert shifts.get_calls == [
        ("SHIFT-A", {"location_id": "TEST_LOCATION"}),
        ("SHIFT-B", {"location_id": "TEST_LOCATION"}),
    ]


@pytest.mark.django_db
@override_settings(CASH_VARIANCE_TOLERANCE_CENTS=1)
def test_configured_tolerance_is_applied_to_cents_differences(employee):
    reconciliation = make_reconciliation(
        employee,
        paper_overrides={"drawer": {"cash_refunds_cents": evidence(99)}},
    )
    client, _, _ = fake_client()

    result = sync_daily_reconciliation(reconciliation, client=client)

    comparison = next(
        item for item in result.comparisons if item["name"] == "square.drawer.cash_refunds"
    )
    assert comparison["delta_cents"] == 1
    assert comparison["tolerance_cents"] == 1
    assert comparison["passed"] is True
    assert result.status == ReconciliationStatus.MATCHED


@pytest.mark.django_db
def test_absent_paper_value_is_missing_but_literal_zero_is_not(employee):
    reconciliation = make_reconciliation(
        employee,
        paper_overrides={
            "drawer": {
                "paid_in_out_cents": {
                    "value": 0,
                    "verbatim": None,
                    "present": False,
                    "legible": False,
                    "location": "test fixture",
                },
                "cash_refunds_cents": evidence(0),
            }
        },
    )
    client, _, _ = fake_client(
        shifts=[
            shift(
                cash_refunds_money=money(0),
                cash_paid_in_money=money(0),
                cash_paid_out_money=money(0),
            )
        ]
    )

    result = sync_daily_reconciliation(reconciliation, client=client)

    by_name = {item["name"]: item for item in result.comparisons}
    assert by_name["square.drawer.paid_in_out"]["paper_cents"] is None
    assert by_name["square.drawer.paid_in_out"]["available"] is False
    assert by_name["square.drawer.cash_refunds"]["paper_cents"] == 0
    assert by_name["square.drawer.cash_refunds"]["square_cents"] == 0
    assert by_name["square.drawer.cash_refunds"]["passed"] is True
    assert result.status == ReconciliationStatus.INCOMPLETE


@pytest.mark.django_db
def test_closing_team_member_must_match_submitting_employee(employee):
    reconciliation = make_reconciliation(employee)
    client, _, _ = fake_client(shifts=[shift(closing_team_member_id="TM-someone-else")])

    result = sync_daily_reconciliation(reconciliation, client=client)

    comparison = next(
        item for item in result.comparisons if item["name"] == "square.drawer.closing_team_member"
    )
    assert comparison["kind"] == "identity"
    assert comparison["paper_value"] == "TM-sync"
    assert comparison["square_value"] == "TM-someone-else"
    assert comparison["passed"] is False
    assert result.status == ReconciliationStatus.MISMATCH


@pytest.mark.django_db
def test_missing_employee_square_mapping_makes_day_incomplete(employee):
    employee.square_team_member_id = None
    employee.save(update_fields=["square_team_member_id"])
    reconciliation = make_reconciliation(employee)
    client, _, _ = fake_client()

    result = sync_daily_reconciliation(reconciliation, client=client)

    comparison = next(
        item for item in result.comparisons if item["name"] == "square.drawer.closing_team_member"
    )
    assert comparison["available"] is False
    assert result.status == ReconciliationStatus.INCOMPLETE


@pytest.mark.django_db
def test_unexplained_cash_outside_tolerance_makes_day_mismatch(employee):
    reconciliation = make_reconciliation(
        employee,
        paper_overrides={"drawer": {"counted_cash_cents": evidence(12_400)}},
    )
    client, _, _ = fake_client(shifts=[shift(closed_cash_money=money(12_400))])

    result = sync_daily_reconciliation(reconciliation, client=client)

    comparison = next(
        item for item in result.comparisons if item["name"] == "square.drawer.unexplained_variance"
    )
    assert comparison["paper_cents"] == 12_400
    assert comparison["square_cents"] == 12_300
    assert comparison["delta_cents"] == 100
    assert comparison["passed"] is False
    assert result.status == ReconciliationStatus.MISMATCH
    reconciliation.refresh_from_db()
    assert reconciliation.unexplained_variance_cents == 100


@pytest.mark.django_db
def test_snapshot_rolls_back_when_atomic_audit_hook_fails(employee):
    reconciliation = make_reconciliation(employee)
    client, _, _ = fake_client()

    def fail_audit(_target, _result):
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        sync_daily_reconciliation(
            reconciliation,
            client=client,
            on_persist=fail_audit,
        )

    reconciliation.refresh_from_db()
    assert reconciliation.status == ReconciliationStatus.PROVISIONAL
    assert reconciliation.square_values == {}
    assert reconciliation.square_synced_at is None
