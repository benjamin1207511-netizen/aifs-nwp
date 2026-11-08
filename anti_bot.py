#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量反爬 / 防滥用模块。

它不是「绝对防止抓取」——任何公开网页都无法做到 100% 防抓取——但可以显著提高
脚本批量抓取成本：

  1. 会话 Cookie：访问页面后签发 HMAC 签名 Cookie，API 必须带有效 Cookie；
  2. IP 滑动窗口限速：全局、HTML、API、图片分别限速（可用环境变量调整）；
  3. User-Agent 黑名单：拦截 requests / curl / scrapy / 无头浏览器 / AI 爬虫等；
  4. 扫描器路径拦截：/wp-admin、/.env、/phpmyadmin 等常见探测直接 404；
  5. 手动黑名单：blocklist.txt 里可写 IP 或 CIDR，支持热加载；
  6. 图片防盗链：带 Referer 时必须是本站；
  7. 安全响应头 + 禁止搜索引擎索引；
  8. 拦截日志：写入 anti_bot.log（超过 2 MB 自动轮转）。

对正常浏览器没有影响。站点被反向代理/Cloudflare 时，设置：
  TRUST_PROXY=1
并使用代理提供的客户端 IP 头。

可选环境变量：
  TRUST_PROXY=1                 信任 X-Forwarded-For / CF-Connecting-IP
  RATE_GLOBAL=420 RATE_HTML=60 RATE_API=180 RATE_IMAGE=360   每 60 秒限额
  ANTIBOT_LOG=0                 关闭拦截日志
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

BOT_UA_RE = re.compile(
    r"(python-requests|python-urllib|curl/|wget|scrapy|aiohttp|httpx|"
    r"go-http-client|java/|libwww|okhttp|node-fetch|axios/|"
    r"phantomjs|selenium|puppeteer|playwright|headlesschrome|electron/|"
    r"gptbot|chatgpt-user|claudebot|anthropic|ccbot|bytespider|perplexitybot|"
    r"google-extended|amazonbot|semrush|ahrefs|mj12bot|dotbot|blexbot|"
    r"petalbot|zoombot|dataforseo|screaming frog|mechanize|winhttp)",
    re.IGNORECASE,
)

# 常见漏洞扫描 / 后台探测路径，直接当作不存在。
SCANNER_PATH_RE = re.compile(
    r"^/("
    r"wp-admin|wp-login|wp-content|wp-includes|xmlrpc\.php|"
    r"phpmyadmin|pma|myadmin|admin|administrator|manager/html|"
    r"cgi-bin|boaform|HNAP1|shell|console|jenkins|solr|actuator|"
    r"\.env|\.git|\.svn|\.hg|\.aws|\.ssh|\.DS_Store|"
    r"config\.json|backup|vendor|server-status|phpinfo"
    r")",
    re.IGNORECASE,
)

EXEMPT_PATHS = {"/healthz", "/api/health", "/robots.txt", "/favicon.ico"}
BASE_DIR = Path(__file__).resolve().parent
BLOCKLIST_FILE = BASE_DIR / "blocklist.txt"
ANTIBOT_LOG = BASE_DIR / "anti_bot.log"
LOG_ENABLED = os.environ.get("ANTIBOT_LOG", "1") != "0"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "")))
    except (TypeError, ValueError):
        return default


@dataclass
class Decision:
    allowed: bool
    status: int = 200
    reason: str = "blocked"
    set_cookie: bool = False
    retry_after: int = 0


