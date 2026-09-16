"""智联招聘平台抓取适配器 — API 直连路线。

智联是重 SPA: CDP 导航打开的页面是空壳 (数据不加载), 真人交互才触发
fe-api.zhaopin.com 请求。因此走"浏览器取凭据 + Python 直调 API":
  1. 从已登录的智联标签页提取 at/rt token + cookie
  2. Python requests 直调:
     - 列表:  fe-api.zhaopin.com/c/i/search/positions (关键参数 cityId/kw/start/pageSize)
     - 详情:  fe-api.zhaopin.com/c/i/jobs/position-detailv3?number={positionNumber}

实测 (2026-09):
  - 详情 API 返回 data.detailedPosition: positionName/salary/workingExp/description 全文/
    welfareTags/positionCityDistrict/positionNumber
  - 列表 API 之前实测返回过 200 (用户手动搜索时捕获), 参数结构待列表响应确认

城市码 (cityId): 杭州=653, 上海=538, 北京=530, 广州=763, 深圳=765, 南京=636,
苏州=639, 无锡=675, 常州=656, 南通=653? 见 CITIES (实测校准)。
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

from ..logging_setup import get_logger
from .base import PlatformCrawler

log = get_logger("platforms.zhaopin")

# 智联城市码 (常用大中城市, 来自智联搜索 URL cityId)
CITIES = {
    "杭州": 653, "上海": 538, "北京": 530, "广州": 763, "深圳": 765,
    "南京": 636, "苏州": 639, "无锡": 675, "常州": 656, "宁波": 640,
    "合肥": 651, "南通": 724, "嘉兴": 721, "温州": 698, "绍兴": 686,
    "台州": 697, "珠海": 765,  # 珠海暂无独立码, 用 HTTP 探测校准
}

API_BASE = "https://fe-api.zhaopin.com/c/i"


class ZhaopinCrawler(PlatformCrawler):
    """智联 API 直连抓取。不走 DOM/渲染, 从已登录浏览器取凭据。"""

    name = "zhaopin"
    login_url = "https://www.zhaopin.com/"

    def __init__(self, cdp_port: int = 9222):
        super().__init__(cdp_port)
        self.at = ""
        self.rt = ""
        self.cookies: dict[str, str] = {}
        self._ready = False

    # ---------------------------------------------------------- 凭据获取

    def _acquire_credentials(self) -> bool:
        """从已登录的智联标签提取 at/rt token + cookie。"""
        targets = requests.get(f"http://127.0.0.1:{self.cdp_port}/json", timeout=10).json()
        zp = [t for t in targets if t.get("type") == "page" and "zhaopin" in t.get("url", "")]
        if not zp:
            log.error("[zhaopin] 未找到智联标签, 请先打开 zhaopin.com")
            return False
        import websocket as wsc

        ws = wsc.create_connection(zp[0]["webSocketDebuggerUrl"], timeout=30)
        mid = [0]

        def wsend(method, params=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == mid[0]:
                    return r

        wsend("Runtime.enable")
        r = wsend("Runtime.evaluate", {
            "expression": "document.body.innerHTML.match(/[\"']at[\"']\\s*:\\s*[\"']([^\"']+)[\"']/)?.[1] || ''",
            "returnByValue": True,
        })
        self.at = r["result"]["result"].get("value", "")

        r = wsend("Runtime.evaluate", {
            "expression": "document.body.innerHTML.match(/[\"']rt[\"']\\s*:\\s*[\"']([^\"']+)[\"']/)?.[1] || ''",
            "returnByValue": True,
        })
        self.rt = r["result"]["result"].get("value", "")

        # cookie
        r = wsend("Network.getCookies", {"urls": ["https://www.zhaopin.com/"]})
        self.cookies = {c["name"]: c["value"] for c in r["result"]["cookies"]}
        ws.close()

        self._ready = bool(self.at and self.cookies)
        if not self._ready:
            log.error("[zhaopin] 凭据不完整 (at=%s, cookies=%d)", self.at[:10], len(self.cookies))
        else:
            log.info(f"[zhaopin] 凭据就绪 at={self.at[:12]}... cookies={len(self.cookies)}")
        return self._ready

    # ---------------------------------------------------------- 平台接口

    def check_login(self) -> bool:
        return self._acquire_credentials()

    def _api_get(self, path: str, params: dict) -> dict | None:
        params = dict(params)
        params.setdefault("at", self.at)
        params.setdefault("rt", self.rt)
        url = f"{API_BASE}/{path}"
        resp = requests.get(url, params=params, cookies=self.cookies, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/152.0 Safari/537.36",
            "Referer": "https://sou.zhaopin.com/",
        }, timeout=25)
        if resp.status_code != 200:
            log.warning(f"[zhaopin] API {path} HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except Exception:
            log.warning(f"[zhaopin] API {path} 非JSON响应: {resp.text[:100]}")
            return None

    # ---------------------------------------------------------- 列表

    def search_positions(self, kw: str, city_id: int, start: int = 0, page_size: int = 30) -> list[dict]:
        """搜索岗位列表。POST JSON body:
        S_SOU_FULL_INDEX=关键词, S_SOU_WORK_CITY=城市码, pageSize/pageIndex 分页。
        实测 (2026-09 监听捕获): 前端实际字段就是 S_SOU_* 系列 + cvNumber + actionid。
        """
        import uuid

        payload = {
            "S_SOU_FULL_INDEX": kw,
            "S_SOU_WORK_CITY": str(city_id),
            "S_SOU_POSITION_TYPE": "2",
            "order": 0,
            "actionid": str(uuid.uuid4()),
            "pageSize": page_size,
            "pageIndex": (start // page_size) + 1,
            "at": self.at,
            "rt": self.rt,
            "eventScenario": "pcSearchedSouSearch",
            "anonymous": 0,
        }
        url = f"{API_BASE}/search/positions"
        # 智联有限频: 连续请求会被断连 (ConnectTimeout)。失败重试 + 请求间最小间隔。
        time.sleep(random.uniform(1.5, 3.0))
        resp = None
        for attempt in range(3):
            try:
                resp = requests.post(url, params={"at": self.at, "rt": self.rt},
                                     json=payload, cookies=self.cookies, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/152.0 Safari/537.36",
                    "Referer": "https://sou.zhaopin.com/",
                    "Content-Type": "application/json",
                }, timeout=25)
                break
            except Exception as e:
                log.warning(f"[zhaopin] search 第{attempt+1}次失败: {e}")
                time.sleep(random.uniform(4, 8))
        if resp is None:
            return []
        if resp.status_code != 200:
            log.warning(f"[zhaopin] search/positions HTTP {resp.status_code}")
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        d = data.get("data") or {}
        lst = d.get("list") or d.get("results") or []
        return lst if isinstance(lst, list) else []

    def parse_list(self) -> list[dict]:
        """基类接口适配: 未指定 kw 时无法用, 直接返回空 (用 search_positions)。"""
        return []

    def parse_detail(self, card: dict) -> dict | None:
        """按 positionNumber 调详情 API。card 需含 'positionNumber'。"""
        num = (card.get("positionNumber") or card.get("number") or "").strip()
        if not num:
            return None
        time.sleep(random.uniform(2, 4))  # 仿真节奏
        data = self._api_get("jobs/position-detailv3", {"number": num})
        if not data:
            return None
        dp = (data.get("data") or {}).get("detailedPosition") or {}
        if not dp:
            log.warning(f"[zhaopin] 详情无数据 {num}")
            return None

        return {
            "source": "zhaopin",
            "url": dp.get("positionUrl") or dp.get("url") or f"https://sou.zhaopin.com/job/{num}.html",
            "job_id": num,
            "title": dp.get("positionName") or dp.get("name") or "",
            "salary_raw": dp.get("salary") or "",
            "city": dp.get("workCity") or dp.get("positionWorkCity") or "",
            "district": dp.get("cityDistrict") or dp.get("positionCityDistrict") or "",
            "exp_raw": dp.get("workingExp") or dp.get("positionWorkingExp") or "",
            "edu_raw": dp.get("education") or "",
            "welfare": dp.get("welfareTags") or [],
            "company": dp.get("companyName", ""),
            "jd": {"full": (dp.get("description") or dp.get("jobDesc") or "").strip()[:6000]},
            "company_intro": ((data.get("data") or {}).get("detailedCompany") or {}).get("companyDescription", ""),
        }

    def run(self, list_url: str, max_jobs: int = 10) -> dict:
        """智联走 API 直连: list_url 仅用于定位城市, 实际用 search_positions。"""
        stats = {"found": 0, "scraped": 0, "failed": 0, "jobs": []}
        if not self._acquire_credentials():
            return stats
        # 从 list_url 提取 kw/city
        kw = ""
        city_id = 653
        m = re.search(r"[?&]kw=([^&]+)", list_url)
        if m:
            import urllib.parse

            kw = urllib.parse.unquote(m.group(1))
        m = re.search(r"[?&]cityId=(\d+)", list_url)
        if m:
            city_id = int(m.group(1))
        if not kw:
            kw = "AI测试"

        cards = self.search_positions(kw, city_id)
        stats["found"] = len(cards)
        log.info(f"[zhaopin] 搜索 {kw} city={city_id} → {len(cards)} 条")

        for i, card in enumerate(cards[:max_jobs]):
            try:
                if i > 0:
                    time.sleep(random.uniform(3, 6))
                job = self.parse_detail(card)
                if job:
                    # 城市过滤由外层 (run_zhaopin) 做
                    stats["scraped"] += 1
                    stats["jobs"].append(job)
                    log.info(f"[zhaopin] {i+1}/{min(len(cards),max_jobs)} ✓ {job.get('title','')[:30]}")
            except Exception as e:
                stats["failed"] += 1
                log.warning(f"[zhaopin] 失败: {e}")
                if stats["failed"] >= 3:
                    break
        return stats


def build_list_url(kw: str, city_id: int = 653) -> str:
    import urllib.parse

    return f"https://sou.zhaopin.com/?kw={urllib.parse.quote(kw)}&cityId={city_id}"