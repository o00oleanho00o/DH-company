from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chatbot_workspace_uses_one_shared_panel() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    chatbot_js = (ROOT / "app/static/chatbot.js").read_text(encoding="utf-8")

    assert html.count('id="chatbot-panel"') == 1
    assert 'data-route="chatbot"' in html
    assert 'id="chatbot-expand"' in html
    assert 'id="chatbot-page-host"' in app_js
    assert 'detail: { mode: "workspace" }' in app_js
    assert 'state.route === "chatbot" || route.base !== "chatbot"' in app_js
    assert "host.appendChild(el.panel)" in chatbot_js
    assert "el.widget.appendChild(el.panel)" in chatbot_js


def test_chatbot_route_is_registered_and_labelled() -> None:
    app_js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")

    assert 'chatbot: "Chatbot"' in app_js
    assert '"benchmark", "chatbot"' in app_js
    assert 'route.base === "chatbot"' in app_js
