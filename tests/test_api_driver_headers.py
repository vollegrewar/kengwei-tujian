"""API driver 的请求头能力 (UA / 自定义头) 回归测试。

背景: opencode.ai/zen 这类渠道前面有 Cloudflare —— urllib 默认的
``Python-urllib/3.x`` UA 直接被回 ``error code: 1010``; 而 OpenCode Go 还要求
``x-opencode-session`` 头, 否则返回 MissingSessionID。driver 因此需要:
  1. 默认带常规浏览器 UA;
  2. 支持通过 GAJ_API_EXTRA_HEADERS 注入任意额外头 (JSON)。
"""

from __future__ import annotations

import json

import pytest

from gaj.browser.llm_driver_api import DEFAULT_USER_AGENT, APIDriver, _parse_extra_headers


# ------------------------------------------------------------ 头解析


def test_default_user_agent_is_browser_like() -> None:
    assert "Python-urllib" not in DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.startswith("Mozilla/5.0")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", {}),
        ("   ", {}),
        ('{"x-opencode-session": "hermes"}', {"x-opencode-session": "hermes"}),
        ('{"a": 1, "b": true}', {"a": "1", "b": "True"}),      # 值统一转字符串
        ("[1,2]", {}),                                        # 非对象 -> 忽略
        ("{不是 json", {}),                                    # 坏 JSON -> 忽略
    ],
)
def test_parse_extra_headers(raw, expected) -> None:
    assert _parse_extra_headers(raw) == expected


# ------------------------------------------------------------ 请求组装


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _driver(monkeypatch, **env) -> APIDriver:
    monkeypatch.setenv("GAJ_API_KEY", "test-key")
    monkeypatch.setenv("GAJ_API_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("GAJ_API_MODEL", "some-model")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return APIDriver()


def test_headers_sent(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["url"] = req.full_url
        body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    d = _driver(monkeypatch, GAJ_API_EXTRA_HEADERS='{"x-opencode-session": "hermes-gaj"}')
    assert d.ask("hi") == "OK"

    h = captured["headers"]
    assert h["authorization"] == "Bearer test-key"
    assert "python-urllib" not in h["user-agent"].lower()
    assert h["x-opencode-session"] == "hermes-gaj"       # 自定义头透传
    assert captured["url"] == "https://relay.example/v1/chat/completions"


def test_custom_user_agent_override(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["ua"] = {k.lower(): v for k, v in req.header_items()}["user-agent"]
        return _FakeResp(json.dumps({"choices": [{"message": {"content": "x"}}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    d = _driver(monkeypatch, GAJ_API_USER_AGENT="MyAgent/1.0")
    d.ask("hi")
    assert captured["ua"] == "MyAgent/1.0"


def test_missing_key_raises(monkeypatch) -> None:
    monkeypatch.delenv("GAJ_API_KEY", raising=False)
    with pytest.raises(ValueError):
        APIDriver()


def test_reasoning_content_fallback(monkeypatch) -> None:
    """推理模型 content 为空时回退到 reasoning_content (不低于返回空串)。"""
    def fake_urlopen(req, timeout=None):
        body = json.dumps({"choices": [{"message": {"content": "", "reasoning_content": "思考结果"}}]}).encode()
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert _driver(monkeypatch).ask("hi") == "思考结果"
