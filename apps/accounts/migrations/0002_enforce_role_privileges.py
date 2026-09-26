from django.db import migrations, models


def normalise_role_privileges(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(role="OWNER").update(is_staff=True)
    User.objects.filter(role="EMPLOYEE").update(is_staff=False, is_superuser=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="user",
            name="employee_is_not_staff",
        ),
        migrations.RunPython(normalise_role_privileges, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(role="OWNER", is_staff=True)
                    | models.Q(role="EMPLOYEE", is_staff=False, is_superuser=False)
                ),
                name="role_matches_privilege_flags",
            ),
        ),
    ]
