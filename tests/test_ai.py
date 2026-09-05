from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.ai import (
    AIBudgetExceeded,
    AIProvider,
    safe_classify,
    safe_classify_sync,
    safe_rerank,
    safe_rerank_sync,
)


class FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"fake status {self.status_code}")


class FakeClient:
    def __init__(self, content: str, *, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        **kwargs: Any,
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json, "kwargs": kwargs})
        return FakeResponse(
            {
                "id": "fake-request-1",
                "model": "fake-model",
                "choices": [{"message": {"content": self.content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
            self.status_code,
        )


def _provider(client: FakeClient, **kwargs: Any) -> AIProvider:
    return AIProvider(
        base_url="https://gateway.invalid/v1",
        api_key="test-secret-key",
        model="fake-model",
        enabled=True,
        client=client,
        max_calls=kwargs.pop("max_calls", 5),
        max_retries=kwargs.pop("max_retries", 0),
        max_candidates=kwargs.pop("max_candidates", 3),
        **kwargs,
    )


def _candidates() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "name": "Cáp CXV 1x240 mm2",
            "unit": "m",
            "score": 0.812,
            "technical_attributes": {"category": "cable", "cross_section_mm2": 240},
            "price": 123456789,
            "source": {"filename": "private.xlsx", "row": 22},
        },
        {
            "id": 2,
            "name": "Cáp CXV 1x240 mm2 Cu",
            "unit": "m",
            "score": 0.811,
            "technical_attributes": {"category": "cable", "cross_section_mm2": 240},
            "price": 999999999,
            "source": {"filename": "private.xlsx", "row": 23},
        },
    ]


def test_safe_rerank_disabled_is_deterministic_and_does_not_call() -> None:
    client = FakeClient(
        '{"ranked_candidate_ids":[2,1],"needs_review":false,"explanation":"model"}'
    )
    provider = AIProvider(
        base_url="https://gateway.invalid/v1",
        api_key="test-secret-key",
        model="fake-model",
        enabled=False,
        client=client,
    )

    result = asyncio.run(
        safe_rerank(
            "Cáp CXV 1x240 mm2",
            {"category": "cable"},
            _candidates(),
            provider=provider,
        )
    )

    assert [candidate["id"] for candidate in result.candidates] == [1, 2]
    assert result.applied is False
    assert result.reason == "disabled"
    assert client.calls == []


def test_rerank_sends_only_safe_fields_and_validates_supplied_ids() -> None:
    client = FakeClient(
        '{"ranked_candidate_ids":[2,1],"needs_review":false,"explanation":"Thông số phù hợp."}'
    )
    provider = _provider(client)
    candidates = _candidates()
    candidates[0]["name"] = "api_key=sk-do-not-send-this Cáp CXV 1x240 mm2"
    candidates[0]["technical_attributes"]["price"] = 777
    candidates[0]["technical_attributes"]["source_file"] = "secret.xlsx"

    result = asyncio.run(
        safe_rerank(
            "Cáp CXV 1x240 mm2 api_key=sk-another-secret",
            {"category": "cable"},
            candidates,
            provider=provider,
        )
    )

    assert result.applied is True
    assert result.needs_review is False
    assert [candidate["id"] for candidate in result.candidates] == [2, 1]
    assert provider.calls_made == 1
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["url"].endswith("/chat/completions")
    assert request["headers"]["Authorization"] == "Bearer test-secret-key"
    user_payload = json.loads(request["json"]["messages"][1]["content"].split(
        "\nReturn JSON matching this shape:", 1
    )[0])
    assert all("price" not in candidate for candidate in user_payload["candidates"])
    assert all("source" not in candidate for candidate in user_payload["candidates"])
    serialized_user_prompt = json.dumps(user_payload, ensure_ascii=False)
    assert "sk-another-secret" not in serialized_user_prompt
    assert "sk-do-not-send-this" not in serialized_user_prompt
    assert "secret.xlsx" not in serialized_user_prompt
    assert '"price"' not in serialized_user_prompt
    # The caller's rich candidate fields are retained in the result, but the
    # model was never allowed to inspect or change them.
    assert result.candidates[0]["price"] == 999999999


def test_invalid_model_id_falls_back_and_marks_review() -> None:
    client = FakeClient(
        '{"ranked_candidate_ids":[999],"needs_review":false,"explanation":"bad"}'
    )
    provider = _provider(client)

    result = asyncio.run(
        safe_rerank(
            "Cáp CXV 1x240 mm2",
            {"category": "cable"},
            _candidates(),
            provider=provider,
        )
    )

    assert result.applied is False
    assert result.needs_review is True
    assert [candidate["id"] for candidate in result.candidates] == [1, 2]
    assert result.reason == "provider_error:AIProtocolError"


def test_call_budget_is_enforced_without_network_or_retry_fanout() -> None:
    client = FakeClient(
        '{"ranked_candidate_ids":[2,1],"needs_review":false,"explanation":"model"}'
    )
    provider = _provider(client, max_calls=0)

    result = asyncio.run(
        safe_rerank(
            "Cáp CXV 1x240 mm2",
            {"category": "cable"},
            _candidates(),
            provider=provider,
        )
    )

    assert result.reason == "budget_exhausted"
    assert result.needs_review is True
    assert result.applied is False
    assert client.calls == []
    with pytest.raises(AIBudgetExceeded):
        provider._reserve_call()


def test_safe_rerank_skips_non_ambiguous_candidates() -> None:
    client = FakeClient(
        '{"ranked_candidate_ids":[2,1],"needs_review":false,"explanation":"model"}'
    )
    provider = _provider(client)
    candidates = _candidates()
    candidates[0]["score"] = 0.99
    candidates[1]["score"] = 0.70

    result = asyncio.run(
        safe_rerank(
            "Cáp CXV 1x240 mm2",
            {"category": "cable"},
            candidates,
            provider=provider,
        )
    )

    assert result.reason == "deterministic_margin"
    assert result.applied is False
    assert result.needs_review is False
    assert client.calls == []


def test_classification_is_limited_to_allowed_labels_and_confidence() -> None:
    client = FakeClient(
        '{"label":"NEW_BOQ","confidence":0.91,"needs_review":false,"explanation":"Rõ ràng."}'
    )
    provider = _provider(client)

    result = asyncio.run(
        safe_classify(
            "Bảng khối lượng cần báo giá mới",
            ["NEW_BOQ", "PRICE_LIST", "LABOR_MASTER"],
            provider=provider,
        )
    )

    assert result.label == "NEW_BOQ"
    assert result.confidence == pytest.approx(0.91)
    assert result.applied is True
    assert result.needs_review is False


def test_classification_rejects_unknown_label_and_safe_fallback() -> None:
    client = FakeClient(
        '{"label":"INVENTED","confidence":1.0,"needs_review":false,"explanation":"bad"}'
    )
    provider = _provider(client)

    result = asyncio.run(
        safe_classify(
            "Không rõ loại bảng",
            ["NEW_BOQ", "PRICE_LIST"],
            provider=provider,
        )
    )

    assert result.label is None
    assert result.applied is False
    assert result.needs_review is True
    assert result.reason == "provider_error:AIProtocolError"


def test_provider_accepts_fenced_json_from_compatible_gateway() -> None:
    client = FakeClient(
        '```json\n{"ranked_candidate_ids":[2,1],"needs_review":false,"explanation":"ok"}\n```'
    )
    provider = _provider(client)

    result = asyncio.run(
        safe_rerank(
            "Cáp CXV 1x240 mm2",
            {"category": "cable"},
            _candidates(),
            provider=provider,
        )
    )

    assert result.applied is True
    assert [candidate["id"] for candidate in result.candidates] == [2, 1]


def test_sync_wrappers_work_for_callers_in_sync_pricing_pipeline() -> None:
    client = FakeClient(
        '{"ranked_candidate_ids":[2,1],"needs_review":false,"explanation":"model"}'
    )
    provider = _provider(client)

    rerank = safe_rerank_sync(
        "Cáp CXV 1x240 mm2",
        {"category": "cable"},
        _candidates(),
        provider=provider,
    )
    assert [candidate["id"] for candidate in rerank.candidates] == [2, 1]

    classifier_client = FakeClient(
        '{"label":"PRICE_LIST","confidence":0.9,"needs_review":false,"explanation":"model"}'
    )
    classifier = safe_classify_sync(
        "Bảng giá vật tư",
        ["PRICE_LIST", "NEW_BOQ"],
        provider=_provider(classifier_client),
    )
    assert classifier.label == "PRICE_LIST"
