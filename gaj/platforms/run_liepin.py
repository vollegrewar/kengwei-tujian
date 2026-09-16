#!/usr/bin/env python3
"""猎聘正式采集: 全国 × 职业漂移关键词, 按画像可接受城市过滤。

用法:
    python -m gaj.platforms.run_liepin          # 全关键词跑一轮 (默认)
    python -m gaj.platforms.run_liepin --kw AI测试
    python -m gaj.platforms.run_liepin --dry    # 只列卡片不抓详情

输出: data/platforms/liepin_{date}.json (统一 job dict 列表)
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path

from ..core.profile import load_profile
from ..logging_setup import get_logger
from .liepin import LiepinCrawler

log = get_logger("platforms.run_liepin")

# 职业漂移关键词集 (与画像 profile.md 技能关键词呼应)
DRIFT_KEYWORDS = [
    "AI测试", "测试开发", "自动化测试", "性能测试",
    "大模型评测", "AI评测", "模型评测", "评测工程师",
    "AI Agent", "大模型", "RAG",
]


def load_acceptable_cities() -> set[str]:
    p = load_profile()
    return set(p.acceptable_cities)


def in_acceptable_city(city: str, acceptable: set[str]) -> bool:
    """城市名匹配: 精确或前缀 (如 '杭州' in 画像; 岗位 '杭州-余杭' 已拆出城市)。"""
    if not city:
        return False  # 城市缺失不采 (避免浪费详情配额)
    return city in acceptable


def run_keyword(c: LiepinCrawler, kw: str, acceptable: set[str], max_jobs: int) -> dict:
    """采集单个关键词 (全国), 城市过滤后抓详情。"""
    import urllib.parse

    url = f"https://www.liepin.com/zhaopin/?key={urllib.parse.quote(kw)}"
    c._connect()
    c._navigate(url, wait_range=(8, 12))
    c._scroll_slow()
    cards = c.parse_list()
    # 城市过滤
    cards_ok = [x for x in cards if in_acceptable_city(x.get("city", ""), acceptable)]
    log.info(f"[{kw}] 全国 {len(cards)} 卡片 → 目标城市 {len(cards_ok)}")

    out = {"kw": kw, "found": len(cards), "matched_city": len(cards_ok), "jobs": []}
    for idx, card in enumerate(cards_ok[:max_jobs]):
        if idx > 0:
            time.sleep(random.uniform(3, 6))
        try:
            job = c.parse_detail(card["url"])
            if job:
                job["title"] = job.get("title") or card.get("title", "")
                job["city"] = card.get("city", "")
                job["salary_raw"] = job.get("salary_raw") or card.get("salary_raw", "")
                job["company"] = card.get("company", "")
                out["jobs"].append(job)
                log.info(f"[{kw}] ✓ {job.get('title','')[:28]} | {card.get('city','')}")
        except Exception as e:
            log.warning(f"[{kw}] 详情失败 {card.get('url','')}: {e}")
            if len([x for x in out["jobs"]]) > 2 and len(out["jobs"]) % 3 == 0:
                pass  # 单条失败继续; 连续 3 次由基类节奏自然放缓
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default="", help="单个关键词; 空=全部漂移关键词")
    ap.add_argument("--dry", action="store_true", help="只列卡片不抓详情")
    ap.add_argument("--max-jobs", type=int, default=15, help="每关键词最多详情数")
    args = ap.parse_args()

    acceptable = load_acceptable_cities()
    log.info(f"画像可接受城市 {len(acceptable)} 个: {sorted(acceptable)}")

    keywords = [args.kw] if args.kw else DRIFT_KEYWORDS
    c = LiepinCrawler()
    all_jobs = []
    stats = {}

    for kw in keywords:
        # 关键词之间间隔 (仿真人: 重新搜索)
        out = run_keyword(c, kw, acceptable, args.max_jobs if not args.dry else 0)
        stats[kw] = {"found": out["found"], "matched": out["matched_city"], "scraped": len(out["jobs"])}
        all_jobs.extend(out["jobs"])
        if not args.dry:
            time.sleep(random.uniform(10, 15))  # 两个搜索之间大间隔

    # 落盘
    out_dir = Path("data/platforms")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d")
    out_path = out_dir / f"liepin_{ts}.json"
    out_path.write_text(
        json.dumps({"collected_at": datetime.now().isoformat(), "stats": stats, "jobs": all_jobs},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n=== 采集汇总 ===")
    for kw, s in stats.items():
        print(f"  {kw:10s} 全国{s['found']:3d} → 目标城市{s['matched']:3d} → 详情{s['scraped']:3d}")
    print(f"\n总详情: {len(all_jobs)} 条 → {out_path}")


if __name__ == "__main__":
    main()