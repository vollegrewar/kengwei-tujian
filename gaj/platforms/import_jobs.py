#!/usr/bin/env python3
"""把 platforms/*.json (猎聘/智联/鱼泡采集结果) 导入 GAJ 正式库。

转换: platforms 统一 job dict → GAJ Job / job.json 结构 (data/jobs/{job_id}/)
       → 进 migrate 索引 → 规则打分 → 图鉴。

用法:
    python -m gaj.platforms.import_jobs data/platforms/liepin_20260909.json
    python -m gaj.platforms.import_jobs data/platforms/liepin_*.json --score
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ..store import repo
from ..core.models import Job, Company


def _norm_id(source: str, raw_id: str) -> str:
    """生成稳定 job_id: 平台前缀 + 原始ID (避免跨平台冲突)。"""
    return f"{source}_{raw_id}"


def _to_ga_job(j: dict) -> Job | None:
    src = j.get("source", "")
    url = j.get("url", "")
    if not url:
        return None

    job = Job(
        job_id=j.get("job_id") or _norm_id(src, re.sub(r"[^A-Za-z0-9]", "_", url.split("/")[-1])[:60]),
        source=src,
        url=url,
        title=j.get("title", ""),
        salary={"raw": j.get("salary_raw", "")},
        city=j.get("city", ""),
        district=j.get("district", ""),
        welfare=j.get("welfare", []),
    )
    # jd 文本
    jd_full = (j.get("jd") or {}).get("full", "") or ""
    job.jd = {"full": jd_full}
    # 经验/学历 raw (列表项解析会处理)
    job.experience = {"raw": j.get("exp_raw", ""), "min_years": None, "max_years": None, "unlimited": False, "fresh_graduate": False}
    job.education = {"raw": j.get("edu_raw", ""), "level": None, "unlimited": False}
    return job


def import_file(path: str, score: bool = False) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    jobs_raw = data.get("jobs", [])
    imported = 0
    skipped = 0
    ids = []

    for j in jobs_raw:
        try:
            job = _to_ga_job(j)
            if job is None:
                skipped += 1
                continue
            # 去重: 同 source+url 已存在则跳过
            if repo.load_job(job.job_id):
                skipped += 1
                continue
            repo.save_job(job)
            ids.append(job.job_id)
            imported += 1
        except Exception as e:
            print(f"导入失败 {j.get('title','')[:20]}: {e}")
            skipped += 1

    # 重建索引
    from ..store import index as index_mod

    index_mod.reindex()
    print(f"导入 {imported} 条 (跳过 {skipped}), 索引已重建")

    if score and imported:
        from ..core.score_runner import main as _  # 仅确认可导入
        print("如需打分: python -m gaj score --all --force")

    return {"imported": imported, "skipped": skipped, "ids": ids}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()
    total = 0
    for p in args.paths:
        r = import_file(p, args.score)
        total += r["imported"]
        print(f"  {p}: +{r['imported']}")
    print(f"\n合计导入 {total} 条")


if __name__ == "__main__":
    main()