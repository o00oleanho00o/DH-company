from __future__ import annotations

import json
from typing import Any

from app import db, pricing
from app.ai import AIResponse, RerankResult
from app.pricing import Candidate, run_pricing


class StubProvider:
    """Configured provider metadata without any network client."""

    configured = True
    model = "stub-model"
    max_calls = 5
    max_candidates = 2
    rerank_margin = 0.05
    calls_made = 1
    remaining_calls = 4

    def reset_budget(self) -> None:
        return None


def _candidate(entity_id: int, name: str, family: str, score: float) -> Candidate:
    return Candidate(
        entity_id=entity_id,
        name=name,
        code=None,
        unit="m",
        brand=None,
        origin=None,
        attrs={
            "category": "cable",
            "cable_family": family,
            "cores": 1,
            "base_cores": 1,
            "cross_section_mm2": 240.0,
        },
        score=score,
        components={"technical_attributes": 1.0},
        explanation="deterministic",
    )


def test_run_pricing_uses_llm_only_for_ambiguous_candidates_and_persists_usage(
    isolated_db,
    monkeypatch,
) -> None:
    project_id: int
    with db.db_session() as conn:
        project_cur = conn.execute(
            """
            INSERT INTO projects(project_name, quotation_date, metadata_json, created_at)
            VALUES (?, ?, '{}', datetime('now'))
            """,
            ("llm-integration-test", "2026-01-01"),
        )
        project_id = int(project_cur.lastrowid)
        conn.execute(
            """
            INSERT INTO products(
                id, canonical_key, normalized_name, product_code, category,
                unit, technical_attributes_json, created_at
            ) VALUES
                (101, 'cap-cxv-1x240-mm2', 'Cáp CXV 1x240 mm2', NULL, 'cable',
                 'm', '{"category":"cable","cable_family":"cxv","cores":1,"cross_section_mm2":240}', datetime('now')),
                (102, 'cap-cvv-1x240-mm2', 'Cáp CVV 1x240 mm2', NULL, 'cable',
                 'm', '{"category":"cable","cable_family":"cvv","cores":1,"cross_section_mm2":240}', datetime('now'))
            """
        )
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit,
                quantity, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', datetime('now'))
            """,
            (
                project_id,
                "Cáp CXV 1x240 mm2",
                "cap cxv 1x240 mm2",
                "m",
                2,
            ),
        )

    first = _candidate(101, "Cáp CXV 1x240 mm2", "cxv", 0.812)
    second = _candidate(102, "Cáp CVV 1x240 mm2", "cvv", 0.811)
    rerank_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(pricing, "find_product_candidates", lambda *args, **kwargs: [first, second])
    monkeypatch.setattr(pricing, "find_labor_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        pricing,
        "choose_product_price",
        lambda conn, product_id, quotation_date=None: (
            (222.0, {"type": "test", "product_id": int(product_id)})
            if int(product_id) == 102
            else (111.0, {"type": "test", "product_id": int(product_id)})
        ),
    )
    monkeypatch.setattr(pricing, "choose_labor_rate", lambda *args, **kwargs: (None, {}))

    def fake_rerank(
        description: str,
        attrs: dict[str, Any],
        candidates: list[Candidate],
        **kwargs: Any,
    ) -> RerankResult:
        rerank_calls.append(
            {
                "description": description,
                "attrs": attrs,
                "candidates": candidates,
                "kwargs": kwargs,
            }
        )
        return RerankResult(
            candidates=[{"id": 102}, {"id": 101}],
            applied=True,
            needs_review=False,
            explanation="stub rerank",
            reason="model_rerank",
            response=AIResponse(
                content='{"ranked_candidate_ids":[102,101],"needs_review":false}',
                model="stub-model",
                usage={"prompt_tokens": 12, "completion_tokens": 4},
                latency_ms=1.5,
            ),
        )

    monkeypatch.setattr(pricing, "safe_rerank_sync", fake_rerank)
    result = run_pricing(
        project_id,
        policy={
            "llm_enabled": True,
            "llm_rerank_margin": 0.05,
            "llm_max_candidates": 2,
        },
        llm_provider=StubProvider(),
    )

    assert result["metrics"]["llm_enabled"] is True
    assert result["metrics"]["llm_calls"] == 1
    assert result["metrics"]["llm_rerank_attempts"] == 1
    assert result["metrics"]["llm_rerank_applied"] == 1
    assert result["metrics"]["llm_rerank_skipped"] == 1  # labor has no ambiguity
    assert len(rerank_calls) == 1
    assert rerank_calls[0]["kwargs"]["max_candidates"] == 2
    assert all(not hasattr(candidate, "price") for candidate in rerank_calls[0]["candidates"])

    with db.db_session() as conn:
        row = conn.execute(
            "SELECT * FROM boq_items WHERE project_id=?", (project_id,)
        ).fetchone()
        usage_row = conn.execute(
            "SELECT model_usage_json FROM pricing_runs WHERE id=?", (result["run_id"],)
        ).fetchone()

    assert row["matched_product_id"] == 102
    assert row["material_price"] == 222.0
    alternatives = json.loads(row["alternatives_json"])
    assert [candidate["id"] for candidate in alternatives["material"]] == [102, 101]
    usage = json.loads(usage_row["model_usage_json"])
    assert usage["model"] == "stub-model"
    assert usage["tokens"] == {"prompt_tokens": 12, "completion_tokens": 4}
    assert "api_key" not in json.dumps(usage)
    assert "price" not in json.dumps(usage)
