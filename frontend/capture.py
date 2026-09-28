"""Capture a public HTTP(S) page as a JPEG screenshot.

The proxy accepts the URL immediately. This module runs inside the ingest
worker: reject schemes and addresses that must never be fetched, then render
a viewport JPEG with headless Chromium. HTTP(S) responses are fetched on a
socket pinned to an address checked at connect time. Chromium does not
resolve those names itself. A blocked iframe is dropped; only a blocked
top-level page fails the capture. Callers persist the JPEG on the existing
upload path.

After navigation, the same page object supplies Flair metadata: document
title (``page.title()``), the post-redirect URL (``page.url``), and a capped
list of outbound ``{href, text}`` links. Link targets are not requested.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

CAPTURE_RENDER_TIMEOUT_SEC = float(os.environ.get("CAPTURE_RENDER_TIMEOUT_SEC", "20"))
CAPTURE_MAX_URL_LENGTH = 2048
CAPTURE_MAX_IMAGE_BYTES = 8 * 1024 * 1024
CAPTURE_VIEWPORT_WIDTH = 1280
CAPTURE_VIEWPORT_HEIGHT = 720
CAPTURE_JPEG_QUALITY = 80
CAPTURE_MAX_SUBRESOURCE_BYTES = 8 * 1024 * 1024
# First N distinct http(s) anchors in document order. Same-document links
# (including fragments) are skipped so the cap is spent on other targets.
# This is document order, not viewport visibility: links below the fold count.
# Targets are not fetched. Over-long hrefs are dropped, not truncated.
CAPTURE_MAX_OUTBOUND_LINKS = 50
CAPTURE_MAX_LINK_TEXT_CHARS = 160
CAPTURE_MAX_PAGE_TITLE_CHARS = 300
# Flair rejects custom_metadata above 64KB. Stay under that with room for
# receipt fields the model may add next to these capture fields. Extra links
# are dropped from the end; the screenshot and core fields still store.
CAPTURE_METADATA_BUDGET_BYTES = 48 * 1024
# Defense in depth if a page returns more raw rows than the in-page cap.
_MAX_RAW_LINKS_SCANNED = CAPTURE_MAX_OUTBOUND_LINKS * 20
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
# In-page read of anchors already matched by Playwright's ``a[href]`` locator.
# Uses the DOM ``href`` property (absolute URL) and short text. No innerHTML,
# and no request to any link target. ``limits`` is
# ``{max, maxHref, maxText}``.
OUTBOUND_LINKS_SCRIPT = """
(anchors, limits) => {
  const max = Number(limits && limits.max) || 0;
  const maxHref = Number(limits && limits.maxHref) || 0;
  const maxText = Number(limits && limits.maxText) || 0;
  const seen = new Set();
  const out = [];
  const bare = (value) => {
    try {
      const parsed = new URL(String(value), location.href);
      parsed.hash = "";
      return parsed.href;
    } catch (e) {
      return "";
    }
  };
  const here = bare(location.href);
  for (const a of anchors) {
    if (out.length >= max) break;
    let href = "";
    try {
      href = String(a.href || "");
    } catch (e) {
      continue;
    }
    if (!href || (maxHref && href.length > maxHref) || seen.has(href)) continue;
    const protocol = String(a.protocol || "").toLowerCase();
    if (protocol !== "http:" && protocol !== "https:") continue;
    if (here && bare(href) === here) continue;
    seen.add(href);
    let text = "";
    try {
      text = a.innerText || a.textContent || a.getAttribute("aria-label") || "";
    } catch (e) {
      text = "";
    }
    text = String(text).replace(/\\s+/g, " ").trim();
    if (maxText && text.length > maxText) text = text.slice(0, maxText).trim();
    out.push({ href: href, text: text });
  }
  return out;
}
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


@dataclass(frozen=True)
class CapturedPage:
    """Viewport JPEG plus the page facts collected at the same moment."""

    image: bytes
    page_title: str = ""
    final_url: str = ""
    outbound_links: tuple[dict[str, str], ...] = ()


