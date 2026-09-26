"""Small authorization helpers used by server-rendered views."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def owner_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_owner:
            raise PermissionDenied("Owner access is required.")
        return view(request, *args, **kwargs)

    return wrapped
