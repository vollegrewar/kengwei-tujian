"""猎聘平台抓取适配器。

列表页: https://www.liepin.com/zhaopin/?key={kw}&city={city}
卡片:   .job-card-pc-container (含 data-tlg-ext JSON, 可提取 jobId)
详情页: https://www.liepin.com/job/{jobId}.shtml

字段来源 (2026-09 实测):
- 卡片文本: "AI产品测试开发工程师 | 杭州-余杭区 | 10-20k | 1-3年 | 统招本科 | 公司名 | 行业+融资+规模"
- 详情页: 标题区/福利标签/职位介绍全文/公司简介
"""

from __future__ import annotations

import json
import random
import re
import time

from ..logging_setup import get_logger
from .base import PlatformCrawler, PAGE_LOAD_WAIT

log = get_logger("platforms.liepin")

# 猎聘城市码 (常用)
CITIES = {
    "杭州": "050090",
    "上海": "050020",
    "北京": "050010",
    "广州": "050080",
    "深圳": "050090_2",  # 深圳
    "南京": "050210",
    "苏州": "050230",
    "合肥": "050330",
}

DEFAULT_CITY = "050090"  # 杭州


class LiepinCrawler(PlatformCrawler):
    name = "liepin"
    login_url = "https://www.liepin.com/"

    # ---------------------------------------------------------- 登录检查

    def check_login(self) -> bool:
        # 顶部用户区: "你好，{用户名}" 出现即已登录
        user = self._eval(
            "document.body.innerText.match(/你好，[\\s\\S]{0,20}/)?.[0] || ''"
        )
        logged = bool(user and "你好" in user)
        log.info(f"[liepin] 登录态: {'✓ ' + user[:30] if logged else '✗ 未登录'}")
        return logged

    # ---------------------------------------------------------- 列表解析

    def parse_list(self) -> list[dict]:
        cards = self._eval("""
        Array.from(document.querySelectorAll('.job-card-pc-container')).map(card => {
          const a = card.querySelector('a[href*="job"]');
          const text = card.innerText || '';
          const ext = card.getAttribute('data-tlg-ext') || '';
          let jobId = '';
          try { jobId = JSON.parse(ext).jobId || ''; } catch(e) {}
          return {
            url: a ? a.href : '',
            jobId,
            text: text.slice(0, 400)
          };
        })
        """)
        cards = cards or []
        result = []

        # 卡片文本行结构 (实测):
        #   0: 标题  1: 【  2: 城市-区域  3: 】  4: 急聘(可选)  5: 薪资  6: 经验  7: 学历  8: 公司  9: 行业+融资+规模  10: HR
        for c in cards:
            lines = [l.strip() for l in (c.get("text") or "").split("\n") if l.strip()]
            if not lines:
                continue
            title = lines[0]
            city = district = ""
            # 城市行独立: 【 ⏎ 杭州-余杭区 ⏎ 】 (三行结构); 有时同行为 【杭州-余杭区】
            for i, l in enumerate(lines):
                m = re.search(r"([一-龥]{2,4})[-—/]([一-龥]{2,6})", l)
                # 标题行也含"-"但不会是 城市-区域 形态 (标题通常带括号/汉字较多)
                if m and ("【" in "".join(lines[max(0,i-1):i+2]) or "[" in "".join(lines[max(0,i-1):i+2])):
                    city, district = m.group(1), m.group(2)
                    break
            # 薪资 (k 或 元)
            m_sal = re.search(r"(\d+(\.\d+)?-\d+(\.\d+)?k(·\d+薪)?)", "\n".join(lines), re.I)
            salary_raw = m_sal.group(1) if m_sal else ""
            # 经验/学历/公司: 按固定行位找
            exp_raw = edu_raw = company = ""
            for i, l in enumerate(lines):
                if re.match(r"^\d+[-—]\d+年$|^经验不限$|^应届生$", l):
                    exp_raw = l
                elif re.match(r"^统招\S+|^(本科|硕士|博士|大专|学历不限)$", l):
                    edu_raw = l
                elif exp_raw and edu_raw and not company and l not in ("急聘", "广告") and len(l) < 40:
                    company = l
            if not company:
                # 回退: 学历行之后的第一行
                for i, l in enumerate(lines):
                    if edu_raw and l == edu_raw and i + 1 < len(lines) and "K" not in lines[i+1].upper():
                        company = lines[i + 1]
                        break

            url = c.get("url", "")
            if not url:
                continue
            result.append({
                "title": title,
                "url": url,
                "job_id": c.get("jobId") or "",
                "salary_raw": salary_raw,
                "city": city,
                "district": district,
                "exp_raw": exp_raw,
                "edu_raw": edu_raw,
                "company": company,
            })
        return result

    # ---------------------------------------------------------- 详情解析

    def parse_detail(self, url: str) -> dict | None:
        # 详情页打开前先等 (仿真人点击)
        time.sleep(random.uniform(2, 4))
        self._navigate(url, wait_range=(8, 12))

        # 等待标题出现 (SPA 可能延迟)
        for _ in range(3):
            title = self._eval("document.title")
            if "招聘" in (title or ""):
                break
            time.sleep(2)

        data = self._eval("""
        (() => {
          const body = document.body.innerText;
          const get = sel => { const e = document.querySelector(sel); return e ? e.innerText.trim() : ''; };

          // 标题: 详情页 job-title-box (含"测试工程师【深圳-龙岗区】"结构)
          const titleEl = document.querySelector('.job-title-box');
          let title = titleEl ? titleEl.innerText.trim() : '';
          title = title.split('【')[0].split('[')[0].trim();  // 去掉城市后缀

          // 薪资: .salary 容器 (唯一匹配 /k/i 的)
          const salaryEl = Array.from(document.querySelectorAll('[class*="salary"]'))
              .filter(e => /k/i.test(e.innerText))[0];
          const salary = salaryEl ? salaryEl.innerText.trim().split('\\n')[0] : '';

          // 福利标签
          const welfare = Array.from(document.querySelectorAll('[class*="welfare"] [class*="tag"], [class*="label"] span, [class*="tag"] span'))
              .map(e => e.innerText.trim()).filter(t => t && t.length <= 8).slice(0, 15);

          // 公司简介
          const intro = get('[class*="company-intro"], [class*="companyIntro"]').slice(0, 800);

          return JSON.stringify({title, salary, welfare, intro, bodyLen: body.length});
        })()
        """)
        if not data:
            return None
        d = json.loads(data)
        if d.get("bodyLen", 0) < 500:
            log.warning(f"[liepin] 详情页疑似空壳/风控 (len={d.get('bodyLen')})")
            return None

        # JD 全文: 从"职位介绍/职责描述"到"公司信息"之间
        text = self._eval("document.body.innerText")
        jd_full = text
        m_start = re.search(r"(职位介绍|职责描述|岗位职责|职位描述)", text)
        m_end = re.search(r"(公司信息|公司简介|工作地址)", text[m_start.end():] if m_start else text)
        if m_start:
            jd_full = text[m_start.end():]
            if m_end:
                jd_full = jd_full[: m_end.start() + 200]

        return {
            "source": "liepin",
            "url": url.split("?")[0],
            "title": (d.get("title") or "").split("】")[-1].split("招聘")[0][:60],
            "salary_raw": d.get("salary", ""),
            "welfare": d.get("welfare", []),
            "jd": {"full": jd_full.strip()[:6000]},
            "company_intro": d.get("intro", ""),
        }


def build_list_url(kw: str, city: str = DEFAULT_CITY) -> str:
    import urllib.parse

    return f"https://www.liepin.com/zhaopin/?key={urllib.parse.quote(kw)}&city={city}"