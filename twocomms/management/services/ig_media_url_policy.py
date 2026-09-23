"""Э3.16 / ADD-SEC-001 — URL policy for the remote media loader.

The bot fetches customer media from URLs that arrive inside a webhook payload.
A signed webhook proves that Meta delivered the batch; it does not prove that
every URL inside it is safe to request from production network context. This
module is the gate that decides whether a URL may be fetched at all, and it
owns the bounded fetch itself so the decision cannot be bypassed by the
transport.

Why the policy is worth having even if the provider only ever sends CDN URLs
(the honest state of `ADD-SEC-001` — see the Meta contract note below):

1. **DNS rebinding.** A hostname that resolved to a public address a moment
   ago can resolve to `127.0.0.1` on the next attempt. Checking the hostname
   is not enough: the address that the socket actually connects to has to be
   the address that was validated. This module resolves once, validates every
   answer, then pins the connection to a validated address while keeping the
   original hostname for SNI and certificate verification.
2. **Timeouts.** A URL that points at a slow endpoint holds the worker. With
   the global capture lock (`ADD-CODE-002`) that delay is shared by every
   client, so a deadline belongs in the policy, not in the caller.
3. **Size.** Content-Length is checked before the body is streamed, and the
   read is bounded even when the header lies or is absent.

Meta contract (checkpoint 1 of Э3.16, verified 2026-09-02 against provider
documentation, not against production traffic):

* The Instagram messaging webhook reference documents these attachment types:
  ``audio``, ``file``, ``image``, ``share``, ``story_mention``, ``video``,
  ``ig_reel``, ``reel`` and ``ephemeral``. ``share`` is documented as a pointer
  to the shared Instagram media or post, ``ephemeral`` carries no URL at all,
  and the only field the page explicitly labels as a CDN URL is
  ``reply_to.story.url``. A URL that a customer types arrives in the message
  ``text``, not as an attachment payload.
* The Messenger Platform reference does document one attachment type whose
  ``payload.url`` is a link the sender shared — ``fallback`` — but that type is
  not part of the documented Instagram messaging contract, and
  ``MEDIA_ATTACH_TYPES`` in the loader does not accept it.
* Conclusion: attacker controllability of a capture-eligible URL is **not
  proven** by the documented contract, so `ADD-SEC-001` stays a validation
  gate rather than an incident. Two facts keep it worth gating anyway. The
  parser accepts a wider shape than Meta documents — any dict-valued
  attachment key exposing ``url``/``file_url``/``preview_url`` becomes a
  candidate, and the accepted type set includes ``link`` and ``story``, which
  neither reference documents — and the three reasons above hold for a
  perfectly legitimate CDN URL.

Adoption: ``fetch_media()`` owns the remote transport for live capture and
avatars. ``download_image()`` is only its compatibility adapter, while
first-party assets explicitly use the own-origin profile. It deliberately does
not monkey-patch anything: a policy that installs itself behind the caller's
back is a policy nobody can reason about.
"""

from __future__ import annotations

import http.client
import ipaddress
import io
import logging
import socket
import ssl
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("management.ig_media_url_policy")

# --- strictness -------------------------------------------------------------
# There is deliberately no "off". The rollback lever for Э3.16 is a flag on
# strictness, and the audit is explicit that it must not disable the policy:
# `relaxed` downgrades ONLY the host allowlist (a new legitimate Meta CDN host
# must not be able to take media capture down), while scheme, address policy,
# per-hop redirect validation, size and content-type keep blocking.
MODE_ENFORCE = "enforce"
MODE_RELAXED = "relaxed"
MODES = frozenset({MODE_ENFORCE, MODE_RELAXED})

# --- profiles ---------------------------------------------------------------
# `provider` — the URL came from a provider payload and is therefore untrusted.
# `own_origin` — the URL was built from our own settings (SITE_BASE_URL plus a
# stored media path). The host is ours by construction, so the address policy
# does not apply: in development that origin is loopback on purpose. What the
# profile still enforces is that the host stays ours across every redirect.
PROFILE_PROVIDER = "provider"
PROFILE_OWN_ORIGIN = "own_origin"

# --- documented Meta media hosts -------------------------------------------
# Suffix entries match the host itself or any subdomain of it. Provider media
# lives on these; the Graph API hosts are intentionally absent because this
# module never fetches API responses.
META_MEDIA_HOST_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
    "fbsbx.com",
)

