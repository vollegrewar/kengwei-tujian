"""存量回填 (backfill-list-item) 的回归测试。

锁定三件事:
  1. 合并原则 = 只补空, 不覆盖: 已有招聘者姓名/头衔/城市必须原样保留;
  2. 布尔字段只在列表项为真时置真 (False 无法区分"否"与"未知", 不能瞎写);
  3. 端到端幂等: 回填一次后 job.json 里 boss 落地 + list_api=True, 再跑一次不再重复变更。
"""

from __future__ import annotations

import json

import pytest

from gaj import config as cfg
from gaj.core.models import Job
from gaj.store import backfill, repo
from gaj.store.backfill import collect_list_items, merge_list_item, run_backfill

ITEM = {
    "encryptJobId": "jid-1",
    "jobName": "AI 测试工程师",
    "cityName": "杭州",
    "areaDistrict": "西湖区",
    "businessDistrict": "西溪",
    "bossName": "黄女士",
    "bossTitle": "Recruiter",
    "bossCert": 3,
    "bossOnline": True,
    "goldHunter": 1,
    "anonymous": 1,
    "proxyJob": 1,
    "proxyType": 2,
    "jobValidStatus": 1,
}


# ------------------------------------------------------------ 合并逻辑


def test_merge_fills_empty_fields() -> None:
    job = Job(job_id="jid-1", title="AI 测试工程师", boss={}, provenance={"list_api": False})
    changes = merge_list_item(job, ITEM)

    assert job.boss["name"] == "黄女士"
    assert job.boss["title"] == "Recruiter"
    assert job.boss["cert"] == 3
    assert job.boss["gold_hunter"] is True
    assert job.boss["anonymous"] is True
    assert job.boss["proxy_job"] is True
    assert job.boss["proxy_type"] == 2
    assert job.provenance["list_api"] is True
    assert job.provenance["employer_anonymous"] is True
    assert job.city == "杭州" and job.district == "西湖区"
    assert job.business_district == "西溪"
    assert job.provenance["city_source"] == "list_api"
    assert changes["boss.name"] == "黄女士"


def test_merge_does_not_overwrite_existing() -> None:
    job = Job(
        job_id="jid-1",
        city="宁波",
        district="鄞州区",
        boss={"name": "张先生", "title": "HRBP", "cert": 9, "online": True,
              "gold_hunter": False, "anonymous": False, "proxy_job": False, "proxy_type": 0},
        provenance={"list_api": True, "city_source": "address"},
    )
    changes = merge_list_item(job, ITEM)

    assert job.boss["name"] == "张先生"      # 原有招聘者保留
    assert job.boss["title"] == "HRBP"
    assert job.city == "宁波"                # 城市不被覆盖
    assert job.provenance["city_source"] == "address"
    # 已有 list_api 标记 → 不再重复写 provenance.list_api
    assert "provenance.list_api" not in changes


def test_merge_no_changes_for_complete_job() -> None:
    job = Job(job_id="jid-1", city="杭州", district="西湖区", business_district="西溪",
              boss={"name": "黄女士", "title": "Recruiter", "cert": 3, "online": True,
                    "gold_hunter": True, "anonymous": True, "proxy_job": True, "proxy_type": 2},
              provenance={"list_api": True, "employer_anonymous": True})
    assert merge_list_item(job, ITEM) == {}


def test_merge_ignores_empty_item() -> None:
    job = Job(job_id="jid-1")
    assert merge_list_item(job, {}) == {}
    assert job.boss == {}


def test_merge_false_flags_stay_unset() -> None:
    """列表项里 goldHunter=0 时不能写成 False（无法区分"否"与"未知"）。"""
    job = Job(job_id="jid-2", boss={})
    merge_list_item(job, {"encryptJobId": "jid-2", "goldHunter": 0, "anonymous": 0})
    assert "gold_hunter" not in job.boss
    assert "anonymous" not in job.boss
    assert "employer_anonymous" not in job.provenance


