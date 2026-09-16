#!/usr/bin/env python3
"""投递候选清单: 从 GAJ 库按画像二次过滤, 输出可投递 top 清单。

过滤链 (B 步骤):
1. 规则分 PASS
2. 导入画像硬约束: 薪资下限 ≥13 万 (H-04 已做, 这里再显式过滤一次避免 bad parse)
3. 杂岗词过滤: 人力资源/销售/招聘/保安/资料/助理/管培/客服/运营/标注
4. 学历: 硕士/博士 要求且无"本科"字样的降级标记 (不硬删, 标注需确认)
5. 去重: 同 title+company 只保留一条 (多平台重复)

输出: data/platforms/投递候选_YYYYMMDD.md (可在 Web 图鉴 / 直接抄送投递)
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..core.profile import load_profile

# 杂岗黑名单 (搜索混入的非目标岗位 —— 已按用户 2026-09-09 意见校准:
# 采购保留、居家公办保留(需确认)、小白保留(需算性价比)、驻场接受不排除)
JUNK_WORDS = [
    "人力资源", "销售", "保安", "资料管理", "资料员", "助理", "管培",
    "客服", "主播", "会计", "仓管", "文员", "前台", "行政",
    "标注", "训练师", "审核", "数据录入", "编辑", "设计", "产品经理", "猎头",
    "顾问", "商务", "财务", "出纳", "司机", "保洁", "普工", "操作工",
    "环境", "环评", "化学", "放射", "卫生", "检测员", "采集员", "分析师",
    "工程监理", "施工", "造价", "建筑", "安装", "维修", "质检员",
    # 开发岗 (用户: 不考虑开发岗, 只要测试/测开)
    "算法工程师", "感知算法", "后端开发", "前端开发", "全栈", "数据开发",
    "Agent开发", "开发工程师", "嵌入式软件", "电源硬件", "数字验证",
    # 转岗跨度大 (用户: 系统流程规划师 pass, 面试没思路)
    "规划师",
    # 用户 pass: 海外社媒运营 (跨度大); 架构师=开发岗
    "海外社媒", "TikTok运营", "Instagram",
    "架构师",
]

# 需人工确认的岗位标记 (保留但提示)
FLAG_WORDS = {
    "居家": "🏠居家/远程, 需确认真实度",
    "公办": "🏠居家公办, 需确认真实性",
    "小白": "🐣小白岗, 需算收入/工作量性价比",
    "驻场": "📍驻场, 已接受但确认客户方",
    "外包": "🤝外包岗, 确认签约主体",
    "运营": "📣非测试岗(运营), 薪资合适可试投",
    "规划师": "📣非测试岗, 薪资合适可试投",
}

# 需要软提示的学历关键词 (不硬删)
MASTER_ONLY = ["硕士", "博士", "研究生"]


def load_all_jobs() -> list[dict]:
    """读 GAJ 库全部岗位 + 对应 rule score。"""
    jobs = []
    for d in (Path("data/jobs")).iterdir():
        jf = d / "job.json"
        rf = d / "scores" / "rule.json"
        if not jf.exists():
            continue
        job = json.loads(jf.read_text(encoding="utf-8"))
        status = ""
        if rf.exists():
            rule = json.loads(rf.read_text(encoding="utf-8"))
            status = rule.get("status", "")
        job["_status"] = status
        jobs.append(job)
    return jobs


def parse_salary_min(salary: dict | str | None) -> float | None:
    """从 salary 提取年薪下限 (万)。支持:
    '10-15K'→120, '15-20k·14薪'→180, '1-1.5万元/月'→12万,
    '9000-13000元'(月薪, 智联)→9*12=108K=10.8万
    """
    if not salary:
        return None
    raw = salary.get("raw", "") if isinstance(salary, dict) else str(salary)
    m = re.search(r"(\d+(\.\d+)?)\s*[-—~]\s*(\d+(\.\d+)?)\s*([kK元]|万元)?", raw)
    if not m:
        return None
    lo = float(m.group(1))
    unit = m.group(5) or ""
    # 月薪单位: k / K / 元 / 万元(月) → 月薪×12
    if unit in ("k", "K") or "元" in raw:
        return lo * 12
    if "万元" in raw and "月" in raw:
        return lo * 12
    # 纯年薪范围 (无单位标记)
    return lo


def main() -> None:
    profile = load_profile()
    hard_min = profile.hard_min_salary_10k or 13.0
    acceptable = set(profile.acceptable_cities)

    jobs = load_all_jobs()
    passed = [j for j in jobs if j["_status"] == "PASS"]
    print(f"全库 {len(jobs)} 条, PASS {len(passed)} 条")

    # 二次过滤
    kept = []
    junked = 0
    for j in passed:
        title = j.get("title", "") or ""
        # 测试开发/测试开发工程师 是主投方向, 不受开发岗黑名单影响
        if "测试开发" in title or "测开" in title:
            kept.append(j)
            continue
        # 杂岗
        if any(w in title for w in JUNK_WORDS):
            junked += 1
            continue
        # 薪资下限
        smin = parse_salary_min(j.get("salary"))
        if smin is not None and smin < hard_min:
            continue
        kept.append(j)

    # 去重 (同 title+company)
    seen = set()
    dedup = []
    for j in kept:
        key = (j.get("title", "").strip()[:20], j.get("company_name", "") or "")
        if key in seen:
            continue
        seen.add(key)
        dedup.append(j)

    print(f"过滤后 {len(kept)} → 去重后 {len(dedup)} (杂岗 {junked})")

    # 排序: 按城市在可接受集合优先 + 薪资
    def sort_key(j):
        city_ok = 0 if j.get("city") in acceptable else 1
        smin = parse_salary_min(j.get("salary")) or 0
        return (city_ok, -smin)

    dedup.sort(key=sort_key)

    # 输出 md
    lines = [
        f"# 投递候选清单 ({datetime.now().strftime('%Y-%m-%d')})",
        "",
        f"- 全库 {len(jobs)} 条 → PASS {len(passed)} → 二次过滤 {len(dedup)} 条",
        f"- 薪资下限: ≥{hard_min} 万/年 | 杂岗词表 {len(JUNK_WORDS)} 个",
        f"- 来源: boss / liepin / zhaopin | 画像城市: {len(acceptable)} 个",
        "",
        "| # | 来源 | 城市 | 岗位 | 薪资 | 公司 | 学历 | 链接 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, j in enumerate(dedup[:50], 1):
        salary = j.get("salary", {})
        raw = salary.get("raw", "") if isinstance(salary, dict) else str(salary)
        edu = j.get("education", {})
        edu_raw = edu.get("raw", "") if isinstance(edu, dict) else ""
        flag = "⚠️" if any(m in edu_raw for m in MASTER_ONLY) else ""
        # 内容标记 (居家/小白/驻场/外包/运营 需人工确认; AI评测运营用户已接受)
        title_full = j.get("title", "") or ""
        if "AI评测运营" in title_full or "AI 评测运营" in title_full:
            notes = []
        else:
            notes = [v for k, v in FLAG_WORDS.items() if k in title_full]
        note = (" " + "；".join(notes)) if notes else ""
        lines.append(
            f"| {i} | {j.get('source','?')} | {j.get('city','?')} | "
            f"{title_full[:30]} | {raw[:12]} | {j.get('company_name','')[:14]} | {edu_raw} {flag} | "
            f"[链接]({j.get('url','')}){note} |"
        )

    out = Path("data/platforms") / f"投递候选_{datetime.now().strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n输出: {out}")


if __name__ == "__main__":
    main()