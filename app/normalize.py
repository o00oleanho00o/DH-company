from __future__ import annotations

import re
import unicodedata
from typing import Any


def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\n", " ")
    # Vietnamese Đ is not decomposed by Unicode NFKD; transliterate it
    # explicitly so header synonyms such as ĐVT/Đơn giá remain searchable.
    text = text.replace("đ", "d").replace("Đ", "D")
    text = strip_accents(text).lower()
    text = text.replace("×", "x").replace("–", "-").replace("—", "-")
    text = re.sub(r"[\u2018\u2019`']", "'", text)
    text = re.sub(r"[^a-z0-9%./+()\-#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_key(value: Any) -> str:
    text = normalize_text(value)
    # Keep engineering separators that carry meaning, while removing cosmetic
    # whitespace around them.
    text = re.sub(r"\s*([x/+_\-./])\s*", r"\1", text)
    return text


def normalize_unit(value: Any) -> str:
    unit = normalize_text(value)
    aliases = {
        "m": "m",
        "met": "m",
        "metre": "m",
        "metres": "m",
        "met": "m",
        "100m": "100m",
        "m2": "m2",
        "m3": "m3",
        "kg": "kg",
        "bo": "bộ",
        "bọ": "bộ",
        "cai": "cái",
        "cai.": "cái",
        "set": "bộ",
        "lot": "lô",
        "lo": "lô",
        "system": "hệ thống",
        "unit": "cái",
        "pcs": "cái",
        "piece": "cái",
    }
    return aliases.get(unit, unit)


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    if not text or text.startswith("="):
        return None
    text = text.replace("\u00a0", "").replace(" ", "")
    # Percentage strings are represented as fractions for discount logic.
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    text = re.sub(r"[^0-9,.\-+]", "", text)
    if not text or text in {"-", "+", ".", ","}:
        return None
    try:
        if "," in text and "." in text:
            # Vietnamese number format: 1.234.567,89. Choose the last
            # separator as decimal only when it has <=2 trailing digits.
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            tail = text.rsplit(",", 1)[1]
            text = text.replace(",", ".") if len(tail) <= 2 else text.replace(",", "")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        number = float(text)
        return number / 100 if percent else number
    except ValueError:
        return None


def _has_token(text: str, token: str) -> bool:
    """Match an engineering abbreviation without matching inside words."""

    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None


def technical_attributes(description: str) -> dict[str, Any]:
    """Extract high-value M&E attributes without pretending to be an LLM."""

    text = normalize_text(description)
    attrs: dict[str, Any] = {}
    family_patterns = (
        "adsta", "adata", "aswa", "dsta", "data", "cxv", "cvv", "cv", "swa",
        "cts", "cws", "fsn", "frn", "dvv", "axv", "acsr", "abc", "vctfk",
        "vctf", "vcmd",
    )
    # Preserve fire-retardant/armoured variants as part of the signature:
    # `FSN-CXV` must not be treated as the same product family as plain `CXV`.
    variant = re.search(
        r"(?<![a-z])((?:fsn|frn)[/-](?:cxv|cvv|cv|dsta|data|swa|axv|abc))(?![a-z])",
        text,
    )
    if variant:
        attrs["cable_family"] = variant.group(1).replace("/", "-")
    else:
        # Longest-first prevents `cv` from winning inside `cxv`/`cvv`.
        for family in sorted(family_patterns, key=len, reverse=True):
            if _has_token(text, family):
                attrs["cable_family"] = family
                break
    # A hyphen separates a cable section from its voltage in supplier tables
    # (``1x10-7.2kV``). Only slash notation denotes a voltage pair.
    voltage = re.search(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*k\s*v\b",
        text,
    )
    if voltage:
        attrs["voltage"] = f"{voltage.group(1)}/{voltage.group(2)}kV"
    else:
        voltage2 = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*kv", text)
        if voltage2:
            attrs["voltage"] = f"{voltage2.group(1)}kV"
    if _has_token(text, "lv") or "ha the" in text or "ha the" in text:
        attrs["voltage_class"] = "lv"
    elif (
        _has_token(text, "mv")
        or "trung the" in text
        or "trung the" in text
    ):
        attrs["voltage_class"] = "mv"
    for token, field in [
        ("xlpe", "insulation"),
        ("pvc", "sheath_or_insulation"),
        ("swa", "armour"),
        ("dsta", "armour"),
        ("cts", "armour"),
        ("cws", "armour"),
        ("cu", "conductor"),
        ("copper", "conductor"),
        ("al", "conductor"),
        ("nhom", "conductor"),
        ("hdpe", "material"),
        ("upvc", "material"),
        ("upvc", "material"),
    ]:
        if _has_token(text, token) and field not in attrs:
            attrs[field] = token
    family = attrs.get("cable_family")
    base_family = str(family).split("-")[-1] if family else ""
    if base_family in {"cxv", "axv"}:
        attrs.setdefault("insulation", "xlpe")
    elif base_family in {"cv", "cvv", "vctf", "vctfk", "vcmd"}:
        attrs.setdefault("insulation", "pvc")
    if base_family in {"dsta", "adsta"}:
        attrs.setdefault("armour", "dsta")
    if base_family in {"swa", "aswa"}:
        attrs.setdefault("armour", "swa")
    if base_family in {
        "cxv",
        "cv",
        "cvv",
        "dsta",
        "data",
        "axv",
        "adsta",
        "adata",
        "aswa",
        "abc",
        "acsr",
    }:
        attrs.setdefault("category", "cable")
    if _has_token(text, "dsta"):
        attrs["armour"] = "dsta"
    if _has_token(text, "swa"):
        attrs["armour"] = "swa"
    # Core / cross-section patterns: 3x240, 3c-240, 1c 120,
    # 12x1c-240 and the common parenthesized form 3x(1C-240). Match the
    # outer multiplier first so the inner "1C" is not mistaken for the core
    # count.
    nested = re.search(
        r"(?<![a-z0-9])(\d+)\s*x\s*\(\s*(\d+)\s*(?:x|c)"
        r"\s*[-x_]?\s*(\d+(?:[.,]\d+)?)\s*(?:mm(?:2|²))?\s*\)",
        text,
    )
    core = re.search(
        r"(?<![a-z0-9])(\d+)\s*x\s*\(\s*1\s*c"
        r"(?:\s*[-x_]\s*|\s+)(\d+(?:[.,]\d+)?)"
        r"\s*(?:mm(?:2|²))?\s*\)",
        text,
    )
    parallel_runs = None
    nested_parsed = False
    if nested:
        outer, inner, section = nested.groups()
        outer_count = int(outer)
        inner_count = int(inner)
        attrs["cores"] = outer_count if inner_count == 1 else inner_count
        attrs["cross_section_mm2"] = float(section.replace(",", "."))
        attrs["parallel_runs"] = outer_count
        attrs["base_cores"] = 1 if inner_count == 1 else inner_count
        # The nested expression is already fully parsed; do not let the
        # simpler patterns below overwrite it with the inner conductor.
        nested_parsed = True
        core = None
    if not nested_parsed and not core:
        core = re.search(
            r"(?<![a-z0-9])(\d+)\s*x\s*1\s*c"
            r"(?:\s*[-x_]\s*|\s+)(\d+(?:[.,]\d+)?)",
            text,
        )
    if not nested_parsed and core:
        parallel_runs = int(core.group(1))
    if not nested_parsed and not core:
        core = re.search(
            r"(?<![a-z0-9])(\d+)\s*(?:x|c)\s*[-x_]?\s*(\d+(?:[.,]\d+)?)\s*(?:mm(?:2|²))?",
            text,
        )
    if not nested_parsed and core:
        attrs["cores"] = int(core.group(1))
        attrs["cross_section_mm2"] = float(core.group(2).replace(",", "."))
        if parallel_runs and parallel_runs > 1:
            # Preserve both readings: the outer count is useful for displaying
            # the BOQ notation, while base_cores lets matching compare against
            # a single-core catalog item deterministically.
            attrs["parallel_runs"] = parallel_runs
            attrs["base_cores"] = 1
    else:
        section = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*mm(?:2|²)", text)
        if section:
            attrs["cross_section_mm2"] = float(section.group(1).replace(",", "."))
    diameter = re.search(
        r"(?<![a-z0-9])(?:d|dn|phi|ø)\s*([0-9]+(?:[.,][0-9]+)?)",
        text,
    )
    if diameter:
        attrs["diameter_mm"] = float(diameter.group(1).replace(",", "."))
        # Pipe/conduit specifications often carry two diameters (outer/inner).
        after_d = text[diameter.end():]
        second = re.search(r"[/x-]\s*(?:d|dn)?\s*([0-9]+(?:[.,][0-9]+)?)", after_d)
        if second:
            attrs["diameters_mm"] = [
                attrs["diameter_mm"],
                float(second.group(1).replace(",", ".")),
            ]
    # Broad item families are intentionally conservative.  An explicit cable
    # marker must win over route/location prose (e.g. "Cáp Cu/PVC ... từ đèn
    # đến công tắc"), while tray/support terms must stay separate from cable.
    support_signal = any(
        term in text
        for term in ("thang cap", "mang cap", "khay cap", "cable tray", "cable ladder")
    )
    explicit_cable = bool(
        attrs.get("cable_family")
        or _has_token(text, "cable")
        or _has_token(text, "cap")
        or (
            re.search(r"\b\d+\s*(?:x|c)\s*[-x_]?\s*\d+(?:[.,]\d+)?", text)
            and any(
                _has_token(text, token)
                for token in ("cu", "al", "xlpe", "pvc", "dsta", "swa")
            )
        )
        or (
            re.search(r"\b\d+(?:[.,]\d+)?\s*mm(?:2|²)\b", text)
            and any(_has_token(text, token) for token in ("xlpe", "pvc", "cu", "al"))
        )
    )
    if support_signal:
        attrs["category"] = "cable_support"
    elif explicit_cable:
        attrs["category"] = "cable"
    elif any(term in text for term in ("tiep dia", "coc tiep dia", "thanh tiep dia", "day dong tran")):
        attrs.setdefault("category", "earthing")
    elif "tu dien" in text:
        attrs.setdefault("category", "panel")
    elif any(term in text for term in ("den ", "den pha", "tru den", "can den", "led")):
        attrs.setdefault("category", "lighting")
    elif any(term in text for term in ("quat ", "quat hut", "quat tran", "bom ", "may bien ap")):
        attrs.setdefault("category", "equipment")
    elif any(term in text for term in ("dao lap dat", "dao lap", "ho ga", "mong tru", "mong cot")):
        attrs.setdefault("category", "construction")
    elif any(_has_token(text, x) for x in ("hdpe", "upvc", "ong")):
        attrs.setdefault("category", "pipe")
    if attrs.get("category") != "cable":
        # Rectangular tray/civil dimensions (500x200x1.5, 1200x1200) are not
        # conductor core specifications. Keep them out of cable matching.
        for key in ("cores", "cross_section_mm2", "parallel_runs", "base_cores"):
            attrs.pop(key, None)
    return attrs
