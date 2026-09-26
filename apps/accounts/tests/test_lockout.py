"""
Regression tests for PIN lockout.

These exist because the lockout failed silently once already, and a silent
failure here is the worst kind: the setting is present, the check passes, axes
logs that it is "blocking by username", and nothing is actually recorded.

The trap: ``axes.helpers.get_client_username`` reads the attempted username from
the ``credentials`` dict *and returns immediately* when that dict is non-empty —
it never falls through to ``request.POST``. Keyed by a single
``AXES_USERNAME_FORM_FIELD``, that means a mismatch between the form field name
and the ``authenticate()`` kwarg produces ``username=None``, axes declines to
record the attempt, and the lockout never fires while looking fully configured.

There are two different login forms here posting two different field names:

* the employee PIN form posts ``login_code`` (the model's ``USERNAME_FIELD``)
* Django's admin form posts ``username``, whatever ``USERNAME_FIELD`` says

so ``AXES_USERNAME_CALLABLE`` resolves either, from either source. Without it,
whichever form the setting did not match would have no brute-force protection —
and with ``AXES_ENABLE_ADMIN`` on, that would be an unprotected admin login.

``ModelBackend`` tolerates a ``login_code`` kwarg because it falls back to
``kwargs[UserModel.USERNAME_FIELD]`` when ``username`` is absent, so the employee
form can stay internally consistent.
"""

import pytest
from axes.models import AccessAttempt
from django.conf import settings
from django.contrib.auth import authenticate
from django.test import RequestFactory

from apps.accounts.models import User

CORRECT_PIN = "1234"
WRONG_PIN = "0000"


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "JD", CORRECT_PIN, display_name="Jamie Doe", square_team_member_id="TMtest0001"
    )


def _attempt(pin: str):
    """A login the way the real view does it: login_code in POST and in credentials."""
    request = RequestFactory().post("/login/", {"login_code": "JD", "password": pin})
    return authenticate(request, login_code="JD", password=pin)


def _admin_style_attempt(pin: str, code: str = "JD"):
    """
    A login the way Django's *admin* form does it: the field is named `username`.

    This is the shape that a single AXES_USERNAME_FORM_FIELD of "login_code"
    silently fails to record.
    """
    request = RequestFactory().post("/admin/login/", {"username": code, "password": pin})
    return authenticate(request, username=code, password=pin)


def test_axes_username_field_matches_the_user_model():
    """The setting that makes the lockout work at all."""
    assert settings.AXES_USERNAME_FORM_FIELD == User.USERNAME_FIELD


def test_axes_username_callable_is_configured():
    """
    Without the callable, only one of the two login forms is protected.
    """
    assert settings.AXES_USERNAME_CALLABLE == "apps.accounts.axes_hooks.get_username"


def test_correct_pin_authenticates(employee):
    assert _attempt(CORRECT_PIN) == employee


def test_wrong_pin_does_not_authenticate(employee):
    assert _attempt(WRONG_PIN) is None


def test_failed_attempts_are_actually_recorded(employee):
    """The regression. Without AXES_USERNAME_FORM_FIELD this stays at zero."""
    _attempt(WRONG_PIN)

    attempt = AccessAttempt.objects.filter(username="JD").first()
    assert attempt is not None, (
        "axes recorded no attempt. AXES_USERNAME_FORM_FIELD and the authenticate() "
        "kwarg have almost certainly drifted apart, and the lockout is inert."
    )
    assert attempt.failures_since_start == 1


def test_failures_accumulate_to_the_limit(employee):
    for expected in range(1, settings.AXES_FAILURE_LIMIT + 1):
        _attempt(WRONG_PIN)
        attempt = AccessAttempt.objects.get(username="JD")
        assert attempt.failures_since_start == expected


def test_correct_pin_is_refused_once_locked_out(employee):
    """
    The whole point: after the limit, even the right PIN gets nowhere.

    ``authenticate()`` returns ``None`` rather than propagating, because Django
    stops the backend chain and returns ``None`` when a backend raises
    ``PermissionDenied`` — which is what AxesStandaloneBackend does. The
    security outcome is what matters: access is denied.
    """
    for _ in range(settings.AXES_FAILURE_LIMIT):
        _attempt(WRONG_PIN)

    assert _attempt(CORRECT_PIN) is None, "Lockout did not hold; the correct PIN still worked."


def test_a_different_employee_is_not_locked_out_by_their_colleague(db, employee):
    """
    Lockout is scoped to the login code, deliberately not the IP address.

    Every employee shares one phone behind the counter, so an IP-scoped lockout
    would let one person mistyping their PIN take the whole store offline
    mid-close. See the AXES_LOCKOUT_PARAMETERS comment in settings/base.py.
    """
    other = User.objects.create_user(
        "MK", "5678", display_name="Mo Khan", square_team_member_id="TMtest0002"
    )

    for _ in range(settings.AXES_FAILURE_LIMIT):
        _attempt(WRONG_PIN)

    request = RequestFactory().post("/login/", {"login_code": "MK", "password": "5678"})
    assert authenticate(request, login_code="MK", password="5678") == other


# --------------------------------------------------------------------------
# The admin login form posts a differently-named field
# --------------------------------------------------------------------------


def test_admin_style_login_failures_are_also_recorded(employee):
    """
    Django's admin form posts `username`, not `login_code`.

    With only AXES_USERNAME_FORM_FIELD="login_code" this records nothing, leaving
    the admin login — which guards the owner account, on a public domain, behind
    a short PIN — with no brute-force protection at all.
    """
    _admin_style_attempt(WRONG_PIN)

    attempt = AccessAttempt.objects.filter(username="JD").first()
    assert attempt is not None, (
        "An admin-form login failure was not recorded. AXES_USERNAME_CALLABLE is "
        "missing or no longer handles the 'username' field name."
    )
    assert attempt.failures_since_start == 1


def test_admin_and_employee_forms_share_one_lockout_counter(employee):
    """
    Both forms authenticate the same account, so they must not each get their own
    allowance — otherwise the effective limit is double what is configured.
    """
    for _ in range(3):
        _attempt(WRONG_PIN)
    for _ in range(2):
        _admin_style_attempt(WRONG_PIN)

    assert AccessAttempt.objects.filter(username="JD").count() == 1
    assert AccessAttempt.objects.get(username="JD").failures_since_start == 5
    assert _attempt(CORRECT_PIN) is None, "Lockout did not hold across the two form shapes."


def test_login_code_case_does_not_multiply_the_allowance(employee):
    """
    The model upper-cases login_code, so "jd" and "JD" are the same account. If
    the lockout key were not normalised the same way, an attacker would get
    AXES_FAILURE_LIMIT guesses per capitalisation instead of in total.
    """
    for variant in ("jd", "Jd", "jD", "JD", " jd "):
        _admin_style_attempt(WRONG_PIN, code=variant)

    assert AccessAttempt.objects.filter(username="JD").count() == 1, (
        "Case variants created separate lockout counters; the PIN allowance is "
        "effectively multiplied."
    )
    assert AccessAttempt.objects.get(username="JD").failures_since_start == 5
    assert _attempt(CORRECT_PIN) is None
