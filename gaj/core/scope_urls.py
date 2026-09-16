"""口径 URL 构造 —— 用「关键词 × 筛选条件」把单口径的岗位池子挖深。

背景（2026-09 实测）：BOSS 列表 API 的 ``resCount`` 是虚高数字 ——
``AI测试@杭州`` 报 ``resCount=450/hasMore=true``，实际第 1、2 页各 15 条互不
重叠，第 3 页起全是重复，单口径实得 ≈30 条。要更多同口径岗位，靠**换筛选
条件**：每换一组筛选，BOSS 就换一批 15~30 条。

本模块把「筛选维度 → 编码」和 URL 拼装沉淀成代码，避免每次手抄：

    python3 -m gaj scope-urls --city 杭州 --keywords "AI测试,大模型评测"

生成的口径 URL 直接喂给 ``python3 -m gaj crawl <url>``，每条 URL 天然是一个
``source_link``（口径隔离的唯一标识），因此扩池与「同口径快照 / 报告」兼容。

编码来源：2026-09 页面实测（筛选下拉项的 ``ka`` 属性）。**编码会漂移**，
可用 ``python3 -m gaj export-filter-codes`` 在已登录页面重新导出刷新
(写入 ``references/boss_filter_codes.json``，本模块优先读它)。
"""

from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlparse

from .. import config as cfg
from ..logging_setup import get_logger

log = get_logger("scope_urls")

SEARCH_URL = "https://www.zhipin.com/web/geek/jobs"

#: 维度 key → BOSS URL 参数名
DIMENSION_PARAMS: dict[str, str] = {
    "job_type": "jobType",
    "salary": "salary",
    "experience": "experience",
    "degree": "degree",
    "scale": "scale",
    "stage": "stage",
}

#: 维度 key → 中文名（报告 / 口径命名用）
DIMENSION_LABELS: dict[str, str] = {
    "job_type": "求职类型",
    "salary": "薪资",
    "experience": "经验",
    "degree": "学历",
    "scale": "公司规模",
    "stage": "融资阶段",
}

#: 内置码表（2026-09 页面实测）。刷新后以 references/boss_filter_codes.json 为准。
DEFAULT_FILTER_CODES: dict[str, dict[str, str]] = {
    "job_type": {"1901": "全职", "1903": "兼职"},
    "salary": {
        "402": "3K以下", "403": "3-5K", "404": "5-10K",
        "405": "10-20K", "406": "20-50K", "407": "50K以上",
    },
    "experience": {
        "108": "在校生", "102": "应届生", "101": "经验不限", "103": "1年以内",
        "104": "1-3年", "105": "3-5年", "106": "5-10年", "107": "10年以上",
    },
    "degree": {
        "209": "初中及以下", "208": "中专", "206": "高中",
        "202": "大专", "203": "本科", "204": "硕士", "205": "博士",
    },
    "scale": {
        "301": "0-20人", "302": "20-99人", "303": "100-499人",
        "304": "500-999人", "305": "1000-9999人", "306": "10000人以上",
    },
    "stage": {
        "801": "未融资", "802": "天使轮", "803": "A轮", "804": "B轮",
        "805": "C轮", "806": "D轮及以上", "807": "已上市", "808": "不需要融资",
    },
}

#: 主关键词跑全量组合；其余同族关键词跑精选组合，避免重复劳动。
SELECTION_FULL: dict[str, list[str]] = {dim: list(codes) for dim, codes in DEFAULT_FILTER_CODES.items()}

#: 精选组合：覆盖画像常见口径（3~10 年经验 / 10-50K / 本科硕士 / 百人以上 / A 轮及以上 / 全职）
SELECTION_SELECTED: dict[str, list[str]] = {
    "job_type": ["1901"],
    "experience": ["104", "105", "106"],
    "salary": ["405", "406", "407"],
    "degree": ["203", "204"],
    "scale": ["304", "305", "306"],
    "stage": ["803", "804", "806", "807"],
}

#: 城市码：**只收录已验证的**。未列入的城市不要猜 —— 用 --city-code 从
#: 页面 URL（手动切城市后）里读，或写进 references/boss_city_codes.json。
CITY_CODES: dict[str, str] = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "南京": "101190100",
}
#: 已验证来源：杭州/上海/南京 = 本机 job-pipeline 配置 + 历史采集 URL；
#: 北京/广州/深圳 = 2026-09 第三方实测文档。其余城市一律走 --city-code。
CITY_CODES_SOURCE = "本机历史采集 URL + 2026-09 实测文档 (部分城市未验证, 用 --city-code 兜底)"

FILTER_CODES_FILE = cfg.REFERENCES_DIR / "boss_filter_codes.json"
CITY_CODES_FILE = cfg.REFERENCES_DIR / "boss_city_codes.json"


# ---------------------------------------------------------------- 码表加载


