"""文本清洗的水印剥离用例 (回归锁定)。

背景: BOSS 直聘在 DOM 里插入隐藏 span, 把水印碎片切进正文。原实现只覆盖
"来自BOSS直聘" 家族的中文串, 纯拉丁字母碎片 (kanzhun / boss) 会漏网 ——
实测 231 条历史 JD 里有 6 条正文残留 "kanzhun", 且 looks_polluted 用同一份
词表, 这 6 条被质量校验判为干净。本测试锁定修复后的行为。
"""

from __future__ import annotations

import pytest

from gaj.core.denoise import clean_text, looks_polluted


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 中文水印 (原有能力, 一并锁住)
        ("负BOSS直聘责 MaaS 平台", "负责 MaaS 平台"),
        ("计直聘算机相关专业", "计算机相关专业"),
        # 拉丁字母水印 #1: 看准网拼音, 无合法用法, 一律剥离
        ("负责制定kanzhun和执行产品的", "负责制定和执行产品的"),
        ("定义评估kanzhun指标体系", "定义评估指标体系"),
        # 拉丁字母水印 #2: 夹在两个汉字之间的 boss/zhipin
        ("岗位职boss责1. 负责测试", "岗位职责1. 负责测试"),
        ("在zhipin上发布", "在上发布"),
    ],
)
def test_watermark_stripped(raw: str, expected: str) -> None:
    assert clean_text(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "熟悉 Python 与 LLM 相关技术, 掌握 Agent 框架",
        "使用 Boss 系统查看简历",          # 有空格, 不是水印插入点
        "kanzhun",                         # 整串是品牌名, 属水印
    ],
)
def test_legit_english_kept(text: str) -> None:
    out = clean_text(text)
    if text == "kanzhun":
        assert out == ""
    else:
        assert out == text


def test_chinese_word_kanzhun_not_touched() -> None:
    """只剥拉丁拼音, 不动"看准"这个正常中文词。"""
    assert clean_text("看准时机再投递") == "看准时机再投递"


@pytest.mark.parametrize(
    "text",
    [
        "来自BOSS直聘",
        "负责制定kanzhun和执行产品",
        "岗位职boss责",
        "⼯程师",  # 康熙部首替身字
    ],
)
def test_looks_polluted_detects(text: str) -> None:
    assert looks_polluted(text) is True


def test_looks_polluted_clean_text() -> None:
    assert looks_polluted("负责制定和执行产品的测试策略") is False
    assert looks_polluted("熟悉 Python 与 LLM") is False
