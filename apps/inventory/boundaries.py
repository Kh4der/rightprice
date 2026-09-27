"""Shared fail-closed checks for inventory integrations."""

from __future__ import annotations


def is_demo_inventory_request(delivery, *, actor=None) -> bool:
    """Return true unless the persisted delivery is known to belong to live data."""

    if getattr(actor, "is_demo", False):
        return True

    delivery_id = getattr(delivery, "pk", None)
    manager = getattr(delivery.__class__, "objects", None)
    if delivery_id is None or manager is None:
        return True

    # Read the owner from the database rather than trusting a possibly stale or
    # caller-supplied related object. Missing deliveries fail closed as well.
    is_demo = (
        manager.filter(pk=delivery_id)
        .values_list("submission__submitted_by__is_demo", flat=True)
        .first()
    )
    return is_demo is not False
