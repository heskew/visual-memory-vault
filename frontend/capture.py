"""Capture a public HTTP(S) page as a JPEG screenshot.

The proxy accepts the URL immediately. This module runs inside the ingest
worker: reject schemes and addresses that must never be fetched, then render
a viewport JPEG with headless Chromium. HTTP(S) responses are fetched on a
socket pinned to an address checked at connect time. Chromium does not
resolve those names itself. A blocked iframe is dropped; only a blocked
top-level page fails the capture. Callers persist the JPEG on the existing
upload path.
"""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import os
import re
import socket
import threading
import zlib
from datetime import UTC, datetime
from urllib.parse import urlparse

CAPTURE_RENDER_TIMEOUT_SEC = float(os.environ.get("CAPTURE_RENDER_TIMEOUT_SEC", "20"))
CAPTURE_MAX_URL_LENGTH = 2048
CAPTURE_MAX_IMAGE_BYTES = 8 * 1024 * 1024
CAPTURE_VIEWPORT_WIDTH = 1280
CAPTURE_VIEWPORT_HEIGHT = 720
CAPTURE_JPEG_QUALITY = 80
CAPTURE_MAX_SUBRESOURCE_BYTES = 8 * 1024 * 1024
# Chromium must not open its own sockets. QUIC bypasses an HTTP route
# handler; DNS prefetch would resolve names the worker never checked.
# ``--disable-features=WebRTC`` is not a Chromium feature name, so it leaves
# RTCPeerConnection enabled. ``disable_non_proxied_udp`` is the real IP-handling
# preference: no non-proxied UDP, which stops STUN/TURN/ICE UDP sockets.
# Page script also loses the peer-connection constructors (see
# WEBRTC_DISABLE_SCRIPT) so TCP ICE cannot bypass the pinned route either.
CHROMIUM_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-quic",
    "--dns-prefetch-disable",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
# Runs in every frame before page scripts. Shadowing the constructors stops
# a page from opening STUN/TURN/ICE sockets that Playwright's HTTP route
# never sees. Feature detection treats the API as missing.
WEBRTC_DISABLE_SCRIPT = """
(() => {
  const names = [
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
    "mozRTCPeerConnection",
  ];
  for (const name of names) {
    try {
      Object.defineProperty(window, name, {
        configurable: false,
        enumerable: false,
        writable: false,
        value: undefined,
      });
    } catch (e) {}
  }
})();
"""
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
        "host",
        "content-length",
    }
)

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
_ipv6_lock = threading.Lock()
_ipv6_egress: bool | None = None


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
            # Drop address families this host has not configured. Ordering
            # and connect fallback still handle a dual-stack answer that
            # includes an unreachable family.
            flags = getattr(socket, "AI_ADDRCONFIG", 0)
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM, flags=flags)
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


def ipv6_egress_available() -> bool:
    """True when this host has a route for public IPv6.

    A UDP connect does not send a packet; it fails immediately when the
    host has no IPv6 route (typical Cloud Run IPv4 egress). The result is
    about this machine's routing, not a DNS answer, and is cached for the
    process. It is not a record of which capture hosts are allowed.
    """
    global _ipv6_egress
    if _ipv6_egress is not None:
        return _ipv6_egress
    with _ipv6_lock:
        if _ipv6_egress is not None:
            return _ipv6_egress
        probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            probe.connect(("2001:4860:4860::8888", 53))
        except OSError:
            _ipv6_egress = False
        else:
            _ipv6_egress = True
        finally:
            probe.close()
        return _ipv6_egress


def _order_public_addresses(addresses: list[str], *, ipv6_ok: bool) -> list[str]:
    """Prefer IPv4 when this host cannot use IPv6. Keep DNS order otherwise."""
    if ipv6_ok:
        return list(addresses)
    v4 = [address for address in addresses if ":" not in address]
    v6 = [address for address in addresses if ":" in address]
    return v4 + v6


