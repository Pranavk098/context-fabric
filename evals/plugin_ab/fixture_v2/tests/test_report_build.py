# mypy: ignore-errors

"""Report-build tests (v2 chain fixture)."""

from decimal import Decimal

from shop.report import build_report


def test_build_report() -> None:
    report = build_report(Decimal("100"), Decimal("0.05"), Decimal("10"))
    assert report == {
        "subtotal": Decimal("100.00"),
        "discount": Decimal("10.00"),
        "tax": Decimal("4.50"),
        "total": Decimal("94.50"),
    }
