from __future__ import annotations

import pytest

from app.normalize import (
    canonical_key,
    normalize_text,
    normalize_unit,
    parse_number,
    technical_attributes,
)


def test_normalize_text_is_unicode_and_engineering_safe() -> None:
    value = "ĐVT: mét\xa0\nCáp 24KV — Cu/XLPE × 3x240"
    result = normalize_text(value)

    assert "dvt" in result
    assert "met" in result
    assert "24kv" in result
    assert "cu/xlpe" in result
    assert "3x240" in result
    assert "\n" not in result
    assert "\xa0" not in result


def test_canonical_key_removes_cosmetic_separator_whitespace() -> None:
    assert canonical_key(" CXV 3 × 240 / XLPE ") == "cxv 3x240/xlpe"
    assert canonical_key("CVV 3 x 2.5 + 1 x 1.5") == "cvv 3x2.5+1x1.5"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234.567,89", 1_234_567.89),
        ("12,5%", 0.125),
        ("1,234", 1234.0),  # Vietnamese thousands separator.
        ("1.234", 1.234),  # A single dot is treated as decimal.
        (" 2 500 ", 2500.0),
        (42, 42.0),
    ],
)
def test_parse_number_handles_vietnamese_and_excel_values(raw: object, expected: float) -> None:
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "-", "+", ".", ",", "=SUM(A1:A2)", True, False])
def test_parse_number_rejects_non_numeric_cells(raw: object) -> None:
    assert parse_number(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("m", "m"),
        ("metre", "m"),
        ("pcs", "cái"),
        ("piece", "cái"),
        ("set", "bộ"),
        ("system", "hệ thống"),
    ],
)
def test_normalize_unit_aliases(raw: str, expected: str) -> None:
    assert normalize_unit(raw) == expected


def test_technical_attributes_extracts_cable_specification() -> None:
    attrs = technical_attributes(
        "Cáp ngầm trung thế 24KV-Cu/XLPE/PVC/DSTA/PVC-W-(3x240)MM2"
    )

    assert attrs["category"] == "cable"
    assert attrs["voltage"] == "24kV"
    assert attrs["conductor"] == "cu"
    assert attrs["insulation"] == "xlpe"
    assert attrs["armour"] == "dsta"
    assert attrs["cores"] == 3
    assert attrs["cross_section_mm2"] == pytest.approx(240)


def test_technical_attributes_prefers_outer_core_count_for_parenthesized_spec() -> None:
    """Regression for strings such as ``3x(1C-240)``.

    The inner ``1C`` describes a single conductor, while the outer ``3x`` is
    the number of cores/cables in the BOQ item.  Matching must not collapse
    this to one core.
    """

    attrs = technical_attributes("CXV 3x(1C-240)")

    assert attrs["category"] == "cable"
    assert attrs["cores"] == 3
    assert attrs["cross_section_mm2"] == pytest.approx(240)


def test_technical_attributes_extracts_pipe_diameter() -> None:
    attrs = technical_attributes("Ống HDPE D195/150")

    assert attrs["category"] == "pipe"
    assert attrs["material"] == "hdpe"
    assert attrs["diameter_mm"] == pytest.approx(195)


def test_technical_attributes_does_not_treat_cable_section_hyphen_as_voltage_pair() -> None:
    attrs = technical_attributes("CXV/CTS-W 1x10-7.2kV")

    assert attrs["voltage"] == "7.2kV"
    assert attrs["cores"] == 1
    assert attrs["cross_section_mm2"] == pytest.approx(10)


@pytest.mark.parametrize(
    "description",
    [
        "Cáp CXV 4x1C_10mm2 Từ DB-29 tới DB-28",
        "Cáp Cu/PVC 4x1C_6.0mm²",
        "Cáp CXV 3x(1C_2.5mm2)",
    ],
)
def test_technical_attributes_handles_underscore_and_nested_core_notation(
    description: str,
) -> None:
    attrs = technical_attributes(description)

    assert attrs["cores"] in {3, 4}
    assert attrs["cross_section_mm2"] in {2.5, 6.0, 10.0}


def test_technical_attributes_preserves_aluminium_family_tokens() -> None:
    assert technical_attributes("ASWA/CTS-W 3x240-24kV")["cable_family"] == "aswa"
    assert technical_attributes("ADATA/CTS-W 1x240-24kV")["cable_family"] == "adata"


def test_technical_attributes_uses_token_boundaries_for_short_abbreviations() -> None:
    attrs = technical_attributes(
        "Aluminum Busway 4P 2000A 50KA with a half earth integral"
    )

    assert "armour" not in attrs
    assert "conductor" not in attrs
    assert "category" not in attrs
