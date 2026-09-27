import apps.accounts.models
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_enforce_role_privileges"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="is_demo",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "Keeps public practice accounts and their sample records "
                    "separate from the store."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="login_code",
            field=models.CharField(
                help_text="Short code the employee types to sign in. Assigned by the owner.",
                max_length=12,
                unique=True,
                validators=[
                    django.core.validators.RegexValidator(
                        message=(
                            "Login code must be 2-12 characters using letters, "
                            "numbers, or an underscore."
                        ),
                        regex="^[A-Z0-9_]{2,12}$",
                    )
                ],
            ),
        ),
        migrations.RemoveConstraint(
            model_name="user",
            name="role_matches_privilege_flags",
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(role="OWNER", is_demo=False, is_staff=True)
                    | models.Q(
                        role="OWNER",
                        is_demo=True,
                        is_staff=False,
                        is_superuser=False,
                    )
                    | models.Q(
                        role="EMPLOYEE",
                        is_demo=False,
                        is_staff=False,
                        is_superuser=False,
                    )
                ),
                name="role_matches_privilege_flags",
            ),
        ),
    ]
