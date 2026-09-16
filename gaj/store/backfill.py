"""存量回填：用采集目录里的列表 API 原始项，补历史岗位的招聘者 / 匿名 / 代招字段。

为什么要这个：列表项只在「这次新采到」时才会走 migrate 落盘；被
``skip_recent_hours`` 跳过的重复岗位永远拿不到 ``job.boss``（实测 265 条里
231 条为空）。而每次采集的每页原始响应都完整存在
``data/_raw/crawl-*/_debug/joblist_page_NN.json`` 里 —— 按 ``encryptJobId``
反查即可给存量补上，**不需要再访问 BOSS**（零风控成本），补完 H-11 猎头/代招
规则对历史岗位也能生效。

合并原则与 ``Job.build`` 一致：**只补空，不覆盖已有值**；布尔字段只在列表项为
真时置真（False 无法区分"否"与"未知"，不写）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import config as cfg
from ..logging_setup import get_logger
from . import index, repo
from .migrate import load_list_items

log = get_logger("backfill")

#: boss dict 里的字段 → 列表项里的键（布尔类）
_BOSS_BOOL_FIELDS = (
    ("online", "bossOnline"),
    ("gold_hunter", "goldHunter"),
    ("anonymous", "anonymous"),
    ("proxy_job", "proxyJob"),
)
#: 字符串类
_BOSS_TEXT_FIELDS = (
    ("name", "bossName"),
    ("title", "bossTitle"),
    ("cert", "bossCert"),
)


@dataclass
class BackfillReport:
    scanned_jobs: int = 0
    pages: int = 0
    items: int = 0
    matched: int = 0
    updated: list[str] = field(default_factory=list)
    already_had: int = 0
    changes: list[tuple[str, str, str]] = field(default_factory=list)  # (job_id, 字段, 值)

    def render(self) -> str:
        lines = [
            "=" * 62,
            "  存量回填报告 (列表 API 字段)",
            "=" * 62,
            f"  页面文件      : {self.pages}",
            f"  列表项        : {self.items}",
            f"  库内职位      : {self.scanned_jobs}",
            f"  命中可补      : {self.matched}",
            f"  新增招聘者信息: {len(self.updated)}",
            f"  已有字段跳过  : {self.already_had}",
        ]
        for jid, key, val in self.changes[:12]:
            lines.append(f"      {jid[:14]}… {key}={val}")
        if len(self.changes) > 12:
            lines.append(f"      … 共 {len(self.changes)} 项变更")
        lines.append("=" * 62)
        return "\n".join(lines)


# ---------------------------------------------------------------- 纯逻辑


def merge_list_item(job, item: dict) -> dict[str, Any]:
    """把列表项合并进 job（就地修改），返回变更摘要 {字段: 新值}。

    只补空：已有招聘者姓名/头衔/城市不会被覆盖（它们来自详情页，可能更准）。
    """
    if not item:
        return {}

    changes: dict[str, Any] = {}
    boss = dict(job.boss or {})

    for key, src in _BOSS_TEXT_FIELDS:
        if not boss.get(key) and item.get(src):
            boss[key] = item[src]
            changes[f"boss.{key}"] = item[src]
    for key, src in _BOSS_BOOL_FIELDS:
        if not boss.get(key) and item.get(src):
            boss[key] = True
            changes[f"boss.{key}"] = True
    if not boss.get("proxy_type") and item.get("proxyType"):
        boss["proxy_type"] = item["proxyType"]
        changes["boss.proxy_type"] = item["proxyType"]

    if changes:
        job.boss = boss

    # 岗位级标志 / 溯源
    if item.get("anonymous") and not job.provenance.get("employer_anonymous"):
        job.provenance["employer_anonymous"] = True
        changes["provenance.employer_anonymous"] = True
    if not job.provenance.get("list_api"):
        job.provenance["list_api"] = True
        changes["provenance.list_api"] = True

    # 城市/区域：只在空的时候补，且标明来源是列表 API（H-01 据此给足置信度）
    for key, src, flag in (
        ("city", "cityName", True),
        ("district", "areaDistrict", False),
        ("business_district", "businessDistrict", False),
    ):
        if not getattr(job, key, "") and item.get(src):
            setattr(job, key, item[src])
            changes[key] = item[src]
            if flag:
                job.provenance["city_source"] = "list_api"
                changes["provenance.city_source"] = "list_api"

    return changes


# ---------------------------------------------------------------- IO


def collect_list_items(raw_dir: Path | None = None, *, limit_dirs: int = 0) -> tuple[dict[str, dict], int]:
    """扫描 ``raw_dir/crawl-*/_debug/joblist_page_*.json``。

    后扫描的目录覆盖先扫描的（目录名带时间戳，天然"新的赢"）。
    返回 (job_id → 列表项, 页面文件数)。
    """
    raw_dir = raw_dir or cfg.RAW_DIR
    if not raw_dir.exists():
        return {}, 0

    items: dict[str, dict] = {}
    pages = 0
    dirs = sorted([d for d in raw_dir.iterdir() if d.is_dir() and d.name.startswith("crawl-")])
    if limit_dirs > 0:
        dirs = dirs[-limit_dirs:]
    for d in dirs:
        pages += len(list((d / "_debug").glob("joblist_page_*.json")))
        items.update(load_list_items(d))
    return items, pages


def run_backfill(
    *,
    raw_dir: Path | None = None,
    dry_run: bool = False,
    limit_dirs: int = 0,
    rescore: bool = False,
) -> BackfillReport:
    """执行回填：写 job.json + 刷新索引（可选只对变更岗位重跑规则打分）。"""
    report = BackfillReport()
    items, pages = collect_list_items(raw_dir, limit_dirs=limit_dirs)
    report.pages, report.items = pages, len(items)
    if not items:
        log.warning("没有找到任何列表项 (_debug/joblist_page_*.json)")
        return report

    touched: list[str] = []
    with index.session() as conn:
        for job in repo.iter_jobs():
            report.scanned_jobs += 1
            item = items.get(job.job_id)
            if not item:
                continue
            if job.provenance.get("list_api") and (job.boss or {}).get("name"):
                report.already_had += 1
                continue

            changes = merge_list_item(job, item)
            if not changes:
                report.already_had += 1
                continue

            report.matched += 1
            report.updated.append(job.job_id)
            for key, val in changes.items():
                report.changes.append((job.job_id, key, str(val)))
            if not dry_run:
                repo.save_job(job)
                index.upsert_job(conn, job, refresh_company=False)
                touched.append(job.job_id)

    if touched and rescore:
        from ..core.score_runner import score_all

        for jid in touched:
            try:
                score_all(force=True, only=jid)
            except Exception as exc:  # 单个失败不影响其余
                log.warning(f"重打分失败 {jid}: {exc}")

    return report
