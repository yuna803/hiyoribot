"""返回公开网页的搜索摘要与来源；不抓取结果页面全文。"""

from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit

from ddgs import DDGS
from ddgs.exceptions import DDGSException


def public_source_url(value: str) -> bool:
    """来源链接只允许 HTTP(S)，排除脚本、本机名称和私网 IP。"""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if url.scheme not in ("http", "https") or not host or url.username or url.password:
            return False
        normalized_host = host.lower().rstrip(".")
        if normalized_host == "localhost" or normalized_host.endswith((".localhost", ".local", ".internal")):
            return False
        _port = url.port  # 无效端口会抛出 ValueError。
        try:
            return ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def search(query: str, timelimit: str | None = None) -> dict:
    try:
        # 固定一个搜索后端，限制请求超时和结果数量；DDGS_PROXY 可设置本机代理。
        rows = DDGS(timeout=8).text(query, region="cn-zh", safesearch="moderate",
                                    max_results=5, backend="bing", timelimit=timelimit)
    except (DDGSException, OSError):
        # 上游异常可能带查询参数或代理信息，不能直接转给模型或浏览器。
        return {"error": "联网搜索失败或未取得可用结果，请稍后重试；不要编造搜索结果。"}

    results, seen = [], set()
    for row in rows:
        url = str(row.get("href") or "").strip()
        if len(url) > 2048 or url in seen or not public_source_url(url):
            continue
        seen.add(url)
        results.append({"title": " ".join(str(row.get("title") or urlsplit(url).hostname).split())[:160],
                        "url": url, "snippet": " ".join(str(row.get("body") or "").split())[:500]})
        if len(results) == 5:
            break
    return {"query": query, "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "results": results, "evidence": "搜索摘要，未读取网页全文；检索时间不等于文章发布时间。"}
