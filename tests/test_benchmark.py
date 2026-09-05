from __future__ import annotations

from pathlib import Path

from benchmarks.holdout import (
    _price_equal,
    _resolve_holdout,
    _technical_match_quality,
)


def test_benchmark_price_tolerance_is_small_and_deterministic() -> None:
    assert _price_equal(1000, 1000)
    assert _price_equal(1000, 1004.9)
    assert not _price_equal(1000, 1010)
    assert not _price_equal(None, 1000)


def test_benchmark_match_quality_distinguishes_name_and_hard_conflict() -> None:
    attrs = {
        "category": "cable",
        "cores": 3,
        "cross_section_mm2": 240.0,
        "voltage": "24kV",
        "armour": "dsta",
    }
    assert (
        _technical_match_quality(
            "Cáp ngầm 24kV Cu/XLPE/DSTA 3x240",
            "cap ngam 24kv cu/xlpe/dsta 3x240",
            attrs,
        )
        is True
    )
    assert (
        _technical_match_quality(
            "Cáp ngầm 12kV Cu/XLPE/DSTA 3x240",
            "cap ngam 24kv cu/xlpe/dsta 3x240",
            attrs,
        )
        is False
    )


def test_holdout_selector_accepts_unique_prefix(tmp_path: Path) -> None:
    files = [
        tmp_path / "price.xlsx",
        tmp_path / "BOQ-HỆ THỐNG.xlsx",
    ]
    for path in files:
        path.touch()
    assert _resolve_holdout(files, "BOQ-") == files[1]
