"""猎头 / 代招帖识别 (H-11) 与列表 API 字段回填的回归测试。

两种识别路径都要锁住:
  1. 打分层 ``headhunter_evidence`` —— 只看 BOSS 列表 API 的结构化字段
     (招聘者头衔 / 金牌猎头 / 代招 / 匿名雇主), 不扫公司名关键词。
  2. 迁移层 —— ``_debug/joblist_page_*.json`` 里的列表项必须真的回填进
     ``job.boss`` / ``provenance``, 否则第 1 条永远拿不到数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gaj.core.models import Company, Job
from gaj.core.profile import Profile
from gaj.core.scoring import REJECT_CONFIDENCE_FLOOR, headhunter_evidence, run_hard_checks
from gaj.store.migrate import load_list_items, migrate


def _h11(job: Job, profile: Profile | None = None, company: Company | None = None):
    checks = run_hard_checks(job, company or Company(), profile or Profile())
    return next(c for c in checks if c.code == "H-11")


# ------------------------------------------------------------ 打分层


def test_plain_job_not_flagged() -> None:
    job = Job(job_id="j1", title="AI 产品经理", company_name="某科技有限公司")
    check = _h11(job)
    assert check.hit is False
    assert check.confidence == 0.0


@pytest.mark.parametrize(
    "boss, expected_conf, expected_fatal",
    [
        ({"title": "猎头顾问", "name": "刘女士"}, 1.0, True),
        ({"title": "招聘者", "name": "张三", "gold_hunter": True}, 1.0, True),
        ({"title": "招聘者", "proxy_job": True}, 0.6, True),
        ({"title": "招聘者", "proxy_type": 1}, 0.6, True),
        ({"title": "招聘者", "anonymous": True}, 0.5, False),
    ],
)
def test_structured_flags(boss, expected_conf, expected_fatal) -> None:
    job = Job(job_id="j2", title="AI 产品经理", boss=boss)
    check = _h11(job)
    assert check.hit is True
    assert check.confidence == pytest.approx(expected_conf)
    assert check.is_fatal is expected_fatal
    assert check.evidence


def test_anonymous_from_provenance_is_suspicion() -> None:
    """公司名「某…公司」走 provenance.employer_anonymous, 只标 REVIEW 交 AI。"""
    job = Job(
        job_id="j3",
        title="AI 产品经理",
        provenance={"employer_anonymous": True},
    )
    check = _h11(job)
    assert check.hit is True
    assert check.confidence < REJECT_CONFIDENCE_FLOOR
    assert check.is_suspicion is True


def test_evidence_lists_both_recruiter_and_flags() -> None:
    job = Job(
        job_id="j4",
        boss={"title": "猎头顾问", "gold_hunter": True, "proxy_job": True},
    )
    evidence, conf = headhunter_evidence(job)
    assert conf == 1.0
    assert len(evidence) == 3


def test_profile_gate_accept_outsourcing_off() -> None:
    """画像允许外包/中介渠道时, H-11 不生效。"""
    job = Job(job_id="j5", boss={"title": "猎头顾问"})
    check = _h11(job, Profile(accept_outsourcing=True))
    assert check.hit is False


def test_hr_of_real_company_not_flagged() -> None:
    """反面案例: 正常公司的 HR 发帖, 公司名带「人力资源」不算猎头帖。"""
    job = Job(
        job_id="j6",
        title="AI 产品经理",
        company_name="上海某某人力资源有限公司",
        boss={"title": "人力资源HR", "name": "徐女士"},
    )
    assert _h11(job).hit is False


# ------------------------------------------------------------ 迁移层


def _write_legacy(src: Path, job_id: str, *, with_debug: bool) -> None:
    job_dir = src / "001"
    job_dir.mkdir(parents=True)
    (job_dir / "meta.json").write_text(
        json.dumps(
            {
                "source_url": f"https://www.zhipin.com/job_detail/{job_id}.html",
                "company_name": "杭州某某科技有限公司",
                "crawled_at": "2026-09-16T10:00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_dir / "jd_dom_data.json").write_text(
        json.dumps(
            {
                "url": f"https://www.zhipin.com/job_detail/{job_id}.html",
                "job_name": "AI 产品经理",
                "jd_full": "岗位职责：负责 AI 产品设计。任职要求：3 年以上经验。",
                "company_address": "杭州·西湖区",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if not with_debug:
        return
    debug = src / "_debug"
    debug.mkdir()
    (debug / "joblist_page_01.json").write_text(
        json.dumps(
            {
                "resCount": 42,
                "hasMore": True,
                "jobList": [
                    {
                        "encryptJobId": job_id,
                        "jobName": "AI 产品经理",
                        "salaryDesc": "30-45K",
                        "cityName": "杭州",
                        "bossName": "刘女士",
                        "bossTitle": "猎头顾问",
                        "bossCert": 3,
                        "bossOnline": True,
                        "goldHunter": 1,
                        "anonymous": 0,
                        "proxyJob": 0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_load_list_items_indexes_by_job_id(tmp_path: Path) -> None:
    job_id = "abc123def456"
    _write_legacy(tmp_path, job_id, with_debug=True)
    items = load_list_items(tmp_path)
    assert list(items) == [job_id]
    assert items[job_id]["bossTitle"] == "猎头顾问"


def test_load_list_items_without_debug_dir(tmp_path: Path) -> None:
    _write_legacy(tmp_path, "abc123def456", with_debug=False)
    assert load_list_items(tmp_path) == {}


def test_migrate_backfills_list_api_fields(tmp_path: Path, monkeypatch) -> None:
    """有 _debug 列表项时, 招聘者/金牌猎头必须回填; 没有时不得伪造。"""
    job_id = "abc123def456"

    for with_debug, should_backfill in ((True, True), (False, False)):
        src = tmp_path / f"crawl-{int(with_debug)}"
        _write_legacy(src, job_id, with_debug=with_debug)
        report = migrate(src=src, dry_run=True, rebuild_index=False)
        assert report.migrated == 1
        assert bool(report.list_api_items) is should_backfill

        # 直接检查 Job.build 的产物 (dry_run 不落盘, 用同一套入参重建一次)
        from gaj.core.denoise import clean_text
        from gaj.store.migrate import _legacy_list_item, _read_json, LegacyRecord

        rec = LegacyRecord(
            dirname="001",
            meta=_read_json(src / "001" / "meta.json"),
            jd_dom=_read_json(src / "001" / "jd_dom_data.json"),
        )
        api_item = load_list_items(src).get(job_id)
        job = Job.build(
            job_id=job_id,
            list_item=api_item or _legacy_list_item(rec, "杭州", "西湖区"),
            jd_dom=rec.jd_dom,
            company=Company(),
        )
        if should_backfill:
            assert job.boss["name"] == "刘女士"
            assert job.boss["title"] == "猎头顾问"
            assert job.boss["gold_hunter"] is True
            assert job.provenance["list_api"] is True
            assert job.city == "杭州"
        else:
            assert job.boss["name"] == ""
            assert job.boss["gold_hunter"] is False
            assert clean_text(job.city) == "杭州"  # 城市仍由地址兜底
