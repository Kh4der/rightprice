from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0005_delivery_printed_quantity_totals"),
    ]

    operations = [
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="catalog_object_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="catalog_version",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="category_path",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="current_price_cents",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="current_price_currency",
            field=models.CharField(blank=True, max_length=3),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="price_from_location_override",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="pricing_type",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="reporting_category_id",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="squarecatalogvariation",
            name="reporting_category_name",
            field=models.CharField(blank=True, max_length=300),
        ),
    ]