def pin_public_addresses(
    host: str, *, resolver=None, ipv6_ok: bool | None = None
) -> list[str]:
    """Fresh public IPs for ``host``. Never reuse an earlier lookup.

    A name that resolves to any non-global address is rejected, even when
    another answer is public. Every returned address passed that check.
    When IPv6 egress is unavailable, IPv4 answers come first so a dual-stack
    name that returns AAAA first is not stuck on an unreachable family.
    """
    normalized = (host or "").lower().rstrip(".")
    if not normalized:
        raise CaptureRejected("bad_url")
    if _host_is_blocked_name(normalized):
        raise CaptureRejected("blocked_url")
    literal = _ip_from_host(normalized)
    if literal is not None:
        if _ip_is_blocked(literal):
            raise CaptureRejected("blocked_url")
        return [str(literal)]
    lookup = resolver or resolve_host
    public: list[str] = []
    for address in lookup(normalized):
        try:
            parsed = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as exc:
            raise CaptureRejected("bad_url") from exc
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            parsed = parsed.ipv4_mapped
        if _ip_is_blocked(parsed):
            raise CaptureRejected("blocked_url")
        text = str(parsed)
        if text not in public:
            public.append(text)
    if not public:
        raise CaptureRejected("bad_url")
    if ipv6_ok is None:
        ipv6_ok = ipv6_egress_available()
    return _order_public_addresses(public, ipv6_ok=ipv6_ok)


def pin_public_address(host: str, *, resolver=None, ipv6_ok: bool | None = None) -> str:
    """First public IP ``pin_public_addresses`` would dial for ``host``."""
    return pin_public_addresses(host, resolver=resolver, ipv6_ok=ipv6_ok)[0]


def _is_top_level_document(request) -> bool:
    """True only for the main-frame document navigation.

    Iframe and other subframe document requests are not fatal: they are
    aborted and the screenshot of the top page continues. A missing frame
    fails closed so an unclassified navigation cannot skip the block.
    """
    resource_type = getattr(request, "resource_type", "") or ""
    is_navigation = False
    checker = getattr(request, "is_navigation_request", None)
    if callable(checker):
        try:
            is_navigation = bool(checker())
        except Exception:
            is_navigation = True
    if resource_type != "document" and not is_navigation:
        return False
    frame = getattr(request, "frame", None)
    if frame is None:
        return True
    return getattr(frame, "parent_frame", None) is None


def _inflate_content_encoding(encoding: str, body: bytes) -> bytes:
    """Return uncompressed bytes. Unknown encodings are not passed through."""
    try:
        if encoding in {"gzip", "x-gzip"}:
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error, ValueError) as exc:
        raise CaptureTransient("render_failed") from exc
    raise CaptureTransient("render_failed")


def _decode_for_fulfill(
    header_pairs: list[tuple[str, str]], body: bytes
) -> tuple[dict[str, str], bytes]:
    """Playwright fulfill wants decoded bytes and no Content-Encoding.

    ``http.client`` already removes chunked transfer encoding. Content-Length
    is dropped with the other hop-by-hop headers because decompression changes
    the size.
    """
    encodings: list[str] = []
    kept: dict[str, str] = {}
    for key, value in header_pairs:
        lowered = key.lower()
        if lowered in _HOP_BY_HOP:
            continue
        if lowered == "content-encoding":
            encodings.extend(
                part.strip().lower() for part in value.split(",") if part.strip()
            )
            continue
        kept[key] = value
    for encoding in encodings:
        if encoding in {"", "identity"}:
            continue
        body = _inflate_content_encoding(encoding, body)
    return kept, body


def _outbound_headers(headers: dict | None) -> dict[str, str]:
    """Copy request headers, but ask for an uncompressed body.

    Chromium's ``Accept-Encoding`` (gzip, brotli, zstd) would make the origin
    return bytes Playwright cannot fulfill. ``identity`` keeps the SSRF pin
    and matches what common public sites send when compression is refused.
    """
    outbound: dict[str, str] = {}
    for key, value in (headers or {}).items():
        lowered = key.lower()
        if (
            lowered in _HOP_BY_HOP
            or lowered.startswith(":")
            or lowered == "accept-encoding"
        ):
            continue
        outbound[key] = value
    outbound["Accept-Encoding"] = "identity"
    return outbound


