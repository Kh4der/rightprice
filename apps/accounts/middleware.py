from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


class DemoWorkspaceBoundaryMiddleware:
    """Fail closed so a practice owner can never enter operational routes."""

    allowed_prefixes = ("/demo/", "/static/")
    allowed_paths = ("/logout/", "/healthz")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.is_demo:
            path = request.path_info
            allowed = path in self.allowed_paths or path.startswith(self.allowed_prefixes)
            if not allowed:
                if request.method in {"GET", "HEAD", "OPTIONS"}:
                    return redirect("demo:dashboard")
                raise PermissionDenied("Practice mode cannot change live store records.")
        return self.get_response(request)
