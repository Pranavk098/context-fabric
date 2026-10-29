# mypy: ignore-errors

"""Report-format tests (v2 chain fixture)."""

from decimal import Decimal

from shop.report import format_report


def test_format_report() -> None:
    report = {
        "subtotal": Decimal("100"),
        "discount": Decimal("10"),
        "tax": Decimal("4.50"),
        "total": Decimal("94.50"),
    }
    assert format_report(report) == "Total: $94.50 (tax $4.50)"
