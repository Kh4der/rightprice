"""
Username resolution for django-axes.

django-axes has to know *which account* a failed login was against, or it records
nothing and the lockout silently does not exist. By default it reads one field
name (``AXES_USERNAME_FORM_FIELD``) and, critically, when a ``credentials`` dict
is supplied it reads only that dict and never falls through to ``request.POST``
(see ``axes.helpers.get_client_username``).

This app has two different login forms posting two different field names:

* the employee PIN form posts ``login_code`` (our ``USERNAME_FIELD``)
* Django's admin login form posts ``username``, regardless of ``USERNAME_FIELD``

A single ``AXES_USERNAME_FORM_FIELD`` can only match one of them, so whichever
one it is not silently loses brute-force protection. With
``AXES_ENABLE_ADMIN = True`` that would mean an unprotected admin login form on
a public domain, guarding an owner account, behind a short PIN.

This resolver accepts either field name from either source.
"""

from django.http import HttpRequest

# Order matters only for the pathological case of both being present; the app's
# own field wins.
CANDIDATE_FIELDS = ("login_code", "username")


def get_username(request: HttpRequest | None, credentials: dict | None) -> str | None:
    """
    Resolve the attempted login identifier.

    Signature is fixed by axes: ``AXES_USERNAME_CALLABLE(request, credentials)``.
    Returns ``None`` only when there is genuinely no identifier to attribute the
    attempt to, which axes handles by declining to record it.
    """
    if credentials:
        for field in CANDIDATE_FIELDS:
            value = credentials.get(field)
            if value:
                return _normalise(value)

    if request is not None:
        # `request.data` covers a DRF-style request; `request.POST` the normal form.
        data = getattr(request, "data", None)
        if data is None:
            data = getattr(request, "POST", None)
        if data:
            for field in CANDIDATE_FIELDS:
                value = data.get(field)
                if value:
                    return _normalise(value)

    return None


def _normalise(value: str) -> str:
    """
    Match the normalisation the user model applies.

    ``User.clean()`` upper-cases and strips ``login_code``, so without this
    "jd", "JD" and " jd " would be three separate lockout counters and five
    attempts each would give fifteen free guesses.
    """
    return str(value).strip().upper()