def _read_json(path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # 码表损坏不该让采集停摆
        log.warning(f"读取 {path} 失败, 回退内置码表: {exc}")
    return {}


def load_filter_codes() -> tuple[dict[str, dict[str, str]], str]:
    """返回 (码表, 来源说明)。

    优先用 ``references/boss_filter_codes.json``（``export-filter-codes`` 刷新
    出来的），缺失或缺少某个维度时用内置默认值补齐。
    """
    data = _read_json(FILTER_CODES_FILE)
    exported = data.get("dimensions") or {}
    if not exported:
        return {d: dict(c) for d, c in DEFAULT_FILTER_CODES.items()}, "内置默认 (2026-09 页面实测)"
    codes: dict[str, dict[str, str]] = {}
    for dim, defaults in DEFAULT_FILTER_CODES.items():
        merged = dict(defaults)
        for code, label in (exported.get(dim) or {}).items():
            merged[str(code)] = str(label)
        codes[dim] = merged
    stamp = data.get("exported_at") or "未知时间"
    return codes, f"references/boss_filter_codes.json (导出时间 {stamp})"


def load_city_codes() -> dict[str, str]:
    """内置城市码 + ``references/boss_city_codes.json`` 覆盖/补充。"""
    codes = dict(CITY_CODES)
    extra = _read_json(CITY_CODES_FILE)
    for name, code in (extra.get("cities") or extra).items():
        if isinstance(code, str) and code.isdigit():
            codes[str(name)] = code
    return codes


def resolve_city_code(city: str) -> str | None:
    """城市名或裸城市码 → BOSS city 码。未知城市返回 None（不要猜）。"""
    city = (city or "").strip()
    if not city:
        return None
    if city.isdigit():
        return city
    codes = load_city_codes()
    if city in codes:
        return codes[city]
    # 兼容「杭州」写成「杭州市」「浙江杭州」
    for name, code in codes.items():
        if name in city:
            return code
    return None


# ---------------------------------------------------------------- URL 构造


def build_url(city_code: str, keyword: str, filters: dict[str, str] | None = None) -> str:
    """拼一条筛选列表页 URL（与历史 source_link 同格式）。"""
    url = f"{SEARCH_URL}?query={quote(keyword)}&city={city_code}"
    for dim, code in (filters or {}).items():
        param = DIMENSION_PARAMS.get(dim)
        if param and code:
            url += f"&{param}={quote(str(code))}"
    return url


def _plan(mode: str) -> dict[str, list[str]]:
    """mode → {维度: [编码]}。baseline = 不加任何筛选。"""
    if mode == "baseline":
        return {}
    if mode == "full":
        return {d: list(c) for d, c in SELECTION_FULL.items()}
    if mode == "selected":
        return {d: list(c) for d, c in SELECTION_SELECTED.items()}
    raise ValueError(f"未知 mode: {mode!r} (可选 baseline / full / selected)")


def build_urls(keyword: str, city_code: str, mode: str = "full") -> list[dict[str, Any]]:
    """单个关键词 → 一组口径 URL。

    Returns:
        [{"url", "keyword", "city_code", "dimension", "code", "label"}]
        第一项恒为基线（不加筛选），其后每个筛选编码一条。
    """
    out: list[dict[str, Any]] = [
        {
            "url": build_url(city_code, keyword),
            "keyword": keyword,
            "city_code": city_code,
            "dimension": "",
            "code": "",
            "label": "基线 (不限筛选)",
        }
    ]
    for dim, codes in _plan(mode).items():
        for code in codes:
            out.append(
                {
                    "url": build_url(city_code, keyword, {dim: code}),
                    "keyword": keyword,
                    "city_code": city_code,
                    "dimension": dim,
                    "code": code,
                    "label": f"{DIMENSION_LABELS.get(dim, dim)}={code}",
                }
            )
    return out


def build_scope_urls(
    city: str,
    keywords: Iterable[str],
    *,
    primary_mode: str = "full",
    others_mode: str = "selected",
) -> list[dict[str, Any]]:
    """多个同族关键词 × 筛选组合。

    第一个关键词当主词跑 ``primary_mode``（默认全量组合），其余跑
    ``others_mode``（默认精选组合）—— 同一批结果在不同筛选下重复度高，
    主词全量 + 其余精选是性价比最高的组合。关键词按 URL 去重。
    """
    city_code = resolve_city_code(city)
    if not city_code:
        raise ValueError(
            f"未知城市 {city!r}: 内置码表只收录已验证城市 {sorted(load_city_codes())}, "
            "其余城市请用 --city-code 传裸码 (从页面 URL 的 city= 读)"
        )
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        raise ValueError("至少给一个关键词")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, kw in enumerate(kws):
        mode = primary_mode if i == 0 else others_mode
        for item in build_urls(kw, city_code, mode):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            out.append(item)
    return out


# ---------------------------------------------------------------- 反解析 / 命名


def parse_scope_url(url: str) -> dict[str, Any]:
    """口径 URL → {keyword, city_code, filters{维度: 编码}}，反查不到的编码原样保留。"""
    q = parse_qs(urlparse(url or "").query)
    codes, _ = load_filter_codes()
    filters: dict[str, str] = {}
    for dim, param in DIMENSION_PARAMS.items():
        val = (q.get(param) or [""])[0]
        if val:
            filters[dim] = val
    return {
        "keyword": (q.get("query") or [""])[0],
        "city_code": (q.get("city") or [""])[0],
        "filters": filters,
        "filters_label": {
            dim: codes.get(dim, {}).get(code, f"{dim}={code}")
            for dim, code in filters.items()
        },
    }


def suggest_label(url: str, *, city_name: str = "") -> str:
    """给口令 URL 起一个可读名（配合 `gaj scope-link rename --label`）。"""
    parsed = parse_scope_url(url)
    parts = [p for p in (city_name, parsed["keyword"]) if p]
    parts.extend(parsed["filters_label"].values())
    return "·".join(parts)


def combo_count(mode: str) -> int:
    """某 mode 下单个关键词会生成多少条口径 URL（含基线）。"""
    return 1 + sum(len(v) for v in _plan(mode).values())