# --- reason codes (metric: rejected URLs by reason) ------------------------
REASON_EMPTY = "empty"
REASON_TOO_LONG = "too_long"
REASON_MALFORMED = "malformed"
REASON_CONTROL_CHARS = "control_chars"
REASON_SCHEME = "scheme"
REASON_USERINFO = "userinfo"
REASON_NO_HOST = "no_host"
REASON_NON_ASCII_HOST = "non_ascii_host"
REASON_IP_LITERAL = "ip_literal"
REASON_PORT = "port"
REASON_HOST_NOT_ALLOWED = "host_not_allowed"
REASON_HOST_NOT_ALLOWED_OBSERVED = "host_not_allowed_observed"
REASON_DNS_FAILED = "dns_failed"
REASON_DNS_EMPTY = "dns_empty"
REASON_UNSPECIFIED_ADDRESS = "unspecified_address"
REASON_LOOPBACK = "loopback"
REASON_LINK_LOCAL = "link_local"
REASON_MULTICAST = "multicast"
REASON_PRIVATE = "private"
REASON_RESERVED = "reserved"
REASON_NOT_GLOBAL = "not_global"
REASON_BAD_ADDRESS = "bad_address"
REASON_REDIRECT_LIMIT = "redirect_limit"
REASON_REDIRECT_NO_LOCATION = "redirect_no_location"
REASON_STATUS = "status"
REASON_CONTENT_TYPE = "content_type"
REASON_DECLARED_TOO_LARGE = "declared_too_large"
REASON_STREAM_TOO_LARGE = "stream_too_large"
REASON_EMPTY_BODY = "empty_body"
REASON_TRANSPORT = "transport"
REASON_DEADLINE = "deadline"
REASON_SIGNATURE = "signature"
REASON_IMAGE_DECODE = "image_decode"
REASON_IMAGE_PIXELS = "image_pixels"
REASON_UNVERIFIABLE_MIME = "unverifiable_mime"
REDIRECT_REASON_PREFIX = "redirect_"

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_DEADLINE_SECONDS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_IMAGE_PIXELS = 40_000_000

SUPPORTED_INLINE_IMAGE_MIMES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
})
SUPPORTED_INLINE_AUDIO_MIMES = frozenset({
    "audio/wav", "audio/mpeg", "audio/mp3", "audio/aiff", "audio/aac",
    "audio/ogg", "audio/flac", "audio/m4a", "audio/opus", "audio/webm",
})
UNVERIFIABLE_AUDIO_MIME_TYPES = frozenset({
    "audio/l16", "audio/alaw", "audio/mulaw",
})

# Kept small on purpose: a caller that needs the loader's own image/audio sets
# passes them explicitly, and anything unlisted is rejected before the body is
# read.
DEFAULT_ALLOWED_MIME_TYPES = SUPPORTED_INLINE_IMAGE_MIMES
_MIME_ALIASES = {
    "audio/x-wav": "audio/wav",
    "audio/x-aiff": "audio/aiff",
    "audio/x-m4a": "audio/m4a",
    "audio/mp4": "audio/m4a",
    "audio/mp3": "audio/mpeg",
}

_REJECTION_COUNTER_PREFIX = "ig_media_url_policy:reject:"
COUNTER_TTL_SECONDS = 7 * 24 * 3600
_resolver_executor: ThreadPoolExecutor | None = None
_resolver_executor_lock = threading.Lock()
_resolver_slot = threading.BoundedSemaphore(1)


# --- data structures --------------------------------------------------------
@dataclass(frozen=True)
class MediaTarget:
    """A validated target ready to be connected."""
    url: str
    scheme: str
    hostname: str
    port: int
    path_and_query: str
    all_addresses: tuple[str, ...] = field(default_factory=tuple)
    pinned_address: str = ""


@dataclass(frozen=True)
class UrlVerdict:
    """Outcome of URL validation."""
    allowed: bool
    reason: str = ""
    target: MediaTarget | None = None


@dataclass(frozen=True)
class FetchOutcome:
    """Result of a bounded media fetch."""
    success: bool
    mime_type: str = ""
    body_bytes: bytes = b""
    reason: str = ""
    status_code: int | None = None