# ------------------------------------------------------------ 采集目录扫描


def _write_page(root, dirname: str, job_id: str, boss_name: str, page: int = 1) -> None:
    debug = root / dirname / "_debug"
    debug.mkdir(parents=True, exist_ok=True)
    (debug / f"joblist_page_{page:02d}.json").write_text(
        json.dumps({"jobList": [{"encryptJobId": job_id, "bossName": boss_name}]},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def test_collect_list_items_newest_dir_wins(tmp_path) -> None:
    root = tmp_path / "_raw"
    _write_page(root, "crawl-20260101T000000", "jid-1", "旧招聘者")
    _write_page(root, "crawl-20260202T000000", "jid-1", "新招聘者")
    _write_page(root, "crawl-20260202T000000", "jid-2", "另一位", page=2)

    items, pages = collect_list_items(root)
    assert pages == 3
    assert items["jid-1"]["bossName"] == "新招聘者"
    assert items["jid-2"]["bossName"] == "另一位"

    # limit_dirs 只看最近 N 个目录
    items2, pages2 = collect_list_items(root, limit_dirs=1)
    assert pages2 == 2 and "jid-1" in items2 and "jid-2" in items2


def test_collect_list_items_missing_dir(tmp_path) -> None:
    assert collect_list_items(tmp_path / "nope") == ({}, 0)


# ------------------------------------------------------------ 端到端


@pytest.fixture()
def temp_store(tmp_path, monkeypatch):
    data = tmp_path / "data"
    jobs = data / "jobs"
    raw = data / "_raw"
    jobs.mkdir(parents=True)
    raw.mkdir(parents=True)
    monkeypatch.setattr(cfg, "DATA_ROOT", data)
    monkeypatch.setattr(cfg, "JOBS_DIR", jobs)
    monkeypatch.setattr(cfg, "RAW_DIR", raw)
    monkeypatch.setattr(cfg, "INDEX_DB", data / "index.db")
    monkeypatch.setattr(repo.cfg, "JOBS_DIR", jobs)
    monkeypatch.setattr(repo.cfg, "DATA_ROOT", data)
    return {"data": data, "jobs": jobs, "raw": raw}


def test_run_backfill_end_to_end(temp_store) -> None:
    job = Job(job_id="jid-1", title="AI 测试工程师", boss={}, provenance={"list_api": False})
    repo.save_job(job)
    _write_page(temp_store["raw"], "crawl-20260916T190000", "jid-1", "黄女士")

    rep = run_backfill(raw_dir=temp_store["raw"], dry_run=False)
    assert rep.matched == 1 and rep.updated == ["jid-1"]
    assert rep.scanned_jobs == 1

    saved = repo.load_job("jid-1")
    assert saved.boss["name"] == "黄女士"
    assert saved.provenance["list_api"] is True

    # 幂等: 再跑一次不再变更
    rep2 = run_backfill(raw_dir=temp_store["raw"], dry_run=False)
    assert rep2.matched == 0
    assert rep2.already_had == 1


def test_run_backfill_dry_run_does_not_write(temp_store) -> None:
    repo.save_job(Job(job_id="jid-1", boss={}, provenance={"list_api": False}))
    _write_page(temp_store["raw"], "crawl-20260916T190000", "jid-1", "黄女士")

    rep = run_backfill(raw_dir=temp_store["raw"], dry_run=True)
    assert rep.matched == 1
    assert repo.load_job("jid-1").boss.get("name") is None    # 未落盘


def test_run_backfill_reports_unmatched_jobs(temp_store) -> None:
    repo.save_job(Job(job_id="jid-other", boss={}))
    _write_page(temp_store["raw"], "crawl-20260916T190000", "jid-1", "黄女士")
    rep = run_backfill(raw_dir=temp_store["raw"], dry_run=True)
    assert rep.matched == 0 and rep.scanned_jobs == 1