def _exchange_on_pin(
    *,
    scheme: str,
    ascii_host: str,
    port: int,
    ip: str,
    method: str,
    path: str,
    body: bytes | None,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP(S) exchange whose TCP peer is ``ip`` and whose SNI is the name."""
    if scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            ascii_host, port, timeout=10
        )
    else:
        conn = http.client.HTTPConnection(ascii_host, port, timeout=10)

    def connect_to_pin(address, timeout=None, source_address=None):
        conn_port = address[1]
        return socket.create_connection((ip, conn_port), timeout, source_address)

    conn._create_connection = connect_to_pin
    try:
        conn.request(method or "GET", path, body=body, headers=headers)
        response = conn.getresponse()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > CAPTURE_MAX_SUBRESOURCE_BYTES:
                raise CaptureTransient("render_failed")
            chunks.append(chunk)
        decoded_headers, decoded = _decode_for_fulfill(
            response.getheaders(), b"".join(chunks)
        )
        return response.status, decoded_headers, decoded
    finally:
        conn.close()


def fetch_pinned_response(
    method: str,
    url: str,
    headers: dict | None,
    body: bytes | None,
    *,
    resolver=None,
    ipv6_ok: bool | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """HTTP(S) exchange connected only to addresses from a fresh public pin.

    The TCP peer is one of those IPs. TLS still uses the original hostname
    for SNI and certificate checks. Chromium never resolves this URL itself.
    If the first validated address cannot be dialed, the next validated
    public address is tried. A private address is never a fallback.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise CaptureRejected("unsupported_scheme")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise CaptureRejected("bad_url")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CaptureRejected("bad_url") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    candidates = pin_public_addresses(host, resolver=resolver, ipv6_ok=ipv6_ok)
    ascii_host = host.encode("idna").decode("ascii")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    outbound = _outbound_headers(headers)
    last_exc: Exception | None = None
    for ip in candidates:
        try:
            return _exchange_on_pin(
                scheme=scheme,
                ascii_host=ascii_host,
                port=port,
                ip=ip,
                method=method or "GET",
                path=path,
                body=body,
                headers=outbound,
            )
        except (CaptureRejected, CaptureTransient):
            raise
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            last_exc = exc
    raise CaptureTransient("render_failed") from last_exc


def handle_capture_route(route, blocked: dict, *, resolver=None, fetch=None) -> None:
    """Abort a blocked request, or fulfill it from a pinned connection.

    ``route.continue_()`` is only used for non-network URLs (data/blob/about).
    Every HTTP(S) request is re-resolved here and served by ``fetch``, so
    Chromium cannot connect to an address this process did not just validate.
    """
    request = route.request
    try:
        scheme = (urlparse(getattr(request, "url", "") or "").scheme or "").lower()
        if scheme in _NON_NETWORK_SCHEMES:
            route.continue_()
            return
        ensure_request_allowed(request.url, resolver=resolver)
        fetcher = fetch if fetch is not None else fetch_pinned_response
        body = getattr(request, "post_data_buffer", None)
        if body is None:
            posted = getattr(request, "post_data", None)
            if isinstance(posted, str):
                body = posted.encode("utf-8")
            elif isinstance(posted, bytes):
                body = posted
        status, response_headers, response_body = fetcher(
            getattr(request, "method", None) or "GET",
            request.url,
            dict(getattr(request, "headers", None) or {}),
            body,
            resolver=resolver,
        )
        route.fulfill(status=status, headers=response_headers, body=response_body)
    except CaptureRejected:
        if _is_top_level_document(request):
            blocked["document"] = True
        route.abort()
    except Exception:
        route.abort()


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
    blocked = {"document": False}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=list(CHROMIUM_LAUNCH_ARGS),
            )
            context = browser.new_context(
                viewport={
                    "width": CAPTURE_VIEWPORT_WIDTH,
                    "height": CAPTURE_VIEWPORT_HEIGHT,
                },
                ignore_https_errors=False,
            )
            context.add_init_script(WEBRTC_DISABLE_SCRIPT)
            page = context.new_page()
            page.set_default_navigation_timeout(timeout_ms)
            page.set_default_timeout(timeout_ms)

            def guard(route) -> None:
                handle_capture_route(route, blocked)

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