# --- config -----------------------------------------------------------------
def configured_mode() -> str:
    value = str(getattr(settings, "IG_MEDIA_URL_POLICY_MODE", MODE_ENFORCE) or MODE_ENFORCE)
    value = value.strip().casefold()
    return value if value in MODES else MODE_ENFORCE


def is_enforced() -> bool:
    return configured_mode() == MODE_ENFORCE


def is_relaxed() -> bool:
    return configured_mode() == MODE_RELAXED


def _own_origin_host() -> str:
    """Return the hostname of SITE_BASE_URL or empty if unconfigured."""
    raw = str(getattr(settings, "SITE_BASE_URL", "") or "").strip()
    if not raw:
        return ""
    try:
        return urlsplit(raw).hostname or ""
    except Exception:
        return ""


def _catalog_allowed_hosts() -> frozenset[str]:
    """Return lowercase hosts from IG_CATALOG_MEDIA_ALLOWED_HOSTS."""
    configured = getattr(settings, "IG_CATALOG_MEDIA_ALLOWED_HOSTS", ()) or ()
    return frozenset(str(h).strip().lower() for h in configured if str(h).strip())


def _custom_allowed_hosts() -> frozenset[str]:
    """Return lowercase hosts from IG_MEDIA_URL_ALLOWED_HOSTS (fetch proxies)."""
    configured = getattr(settings, "IG_MEDIA_URL_ALLOWED_HOSTS", ()) or ()
    return frozenset(str(h).strip().lower() for h in configured if str(h).strip())


# --- metrics ----------------------------------------------------------------
def _bump_rejection_counter(reason: str) -> None:
    key = f"{_REJECTION_COUNTER_PREFIX}{reason}"
    try:
        if not cache.add(key, 1, COUNTER_TTL_SECONDS):
            cache.incr(key)
    except Exception:
        pass


def rejection_count(reason: str) -> int:
    """Return the rejection count for the given reason."""
    key = f"{_REJECTION_COUNTER_PREFIX}{reason}"
    try:
        return int(cache.get(key, 0) or 0)
    except Exception:
        return 0


# --- address classification -------------------------------------------------
def _classify_address(addr_str: str) -> tuple[bool, str]:
    """Return (is_global, reason).
    
    The reason identifies the specific private/reserved category; empty if global.
    IPv6 addresses with embedded IPv4 are unwrapped and checked, and a non-global
    embedded address is reported as "embedded_<type>_<reason>".
    """
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return False, REASON_BAD_ADDRESS

    # IPv6 with embedded IPv4
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = None
        embedded_type = ""
        
        if addr.ipv4_mapped:
            embedded = addr.ipv4_mapped
            embedded_type = "ipv4_mapped"
        elif addr.sixtofour:
            embedded = addr.sixtofour
            embedded_type = "6to4"
        elif addr.teredo:
            # teredo is (server, client); we care about the client
            embedded = addr.teredo[1]
            embedded_type = "teredo"

        if embedded and not embedded.is_global:
            _, reason = _classify_address(str(embedded))
            return False, f"embedded_{embedded_type}_{reason}"

    # The guards below are ordered from most specific to least specific so the
    # reason code identifies the narrowest category. is_global is the definitive
    # gate; the rest just refine telemetry.
    if addr.is_unspecified:
        return False, REASON_UNSPECIFIED_ADDRESS
    if addr.is_loopback:
        return False, REASON_LOOPBACK
    if addr.is_link_local:
        return False, REASON_LINK_LOCAL
    if addr.is_multicast:
        # Multicast can have global scope, but we reject it anyway.
        return False, REASON_MULTICAST
    if addr.is_private:
        return False, REASON_PRIVATE
    if addr.is_reserved:
        return False, REASON_RESERVED
    if not addr.is_global:
        # Catches CGNAT (100.64/10 is not private but not global either)
        # and other future non-global special-purpose ranges.
        return False, REASON_NOT_GLOBAL

    return True, ""


# --- host policy ------------------------------------------------------------
def _meta_cdn_match(hostname: str) -> bool:
    """Check if hostname matches a documented Meta CDN suffix."""
    for suffix in META_MEDIA_HOST_SUFFIXES:
        if hostname == suffix or hostname.endswith(f".{suffix}"):
            return True
    return False


