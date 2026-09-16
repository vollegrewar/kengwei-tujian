"""列表 API 单页条数 (pageSize) 的回归测试。

背景：GAJ 老代码把 pageSize 写死 15，三处各写一遍。2026-09-16 实测 API 接受 30
且确实按 ~20 条/页返回（job-pipeline 六个请求 120 条），因此统一为常量
``DEFAULT_PAGE_SIZE``，三处引用同一来源 —— 本测试锁住"不回归到 15"和"可显式覆盖"。
"""

from __future__ import annotations

from boss_scraper.har_parser import extract_search_params_from_url
from boss_scraper.network import DEFAULT_PAGE_SIZE, build_fetch_js


def test_default_page_size_is_30() -> None:
    assert DEFAULT_PAGE_SIZE == "30"


def test_build_fetch_js_uses_default() -> None:
    js = build_fetch_js(1, {})
    assert f'"pageSize": "{DEFAULT_PAGE_SIZE}"' in js


def test_build_fetch_js_explicit_override() -> None:
    js = build_fetch_js(1, {"pageSize": "15"})
    assert '"pageSize": "15"' in js


def test_url_params_use_default_page_size() -> None:
    params = extract_search_params_from_url(
        "https://www.zhipin.com/web/geek/jobs?query=AI%E6%B5%8B%E8%AF%95&city=101210100&experience=105"
    )
    assert params["pageSize"] == DEFAULT_PAGE_SIZE
    assert params["city"] == "101210100"
    assert params["experience"] == "105"


def test_crawler_har_fallback_uses_default() -> None:
    from types import SimpleNamespace

    from boss_scraper.crawler import JobCrawler

    # _params_from_har 只读 request_params 一个属性, 用最小对象即可
    har = SimpleNamespace(request_params={"city": "101210100"})
    params = JobCrawler._params_from_har(har)
    assert params["pageSize"] == DEFAULT_PAGE_SIZE
    assert params["scene"] == "1"
