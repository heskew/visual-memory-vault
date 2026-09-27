"""Capture a public HTTP(S) page as a JPEG screenshot.

The proxy accepts the URL immediately. This module runs inside the ingest
worker: reject schemes and addresses that must never be fetched, then render
a viewport JPEG with headless Chromium. Callers persist that JPEG on the
existing upload path.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
from datetime import UTC, datetime
from urllib.parse import urlparse

CAPTURE_RENDER_TIMEOUT_SEC = float(os.environ.get("CAPTURE_RENDER_TIMEOUT_SEC", "20"))
CAPTURE_MAX_URL_LENGTH = 2048
CAPTURE_MAX_IMAGE_BYTES = 8 * 1024 * 1024
CAPTURE_VIEWPORT_WIDTH = 1280
CAPTURE_VIEWPORT_HEIGHT = 720
CAPTURE_JPEG_QUALITY = 80

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_NON_NETWORK_SCHEMES = frozenset({"data", "blob", "about"})
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google",
        "metadata.google.internal",
        "metadata.google.com",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".metadata.google.internal",
)
_LABEL_RE = re.compile(r"(?:0x[0-9a-fA-F]+|\d+)")
_dns_lock = threading.Lock()


class CaptureRejected(Exception):
    """Terminal capture failure. ``reason`` is a stable job error code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CaptureTransient(Exception):
    """Retryable capture failure. The job stays pending."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def capture_store_metadata(
    source_url: str, captured_at: str, image_url: str
) -> dict[str, str]:
    """Structured fields the Flair store must keep for a URL capture."""
    return {
        "image_url": image_url,
        "source_url": source_url,
        "captured_at": captured_at,
        "capture_kind": "url",
    }


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Non-global covers loopback, link-local (169.254/16, metadata), RFC1918,
    # and shared ranges. Public pages are global addresses only.
    return not ip.is_global


def _obscure_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Decode decimal, hex, octal, and short inet_aton forms browsers still accept."""
    if host.lower().startswith("0x") and re.fullmatch(r"0x[0-9a-fA-F]+", host):
        try:
            value = int(host, 16)
        except ValueError:
            return None
        if 0 <= value <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(value)
        return None
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    if not all(_LABEL_RE.fullmatch(part) for part in parts):
        return None
    numbers: list[int] = []
    for part in parts:
        try:
            if part.lower().startswith("0x"):
                numbers.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None
    try:
        if len(numbers) == 4:
            return ipaddress.IPv4Address(".".join(str(number) for number in numbers))
        if len(numbers) == 1:
            return ipaddress.IPv4Address(numbers[0])
        if len(numbers) == 2:
            return ipaddress.IPv4Address((numbers[0] << 24) | numbers[1])
        return ipaddress.IPv4Address(
            (numbers[0] << 24) | (numbers[1] << 16) | numbers[2]
        )
    except (ValueError, ipaddress.AddressValueError):
        return None


def _ip_from_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = host.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        parsed = None
    else:
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            return parsed.ipv4_mapped
        return parsed
    if ":" in candidate:
        return None
    return _obscure_ipv4(candidate)


def resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to IP strings. DNS blips are retryable; NXDOMAIN is not."""
    # getaddrinfo has no per-call timeout. Hold the process default only for
    # this lookup so a blackhole resolver cannot stall the worker.
    with _dns_lock:
        previous = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(5.0)
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except TimeoutError as exc:
            raise CaptureTransient("dns_unavailable") from exc
        except socket.gaierror as exc:
            again = getattr(socket, "EAI_AGAIN", None)
            if again is not None and exc.errno == again:
                raise CaptureTransient("dns_unavailable") from exc
            raise CaptureRejected("bad_url") from exc
        except OSError as exc:
            raise CaptureTransient("dns_unavailable") from exc
        finally:
            socket.setdefaulttimeout(previous)
    addresses = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            addresses.append(str(sockaddr[0]).split("%", 1)[0])
    if not addresses:
        raise CaptureRejected("bad_url")
    return addresses


def _host_is_blocked_name(host: str) -> bool:
    if host in _BLOCKED_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)


def validate_capture_url(url: str, *, resolver=None, limit_length: bool = True) -> str:
    """Return a stripped http(s) URL, or raise a terminal/retryable capture error."""
    if not isinstance(url, str):
        raise CaptureRejected("bad_url")
    raw = url.strip()
    if (
        not raw
        or any(char in raw for char in "\r\n\t ")
        or (limit_length and len(raw) > CAPTURE_MAX_URL_LENGTH)
    ):
        raise CaptureRejected("bad_url")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise CaptureRejected("bad_url")
    if scheme not in _ALLOWED_SCHEMES:
        raise CaptureRejected("unsupported_scheme")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CaptureRejected("bad_url") from exc
    if port == 0:
        raise CaptureRejected("bad_url")
    host = parsed.hostname
    if not host or host != host.strip("."):
        raise CaptureRejected("bad_url")
    host = host.lower().rstrip(".")
    if not host or _host_is_blocked_name(host):
        raise CaptureRejected("blocked_url")
    ip = _ip_from_host(host)
    if ip is not None:
        if _ip_is_blocked(ip):
            raise CaptureRejected("blocked_url")
        return raw
    lookup = resolver or resolve_host
    for address in lookup(host):
        try:
            resolved = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as exc:
            raise CaptureRejected("bad_url") from exc
        if _ip_is_blocked(resolved):
            raise CaptureRejected("blocked_url")
    return raw


def ensure_request_allowed(url: str, *, resolver=None) -> None:
    """Allow non-network subresources; every HTTP(S) hop must pass validation."""
    scheme = (urlparse(url).scheme or "").lower()
    if scheme in _NON_NETWORK_SCHEMES:
        return
    validate_capture_url(url, resolver=resolver, limit_length=False)


def capture_screenshot_bytes(url: str) -> bytes:
    """Validate ``url`` and return a viewport JPEG. Safe to run off the event loop."""
    normalized = validate_capture_url(url)
    data = render_page_screenshot(normalized)
    if not data:
        raise CaptureTransient("render_failed")
    if len(data) > CAPTURE_MAX_IMAGE_BYTES:
        raise CaptureRejected("render_too_large")
    return data


def render_page_screenshot(url: str) -> bytes:
    """Headless Chromium viewport JPEG. Timeouts are terminal; other blips retry."""
    validate_capture_url(url)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureRejected("render_unavailable") from exc

    timeout_ms = max(1, int(CAPTURE_RENDER_TIMEOUT_SEC * 1000))
    cache: dict[str, list[str]] = {}

    def cached_resolve(host: str) -> list[str]:
        if host not in cache:
            cache[host] = resolve_host(host)
        return cache[host]

    blocked = {"document": False}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = browser.new_context(
                viewport={
                    "width": CAPTURE_VIEWPORT_WIDTH,
                    "height": CAPTURE_VIEWPORT_HEIGHT,
                },
                ignore_https_errors=False,
            )
            page = context.new_page()
            page.set_default_navigation_timeout(timeout_ms)
            page.set_default_timeout(timeout_ms)

            def guard(route) -> None:
                request = route.request
                try:
                    ensure_request_allowed(request.url, resolver=cached_resolve)
                except CaptureRejected:
                    is_document = request.resource_type == "document"
                    is_navigation = False
                    checker = getattr(request, "is_navigation_request", None)
                    if callable(checker):
                        is_navigation = bool(checker())
                    if is_document or is_navigation:
                        blocked["document"] = True
                    route.abort()
                    return
                except CaptureTransient:
                    route.abort()
                    return
                route.continue_()

            page.route("**/*", guard)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeout as exc:
                if blocked["document"]:
                    raise CaptureRejected("blocked_url") from exc
                raise CaptureRejected("render_timeout") from exc
            except CaptureRejected:
                raise
            except CaptureTransient:
                raise
            except Exception as exc:
                if blocked["document"]:
                    raise CaptureRejected("blocked_url") from exc
                raise CaptureTransient("render_failed") from exc
            if blocked["document"]:
                raise CaptureRejected("blocked_url")
            try:
                return page.screenshot(
                    type="jpeg",
                    quality=CAPTURE_JPEG_QUALITY,
                    full_page=False,
                )
            except PlaywrightTimeout as exc:
                raise CaptureRejected("render_timeout") from exc
            except CaptureRejected:
                raise
            except CaptureTransient:
                raise
            except Exception as exc:
                raise CaptureTransient("render_failed") from exc
    except (CaptureRejected, CaptureTransient):
        raise
    except Exception as exc:
        text = str(exc).lower()
        if "executable doesn't exist" in text or "browsertype.launch" in text:
            raise CaptureRejected("render_unavailable") from exc
        raise CaptureTransient("render_failed") from exc