_TAG_RE = re.compile(r"<[^>]*>")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_short_text(value: object, limit: int) -> str:
    """Whitespace-collapsed plain text. Tags and control characters are removed."""
    if not isinstance(value, str) or limit <= 0:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = _CONTROL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _strip_userinfo(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return parsed._replace(netloc=netloc).geturl()


def _http_url_or_blank(value: object) -> str:
    """Absolute http(s) URL with no userinfo, or ``""``.

    Over-long values are dropped. Nothing is fetched.
    """
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or any(char in raw for char in "\r\n\t "):
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        return ""
    cleaned = _strip_userinfo(raw)
    if not cleaned:
        return ""
    rebuilt = urlparse(cleaned)._replace(scheme=scheme).geturl()
    if len(rebuilt) > CAPTURE_MAX_URL_LENGTH or any(
        char in rebuilt for char in "\r\n\t "
    ):
        return ""
    return rebuilt


def _without_fragment(url: str) -> str:
    return urlparse(url)._replace(fragment="").geturl()


def normalize_outbound_links(
    raw: object, *, page_url: str = ""
) -> list[dict[str, str]]:
    """Capped ``{href, text}`` list. Same-document links and non-http(s) are dropped.

    First occurrence of an href wins (document order). Link targets are not
    fetched. Extra keys on each row, including any HTML blob, are discarded.
    """
    if not isinstance(raw, list):
        return []
    here = _without_fragment(_http_url_or_blank(page_url))
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for item in raw[:_MAX_RAW_LINKS_SCANNED]:
        if len(links) >= CAPTURE_MAX_OUTBOUND_LINKS:
            break
        if not isinstance(item, dict):
            continue
        href = _http_url_or_blank(item.get("href"))
        if not href or href in seen:
            continue
        if here and _without_fragment(href) == here:
            continue
        seen.add(href)
        links.append(
            {
                "href": href,
                "text": _clean_short_text(
                    item.get("text"), CAPTURE_MAX_LINK_TEXT_CHARS
                ),
            }
        )
    return links


def _fit_capture_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Shorten enrich fields until the JSON fits ``CAPTURE_METADATA_BUDGET_BYTES``.

    Core fields (``image_url``, ``source_url``, ``captured_at``,
    ``capture_kind``) stay. Links drop from the end of document order. A
    missing title or an empty link list is not an error.
    """
    fitted = dict(metadata)
    links = fitted.get("outbound_links")
    if isinstance(links, list):
        fitted["outbound_links"] = list(links)
    while len(json.dumps(fitted).encode("utf-8")) > CAPTURE_METADATA_BUDGET_BYTES:
        current = fitted.get("outbound_links")
        if isinstance(current, list) and current:
            current.pop()
            if not current:
                del fitted["outbound_links"]
            continue
        if "page_title" in fitted:
            del fitted["page_title"]
            continue
        if "final_url" in fitted:
            del fitted["final_url"]
            continue
        break
    return fitted


def collect_page_facts(page) -> dict[str, object]:
    """Title, final URL, and capped links from a Playwright page.

    ``page.title()`` and ``page.url`` are the Playwright page APIs. Links come
    from ``locator("a[href]").evaluate_all`` (the DOM ``href`` property and
    short text), then ``normalize_outbound_links``. Any failure leaves that
    field empty; the screenshot still proceeds. Link targets are not fetched.
    """
    title = ""
    final_url = ""
    raw: list = []
    try:
        got = page.title()
        if isinstance(got, str):
            title = got
    except Exception:
        title = ""
    try:
        got_url = page.url
        if isinstance(got_url, str):
            final_url = got_url
    except Exception:
        final_url = ""
    try:
        evaluated = page.locator("a[href]").evaluate_all(
            OUTBOUND_LINKS_SCRIPT,
            {
                "max": CAPTURE_MAX_OUTBOUND_LINKS,
                "maxHref": CAPTURE_MAX_URL_LENGTH,
                "maxText": CAPTURE_MAX_LINK_TEXT_CHARS,
            },
        )
        if isinstance(evaluated, list):
            raw = evaluated
    except Exception:
        raw = []
    cleaned_final = _http_url_or_blank(final_url)
    return {
        "page_title": _clean_short_text(title, CAPTURE_MAX_PAGE_TITLE_CHARS),
        "final_url": cleaned_final,
        "outbound_links": normalize_outbound_links(raw, page_url=cleaned_final),
    }


def _empty_page_facts() -> dict[str, object]:
    return {"page_title": "", "final_url": "", "outbound_links": []}


def capture_store_metadata(
    source_url: str,
    captured_at: str,
    image_url: str,
    *,
    page_title: str | None = None,
    final_url: str | None = None,
    outbound_links: list | None = None,
) -> dict[str, object]:
    """Structured fields the Flair store must keep for a URL capture.

    ``source_url``, ``captured_at``, and ``capture_kind`` are always present.
    ``page_title``, ``final_url``, and ``outbound_links`` are added only when
    they survive cleaning. A missing title or an empty link list is omitted,
    not an error. ``outbound_links`` is at most ``CAPTURE_MAX_OUTBOUND_LINKS``
    ``{"href", "text"}`` objects, and the whole dict is shortened until its
    JSON fits ``CAPTURE_METADATA_BUDGET_BYTES``. Link targets are not fetched.
    """
    metadata: dict[str, object] = {
        "image_url": image_url,
        "source_url": source_url,
        "captured_at": captured_at,
        "capture_kind": "url",
    }
    title = _clean_short_text(page_title, CAPTURE_MAX_PAGE_TITLE_CHARS)
    if title:
        metadata["page_title"] = title
    cleaned_final = _http_url_or_blank(final_url)
    if cleaned_final:
        metadata["final_url"] = cleaned_final
    if outbound_links is not None:
        links = normalize_outbound_links(outbound_links, page_url=cleaned_final)
        if links:
            metadata["outbound_links"] = links
    return _fit_capture_metadata(metadata)


def remember_page_facts(record: dict, captured: CapturedPage) -> None:
    """Copy fitted title, final URL, and links onto a durable job record.

    Empty fields are left unset. The list matches what ``store_memory`` is
    asked to copy, including the metadata size budget.
    """
    for key in ("page_title", "final_url", "outbound_links"):
        record.pop(key, None)
    if captured.page_title:
        record["page_title"] = captured.page_title
    if captured.final_url:
        record["final_url"] = captured.final_url
    if captured.outbound_links:
        record["outbound_links"] = [dict(link) for link in captured.outbound_links]
    fitted = capture_metadata_from_record(record)
    for key in ("page_title", "final_url", "outbound_links"):
        record.pop(key, None)
        if key in fitted:
            record[key] = fitted[key]


def capture_metadata_from_record(
    record: dict, *, image_url: str = ""
) -> dict[str, object]:
    """Build Flair metadata from a job record or a previous metadata dict.

    Reads ``image_url`` or ``image_path``. Non-string enrich fields are
    ignored so a corrupt job still stores the screenshot fields.
    """
    title = record.get("page_title")
    final = record.get("final_url")
    links = record.get("outbound_links")
    return capture_store_metadata(
        record.get("source_url") or "",
        record.get("captured_at") or "",
        record.get("image_url") or record.get("image_path") or image_url or "",
        page_title=title if isinstance(title, str) else "",
        final_url=final if isinstance(final, str) else "",
        outbound_links=links if isinstance(links, list) else None,
    )


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


def _decompress_capped(payload: bytes, wbits: int) -> bytes:
    """Inflate ``payload`` without going past the subresource cap.

    ``decompress(..., max_length=room + 1)`` never returns the rest of a
    bomb, so a small gzip or deflate body cannot expand into an unbounded
    buffer. One extra byte is enough to see that the cap would be crossed;
    that byte is not kept.
    """
    limit = CAPTURE_MAX_SUBRESOURCE_BYTES
    decompressor = zlib.decompressobj(wbits)
    out = bytearray()
    pending = payload
    while not decompressor.eof:
        room = limit - len(out)
        produced = decompressor.decompress(pending, room + 1)
        if len(produced) > room:
            raise CaptureTransient("render_failed")
        out.extend(produced)
        nxt = decompressor.unconsumed_tail
        if decompressor.eof:
            break
        if not produced and nxt == pending:
            raise zlib.error("incomplete compressed stream")
        pending = nxt
    return bytes(out)


def _inflate_content_encoding(encoding: str, body: bytes) -> bytes:
    """Return uncompressed bytes. Unknown encodings are not passed through."""
    try:
        if encoding in {"gzip", "x-gzip"}:
            return _decompress_capped(body, 16 + zlib.MAX_WBITS)
        if encoding == "deflate":
            try:
                return _decompress_capped(body, zlib.MAX_WBITS)
            except zlib.error:
                return _decompress_capped(body, -zlib.MAX_WBITS)
    except CaptureTransient:
        raise
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


def _as_captured_page(rendered: object) -> CapturedPage:
    if isinstance(rendered, CapturedPage):
        return rendered
    if isinstance(rendered, bytes | bytearray):
        return CapturedPage(image=bytes(rendered))
    raise CaptureTransient("render_failed")


def capture_public_page(url: str) -> CapturedPage:
    """Validate ``url`` and return a viewport JPEG plus page facts.

    Safe to run off the event loop. A missing title or an empty link list
    does not fail the capture. Link targets are not fetched. A renderer that
    returns JPEG bytes only (no facts) still succeeds.
    """
    normalized = validate_capture_url(url)
    page = _as_captured_page(render_page_screenshot(normalized))
    if not page.image:
        raise CaptureTransient("render_failed")
    if len(page.image) > CAPTURE_MAX_IMAGE_BYTES:
        raise CaptureRejected("render_too_large")
    title = _clean_short_text(page.page_title, CAPTURE_MAX_PAGE_TITLE_CHARS)
    final = _http_url_or_blank(page.final_url)
    links = normalize_outbound_links(list(page.outbound_links), page_url=final)
    if (
        title == page.page_title
        and final == page.final_url
        and links == list(page.outbound_links)
    ):
        return page
    return CapturedPage(
        image=page.image,
        page_title=title,
        final_url=final,
        outbound_links=tuple(links),
    )


def capture_screenshot_bytes(url: str) -> bytes:
    """Validate ``url`` and return a viewport JPEG. Safe to run off the event loop."""
    return capture_public_page(url).image


def render_page_screenshot(url: str) -> CapturedPage:
    """Headless Chromium viewport JPEG plus page facts.

    Timeouts are terminal; other blips retry. Title and link collection
    failures are empty fields, not a failed capture.
    """
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
                facts = collect_page_facts(page)
            except Exception:
                facts = _empty_page_facts()
            try:
                image = page.screenshot(
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
            if not isinstance(image, bytes | bytearray) or not image:
                raise CaptureTransient("render_failed")
            links = facts.get("outbound_links")
            if not isinstance(links, list):
                links = []
            title = facts.get("page_title")
            final = facts.get("final_url")
            return CapturedPage(
                image=bytes(image),
                page_title=title if isinstance(title, str) else "",
                final_url=final if isinstance(final, str) else "",
                outbound_links=tuple(links),
            )
    except (CaptureRejected, CaptureTransient):
        raise
    except Exception as exc:
        text = str(exc).lower()
        if "executable doesn't exist" in text or "browsertype.launch" in text:
            raise CaptureRejected("render_unavailable") from exc
        raise CaptureTransient("render_failed") from exc
