"""Chrome 预检门控的回归测试。

背景: cmd_analyze / cmd_daily 曾无条件预检 Chrome —— 明明配了 API key 走
HTTP driver, 也必须先开浏览器才能打分, 等于把 ``--provider api`` 的意义抹掉。
本测试锁住「api provider 不需要 Chrome」这一约定。
"""

from __future__ import annotations

from gaj.browser import available_providers, needs_chrome


def test_api_provider_needs_no_chrome() -> None:
    assert needs_chrome("api") is False


def test_browser_providers_need_chrome() -> None:
    for p in ("deepseek", "doubao", "tongyi", "kimi"):
        assert needs_chrome(p) is True


def test_unknown_provider_treated_as_browser() -> None:
    # 未知 provider 走浏览器分支 -> 预检会拦住并给出可读错误, 优于静默失败
    assert needs_chrome("no-such-provider") is True


def test_analyze_with_api_provider_bypasses_chrome_gate(capsys, monkeypatch) -> None:
    """Chrome 不可用时, api provider 的 analyze 不应报 chrome_not_ready。"""
    import json

    from gaj.agent import cli as agent_cli

    monkeypatch.setattr(agent_cli, "_chrome_ready", lambda: False)
    monkeypatch.delenv("GAJ_API_KEY", raising=False)

    code = agent_cli.main(["analyze", "--job", "some-job-id", "--provider", "api"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] != "chrome_not_ready"
    assert code != 0


def test_analyze_with_browser_provider_still_gated(capsys, monkeypatch) -> None:
    import json

    from gaj.agent import cli as agent_cli

    monkeypatch.setattr(agent_cli, "_chrome_ready", lambda: False)
    code = agent_cli.main(["analyze", "--job", "some-job-id", "--provider", "deepseek"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"]["code"] == "chrome_not_ready"
    assert code != 0


def test_provider_list_contains_api() -> None:
    assert "api" in available_providers()
