from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import chatbot
from app.chatbot import _direct_execution_intent


ROOT = Path(__file__).resolve().parents[1]


def test_chatbot_workspace_uses_one_shared_panel() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    chatbot_js = (ROOT / "app/static/chatbot.js").read_text(encoding="utf-8")

    assert html.count('id="chatbot-panel"') == 1
    assert 'data-route="chatbot"' in html
    assert 'id="chatbot-expand"' in html
    assert "?v=20260908-chat-downloads" in html
    assert 'id="chatbot-page-host"' in app_js
    assert 'detail: { mode: "workspace" }' in app_js
    assert 'state.route === "chatbot" || route.base !== "chatbot"' in app_js
    assert "host.appendChild(el.panel)" in chatbot_js
    assert "el.widget.appendChild(el.panel)" in chatbot_js


def test_chatbot_route_is_registered_and_labelled() -> None:
    app_js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")

    assert 'chatbot: "Trợ Lý AI"' in app_js
    assert '"benchmark", "chatbot"' in app_js
    assert 'route.base === "chatbot"' in app_js


def test_chatbot_download_contract_and_status_semantics() -> None:
    chatbot_js = (ROOT / "app/static/chatbot.js").read_text(encoding="utf-8")
    main_py = (ROOT / "app/main.py").read_text(encoding="utf-8")
    chatbot_py = (ROOT / "app/chatbot.py").read_text(encoding="utf-8")

    assert '"downloads": downloads' in main_py
    assert "appendDownloads(data.downloads)" in chatbot_js
    assert "link.download = filename" in chatbot_js
    assert '"status_counts": status_counts' in chatbot_py
    assert '"unresolved": unresolved' in chatbot_py


def test_direct_command_is_confirmation_but_question_stays_preview() -> None:
    assert _direct_execution_intent("Xuất Excel đi", "export_quotation_file", {"project_id": 10})
    assert _direct_execution_intent("Áp giá đi", "run_quotation_pricing", {"project_id": 10})
    assert not _direct_execution_intent("Có thể xuất Excel không?", "export_quotation_file", {"project_id": 10})
    assert not _direct_execution_intent("Cho tôi xem trước thao tác xuất Excel", "export_quotation_file", {"project_id": 10})
    assert not _direct_execution_intent("Xuất Excel đi", "run_quotation_pricing", {"project_id": 10})


@pytest.mark.parametrize("content", [
    "Đừng áp giá", "Không xuất Excel", "Áp giá là gì?", "Xuất Excel có mất công thức không?",
    'Ví dụ: "Xuất Excel"', "Nếu đúng thì áp giá", "Chưa áp giá", "pricing",
    "Xuất Excel nhưng đừng chạy pricing", "Hướng dẫn nhập dữ liệu",
])
def test_non_commands_never_override_preview(content: str) -> None:
    for tool in ("export_quotation_file", "run_quotation_pricing", "import_workbook_upload"):
        assert not _direct_execution_intent(content, tool, {"project_id": 10})


def test_short_command_does_not_authorize_force() -> None:
    assert not _direct_execution_intent("Áp giá đi", "run_quotation_pricing", {"force": True})


@pytest.mark.parametrize("content,tool_name,expected_calls", [
    ("Xuất Excel", "export_quotation_file", 1),
    ("Áp giá đi", "run_quotation_pricing", 1),
    ("Đừng xuất Excel", "export_quotation_file", 0),
    ("Áp giá là gì?", "run_quotation_pricing", 0),
])
def test_tool_loop_executes_direct_command_once(monkeypatch, content, tool_name, expected_calls):
    monkeypatch.setattr(chatbot, "settings", SimpleNamespace(enable_chatbot=True, chatbot_model="fake", chatbot_api_key=""))
    operation = Mock(return_value={"status": "completed"})

    def handler(arguments):
        guard = chatbot._confirm_guard(arguments, "test action")
        return guard if guard is not None else operation()

    monkeypatch.setitem(chatbot._TOOL_HANDLERS, tool_name, handler)
    tool_call = SimpleNamespace(id="call-1", function=SimpleNamespace(
        name=tool_name, arguments='{"project_id":10,"confirm":false}'
    ))
    client = Mock()
    client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Kết quả", tool_calls=None))]),
    ]
    reply, downloads = chatbot.generate_reply_with_metadata([
        {"role": "assistant", "content": "Đang thao tác báo giá #10."},
        {"role": "user", "content": content},
    ], client=client)
    assert reply == "Kết quả"
    assert downloads == []
    assert operation.call_count == expected_calls
