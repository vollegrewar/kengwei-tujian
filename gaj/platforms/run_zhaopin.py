#!/usr/bin/env python3
"""智联批量采集: 职业漂移关键词 × 画像可接受城市, API 直连。

用法:
    python -m gaj.platforms.run_zhaopin          # 默认: 全关键词 × 主要城市
    python -m gaj.platforms.run_zhaopin --kw AI测试 --city 653
    python -m gaj.platforms.run_zhaopin --dry    # 只列搜索结果不抓详情

输出: data/platforms/zhaopin_{date}.json
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
from .zhaopin import ZhaopinCrawler, CITIES

log = get_logger("platforms.run_zhaopin")

DRIFT_KEYWORDS = [
    "AI测试", "测试开发", "自动化测试", "性能测试",
    "大模型评测", "AI评测", "模型评测", "评测工程师",
    "AI Agent", "大模型", "RAG",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default="")
    ap.add_argument("--city", type=int, default=0, help="城市码; 0=用画像可接受城市")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--max-jobs", type=int, default=10)
    args = ap.parse_args()

    c = ZhaopinCrawler()
    if not c.check_login():
        print("凭据获取失败")
        return

    # 城市: 画像可接受城市 ∩ 智联城市码表
    profile = load_profile()
    city_map = {name: code for name, code in CITIES.items() if name in profile.acceptable_cities}
    if args.city:
        city_map = {"指定": args.city}
    if not city_map:
        print("画像可接受城市中无智联城市码")
        return
    log.info(f"智联城市: {list(city_map.items())}")

    keywords = [args.kw] if args.kw else DRIFT_KEYWORDS
    all_jobs = []
    stats = {}

    for kw in keywords:
        for city_name, city_id in city_map.items():
            cards = c.search_positions(kw, city_id, page_size=20)
            tag = f"{kw}@{city_name}"
            stats[tag] = {"found": len(cards), "scraped": 0}
            log.info(f"[{tag}] 搜索 {len(cards)} 条")

            # 第一步过滤: 排除明显杂岗 (销售/保安/标注等)
            filtered = [
                x for x in cards
                if not any(b in (x.get("name") or "") for b in
                           ["销售", "保安", "标注", "招聘", "外包", "兼职", "实习"])
            ]
            stats[tag]["filtered"] = len(filtered)

            for i, card in enumerate(filtered[:args.max_jobs]):
                if args.dry:
                    continue
                try:
                    if i > 0:
                        time.sleep(random.uniform(3, 5))
                    job = c.parse_detail(card)
                    if job:
                        job["city"] = job.get("city") or city_name
                        all_jobs.append(job)
                        stats[tag]["scraped"] += 1
                        log.info(f"[{tag}] ✓ {job.get('title','')[:28]}")
                except Exception as e:
                    log.warning(f"[{tag}] 失败: {e}")
                    if sum(s["scraped"] for s in stats.values()) > 5 and stats[tag]["scraped"] % 3 == 0:
                        pass  # 单个失败继续
                if args.dry:
                    pass
            if not args.dry:
                time.sleep(random.uniform(5, 8))

    out_dir = Path("data/platforms")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d")
    out_path = out_dir / f"zhaopin_{ts}.json"
    out_path.write_text(
        json.dumps({"collected_at": datetime.now().isoformat(), "stats": stats, "jobs": all_jobs},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n=== 智联采集汇总 ===")
    for tag, s in stats.items():
        print(f"  {tag:20s} 搜索{s['found']:3d} → 过滤{s.get('filtered',0):3d} → 详情{s['scraped']:3d}")
    print(f"\n总详情: {len(all_jobs)} 条 → {out_path}")


if __name__ == "__main__":
    main()