def _hostname_allowed(hostname: str, *, profile: str) -> bool:
    """Check if hostname is allowed for the given profile."""
    if not hostname:
        return False

    hostname_lower = hostname.lower().rstrip(".")

    if profile == PROFILE_OWN_ORIGIN:
        own = _own_origin_host()
        if own and hostname_lower == own.lower():
            return True
        if hostname_lower in _catalog_allowed_hosts():
            return True
        return False

    # PROFILE_PROVIDER
    if _meta_cdn_match(hostname_lower):
        return True
    if hostname_lower in _custom_allowed_hosts():
        return True
    return False


# --- URL validation ---------------------------------------------------------
def validate_media_url(
    url: str,
    *,
    profile: str = PROFILE_PROVIDER,
    resolver=None,
) -> UrlVerdict:
    """Validate a media URL before fetching it.
    
    Args:
        url: The URL to validate
        profile: PROFILE_PROVIDER (untrusted) or PROFILE_OWN_ORIGIN (settings-derived)
        resolver: Optional callable(hostname, port) -> list[ip_str]; defaults to DNS

    Returns:
        UrlVerdict(allowed=True, target=...) if safe, otherwise reason explains rejection
    """
    if resolver is None:
        resolver = _default_resolver

    if not url:
        return UrlVerdict(allowed=False, reason=REASON_EMPTY)

    url = str(url or "").strip()
    if len(url) > MAX_URL_LENGTH:
        return UrlVerdict(allowed=False, reason=REASON_TOO_LONG)

    # Control characters are a red flag
    if any(ord(c) < 32 for c in url):
        return UrlVerdict(allowed=False, reason=REASON_CONTROL_CHARS)

    try:
        parsed = urlsplit(url)
    except Exception:
        return UrlVerdict(allowed=False, reason=REASON_MALFORMED)

    # --- scheme -------------------------------------------------------------
    allowed_schemes = {"https", "http"} if profile == PROFILE_OWN_ORIGIN else {"https"}
    if parsed.scheme not in allowed_schemes:
        return UrlVerdict(allowed=False, reason=REASON_SCHEME)

    # --- reject URLs with embedded credentials ------------------------------
    if parsed.username or parsed.password:
        return UrlVerdict(allowed=False, reason=REASON_USERINFO)

    # --- hostname -----------------------------------------------------------
    hostname = parsed.hostname
    if not hostname:
        return UrlVerdict(allowed=False, reason=REASON_NO_HOST)

    # Non-ASCII hostnames are suspicious (IDN should be punycode at this point)
    if not all(ord(c) < 128 for c in hostname):
        return UrlVerdict(allowed=False, reason=REASON_NON_ASCII_HOST)

    # Meta CDN never uses raw IP addresses
    try:
        ipaddress.ip_address(hostname)
        return UrlVerdict(allowed=False, reason=REASON_IP_LITERAL)
    except ValueError:
        pass  # good, it's a hostname

    # --- port ---------------------------------------------------------------
    # Provider media is served on the default port; a non-default port in a
    # payload URL is a pivot attempt, not a CDN address. The own-origin profile
    # is exempt because our own configured origin legitimately runs on another
    # port in development.
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        # SplitResult.port raises for a non-numeric port, and it raises on
        # attribute access rather than during parsing, so it has to be guarded
        # here or a junk port would escape as an exception instead of a verdict.
        actual_port = parsed.port or default_port
    except ValueError:
        return UrlVerdict(allowed=False, reason=REASON_PORT)
    if profile != PROFILE_OWN_ORIGIN and actual_port != default_port:
        return UrlVerdict(allowed=False, reason=REASON_PORT)

    # --- host allowlist -----------------------------------------------------
    if not _hostname_allowed(hostname, profile=profile):
        # In relaxed mode, record the mismatch but do not block.
        if is_relaxed():
            _bump_rejection_counter(REASON_HOST_NOT_ALLOWED_OBSERVED)
        else:
            return UrlVerdict(allowed=False, reason=REASON_HOST_NOT_ALLOWED)

    # --- DNS + address policy -----------------------------------------------
    # Skip DNS for own_origin: the host is from settings, not the payload, so
    # address checks don't apply (production uses a real domain; dev uses loopback).
    if profile == PROFILE_OWN_ORIGIN:
        target = MediaTarget(
            url=url,
            scheme=parsed.scheme,
            hostname=hostname,
            port=actual_port,
            path_and_query=(parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
        )
        return UrlVerdict(allowed=True, target=target)

    # Provider profile: resolve and validate every address
    try:
        addresses = resolver(hostname, actual_port)
    except TimeoutError:
        return UrlVerdict(allowed=False, reason=REASON_DEADLINE)
    except socket.gaierror:
        return UrlVerdict(allowed=False, reason=REASON_DNS_FAILED)
    except Exception:
        # catch-all: OSError, timeout, MemoryError, etc.
        logger.warning(
            "dns_error",
            extra={"hostname": hostname, "port": actual_port},
            exc_info=True,
        )
        return UrlVerdict(allowed=False, reason=REASON_DNS_FAILED)

    if not addresses:
        return UrlVerdict(allowed=False, reason=REASON_DNS_EMPTY)

    # All addresses must be globally routable; one private IP fails the whole set
    for addr_str in addresses:
        is_global, reason = _classify_address(addr_str)
        if not is_global:
            return UrlVerdict(allowed=False, reason=reason)

    # Pin to first address for DNS rebinding defense
    target = MediaTarget(
        url=url,
        scheme=parsed.scheme,
        hostname=hostname,
        port=actual_port,
        path_and_query=(parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
        all_addresses=tuple(addresses),
        pinned_address=addresses[0],
    )
    return UrlVerdict(allowed=True, target=target)


def _default_resolver_sync(hostname: str, port: int) -> list[str]:
    """Resolve hostname to list of unique IP addresses."""
    resolved = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return list({info[4][0] for info in resolved})


def _default_resolver(hostname: str, port: int, *, timeout_seconds: float | None = None) -> list[str]:
    """Resolve DNS within one process-wide bounded worker, with no queued jobs."""
    if timeout_seconds is None:
        return _default_resolver_sync(hostname, port)
    if timeout_seconds <= 0 or not _resolver_slot.acquire(blocking=False):
        raise TimeoutError("media DNS deadline")
    global _resolver_executor
    with _resolver_executor_lock:
        if _resolver_executor is None:
            _resolver_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="ig-media-dns",
            )
        future = _resolver_executor.submit(_default_resolver_sync, hostname, port)
    future.add_done_callback(lambda _future: _resolver_slot.release())
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        raise TimeoutError("media DNS deadline") from exc


# --- transport --------------------------------------------------------------
def _pinned_connector(target: MediaTarget):
    """Return a connector function that connects to the pinned address."""
    def connector(address, timeout=None, source_address=None):
        # http.client.HTTPConnection.connect calls _create_connection with
        # (host, port), timeout, source_address. We substitute the pinned IP
        # for the host while keeping port and other args intact.
        pinned_address = (target.pinned_address, address[1])
        return socket.create_connection(pinned_address, timeout, source_address)
    return connector


def _default_transport(target: MediaTarget, *, timeout_seconds: float | None = None):
    """Default HTTP/HTTPS transport with DNS rebinding defense."""
    timeout = max(0.001, min(
        float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS), DEFAULT_TIMEOUT_SECONDS,
    ))
    if target.scheme == "https":
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            target.hostname,
            port=target.port,
            timeout=timeout,
            context=context,
        )
        # Pin the connection to the validated IP, but keep the hostname for SNI
        # and certificate verification (server_hostname in wrap_socket comes from
        # the host we pass to HTTPSConnection, not from the IP we connect to).
        if target.pinned_address:
            conn._create_connection = _pinned_connector(target)
    else:
        conn = http.client.HTTPConnection(
            target.hostname,
            port=target.port,
            timeout=timeout,
        )

    try:
        conn.request("GET", target.path_and_query, headers={
            "User-Agent": "TwoCommsBot/1.0",
            "Accept": "*/*",
        })
        return conn.getresponse()
    except Exception:
        conn.close()
        raise