class RequestGuard:
    def __init__(self) -> None:
        self.secret = os.environ.get("ANTIBOT_SECRET") or secrets.token_hex(32)
        self.cookie_name = "bamc_sid"
        self.max_age = 12 * 3600
        self.trust_proxy = os.environ.get("TRUST_PROXY", "0") == "1"
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.buckets: dict[tuple[str, str], list[float]] = {}
        self.last_cleanup = time.time()
        self.limits = {
            "global": (_env_int("RATE_GLOBAL", 420), 60),
            "html": (_env_int("RATE_HTML", 60), 60),
            "api": (_env_int("RATE_API", 180), 60),
            "image": (_env_int("RATE_IMAGE", 360), 60),
        }
        self.blocked_networks: list = []
        self.blocklist_mtime = 0.0
        self.last_blocklist_check = 0.0
        self.stats = {"blocked_ua": 0, "blocked_scanner": 0, "blocked_ip": 0,
                      "blocked_hotlink": 0, "blocked_rate": 0}

    # ---------------- 会话 Cookie ----------------

    def _sign(self, ts: str) -> str:
        return hmac.new(self.secret.encode("utf-8"), ts.encode("ascii"), hashlib.sha256).hexdigest()[:32]

    def issue_cookie(self, secure: bool = False) -> str:
        ts = str(int(time.time()))
        token = "%s.%s" % (ts, self._sign(ts))
        parts = [
            "%s=%s" % (self.cookie_name, token),
            "Path=/",
            "Max-Age=%d" % self.max_age,
            "HttpOnly",
            "SameSite=Lax",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _valid_cookie(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        ts_text, sig = value.split(".", 1)
        if not ts_text.isdigit():
            return False
        try:
            ts = int(ts_text)
        except ValueError:
            return False
        now = int(time.time())
        if ts > now + 60 or now - ts > self.max_age:
            return False
        return hmac.compare_digest(sig, self._sign(ts_text))

    # ---------------- 黑名单 ----------------

    def _load_blocklist(self, now: float) -> None:
        if now - self.last_blocklist_check < 30:
            return
        self.last_blocklist_check = now
        if not BLOCKLIST_FILE.is_file():
            return
        try:
            mtime = BLOCKLIST_FILE.stat().st_mtime
            if mtime == self.blocklist_mtime:
                return
            networks = []
            for raw in BLOCKLIST_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    networks.append(ipaddress.ip_network(line, strict=False))
                except ValueError:
                    continue
            self.blocked_networks = networks
            self.blocklist_mtime = mtime
        except OSError:
            pass

    def _in_blocklist(self, ip: str) -> bool:
        if not self.blocked_networks:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.blocked_networks)

    # ---------------- 日志 ----------------

    def _log_block(self, ip: str, path: str, reason: str, ua: str) -> None:
        if not LOG_ENABLED:
            return
        line = "%s\t%s\t%s\t%s\t%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), ip, path, reason, (ua or "-")[:120],
        )
        with self.log_lock:
            try:
                if ANTIBOT_LOG.exists() and ANTIBOT_LOG.stat().st_size > 2 * 1024 * 1024:
                    ANTIBOT_LOG.replace(ANTIBOT_LOG.with_suffix(".log.1"))
                with ANTIBOT_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass

    # ---------------- IP / 限速 ----------------

    def client_ip(self, headers, remote_addr: str) -> str:
        if self.trust_proxy:
            for name in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
                value = headers.get(name)
                if value:
                    return value.split(",")[0].strip()
        return remote_addr or "unknown"

    def _hit(self, ip: str, bucket: str) -> tuple[bool, int]:
        limit, window = self.limits[bucket]
        now = time.time()
        key = (ip, bucket)
        with self.lock:
            values = self.buckets.setdefault(key, [])
            cutoff = now - window
            while values and values[0] < cutoff:
                values.pop(0)
            if len(values) >= limit:
                retry = max(1, int(values[0] + window - now))
                return False, min(60, retry)
            values.append(now)
            if now - self.last_cleanup > 300:
                self._cleanup(now)
                self.last_cleanup = now
            return True, 0

    def _cleanup(self, now: float) -> None:
        cutoff = now - 3600
        for key in list(self.buckets):
            values = self.buckets[key]
            if not values or values[-1] < cutoff:
                self.buckets.pop(key, None)

    # ---------------- 主检查 ----------------

    def check(self, ip: str, path: str, headers, query: str = "") -> Decision:
        if path in EXEMPT_PATHS:
            return Decision(True)

        self._load_blocklist(time.time())

        ua = str(headers.get("User-Agent") or "").strip()
        if not ua or BOT_UA_RE.search(ua):
            self.stats["blocked_ua"] += 1
            self._log_block(ip, path, "bad-ua", ua)
            return Decision(False, 403, "Automated access blocked", retry_after=0)

        if SCANNER_PATH_RE.match(path):
            self.stats["blocked_scanner"] += 1
            self._log_block(ip, path, "scanner", ua)
            return Decision(False, 404, "Not found")

        if self._in_blocklist(ip):
            self.stats["blocked_ip"] += 1
            self._log_block(ip, path, "blocklist", ua)
            return Decision(False, 403, "Forbidden")

        # 图片防盗链：有 Referer 时必须是本站域名。
        if path == "/api/img":
            referer = headers.get("Referer")
            if referer:
                host = str(headers.get("Host") or "")
                ref_host = urlparse(referer).netloc
                if host and ref_host and ref_host != host:
                    self.stats["blocked_hotlink"] += 1
                    self._log_block(ip, path, "hotlink", ua)
                    return Decision(False, 403, "Hotlinking is not allowed")

        bucket = "html" if (path in ("/", "/index.html") or path.endswith(".html")) else None
        if path.startswith("/api/"):
            bucket = "api"
        if path == "/api/img":
            bucket = "image"

        ok, retry = self._hit(ip, "global")
        if not ok:
            self.stats["blocked_rate"] += 1
            self._log_block(ip, path, "rate-global", ua)
            return Decision(False, 429, "Too many requests", retry_after=retry)
        if bucket:
            ok, retry = self._hit(ip, bucket)
            if not ok:
                self.stats["blocked_rate"] += 1
                self._log_block(ip, path, "rate-" + bucket, ua)
                return Decision(False, 429, "Too many requests", retry_after=retry)

        cookie = self._cookie_value(headers.get("Cookie"), self.cookie_name)

        if path in ("/", "/index.html") or path.endswith(".html"):
            if not self._valid_cookie(cookie):
                return Decision(True, set_cookie=True)
            return Decision(True)

        if path.startswith("/api/") and not self._valid_cookie(cookie):
            return Decision(False, 403, "Session cookie required")

        return Decision(True)

    @staticmethod
    def _cookie_value(cookie_header: str | None, name: str) -> str | None:
        if not cookie_header:
            return None
        for part in cookie_header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None


SECURITY_HEADERS = {
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}
