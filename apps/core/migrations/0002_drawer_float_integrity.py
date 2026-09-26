from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="drawerfloat",
            options={"ordering": ["-effective_from", "-created_at", "-pk"]},
        ),
        migrations.AddConstraint(
            model_name="drawerfloat",
            constraint=models.CheckConstraint(
                condition=models.Q(amount_cents__gte=0),
                name="drawer_float_amount_nonnegative",
            ),
        ),
    ]