def _sniff_supported_mime(body: bytes, declared_mime: str = "") -> str:
    """Return a supported inline MIME from bounded bytes, or empty string.

    Provider CDNs occasionally return generic content types; Meta voice objects
    have also been observed as video/mp4.  Accept this fallback only for the
    already SSRF-validated provider profile and require a strong signature.
    """
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"OggS"):
        return "audio/ogg"
    if body.startswith(b"fLaC"):
        return "audio/flac"
    if body.startswith(b"ID3") or (len(body) >= 2 and body[0] == 0xff and body[1] & 0xe0 == 0xe0):
        return "audio/mpeg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WAVE":
        return "audio/wav"
    if len(body) >= 12 and body[:4] == b"FORM" and body[8:12] in {b"AIFF", b"AIFC"}:
        return "audio/aiff"
    if body.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if len(body) >= 12 and body[4:8] == b"ftyp":
        if b"soun" in body and b"vide" not in body:
            return "audio/m4a"
    return ""


def _signature_matches(mime_type: str, body: bytes) -> bool:
    """Confirm a declared supported MIME from file bytes, never headers alone."""
    if mime_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    if mime_type in {"image/heic", "image/heif", "audio/m4a"}:
        if len(body) < 12 or body[4:8] != b"ftyp":
            return False
        brand = body[8:12].lower()
        if mime_type == "audio/m4a":
            return brand in {b"m4a ", b"mp41", b"mp42", b"isom"}
        return brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
    if mime_type == "audio/wav":
        return len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WAVE"
    if mime_type == "audio/aiff":
        return len(body) >= 12 and body[:4] == b"FORM" and body[8:12] in {b"AIFF", b"AIFC"}
    if mime_type == "audio/aac":
        return len(body) >= 2 and body[0] == 0xff and body[1] & 0xf6 in {0xf0, 0xf2}
    if mime_type == "audio/ogg" or mime_type == "audio/opus":
        return body.startswith(b"OggS")
    if mime_type == "audio/flac":
        return body.startswith(b"fLaC")
    if mime_type == "audio/mpeg":
        return body.startswith(b"ID3") or (
            len(body) >= 2 and body[0] == 0xff and body[1] & 0xe0 == 0xe0
        )
    if mime_type == "audio/webm":
        return body.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _validate_image_payload(mime_type: str, body: bytes) -> str:
    """Decode supported images before persistence and enforce their pixel cap."""
    if not mime_type.startswith("image/"):
        return ""
    try:
        from PIL import Image, UnidentifiedImageError

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    return REASON_IMAGE_PIXELS
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return REASON_IMAGE_PIXELS
    except (OSError, SyntaxError, UnidentifiedImageError):
        return REASON_IMAGE_DECODE
    return ""


