"""刷新 BOSS 筛选编码表 —— 从已登录的搜索页导出各筛选下拉的 ``ka`` 编码。

为什么需要：``gaj/core/scope_urls.py`` 的码表（经验/薪资/学历/规模/融资…）
是从筛选下拉项的 ``ka`` 属性抄下来的，**BOSS 改版会漂移**。与其等人肉发现，
不如在已登录会话里跑一次导出，和内置码表 diff。

    python3 -m gaj export-filter-codes            # 导出并写入 references/
    python3 -m gaj export-filter-codes --dry-run  # 只看 diff, 不落盘

依赖：CDP 调试端口上已登录 zhipin 的浏览器（``gaj setup-chrome`` 起的那只）。
只发 1 次导航 + 1 次页面内 JS，不碰列表接口，风控成本可忽略。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .. import config as cfg
from ..core import scope_urls
from ..logging_setup import get_logger

log = get_logger("filter_codes")

#: 搜索页：筛选下拉是 SPA 一次性渲染的，打开就都在 DOM 里
DEFAULT_URL = f"{scope_urls.SEARCH_URL}?query=%E6%B5%8B%E8%AF%95&city=101210100"

#: 页面里下拉框的索引顺序（2026-09 实测）→ 我们的维度 key。
#: 索引 0「职位类型」与 5「行业」不在生成器里用，仍原样保留进 raw 段备查。
DD_INDEX_TO_DIM: dict[int, str] = {
    1: "job_type",
    2: "salary",
    3: "experience",
    4: "degree",
    6: "scale",
    7: "stage",
}
DD_INDEX_LABELS: dict[int, str] = {
    0: "职位类型", 1: "求职类型", 2: "薪资", 3: "经验",
    4: "学历", 5: "行业", 6: "公司规模", 7: "融资阶段",
}

EXPORT_JS = r"""
(() => {
  const out = [];
  const strips = document.querySelectorAll(".filter-select-dropdown");
  strips.forEach((dd, i) => {
    const items = [];
    dd.querySelectorAll("li").forEach((li) => {
      const ka = (li.getAttribute("ka") || "").replace(/^sel-job-rec-/, "");
      const text = (li.textContent || "").trim();
      if (ka && text) items.push({ code: ka, label: text });
    });
    out.push({ index: i, count: items.length, items });
  });
  return JSON.stringify({ dropdowns: out, url: location.href });
})()
"""


def export_filter_codes(
    *,
    cdp_port: int | None = None,
    url: str = DEFAULT_URL,
    out_path: Path | None = None,
    dry_run: bool = False,
    wait_sec: float = 12.0,
) -> dict:
    """导出编码表。

    Returns:
        {dimensions, raw, diffs, written, source_url, exported_at}
        ``diffs`` 是「内置码表 vs 页面实际」的差异列表（空 = 没有漂移）。
    """
    from boss_scraper.cdp_session import CDPSession

    port = cdp_port or cfg.SETTINGS.crawl.cdp_port
    try:
        ws = CDPSession(port)
    except Exception as exc:
        raise RuntimeError(
            f"连不上 CDP 端口 {port} ({exc.__class__.__name__}): 先确认调试浏览器在跑 —— "
            f"`python3 -m gaj setup-chrome`, 并已在该窗口登录 zhipin"
        ) from exc
    tid = sid = None
    try:
        tid, sid = ws.create_target(url)
        log.info(f"等待页面渲染筛选控件 ({wait_sec:.0f}s)...")
        time.sleep(wait_sec)
        raw = ws.eval_js(EXPORT_JS, sid, timeout=30)
    finally:
        try:
            if tid:
                ws.close_target(tid)
        except Exception:
            pass
        ws.close()

    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    dropdowns = payload.get("dropdowns") or []
    if not dropdowns:
        raise RuntimeError(
            "页面上没找到 .filter-select-dropdown —— 可能是未登录/被风控降级为推荐流, "
            "或 BOSS 改版了筛选控件结构"
        )

    dimensions: dict[str, dict[str, str]] = {}
    raw_dims: dict[str, dict[str, str]] = {}
    for dd in dropdowns:
        idx = dd.get("index")
        dim = DD_INDEX_TO_DIM.get(idx)
        bucket = {str(it["code"]): str(it["label"]) for it in dd.get("items") or []}
        if not bucket:
            continue
        if dim:
            dimensions[dim] = bucket
        raw_dims[f"dd{idx}_{DD_INDEX_LABELS.get(idx, idx)}"] = bucket

    builtin = scope_urls.DEFAULT_FILTER_CODES
    diffs: list[dict] = []
    for dim, page_codes in dimensions.items():
        local = builtin.get(dim) or {}
        for code, label in page_codes.items():
            if code not in local:
                diffs.append({"dimension": dim, "code": code, "label": label,
                              "kind": "页面有/本地无"})
            elif local[code] != label:
                diffs.append({"dimension": dim, "code": code, "label": label,
                              "local": local[code], "kind": "名称不一致"})
        for code, label in local.items():
            if code not in page_codes:
                # 只有页面该维度确实渲染出来了才判「消失」, 避免半截 DOM 误报
                if page_codes:
                    diffs.append({"dimension": dim, "code": code, "label": label,
                                  "kind": "本地有/页面无"})

    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    doc = {
        "exported_at": exported_at,
        "source_url": payload.get("url") or url,
        "note": "由 `python3 -m gaj export-filter-codes` 导出; 维度 key 与 gaj/core/scope_urls.py 对齐",
        "dimensions": dimensions,
        "raw": raw_dims,
    }

    written = False
    if not dry_run and dimensions:
        target = out_path or scope_urls.FILTER_CODES_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        written = True
        log.info(f"码表已写入 {target}")

    return {
        "exported_at": exported_at,
        "source_url": doc["source_url"],
        "dimensions": dimensions,
        "dropdown_count": len(dropdowns),
        "diffs": diffs,
        "written": written,
        "out_path": str(out_path or scope_urls.FILTER_CODES_FILE),
    }
