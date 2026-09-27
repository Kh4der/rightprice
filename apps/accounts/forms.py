from __future__ import annotations

from typing import ClassVar

from django import forms
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError

from .models import Role, User


class LoginForm(forms.Form):
    login_code = forms.CharField(
        label="Login code",
        max_length=12,
        widget=forms.TextInput(
            attrs={"autocomplete": "username", "autocapitalize": "characters", "autofocus": True}
        ),
    )
    pin = forms.CharField(
        label="PIN or owner password",
        min_length=4,
        max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user = None

    def clean(self):
        cleaned = super().clean()
        code = (cleaned.get("login_code") or "").strip().upper()
        pin = cleaned.get("pin")
        if code and pin:
            self.user = authenticate(self.request, login_code=code, password=pin)
            if self.user is None:
                raise ValidationError("The login code or PIN/password is not correct.")
            if not self.user.is_active:
                raise ValidationError("This account is inactive. Ask the owner for help.")
        return cleaned


class EmployeeForm(forms.ModelForm):
    pin = forms.CharField(
        label="New PIN",
        min_length=4,
        max_length=12,
        required=False,
        help_text="Use 4-12 digits. Leave blank when editing to keep the current PIN.",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "inputmode": "numeric"}),
    )

    class Meta:
        model = User
        fields: ClassVar = ["display_name", "login_code", "square_team_member_id", "is_active"]
        labels: ClassVar = {"square_team_member_id": "Square team member ID"}
        help_texts: ClassVar = {
            "square_team_member_id": (
                "Square generates this ID. Copy it from the Square team list; it is not the employee's PIN."
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["pin"].required = True

    def clean_pin(self):
        pin = self.cleaned_data.get("pin", "")
        if pin and not pin.isdigit():
            raise ValidationError("PIN must contain digits only.")
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = Role.EMPLOYEE
        user.is_staff = False
        user.is_superuser = False
        pin = self.cleaned_data.get("pin")
        if pin:
            user.set_password(pin)
        if commit:
            user.save()
        return user
