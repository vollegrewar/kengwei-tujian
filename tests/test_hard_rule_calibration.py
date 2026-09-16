"""硬规则判据校准回归测试 (H-04 薪资 / H-10 经验)。

2026-09-16 用户拍板修正两条判据 —— 修正前实测误杀 130 条真实岗位:
  * H-04 用「薪资下限 < 硬性底线」直接淘汰 (误杀 90 条)。下限只是谈判起点,
    只有「上限也够不到底线」才该致命淘汰; 下限低于底线降级为 REVIEW。
  * H-10 对「岗位 exp_min > 本人年限 + 1」零容忍淘汰 (误杀 40 条)。
    市场里「5-10年」岗对 3.5 年候选人并非绝对关闭, 容忍放宽到 1.5 年。

这两个判据直接决定"投递建议"的候选池大小, 必须锁住边界行为。
"""

from __future__ import annotations

import pytest

from gaj.core.models import Company, Job
from gaj.core.profile import Profile
from gaj.core.scoring import run_hard_checks


def _check(code: str, job: Job, profile: Profile | None = None):
    checks = run_hard_checks(job, Company(), profile or Profile())
    return next(c for c in checks if c.code == code)


def _profile(**kw) -> Profile:
    # hard_min_salary_10k 默认 13 万 (画像硬性底线)
    return Profile(total_years=3.5, hard_min_salary_10k=13.0, **kw)


# ---------------------------------------------------------------- H-04 薪资


@pytest.mark.parametrize(
    "smin, smax, raw, expect_fatal, expect_hit",
    [
        # 上限也够不到底线 → 致命淘汰
        (6.0, 7.2, "5000-6000元", True, True),
        (10.8, 12.0, "9-10K", True, True),
        # 下限低于底线但上限可达 → 只 REVIEW (修正前这里被直接枪毙)
        (10.8, 15.6, "9-13K", False, True),
        (12.0, 18.0, "1-1.5万", False, True),
        (12.0, 13.2, "10-11K", False, True),
        # 下限就达标 → 不命中
        (13.2, 21.6, "1.1-1.8万", False, False),
    ],
)
def test_h04_uses_salary_ceiling(smin, smax, raw, expect_fatal, expect_hit) -> None:
    job = Job(job_id="j", title="测试工程师", salary={"raw": raw, "min_10k": smin, "max_10k": smax})
    check = _check("H-04", job, _profile())
    assert check.hit is expect_hit
    assert check.is_fatal is expect_fatal
    if expect_hit:
        assert "年薪上限" in check.reason or "年薪下限" in check.reason


def test_h04_missing_salary_does_not_reject() -> None:
    """面议/未知不能当淘汰理由 (conf=0)。"""
    for sal in ({"raw": "面议", "negotiable": True}, {"raw": ""}):
        check = _check("H-04", Job(job_id="j", title="测试工程师", salary=sal), _profile())
        assert check.hit is False
        assert check.confidence == 0.0


def test_h04_no_ceiling_falls_back_to_floor_value() -> None:
    """只有下限没有上限时 (如「8K以上」), 用下限当上天花板判定。"""
    job = Job(job_id="j", title="测试工程师", salary={"raw": "8K以上", "min_10k": 9.6, "max_10k": None})
    check = _check("H-04", job, _profile())
    assert check.is_fatal is True


# ---------------------------------------------------------------- H-10 经验


@pytest.mark.parametrize(
    "min_years, raw, expect_fatal, expect_hit, expect_conf",
    [
        # 3.5 年 + 1.5 容忍 → 5.0 年及以内只 REVIEW
        (5.0, "5-10年", False, True, 0.5),
        (4.0, "3-5年", False, True, 0.5),
        # 超过容忍线 → 致命
        (6.0, "5-10年", True, True, 1.0),
        (10.0, "10年以上", True, True, 1.0),
        # 不超本人年限 → 不命中
        (3.0, "3-5年", False, False, 0.0),
        (0.0, "经验不限", False, False, 0.0),
    ],
)
def test_h10_tolerance_one_and_half_year(min_years, raw, expect_fatal, expect_hit, expect_conf) -> None:
    job = Job(
        job_id="j",
        title="测试工程师",
        experience={"raw": raw, "min_years": min_years, "max_years": None, "unlimited": min_years == 0.0, "fresh_graduate": False},
    )
    check = _check("H-10", job, _profile())
    assert check.hit is expect_hit
    assert check.is_fatal is expect_fatal
    if expect_hit:
        assert check.confidence == pytest.approx(expect_conf)


def test_h10_boundary_value_is_not_fatal() -> None:
    """恰好等于容忍线 (3.5 + 1.5 = 5.0) 不算超出 —— 边界必须是闭区间。"""
    job = Job(job_id="j", title="测试工程师", experience={"raw": "5-10年", "min_years": 5.0})
    check = _check("H-10", job, _profile())
    assert check.is_fatal is False
    assert check.confidence == pytest.approx(0.5)


def test_h10_unlimited_experience_never_hits() -> None:
    job = Job(job_id="j", title="测试工程师", experience={"raw": "经验不限", "min_years": 0, "unlimited": True})
    check = _check("H-10", job, _profile())
    assert check.hit is False
