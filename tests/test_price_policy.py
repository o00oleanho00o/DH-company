from __future__ import annotations

import json
from pathlib import Path

from app import db
from app.excel import parse_workbook
from app.ingest import ingest_workbook
from app.price_policy import (
    load_manual_pricing_rules,
    select_price_observation,
)

from .conftest import find_input_file


def test_real_cable_tong_preserves_base_vat_and_discount_tiers() -> None:
    parsed = parse_workbook(find_input_file("1. Bảng giá CÁP HẠ"))
    tong = next(sheet for sheet in parsed["sheets"] if sheet.snapshot.name == "TONG")
    assert tong.metadata["price_columns"]["base"] == {"ex_vat": 1, "inc_vat": 2}
    tiers = tong.metadata["price_columns"]["discount_tiers"]
    assert [tier["discount_rate"] for tier in tiers] == [
        0.1,
        0.11,
        0.12,
        0.13,
        0.14,
        0.15,
        0.16,
        0.17,
        0.18,
        0.19,
        0.2,
        0.21,
        0.22,
        0.23,
        0.24,
        0.25,
        0.26,
        0.27,
        0.28,
        0.29,
        0.3,
    ]
    row = next(row for row in tong.rows if row["row_kind"] == "data")
    observations = row["fields"]["price_observations"]
    assert len(observations) == 44
    assert observations[0]["amount"] == 525113.0
    assert observations[1]["amount"] == 577624.0
    assert observations[2]["discount_rate"] == 0.1
    assert observations[2]["amount"] == 472602.0
    assert observations[3]["tax_mode"] == "inc_vat"
    assert observations[3]["amount"] == 519862.0
    # The old parser exposed the first discounted amount as ``discount``;
    # that value is not a percentage and must no longer be mislabelled.
    assert row["fields"]["discount"] is None


def test_manual_rule_selects_published_tier_without_inventing_price() -> None:
    rules = load_manual_pricing_rules(
        json.dumps(
            [
                {
                    "id": "cadi-10",
                    "supplier": "CADI-SUN",
                    "category": "cable",
                    "discount": "10%",
                    "tax_mode": "ex_vat",
                    "effective_from": "2026-01-01",
                }
            ]
        )
    )
    selected, metadata = select_price_observation(
        [
            {
                "price_type": "supplier_list",
                "tax_mode": "ex_vat",
                "amount": 100,
                "discount_rate": 0.0,
            },
            {
                "price_type": "supplier_discounted",
                "tax_mode": "ex_vat",
                "amount": 90,
                "discount_rate": 0.1,
            },
        ],
        rule=rules[0],
    )
    assert selected is not None
    assert selected["amount"] == 90
    assert selected.get("calculated", False) is not True
    assert metadata["selection"] == "manual_pricing_rule_observation"


def test_ingest_keeps_observations_and_uses_configured_tier(
    isolated_db: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "MANUAL_PRICING_RULES_JSON",
        json.dumps(
            [
                {
                    "id": "cadi-10",
                    "supplier": "CADI-SUN",
                    "category": "cable",
                    "discount": 0.1,
                    "effective_from": "2026-01-01",
                }
            ]
        ),
    )
    stats = ingest_workbook(find_input_file("1. Bảng giá CÁP HẠ"))
    assert stats["price_observations"] > stats["product_prices"] > 0
    assert stats["manual_pricing_rules_applied"] > 0
    with db.db_session() as conn:
        price = conn.execute(
            """
            SELECT pp.net_price, pp.discount, pp.tax_mode, pp.calc_json
            FROM product_prices pp
            ORDER BY pp.id
            LIMIT 1
            """
        ).fetchone()
        assert price["discount"] == 0.1
        assert price["net_price"] == 472602.0
        assert price["tax_mode"] == "ex_vat"
        assert json.loads(price["calc_json"])["selection"]["selection"] == (
            "manual_pricing_rule_observation"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE product_id = 1"
        ).fetchone()[0] >= 44


def test_supplier_detail_sheet_enriches_tong_product_identity(
    isolated_db: Path,
) -> None:
    """The canonical summary price remains single-source while detail specs persist."""

    ingest_workbook(find_input_file("1. Bảng giá CÁP HẠ"))
    with db.db_session() as conn:
        rows = conn.execute(
            """
            SELECT normalized_name, technical_attributes_json
            FROM products
            WHERE normalized_name LIKE 'cxv 1x1.5%'
            """
        ).fetchall()

    assert rows
    enriched = [json.loads(row["technical_attributes_json"]) for row in rows]
    assert any(
        attrs.get("construction") or attrs.get("voltage") or attrs.get("standard")
        for attrs in enriched
    )


def test_holdout_exclusion_does_not_publish_price_observations(
    isolated_db: Path,
) -> None:
    stats = ingest_workbook(
        find_input_file("1. Bảng giá CÁP HẠ"),
        exclude_prices=True,
    )
    assert stats["price_observations"] == 0
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0] == 0
