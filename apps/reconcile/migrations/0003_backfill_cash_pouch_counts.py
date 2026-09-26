from django.db import migrations


def backfill_existing_counts(apps, schema_editor):
    DailyReconciliation = apps.get_model("reconcile", "DailyReconciliation")
    CashPouchCount = apps.get_model("reconcile", "CashPouchCount")
    existing = DailyReconciliation.objects.exclude(owner_collected_cents=None).iterator()
    for reconciliation in existing:
        required = (
            reconciliation.drawer_float_cents,
            reconciliation.expected_collection_cents,
            reconciliation.collection_recorded_at,
            reconciliation.collection_recorded_by_id,
        )
        if any(value is None for value in required):
            continue
        variance = reconciliation.collection_variance_cents
        if variance is None:
            variance = (
                reconciliation.owner_collected_cents
                - reconciliation.expected_collection_cents
            )
        CashPouchCount.objects.create(
            reconciliation_id=reconciliation.pk,
            drawer_float_cents=reconciliation.drawer_float_cents,
            expected_cents=reconciliation.expected_collection_cents,
            counted_cents=reconciliation.owner_collected_cents,
            variance_cents=variance,
            evidence_hash=reconciliation.collection_evidence_hash,
            note=reconciliation.collection_note,
            entered_by_id=reconciliation.collection_recorded_by_id,
            created_at=reconciliation.collection_recorded_at,
        )


class Migration(migrations.Migration):
    dependencies = [("reconcile", "0002_cashpouchcount")]

    operations = [
        migrations.RunPython(backfill_existing_counts, migrations.RunPython.noop),
    ]
