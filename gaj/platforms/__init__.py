"""多平台 JD 抓取适配器。

用法:
    from gaj.platforms.liepin import LiepinCrawler, build_list_url
    c = LiepinCrawler()
    stats = c.run(build_list_url("AI测试", "050090"), max_jobs=10)
    # stats["jobs"]  → 统一 job dict 列表
"""

from .base import PlatformCrawler
from .liepin import LiepinCrawler, build_list_url
from .yupao import YupaoCrawler, build_list_url as build_yupao_url
from .zhaopin import ZhaopinCrawler, build_list_url as build_zhaopin_url

__all__ = ["PlatformCrawler", "LiepinCrawler", "YupaoCrawler", "ZhaopinCrawler",
           "build_list_url", "build_yupao_url", "build_zhaopin_url"]