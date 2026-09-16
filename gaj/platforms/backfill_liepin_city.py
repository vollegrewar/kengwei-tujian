#!/usr/bin/env python3
"""回填猎聘岗位城市 (纯 HTTP, 零风控, 不打开浏览器)。

猎聘详情页 <title> 格式: 【苏州 AI应用工程师招聘】-芯长征苏州招聘信息-猎聘
第一段 【城市 ...】 里的城市即岗位城市。HTTP 直取, 每 8-15s 一条 (仿真人节奏)。

用法: python -m gaj.platforms.backfill_liepin_city [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from pathlib import Path

import requests

from ..logging_setup import get_logger

log = get_logger("platforms.backfill")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/152.0 Safari/537.36"


def fetch_city_from_title(url: str) -> str:
    """从猎聘详情页 <title> 提取城市。返回 '' 表示失败。"""
    try:
        resp = requests.get(url.split("?")[0], headers={"User-Agent": UA}, timeout=20)
        if resp.status_code != 200:
            return ""
        html = resp.text
        m = re.search(r"【([\u4e00-\u9fa5]{2,4})\s+", html)
        return m.group(1) if m else ""
    except Exception:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多回填多少 (0=全部)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    jobs_dir = Path("data/jobs")
    fixed = 0
    skipped = 0
    examined = 0

    for d in sorted(jobs_dir.iterdir()):
        jf = d / "job.json"
        if not jf.exists():
            continue
        job = json.loads(jf.read_text(encoding="utf-8"))
        if job.get("source") != "liepin":
            continue
        if job.get("city") and not args.force:
            skipped += 1
            continue
        url = job.get("url", "")
        if not url:
            skipped += 1
            continue

        examined += 1
        city = fetch_city_from_title(url)
        if city:
            job["city"] = city
            jf.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            fixed += 1
            log.info(f"✓ {city} | {job.get('title','')[:24]}")
        else:
            log.warning(f"✗ 无法提取 | {job.get('title','')[:24]}")
        # 仿真人节奏
        time.sleep(random.uniform(8, 15))
        if args.limit and fixed >= args.limit:
            break

    print(f"\n回填完成: 处理 {examined} 条, 修复 {fixed} 条, 跳过(已有城市) {skipped} 条")


if __name__ == "__main__":
    main()