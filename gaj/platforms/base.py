"""多平台 JD 抓取框架 —— 仿真人操作, 规避风控。

设计原则 (来自 BOSS `code=37` 与搜索降级教训):
1. **单标签复用**: 全程只用一个标签页, 不在多个 tab 间快速跳转
2. **自然节奏**: 页面加载等 6-12s; 翻页间隔 8-15s 随机; 详情页逐个打开间隔 3-6s
3. **滚动模拟**: 打开列表页后模拟滚动到底 (触发懒加载, 也像真人浏览)
4. **负载上限**: 单轮采集 ≤50 条, 分多轮跨天执行 (BOSS code=37 教训: 60 请求/轮即触发)
5. **失败即停**: 空页/验证码/无卡片 → 立即停止并报告, 不重试 (重试加重风控标记)
6. **登录态检查**: 每次采集前先验证目标平台登录态, 未登录不行动

输出: 统一 `job.json` 结构 (与 boss_scraper 对齐), 后续进 migrate/打分/图鉴全链路复用。
"""

from __future__ import annotations

import json
import random
import time
from abc import ABC, abstractmethod
from typing import Any

import requests

from ..logging_setup import get_logger

log = get_logger("platforms")

# 仿真人节奏参数
PAGE_LOAD_WAIT = (6, 12)      # 秒: 页面加载等待
PAGE_TURN_DELAY = (8, 15)     # 秒: 翻页间隔 (随机)
DETAIL_DELAY = (3, 6)         # 秒: 详情页逐个打开间隔
SCROLL_STEPS = 6              # 列表页滚动步数
SCROLL_STEP_DELAY = (1, 2)    # 秒: 每步滚动间隔
MAX_JOBS_PER_RUN = 50         # 单轮采集上限


class PlatformCrawler(ABC):
    """平台抓取基类。每个平台实现: 登录检查 / 列表解析 / 详情解析。"""

    name = "base"
    login_url = ""
    list_url_template = ""

    def __init__(self, cdp_port: int = 9222):
        self.cdp_port = cdp_port
        self._ws = None
        self._msg_id = 0

    # ---------------------------------------------------------- CDP 基础

    def _connect(self) -> None:
        """连接 CDP。优先复用现有平台标签, 否则新建。"""
        import websocket

        target = None
        for attempt in range(2):
            targets = requests.get(f"http://127.0.0.1:{self.cdp_port}/json", timeout=10).json()
            # 找已打开的该平台标签 (仿真人: 复用!)
            existing = [t for t in targets if t.get("type") == "page" and self.name in t.get("url", "")]
            if existing:
                target = existing[0]
                break
            # 标签不存在时新建 (用户可能关过该平台窗口)
            if attempt == 0:
                try:
                    # /json/new 必须用 PUT 方法 (GET 返回空)
                    target = requests.put(
                        f"http://127.0.0.1:{self.cdp_port}/json/new?{self.login_url}",
                        timeout=10,
                    ).json()
                    time.sleep(random.uniform(4, 7))  # 新标签等加载
                    break
                except Exception as e:
                    log.warning(f"[{self.name}] 新建标签失败: {e}")

        if not target:
            raise RuntimeError(f"[{self.name}] 无法连接 CDP/创建标签页")

        self._tab_url = target.get("url", self.login_url)
        self._ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60)
        self._send("Runtime.enable")
        self._send("Page.enable")

    def _send(self, method: str, params: dict | None = None) -> dict:
        # 断线自动重连一次: 目标页面跳转/重载可能断开旧的 WS
        for attempt in range(2):
            try:
                self._msg_id += 1
                self._ws.send(json.dumps({"id": self._msg_id, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(self._ws.recv())
                    if msg.get("id") == self._msg_id:
                        return msg
            except Exception:
                if attempt == 0:
                    log.warning(f"[{self.name}] CDP 连接中断, 重连中...")
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    self._connect()
                else:
                    raise
        raise RuntimeError(f"[{self.name}] CDP 通信失败")

    def _eval(self, expr: str) -> Any:
        r = self._send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        result = r.get("result", {}).get("result", {})
        return result.get("value")

    def _navigate(self, url: str, wait_range: tuple = PAGE_LOAD_WAIT) -> None:
        self._send("Page.navigate", {"url": url})
        time.sleep(random.uniform(*wait_range))

    def _scroll_slow(self) -> None:
        """模拟真人滚动: 分步滚动到底, 每步停顿。"""
        for i in range(1, SCROLL_STEPS + 1):
            self._eval(f"window.scrollTo(0, document.body.scrollHeight * {i}/{SCROLL_STEPS})")
            time.sleep(random.uniform(*SCROLL_STEP_DELAY))

    def close(self) -> None:
        if self._ws:
            self._ws.close()

    # ---------------------------------------------------------- 平台接口

    @abstractmethod
    def check_login(self) -> bool:
        """验证登录态。子类实现: 读页面顶部用户区文本或关键 cookie。"""

    @abstractmethod
    def parse_list(self) -> list[dict]:
        """解析当前列表页, 返回 [{'title', 'url', 'job_id', 'salary_raw', 'city', ...}]"""

    @abstractmethod
    def parse_detail(self, url: str) -> dict:
        """打开详情页并解析完整 JD。返回统一 job dict。"""

    # ---------------------------------------------------------- 主流程

    def run(self, list_url: str, max_jobs: int = MAX_JOBS_PER_RUN) -> dict:
        """执行一轮采集。返回统计。"""
        stats = {"found": 0, "scraped": 0, "failed": 0, "jobs": []}
        self._connect()
        try:
            if not self.check_login():
                log.error(f"[{self.name}] 未登录! 请先在 Chrome 登录 {self.login_url}")
                return stats

            # 导航到列表页并慢滚
            self._navigate(list_url)
            self._scroll_slow()
            cards = self.parse_list()
            stats["found"] = len(cards)
            log.info(f"[{self.name}] 列表页解析到 {len(cards)} 张卡片")

            for idx, card in enumerate(cards[:max_jobs]):
                try:
                    # 详情页间隔拉长 (仿真人点击查看)
                    if idx > 0:
                        time.sleep(random.uniform(*DETAIL_DELAY))
                    job = self.parse_detail(card["url"])
                    if job:
                        stats["scraped"] += 1
                        stats["jobs"].append(job)
                        log.info(f"[{self.name}] {idx+1}/{min(len(cards),max_jobs)} ✓ {job.get('title','?')[:30]}")
                except Exception as e:
                    stats["failed"] += 1
                    # 失败即停? 不——单个失败继续, 但连续 3 个失败终止 (可能是风控)
                    log.warning(f"[{self.name}] 详情失败 {card.get('url','')}: {e}")
                    if stats["failed"] >= 3:
                        log.error(f"[{self.name}] 连续 3 个详情失败, 疑似风控, 停止本轮")
                        break
        finally:
            self.close()
        return stats