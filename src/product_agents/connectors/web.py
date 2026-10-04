"""Web-page connector for competitor scans. Read-only, with SSRF protection.

A person gives it a list of URLs they are entitled to read. It fetches each one, extracts the
visible text, and stores it as evidence. Each fetch is a dated snapshot, so a changed page is
a new record rather than an overwrite of old evidence.

Safety rules (see docs/threat-model.md):
  * http and https only, ports 80 and 443 only, no credentials in the URL
  * every address the host resolves to must be a public one: no loopback, private, link-local
    (cloud metadata) or reserved ranges. Redirects are followed by hand and re-checked each hop
  * size and time limits, no cookies
Limitation: a hostile DNS server could answer differently between the check and the request
(DNS rebinding). Run this on a network where internal services are not reachable if that matters.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Iterable, Iterator
from urllib.parse import urljoin, urlparse

import httpx

from ..models import RawRecord
from .base import BaseConnector

USER_AGENT = "product-agents-research/0.1 (self-hosted, read-only)"
ALLOWED_PORTS = (None, 80, 443)
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class UnsafeUrl(ValueError):
    """The URL is not one this connector is willing to fetch."""


def _resolve(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def check_public_url(url: str, resolver: Callable[[str, int], list[str]] = _resolve) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrl(f"only http and https are allowed: {url}")
    if not parsed.hostname:
        raise UnsafeUrl(f"no host in {url}")
    if parsed.username or parsed.password:
        raise UnsafeUrl("URLs with embedded credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrl(f"bad port in {url}") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl(f"port {port} is not allowed (only 80 and 443)")
    try:
        addresses = resolver(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise UnsafeUrl(f"could not resolve {parsed.hostname}") from exc
    if not addresses:
        raise UnsafeUrl(f"could not resolve {parsed.hostname}")
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeUrl(f"{parsed.hostname} resolves to a non-public address ({ip})")


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract_text(html: str, limit: int = 200_000) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    body = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    title = " ".join(parser.title.split())
    text = f"{title}\n\n{body}".strip() if title else body
    return text[:limit]


class WebPagesConnector(BaseConnector):
    id = "web"
    category = "research"
    scopes = ("read",)

    def __init__(
        self,
        urls: Iterable[str],
        client: httpx.Client | None = None,
        resolver: Callable[[str, int], list[str]] = _resolve,
        max_bytes: int = 1_000_000,
        max_redirects: int = 3,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        super().__init__()
        self.urls = [u.strip() for u in urls if u.strip() and not u.strip().startswith("#")]
        self.client = client or httpx.Client(
            follow_redirects=False, timeout=20.0, headers={"User-Agent": USER_AGENT}
        )
        self.resolver, self.max_bytes, self.max_redirects, self.now = resolver, max_bytes, max_redirects, now
        self.errors: list[str] = []

    def _get(self, url: str) -> str:
        for _ in range(self.max_redirects + 1):
            check_public_url(url, self.resolver)
            with self.client.stream("GET", url) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise UnsafeUrl("redirect without a location")
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
                if ctype not in TEXT_TYPES:
                    raise UnsafeUrl(f"not a text page ({ctype})")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UnsafeUrl(f"page is larger than {self.max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
        raise UnsafeUrl("too many redirects")

    def fetch(self, since: str | None) -> Iterator[RawRecord]:
        stamp = self.now()
        for url in self.urls:
            try:
                html = self._get(url)
            except (UnsafeUrl, httpx.HTTPError) as exc:
                self.errors.append(f"{url}: {exc}")
                continue
            text = extract_text(html)
            if text:
                yield RawRecord(
                    source_id=f"{url}#{stamp.date()}",
                    source_url=url,
                    timestamp=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    text=text,
                    segment="web",
                )
