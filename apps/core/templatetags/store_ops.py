import json
from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(cents):
    if cents is None or cents == "":
        return "Not confirmed"
    try:
        value = Decimal(str(cents)) / 100
    except Exception:
        return str(cents)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


@register.filter
def pretty_json(value):
    return json.dumps(value or {}, indent=2, sort_keys=True, ensure_ascii=False)


@register.filter
def status_class(value):
    return str(value or "").lower().replace("_", "-")


@register.filter
def square_check_label(value):
    labels = {
        "square.drawer.starting_cash": "Drawer starting cash",
        "square.drawer.paid_in_out": "Drawer paid in / paid out",
        "square.drawer.cash_sales": "Drawer cash sales",
        "square.drawer.cash_refunds": "Drawer cash refunds",
        "square.drawer.expected_cash": "Expected cash in drawer",
        "square.drawer.counted_cash": "Counted cash at close",
        "square.drawer.unexplained_variance": "Cash explained by Square and lottery",
        "square.drawer.closing_team_member": "Employee who closed the drawer",
        "square.payments.cash": "Cash payments",
        "square.payments.card": "Card payments",
        "square.payments.total_collected": "Total collected",
    }
    return labels.get(str(value), str(value).replace(".", " ").replace("_", " ").title())
