"""Explicit, deterministic supplier-price policy helpers.

Supplier workbooks frequently publish a base list price, a VAT-inclusive
amount and several discount tiers.  The workbook alone does not tell us which
commercial tier applies to a customer, so ingestion preserves every
observation and uses a rule only when the business explicitly configures one.

The optional ``MANUAL_PRICING_RULES_JSON`` environment setting is intentionally
small and auditable.  A rule never invents a source: it either selects a
discount observation present in the workbook or records a clearly labelled
calculation derived from the source list/VAT amount.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .normalize import normalize_text


def _normalized_label(value: Any, default: str) -> str:
    """Return a stable lowercase token for tax/basis labels."""

    text = normalize_text(value if value not in (None, "") else default)
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def normalize_tax_mode(value: Any, *, default: str = "ex_vat") -> str:
    """Normalize a tax label to ``ex_vat``/``inc_vat`` where recognizable."""

    token = _normalized_label(value, default)
    if token in {
        "ex_vat",
        "exvat",
        "exclusive_vat",
        "excluding_vat",
        "exclude_vat",
        "without_vat",
        "before_vat",
        "net",
        "net_price",
        "net_amount",
    }:
        return "ex_vat"
    if token in {
        "inc_vat",
        "incvat",
        "inclusive_vat",
        "including_vat",
        "include_vat",
        "with_vat",
        "gross",
        "gross_price",
        "gross_amount",
        "vat",
        "vat_price",
    }:
        return "inc_vat"
    return token or default


def normalize_price_basis(value: Any, *, default: str = "net") -> str:
    """Normalize a price-basis label to ``net``/``gross`` where recognizable."""

    token = _normalized_label(value, default)
    if token in {
        "net",
        "net_price",
        "net_amount",
        "ex_vat",
        "exvat",
        "exclusive_vat",
        "excluding_vat",
    }:
        return "net"
    if token in {
        "gross",
        "gross_price",
        "gross_amount",
        "inc_vat",
        "incvat",
        "inclusive_vat",
        "including_vat",
        "vat",
        "vat_price",
    }:
        return "gross"
    return token or default


def normalize_discount_rate(value: Any) -> float | None:
    """Normalize ``10``, ``"10%"`` and ``0.10`` to a fraction."""

    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", ".")
            if text.endswith("%"):
                return max(0.0, min(1.0, float(text[:-1]) / 100.0))
            number = float(text)
        else:
            number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number /= 100.0
    if 0.0 <= number <= 1.0:
        return round(number, 8)
    return None


def _date_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return text


@dataclass(frozen=True)
class ManualPricingRule:
    """A business-owned pricing override with explicit provenance."""

    rule_id: str
    supplier: str | None = None
    category: str | None = None
    product_family: str | None = None
    discount_rate: float | None = None
    tax_mode: str = "ex_vat"
    effective_from: str | None = None
    effective_to: str | None = None
    priority: int = 0
    provenance: dict[str, Any] | None = None

    @property
    def specificity(self) -> int:
        return sum(
            value not in (None, "")
            for value in (self.supplier, self.category, self.product_family)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "supplier": self.supplier,
            "category": self.category,
            "product_family": self.product_family,
            "discount_rate": self.discount_rate,
            "tax_mode": self.tax_mode,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "priority": self.priority,
            "provenance": self.provenance or {},
        }


def _rule_id(raw: dict[str, Any], index: int, source: str) -> str:
    explicit = str(raw.get("id") or raw.get("rule_id") or "").strip()
    if explicit:
        return explicit
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{source}:{index}:{digest}"


def _coerce_rule(raw: Any, index: int, source: str) -> ManualPricingRule | None:
    if not isinstance(raw, dict):
        return None
    discount = normalize_discount_rate(
        raw.get("discount_rate", raw.get("discount", raw.get("rate")))
    )
    try:
        priority = int(raw.get("priority", 0) or 0)
    except (TypeError, ValueError):
        priority = 0
    tax_mode = normalize_tax_mode(raw.get("tax_mode") or "ex_vat")
    if tax_mode not in {"ex_vat", "inc_vat"}:
        tax_mode = "ex_vat"
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {
            "source_type": "MANUAL_PRICING_RULE",
            "source": source,
            "config_key": "MANUAL_PRICING_RULES_JSON",
        }
    return ManualPricingRule(
        rule_id=_rule_id(raw, index, source),
        supplier=str(raw.get("supplier") or "").strip() or None,
        category=normalize_text(raw.get("category")) or None,
        product_family=normalize_text(
            raw.get("product_family", raw.get("family", ""))
        )
        or None,
        discount_rate=discount,
        tax_mode=tax_mode,
        effective_from=_date_value(raw.get("effective_from")),
        effective_to=_date_value(raw.get("effective_to")),
        priority=priority,
        provenance=provenance,
    )


def load_manual_pricing_rules(raw: str | None = None) -> list[ManualPricingRule]:
    """Load rules from JSON without failing application startup."""

    text = raw if raw is not None else os.getenv("MANUAL_PRICING_RULES_JSON", "")
    if not text:
        return []
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("rules", [payload])
    if not isinstance(payload, list):
        return []
    rules = [
        rule
        for index, item in enumerate(payload)
        if (rule := _coerce_rule(item, index, "environment")) is not None
    ]
    return rules


def _matches(value: str | None, expected: str | None) -> bool:
    if expected in (None, ""):
        return True
    if value in (None, ""):
        return False
    return normalize_text(value) == normalize_text(expected)


def rule_matches(
    rule: ManualPricingRule,
    *,
    supplier: str | None,
    category: str | None,
    product_family: str | None,
    effective_date: str | None,
) -> bool:
    if not _matches(supplier, rule.supplier):
        return False
    if not _matches(category, rule.category):
        return False
    if not _matches(product_family, rule.product_family):
        return False
    current = _date_value(effective_date)
    if current and rule.effective_from and current < rule.effective_from:
        return False
    if current and rule.effective_to and current > rule.effective_to:
        return False
    return True


def select_manual_pricing_rule(
    rules: Iterable[ManualPricingRule],
    *,
    supplier: str | None,
    category: str | None,
    product_family: str | None,
    effective_date: str | None,
) -> ManualPricingRule | None:
    """Choose the most specific/highest-priority applicable rule."""

    matches = [
        rule
        for rule in rules
        if rule_matches(
            rule,
            supplier=supplier,
            category=category,
            product_family=product_family,
            effective_date=effective_date,
        )
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda rule: (rule.specificity, rule.priority, rule.rule_id),
        reverse=True,
    )[0]


def find_observation(
    observations: Iterable[dict[str, Any]],
    *,
    tax_mode: str,
    discount_rate: float | None,
) -> dict[str, Any] | None:
    """Find a source observation matching tax basis and discount tier."""

    desired = normalize_discount_rate(discount_rate)
    tax_mode = normalize_tax_mode(tax_mode or "ex_vat")
    for observation in observations:
        if normalize_tax_mode(observation.get("tax_mode")) != tax_mode:
            continue
        observed = normalize_discount_rate(observation.get("discount_rate"))
        if desired is None and observed in (None, 0.0):
            return observation
        if desired is not None and observed is not None and abs(desired - observed) <= 1e-6:
            return observation
    return None


def select_price_observation(
    observations: list[dict[str, Any]],
    *,
    rule: ManualPricingRule | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return selected source observation and an explainable calculation."""

    if not observations:
        return None, {"selection": "no_observation"}
    if rule is None:
        selected = find_observation(
            observations,
            tax_mode="ex_vat",
            discount_rate=0.0,
        )
        return selected, {
            "selection": "base_list",
            "reason": "No manual pricing rule; discount tiers retained but not assumed.",
        }
    desired = rule.discount_rate
    selected = find_observation(
        observations,
        tax_mode=rule.tax_mode,
        discount_rate=desired,
    )
    if selected is not None:
        return selected, {
            "selection": "manual_pricing_rule_observation",
            "rule": rule.as_dict(),
            "calculation": "selected_source_observation",
        }
    # A rule may intentionally target a discount not published in this
    # workbook.  Derive it from the corresponding base amount while clearly
    # recording that this is a calculation, never a source quote.
    base = find_observation(
        observations,
        tax_mode=rule.tax_mode,
        discount_rate=0.0,
    )
    if base is None or desired is None:
        return None, {
            "selection": "manual_pricing_rule_unusable",
            "rule": rule.as_dict(),
            "reason": "No matching tax-basis base observation.",
        }
    amount = float(base["amount"]) * (1.0 - desired)
    calculated = {
        **base,
        "price_type": "supplier_discounted_calculated",
        "amount": round(amount, 8),
        "discount_rate": desired,
        "calculated": True,
    }
    return calculated, {
        "selection": "manual_pricing_rule_calculated",
        "rule": rule.as_dict(),
        "calculation": "base_amount*(1-discount_rate)",
        "base_observation": base,
    }
