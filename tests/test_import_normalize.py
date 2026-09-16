"""平台导入归一化回归测试。

背景: 老版本 import_jobs 只写 raw 文本 (salary={"raw": ...}), 未解析薪资/经验/学历、
未切分 JD、未提信号 → 规则引擎认为"岗位没数据", 四个维度全部 0 分。
201 条真实好岗 (吉利/移远/小米的 AI 测试工程师等) 因此被打到 1.0~1.5 分并判 REJECTED。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gaj.core.models import Job
from gaj.platforms import import_jobs as ij


ROW = {
    "source": "liepin",
    "url": "https://www.liepin.com/job/1985345289.shtml",
    "title": "大模型测试工程师",
    "salary_raw": "2.5-3万·15薪",
    "exp_raw": "5-10年",
    "edu_raw": "本科",
    "city": "杭州",
    "company": "某某科技有限公司",
    "jd": {"full": "岗位职责：负责大模型效果评测与自动化测试。\n任职要求：熟悉 Python 与 pytest。"},
}


def test_to_ga_job_normalizes_numeric_fields():
    job = ij._to_ga_job(ROW)
    assert job is not None
    # 薪资解析成数值 (老版本这里只有 raw)
    assert job.salary.get("min_10k")
    assert job.salary.get("max_10k")
    # 经验/学历解析成码值
    assert job.experience.get("min_years") == 5
    assert job.education.get("level") is not None
    # 公司名与水印清洗
    assert job.company_name
    # JD 分节 + 信号
    assert job.jd.get("full")
    assert job.signals, "signals 不能为空, 否则 W/R 维度拿不到信号"
    assert job.provenance.get("platform_import") is True


def test_to_ga_job_marks_missing_salary_without_crash():
    row = dict(ROW, salary_raw="")
    job = ij._to_ga_job(row)
    assert job is not None
    assert job.salary.get("raw") == ""


def test_renormalize_fills_and_is_idempotent(tmp_path, monkeypatch):
    """重解析: 补数值 → 再跑一次不再变更 (幂等)。"""
    job = Job(
        job_id="liepin_test_1",
        source="liepin",
        url="https://www.liepin.com/job/1.shtml",
        title="AI 测试工程师",
        salary={"raw": "1.5-2.5万·13薪"},
        city="杭州",
        experience={"raw": "3-5年", "min_years": None, "max_years": None, "unlimited": False, "fresh_graduate": False},
        education={"raw": "本科", "level": None, "unlimited": False},
        jd={"full": "任职要求: 3 年经验, 熟悉 pytest 与接口自动化。"},
    )
    saves: list[str] = []
    monkeypatch.setattr(ij.repo, "save_job", lambda j: saves.append(j.job_id))

    changed = ij._renormalize_job(job, meta={"https://www.liepin.com/job/1.shtml": {"company": "测试公司"}})
    assert "salary" in changed and "experience" in changed and "education" in changed
    assert job.salary["min_10k"]
    assert job.experience["min_years"] == 3
    assert job.education["level"] is not None
    assert job.company_name == "测试公司"
    assert saves == ["liepin_test_1"]

    # 幂等: 第二次跑没有可补的字段
    again = ij._renormalize_job(job, meta={})
    assert again == {}
    assert saves == ["liepin_test_1"], "幂等时不应重复落盘"


def test_renormalize_dry_run_does_not_save(monkeypatch):
    job = Job(job_id="zhaopin_x", source="zhaopin", url="u", title="t",
              salary={"raw": "2-3万"}, experience={"raw": "5-10年"}, education={"raw": "本科"},
              jd={"full": "要求 5 年经验"})
    saves: list[str] = []
    monkeypatch.setattr(ij.repo, "save_job", lambda j: saves.append(j.job_id))
    changed = ij._renormalize_job(job, dry_run=True)
    assert changed
    assert saves == []


def test_renormalize_idempotent_for_unlimited_education(monkeypatch):
    """学历不限 → level=0 (falsy); 判空必须用 is None, 否则每次都被判定"可补"。"""
    job = Job(job_id="CC1", source="zhaopin", url="u", title="t",
              education={"raw": "学历不限", "level": None, "unlimited": False},
              experience={"raw": "经验不限", "min_years": None, "max_years": None, "unlimited": False, "fresh_graduate": False},
              jd={"full": "职责: 无特殊要求。", "responsibility": "职责: 无特殊要求。"})
    monkeypatch.setattr(ij.repo, "save_job", lambda j: None)
    first = ij._renormalize_job(job)
    assert "education" in first and job.education["level"] == 0
    assert ij._renormalize_job(job) == {}, "不限学历的岗位第二次不应再被判定变更"


def test_load_platform_meta_accepts_list_and_dict(tmp_path, monkeypatch):
    """平台导出有 {"jobs": [...]} 和 直接 [...] 两种形态, 都要能吃。"""
    from gaj import config as cfg

    d = tmp_path / "platforms"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"jobs": [{"url": "u1", "company": "A"}]}), encoding="utf-8")
    (d / "b.json").write_text(json.dumps([{"url": "u2", "company": "B"}]), encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cfg, "DATA_ROOT", tmp_path)

    meta = ij._load_platform_meta()
    assert meta["u1"]["company"] == "A"
    assert meta["u2"]["company"] == "B"
    assert len(meta) == 2
