from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.audit.services import record_event

from .access import owner_required
from .forms import EmployeeForm, LoginForm
from .models import Role, User


def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:home")
    form = LoginForm(request, request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.user)
        record_event(request, "session.login", form.user)
        next_url = request.POST.get("next") or request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect("core:home")
    return render(request, "accounts/login.html", {"form": form})


@login_required
@require_POST
def logout_view(request):
    user = request.user
    record_event(request, "session.logout", user)
    logout(request)
    return redirect("accounts:login")


@owner_required
def employee_list(request):
    employees = User.objects.filter(role=Role.EMPLOYEE).order_by("display_name")
    return render(request, "accounts/employee_list.html", {"employees": employees})


@owner_required
def employee_create(request):
    form = EmployeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            employee = form.save()
            record_event(request, "employee.created", employee, {"login_code": employee.login_code})
        messages.success(
            request, f"{employee.display_name} can now sign in with code {employee.login_code}."
        )
        return redirect("accounts:employees")
    return render(request, "accounts/employee_form.html", {"form": form, "employee": None})


@owner_required
def employee_edit(request, pk):
    employee = get_object_or_404(User, pk=pk, role=Role.EMPLOYEE)
    form = EmployeeForm(request.POST or None, instance=employee)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            employee = form.save()
            record_event(request, "employee.updated", employee, {"login_code": employee.login_code})
        messages.success(request, f"Updated {employee.display_name}.")
        return redirect("accounts:employees")
    return render(request, "accounts/employee_form.html", {"form": form, "employee": employee})
