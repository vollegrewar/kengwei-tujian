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

from .. import config as cfg
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

    # 与 Boss 采集路径对齐: 薪资/经验/学历必须解析成数值, JD 要切分并提信号。
    # 只存 raw 文本会让规则引擎把岗位当成"没数据" —— 四个维度全部 0 分,
    # 实测把 201 条真实好岗压到 1.0~1.5 分 (其中 137 条被判 REJECTED)。
    from ..core.denoise import clean_list
    from ..core import normalize as nz
    from ..core.signals import extract_signals

    jd_full = (j.get("jd") or {}).get("full", "") or ""
    sections = nz.split_jd(jd_full)
    salary = nz.parse_salary(j.get("salary_raw", ""))
    exp = nz.parse_experience(j.get("exp_raw", ""))
    edu = nz.parse_education(j.get("edu_raw", ""))
    welfare = clean_list(j.get("welfare") or [], context="welfare")
    company_name = clean_text_company(j.get("company", ""))
    industry = j.get("industry", "") or ""

    signals = extract_signals(
        jd_text=sections.full,
        welfare=welfare,
        hours_per_day=None,
        company_industry=industry,
        company_name=company_name,
        company_intro=j.get("company_intro", "") or "",
        blacklist=[],
    )

    job = Job(
        job_id=j.get("job_id") or _norm_id(src, re.sub(r"[^A-Za-z0-9]", "_", url.split("/")[-1])[:60]),
        source=src,
        url=url,
        title=j.get("title", ""),
        salary=salary.to_dict(),
        city=nz.normalize_city(j.get("city", "")),
        district=j.get("district", ""),
        welfare=welfare,
        skills=clean_list(j.get("skills") or [], context="skills"),
        company_name=company_name,
        experience=exp.to_dict(),
        education=edu.to_dict(),
        jd=sections.to_dict(),
        signals=signals.to_dict(),
    )
    job.provenance["platform_import"] = True
    return job


def clean_text_company(raw: str) -> str:
    from ..core.denoise import clean_text

    return clean_text(raw or "", context="company_name")


def _renormalize_job(job: Job, *, dry_run: bool = False, meta: dict | None = None) -> dict:
    """按现存 raw 文本重算归一化字段 (只补空, 不覆盖已有数值)。

    用于修历史导入数据: 老版本 import 只写了 raw, 数值字段全空。
    """
    from ..core.denoise import clean_list
    from ..core import normalize as nz
    from ..core.signals import extract_signals

    changed: dict[str, object] = {}

    sal = job.salary or {}
    if not sal.get("min_10k") and sal.get("raw"):
        parsed = nz.parse_salary(sal["raw"])
        if parsed.min_10k:
            job.salary = parsed.to_dict()
            changed["salary"] = job.salary.get("raw")

    exp = job.experience or {}
    if exp.get("min_years") is None and exp.get("raw"):
        parsed = nz.parse_experience(exp["raw"])
        if parsed.min_years is not None or parsed.unlimited:
            job.experience = parsed.to_dict()
            changed["experience"] = exp["raw"]

    edu = job.education or {}
    # 注意用 is None: "学历不限" 解析出的 level 是 0 (falsy), 用 not 会反复重算
    if edu.get("level") is None and edu.get("raw"):
        parsed = nz.parse_education(edu["raw"])
        if parsed.level or parsed.unlimited:
            job.education = parsed.to_dict()
            changed["education"] = edu["raw"]

    jd = job.jd or {}
    full = jd.get("full") or ""
    if full and not (jd.get("responsibility") or jd.get("requirement")):
        sections = nz.split_jd(full)
        # 只有真的切出分节才算变更 —— 否则短 JD 每次都会被判定"可补", 破坏幂等
        if sections.responsibility or sections.requirement:
            job.jd = sections.to_dict()
            changed["jd_sections"] = True

    if full and not job.signals:
        sig = extract_signals(
            jd_text=full,
            welfare=job.welfare or [],
            hours_per_day=None,
            company_industry="",
            company_name=job.company_name or "",
            company_intro="",
            blacklist=[],
        )
        job.signals = sig.to_dict()
        changed["signals"] = True

    # 公司名/福利在导入时丢了 —— 从 data/platforms/*.json 里按 url 回填
    if meta:
        src_row = meta.get(job.url) or {}
        if not job.company_name and src_row.get("company"):
            job.company_name = clean_text_company(src_row["company"])
            changed["company_name"] = job.company_name
        if not job.welfare and src_row.get("welfare"):
            job.welfare = clean_list(src_row["welfare"], context="welfare")
            changed["welfare"] = len(job.welfare)
        if not job.skills and src_row.get("skills"):
            job.skills = clean_list(src_row["skills"], context="skills")
            changed["skills"] = len(job.skills)

    job.provenance["renormalized"] = "2026-09-16"
    if changed and not dry_run:
        repo.save_job(job)
    return changed


def _load_platform_meta() -> dict[str, dict]:
    """url → 原始平台记录 (data/platforms/*.json), 用于回填公司/福利/技能。"""
    out: dict[str, dict] = {}
    base = cfg.DATA_ROOT / "platforms"
    if not base.exists():
        return out
    for p in sorted(base.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # 平台导出有两种形态: {"jobs": [...]} 或 直接 [...]
        rows = data if isinstance(data, list) else (data.get("jobs") or [])
        for row in rows:
            url = row.get("url")
            if url:
                out.setdefault(url, row)
    return out


def renormalize_imported(
    *,
    dry_run: bool = False,
    sources: tuple[str, ...] = ("liepin", "zhaopin"),
    rescore: bool = False,
) -> dict:
    """给已导入的平台岗位补归一化字段 + 可选重打分 (幂等: 已补过的不再变)。"""
    report = {"scanned": 0, "changed": 0, "details": [], "rescored": 0, "changed_ids": []}
    meta = _load_platform_meta()
    report["meta_rows"] = len(meta)
    for job in list(repo.iter_jobs()):
        if job.source not in sources:
            continue
        report["scanned"] += 1
        changed = _renormalize_job(job, dry_run=dry_run, meta=meta)
        if changed:
            report["changed"] += 1
            report["changed_ids"].append(job.job_id)
            if len(report["details"]) < 10:
                report["details"].append((job.job_id, sorted(changed)))

    if rescore and not dry_run and report["changed_ids"]:
        from ..core.score_runner import score_all

        for jid in report["changed_ids"]:
            try:
                score_all(force=True, only=jid)
                report["rescored"] += 1
            except Exception as exc:
                print(f"重打分失败 {jid}: {exc}")
    return report


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