def _set_response_timeout(response, seconds: float) -> None:
    """Tighten the connected socket to the remaining total deadline if exposed."""
    try:
        response.fp.raw._sock.settimeout(max(0.001, seconds))
    except (AttributeError, OSError):
        pass


def _read_bounded_before_deadline(response, *, max_bytes: int, deadline: float):
    """Read in bounded chunks so a redirect chain never receives a new budget."""
    reader = getattr(response, "read1", None)
    if not callable(reader):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b"", REASON_DEADLINE
        _set_response_timeout(response, remaining)
        body = response.read(max_bytes + 1)
        return (
            (b"", REASON_STREAM_TOO_LARGE)
            if len(body) > max_bytes
            else (body, "")
        )
    chunks = []
    received = 0
    while received <= max_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b"", REASON_DEADLINE
        _set_response_timeout(response, remaining)
        chunk = reader(min(64 * 1024, max_bytes + 1 - received))
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
        if received > max_bytes:
            return b"", REASON_STREAM_TOO_LARGE
    return b"".join(chunks), ""


# --- bounded fetch with per-hop validation ----------------------------------
def fetch_media(
    url: str,
    *,
    profile: str = PROFILE_PROVIDER,
    allowed_mime_types: frozenset[str] | None = None,
    max_bytes: int = 6 * 1024 * 1024,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    resolver=None,
    transport=None,
) -> FetchOutcome:
    """Fetch media with SSRF protection and bounded read.
    
    This is a drop-in replacement for ``download_image`` with the same contract:
    returns (mime_type, body_bytes) on success, None on rejection/failure. The
    difference is that this function owns the full validation and fetch, so a
    caller cannot skip the policy by directly calling urllib.
    
    Every redirect is re-validated against the same profile (scheme, host,
    address policy). The read is bounded even when Content-Length is absent or
    dishonest.
    
    Args:
        url: Initial URL
        profile: PROFILE_PROVIDER or PROFILE_OWN_ORIGIN
        allowed_mime_types: Permitted Content-Type values; defaults to image set
        max_bytes: Maximum body size
        deadline_seconds: Total fetch budget, shared by DNS/redirect hops
        resolver: Optional DNS resolver (hostname, port) -> [ip, ...]
        transport: Optional HTTP transport (target) -> response object
    
    Returns:
        FetchOutcome with success=True and mime_type/body_bytes if fetched,
        otherwise reason explains the rejection
    """
    if allowed_mime_types is None:
        allowed_mime_types = DEFAULT_ALLOWED_MIME_TYPES

    if transport is None:
        transport = _default_transport

    try:
        deadline_seconds = max(0.001, float(deadline_seconds))
    except (TypeError, ValueError):
        deadline_seconds = DEFAULT_DEADLINE_SECONDS
    deadline = time.monotonic() + deadline_seconds
    current_url = url
    hop = 0

    while True:
        if time.monotonic() >= deadline:
            _bump_rejection_counter(REASON_DEADLINE)
            return FetchOutcome(success=False, reason=REASON_DEADLINE)
        active_resolver = resolver
        if active_resolver is None:
            active_resolver = lambda hostname, port: _default_resolver(
                hostname,
                port,
                timeout_seconds=deadline - time.monotonic(),
            )
        verdict = validate_media_url(
            current_url, profile=profile, resolver=active_resolver,
        )
        if not verdict.allowed:
            reason = f"{REDIRECT_REASON_PREFIX}{verdict.reason}" if hop > 0 else verdict.reason
            _bump_rejection_counter(reason)
            return FetchOutcome(success=False, reason=reason)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _bump_rejection_counter(REASON_DEADLINE)
            return FetchOutcome(success=False, reason=REASON_DEADLINE)

        try:
            if transport is _default_transport:
                response = transport(verdict.target, timeout_seconds=remaining)
            else:
                response = transport(verdict.target)
        except Exception as exc:
            _bump_rejection_counter(REASON_TRANSPORT)
            logger.warning(
                "media_fetch_transport_error",
                extra={"hop": hop, "error_type": type(exc).__name__},
            )
            return FetchOutcome(success=False, reason=REASON_TRANSPORT)

        try:
            status = response.status

            # --- redirect ---------------------------------------------------
            if status in REDIRECT_STATUSES:
                if hop >= MAX_REDIRECTS:
                    _bump_rejection_counter(REASON_REDIRECT_LIMIT)
                    return FetchOutcome(
                        success=False,
                        reason=REASON_REDIRECT_LIMIT,
                        status_code=status,
                    )

                location = response.getheader("Location")
                if not location:
                    _bump_rejection_counter(REASON_REDIRECT_NO_LOCATION)
                    return FetchOutcome(
                        success=False,
                        reason=REASON_REDIRECT_NO_LOCATION,
                        status_code=status,
                    )

                # Resolve relative redirect against current URL
                current_url = urljoin(current_url, location)
                hop += 1
                continue

            # --- non-success ------------------------------------------------
            if status != 200:
                _bump_rejection_counter(REASON_STATUS)
                return FetchOutcome(
                    success=False,
                    reason=REASON_STATUS,
                    status_code=status,
                )

            # --- Content-Type -----------------------------------------------
            content_type_raw = response.getheader("Content-Type") or ""
            mime_type = content_type_raw.split(";")[0].strip().lower()
            mime_type = _MIME_ALIASES.get(mime_type, mime_type)
            if mime_type in UNVERIFIABLE_AUDIO_MIME_TYPES:
                _bump_rejection_counter(REASON_UNVERIFIABLE_MIME)
                return FetchOutcome(
                    success=False,
                    reason=REASON_UNVERIFIABLE_MIME,
                    status_code=status,
                )
            allow_provider_sniff = (
                profile == PROFILE_PROVIDER
                and mime_type in {"", "application/octet-stream", "binary/octet-stream", "video/mp4"}
            )
            if mime_type not in allowed_mime_types and not allow_provider_sniff:
                _bump_rejection_counter(REASON_CONTENT_TYPE)
                return FetchOutcome(
                    success=False,
                    reason=REASON_CONTENT_TYPE,
                    status_code=status,
                )

            # --- Content-Length gate before streaming -----------------------
            content_length_str = response.getheader("Content-Length")
            if content_length_str:
                try:
                    declared = int(content_length_str)
                    if declared > max_bytes:
                        _bump_rejection_counter(REASON_DECLARED_TOO_LARGE)
                        return FetchOutcome(
                            success=False,
                            reason=REASON_DECLARED_TOO_LARGE,
                            status_code=status,
                        )
                except (ValueError, OverflowError):
                    pass

            # --- bounded read -----------------------------------------------
            body, read_error = _read_bounded_before_deadline(
                response, max_bytes=max_bytes, deadline=deadline,
            )
            if read_error:
                _bump_rejection_counter(read_error)
                return FetchOutcome(
                    success=False,
                    reason=read_error,
                    status_code=status,
                )
            if not body:
                _bump_rejection_counter(REASON_EMPTY_BODY)
                return FetchOutcome(
                    success=False,
                    reason=REASON_EMPTY_BODY,
                    status_code=status,
                )
            if time.monotonic() > deadline:
                _bump_rejection_counter(REASON_DEADLINE)
                return FetchOutcome(
                    success=False,
                    reason=REASON_DEADLINE,
                    status_code=status,
                )

            if allow_provider_sniff:
                sniffed_mime = _sniff_supported_mime(body, mime_type)
                if not sniffed_mime:
                    _bump_rejection_counter(REASON_SIGNATURE)
                    return FetchOutcome(
                        success=False,
                        reason=REASON_SIGNATURE,
                        status_code=status,
                    )
                mime_type = sniffed_mime
            if not _signature_matches(mime_type, body):
                _bump_rejection_counter(REASON_SIGNATURE)
                return FetchOutcome(
                    success=False,
                    reason=REASON_SIGNATURE,
                    status_code=status,
                )
            image_error = _validate_image_payload(mime_type, body)
            if image_error:
                _bump_rejection_counter(image_error)
                return FetchOutcome(
                    success=False,
                    reason=image_error,
                    status_code=status,
                )

            return FetchOutcome(
                success=True,
                mime_type=mime_type,
                body_bytes=body,
                status_code=status,
            )

        except TimeoutError:
            _bump_rejection_counter(REASON_DEADLINE)
            return FetchOutcome(success=False, reason=REASON_DEADLINE)
        except Exception as exc:
            _bump_rejection_counter(REASON_TRANSPORT)
            logger.warning(
                "media_fetch_response_error",
                extra={"hop": hop, "error_type": type(exc).__name__},
            )
            return FetchOutcome(success=False, reason=REASON_TRANSPORT)
        finally:
            response.close()


