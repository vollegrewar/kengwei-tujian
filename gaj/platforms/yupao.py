"""鱼泡直聘平台抓取适配器。

列表页: https://www.yupao.com/zhaogong/{category}.html (分类页)
        https://www.yupao.com/topic/{topic}/?keywords={kw} (关键词页)
卡片:   a[href*="zhaogong/*.html"] (文本含 岗位名/薪资万/公司/行业)
详情页: https://www.yupao.com/zhaogong/{id}.html

注意: 鱼泡偏蓝领+本地服务, 测试岗多为硬件/传感器方向, 城市分站明显。
     "软件测试"类目 a393c1501; "自动化测试" a393c1502; "测试开发" a393c1504。
"""

from __future__ import annotations

import json
import random
import re
import time

from ..logging_setup import get_logger
from .base import PlatformCrawler

log = get_logger("platforms.yupao")

# 常用分类: 测试工程师/软件测试/自动化测试/测试开发/性能测试
CATEGORIES = {
    "测试工程师": "a393c1500",
    "软件测试": "a393c1501",
    "自动化测试": "a393c1502",
    "测试开发": "a393c1504",
    "性能测试": "a393c1507",
}


class YupaoCrawler(PlatformCrawler):
    name = "yupao"
    login_url = "https://www.yupao.com/"

    def check_login(self) -> bool:
        # 顶栏用户区: 出现 "简历" "消息" 且无 "登录丨注册"
        user = self._eval(
            "document.body.innerText.slice(0, 200).match(/登录丨注册|登录|注册/) || ''"
        )
        logged = not bool(user)
        log.info(f"[yupao] 登录态: {'✓ 已登录' if logged else '✗ 未登录'}")
        return logged

    def parse_list(self) -> list[dict]:
        cards = self._eval("""
        Array.from(document.querySelectorAll('a[href*="zhaogong/"]'))
          .filter(a => /测试|工程师/.test(a.innerText) && /万元/.test(a.innerText))
          .map(a => {
            const text = a.innerText || '';
            const m = text.match(/\d+(\.\d+)?-\d+(\.\d+)?万元\/月/);
            return {url: a.href, text: text.trim().slice(0, 300), salary_raw: m ? m[0] : ''};
          })
        """)
        cards = cards or []
        result = []
        seen = set()
        for c in cards:
            url = c.get("url", "")
            # 站内相对路径 (如 /zhaogong/338388160/xxx.html) 转绝对
            if url.startswith("/"):
                url = "https://www.yupao.com" + url
            jid = re.search(r"zhaogong/(\d+)/", url)
            if not jid or jid.group(1) in seen:
                continue
            seen.add(jid.group(1))
            text = c.get("text", "")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            result.append({
                "title": lines[0] if lines else "",
                "url": url,
                "job_id": jid.group(1),
                "salary_raw": c.get("salary_raw", ""),
                "company": lines[2] if len(lines) > 2 else "",
                "industry": lines[3] if len(lines) > 3 else "",
                "text": text,
            })
        return result

    def run(self, list_url: str, max_jobs: int = 2) -> dict:
        """鱼泡是 SPA: 详情必须点击进入, 整页导航会被重定向回首页。"""
        import json as _json
        from .base import MAX_JOBS_PER_RUN

        max_jobs = min(max_jobs, MAX_JOBS_PER_RUN)
        stats = {"found": 0, "scraped": 0, "failed": 0, "jobs": []}
        self._connect()
        try:
            if not self.check_login():
                log.error(f"[{self.name}] 未登录!")
                return stats
            # 列表页: 导航可能被重定向, 多试几次 + 滚动触发懒加载
            self._navigate(list_url)
            self._scroll_slow()
            cards = self.parse_list()
            for retry in range(3):
                if cards:
                    break
                self._navigate(list_url)
                self._scroll_slow()
                cards = self.parse_list()
            stats["found"] = len(cards)
            log.info(f"[{self.name}] 列表解析到 {len(cards)} 张卡片 (重试后)")

            for idx, card in enumerate(cards[:max_jobs]):
                try:
                    if idx > 0:
                        import random as _r
                        time.sleep(_r.uniform(3, 6))
                    job = self.parse_detail(card)
                    if job:
                        stats["scraped"] += 1
                        stats["jobs"].append(job)
                        log.info(f"[{self.name}] {idx+1}/{min(len(cards),max_jobs)} ✓ {job.get('title','')[:30]}")
                except Exception as e:
                    stats["failed"] += 1
                    log.warning(f"[{self.name}] 详情失败: {e}")
                    if stats["failed"] >= 3:
                        break
        finally:
            self.close()
        return stats

    def parse_detail(self, card: dict) -> dict | None:
        """SPA 点击式详情: 点卡片(target=_blank 开新标签) → 连新标签读取 → 关闭新标签。"""
        jid = (card.get("job_id") or "").strip()
        if not jid:
            return None
        import requests as _req
        import websocket as _wsc

        clicked = self._eval(f"""(() => {{
          const a = Array.from(document.querySelectorAll('a')).find(a => a.href && a.href.includes('{jid}'));
          if (!a) return 'notfound';
          a.click(); return 'clicked';
        }})()""")
        if clicked != "clicked":
            log.warning(f"[{self.name}] 卡片 {jid} 不在当前列表页")
            return None

        # 等新标签出现 (target=_blank 异步创建)
        detail_target = None
        for _ in range(6):
            time.sleep(2)
            try:
                tabs = _req.get(f"http://127.0.0.1:{self.cdp_port}/json", timeout=5).json()
            except Exception:
                continue
            detail_target = next(
                (t for t in tabs if t.get("type") == "page" and f"/zhaogong/{jid}" in t.get("url", "")),
                None,
            )
            if detail_target:
                break
        if not detail_target:
            log.warning(f"[{self.name}] 详情标签未出现 (jid={jid})")
            return None

        old_ws = self._ws
        try:
            # 切到详情标签
            self._ws = _wsc.create_connection(detail_target["webSocketDebuggerUrl"], timeout=30)
            self._send("Runtime.enable")
            time.sleep(random.uniform(4, 6))  # SPA 内容渲染
            data = self._eval("""
            (() => {
              const body = document.body.innerText;
              return JSON.stringify({url: location.href, title: (document.title||'').slice(0,60), bodyLen: body.length});
            })()
            """)
            if not data:
                return None
            d = json.loads(data)
            if d.get("bodyLen", 0) < 400 or "zhaogong" not in d.get("url", ""):
                log.warning(f"[{self.name}] 详情页异常 (url={d.get('url','')[:40]}, len={d.get('bodyLen')})")
                return None

            text = self._eval("document.body.innerText")
            # 标题: 详情页 h1
            h1 = self._eval("(document.querySelector('h1')||{}).innerText || ''")
            title = (h1 or "").strip().split("\n")[0][:60]
            salary = ""
            m = re.search(r"(\d+(\.\d+)?-\d+(\.\d+)?万元/月)", text)
            if m:
                salary = m.group(1)
            # JD: 从职位描述/岗位要求开始
            jd_full = text
            for marker in ("职位描述", "岗位职责", "工作内容", "岗位要求"):
                if marker in text:
                    jd_full = text[text.index(marker):]
                    break
            return {
                "source": "yupao",
                "url": d.get("url", "").split("?")[0],
                "title": title,
                "salary_raw": salary,
                "jd": {"full": jd_full.strip()[:6000]},
                "welfare": [],
            }
        finally:
            # 关闭详情标签 (避免标签累积), 恢复列表页 WS
            try:
                _req.get(
                    f"http://127.0.0.1:{self.cdp_port}/json/close/{detail_target.get('id')}",
                    timeout=5,
                )
            except Exception:
                pass
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = old_ws

def build_list_url(kw: str, city: str = "") -> str:
    """按关键词构造鱼泡搜索页 (topic 页带 keywords 参数)。"""
