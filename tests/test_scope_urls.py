"""口径 URL 生成器 (scope_urls) 的回归测试。

锁定三件事:
  1. 组合数量: 主词全量 38 条 (1 基线 + 37 编码), 精选 17 条 —— 数量变了说明
     码表或选择策略被改动, 必须是有意的。
  2. URL 形态: 与历史 source_link 同格式 (`/web/geek/jobs?query=&city=&<维度>=`),
     中文关键词要 percent-encode, 否则 crawl 拿到的是别的口径。
  3. 反解析与命名: parse/suggest_label 与 build 必须互逆 —— 口径名会进报告标题。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from gaj.core import scope_urls as su


# ------------------------------------------------------------ 城市码


@pytest.mark.parametrize(
    "city, expected",
    [
        ("杭州", "101210100"),
        ("杭州市", "101210100"),
        ("101010100", "101010100"),   # 裸码直接透传
        ("衢州", None),               # 未验证城市不猜
        ("", None),
    ],
)
def test_resolve_city_code(city, expected) -> None:
    assert su.resolve_city_code(city) == expected


def test_city_codes_override_file(tmp_path, monkeypatch) -> None:
    f = tmp_path / "boss_city_codes.json"
    f.write_text(json.dumps({"cities": {"衢州": "101211000"}}, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setattr(su, "CITY_CODES_FILE", f)
    assert su.resolve_city_code("衢州") == "101211000"
    assert su.resolve_city_code("杭州") == "101210100"   # 内置仍在


# ------------------------------------------------------------ URL 形态


def test_baseline_url_shape() -> None:
    url = su.build_url("101210100", "AI测试")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(su.SEARCH_URL + "?")
    assert q["city"] == ["101210100"]
    assert q["query"] == ["AI测试"]
    assert len(q) == 2, "基线不应带任何筛选参数"


def test_url_encodes_keyword_and_carries_filter() -> None:
    url = su.build_url("101210100", "AI 测试/评测", {"experience": "105"})
    assert "%E6%B5%8B%E8%AF%95" in url          # percent-encoded, 不是原始中文
    q = parse_qs(urlparse(url).query)
    assert q["query"] == ["AI 测试/评测"]
    assert q["experience"] == ["105"]


def test_dimension_param_mapping() -> None:
    url = su.build_url("101210100", "kw", {
        "job_type": "1901", "salary": "406", "degree": "203",
        "scale": "305", "stage": "807",
    })
    q = parse_qs(urlparse(url).query)
    for param in ("jobType", "salary", "degree", "scale", "stage"):
        assert param in q, f"{param} 参数名不对"


# ------------------------------------------------------------ 组合数量


def test_combo_counts() -> None:
    assert su.combo_count("baseline") == 1
    assert su.combo_count("selected") == 17
    assert su.combo_count("full") == 38


def test_build_urls_full_has_baseline_first() -> None:
    items = su.build_urls("AI测试", "101210100", "full")
    assert len(items) == 38
    assert items[0]["dimension"] == ""
    assert "&experience=" not in items[0]["url"]
    dims = {it["dimension"] for it in items if it["dimension"]}
    assert dims == set(su.DIMENSION_PARAMS)


def test_build_urls_selected_codes() -> None:
    items = su.build_urls("AI测试", "101210100", "selected")
    got = {(it["dimension"], it["code"]) for it in items if it["dimension"]}
    assert ("job_type", "1901") in got
    assert ("experience", "104") in got
    assert ("stage", "807") in got
    assert len(items) == 17


def test_build_urls_unknown_mode() -> None:
    with pytest.raises(ValueError):
        su.build_urls("kw", "101210100", "whatever")


def test_scope_urls_primary_full_others_selected() -> None:
    items = su.build_scope_urls("杭州", ["AI测试", "大模型评测"])
    assert len(items) == 38 + 17
    urls = [it["url"] for it in items]
    assert len(urls) == len(set(urls)), "URL 必须唯一"
    # 主词有 38 条, 其余词 17 条
    assert sum(1 for it in items if it["keyword"] == "AI测试") == 38
    assert sum(1 for it in items if it["keyword"] == "大模型评测") == 17
    assert items[0]["keyword"] == "AI测试" and items[0]["dimension"] == ""


def test_scope_urls_dedupes_repeated_keyword() -> None:
    items = su.build_scope_urls("杭州", ["AI测试", "AI测试"])
    assert len(items) == 38


def test_scope_urls_unknown_city_raises() -> None:
    with pytest.raises(ValueError):
        su.build_scope_urls("衢州", ["AI测试"])


# ------------------------------------------------------------ 反解析 / 命名


def test_parse_scope_url_roundtrip() -> None:
    url = su.build_url("101210100", "AI产品经理", {"stage": "807", "degree": "203"})
    parsed = su.parse_scope_url(url)
    assert parsed["keyword"] == "AI产品经理"
    assert parsed["city_code"] == "101210100"
    assert parsed["filters"] == {"stage": "807", "degree": "203"}
    assert parsed["filters_label"]["stage"] == "已上市"
    assert parsed["filters_label"]["degree"] == "本科"


def test_parse_scope_url_legacy_link() -> None:
    """历史采集链接 (带 percent-encode, 无筛选) 也要能解析。"""
    parsed = su.parse_scope_url(
        "https://www.zhipin.com/web/geek/jobs?query=AI%E6%B5%8B%E8%AF%95&city=101210100"
    )
    assert parsed["keyword"] == "AI测试"
    assert parsed["filters"] == {}


def test_suggest_label() -> None:
    url = su.build_url("101210100", "AI测试", {"experience": "105"})
    assert su.suggest_label(url, city_name="杭州") == "杭州·AI测试·3-5年"
    assert su.suggest_label(url) == "AI测试·3-5年"


def test_parse_unknown_code_kept_raw() -> None:
    parsed = su.parse_scope_url(su.build_url("101210100", "kw", {"experience": "999"}))
    assert parsed["filters_label"]["experience"].startswith("experience=")


# ------------------------------------------------------------ 码表加载


def test_load_filter_codes_defaults() -> None:
    codes, source = su.load_filter_codes()
    assert codes["experience"]["105"] == "3-5年"
    assert "内置默认" in source or "references" in source


def test_load_filter_codes_merges_exported(tmp_path, monkeypatch) -> None:
    f = tmp_path / "boss_filter_codes.json"
    f.write_text(json.dumps({
        "exported_at": "2026-09-16T10:00:00+08:00",
        "dimensions": {"experience": {"110": "新编码"}},   # 只覆盖一个维度
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(su, "FILTER_CODES_FILE", f)
    codes, source = su.load_filter_codes()
    assert codes["experience"]["110"] == "新编码"
    assert codes["experience"]["105"] == "3-5年"        # 未覆盖的仍在内置表里
    assert codes["salary"]["406"] == "20-50K"           # 整维度缺失时回退内置
    assert "2026-09-16" in source


def test_load_filter_codes_broken_file_falls_back(tmp_path, monkeypatch) -> None:
    f = tmp_path / "boss_filter_codes.json"
    f.write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(su, "FILTER_CODES_FILE", f)
    codes, source = su.load_filter_codes()
    assert codes["scale"]["305"] == "1000-9999人"
    assert "内置默认" in source


# ------------------------------------------------------------ agent JSON 接口


def _last_json(capsys) -> dict:
    payload = capsys.readouterr().out.strip()
    return json.loads(payload.splitlines()[-1])


def test_agent_scope_urls_envelope(capsys) -> None:
    from gaj.agent.cli import main

    assert main(["scope-urls", "--city", "杭州", "--keywords", "AI测试,大模型评测"]) == 0
    out = _last_json(capsys)
    assert out["ok"] is True
    assert out["data"]["url_count"] == 55
    assert out["data"]["urls"][0]["dimension"] == ""


def test_agent_scope_urls_unknown_city(capsys) -> None:
    from gaj.agent.cli import main

    assert main(["scope-urls", "--city", "衢州", "--keywords", "AI测试"]) == 2
    out = _last_json(capsys)
    assert out["ok"] is False
    assert out["error"]["code"] == "unknown_city"