# --- convenience gate for existing download_image call sites ----------------
def guard_media_url(url: str, *, profile: str = PROFILE_PROVIDER) -> tuple[bool, str]:
    """Return (allowed, reason) for use in legacy download_image call sites.
    
    Call this before calling the existing downloader. If allowed=False, do not
    fetch; log or return None directly. This does not replace the downloader —
    it only rejects the URL before the transport gets involved.
    """
    verdict = validate_media_url(url, profile=profile)
    if not verdict.allowed:
        _bump_rejection_counter(verdict.reason)
        logger.warning(
            "media_url_rejected",
            extra={"hostname": urlsplit(url).hostname, "reason": verdict.reason, "profile": profile},
        )
    return verdict.allowed, verdict.reason


# --- preferred media source heuristic ---------------------------------------
# Task 6 of Э3.16: "prefer provider object ID or owned bytes over arbitrary URL".
# This function documents the priority order for source selection. It doesn't
# change code paths — the caller decides — but the audit wants the order stated.
def preferred_media_source(item: dict) -> str:
    """Return the preferred data source for a media item: owned, provider_id, url, none."""
    # Owned bytes are best: no remote fetch, no SSRF boundary.
    if item.get("storage_name") and item.get("status") == "owned":
        return "owned"
    
    # Provider object ID is second: the fetch goes to a known Graph endpoint with
    # a typed object reference, not to an arbitrary URL from the payload.
    if item.get("provider_object_key") or item.get("ig_post_media_id"):
        return "provider_id"
    
    # URL is third: it crosses the boundary this module guards.
    if item.get("url"):
        return "url"
    
    return "none"


def should_fetch_remote_url(item: dict) -> bool:
    """Return True if the item should be fetched from its URL field.
    
    This just expresses the priority: owned > provider_id > url. It doesn't
    enforce anything; the loader decides.
    """
    source = preferred_media_source(item)
    return source == "url"
