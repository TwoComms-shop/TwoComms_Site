"""Tests for Э3.16 (ADD-SEC-001) — SSRF policy for the media loader.

All tests are mock-based: no real network calls, no live SSRF probes.
"""
import socket
from io import BytesIO
from dataclasses import dataclass
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from management.services import ig_media_url_policy as policy


def valid_jpeg() -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="JPEG")
    return output.getvalue()


@dataclass
class FakeResponse:
    """Mock HTTP response for transport tests."""
    status: int
    headers: dict
    body: bytes

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, amt: int = -1) -> bytes:
        if amt < 0:
            return self.body
        return self.body[:amt]

    def close(self) -> None:
        pass


def fake_resolver_factory(mapping: dict[str, list[str]]):
    """Return a fake resolver that returns addresses from a static map."""
    def resolver(hostname: str, port: int) -> list[str]:
        if hostname not in mapping:
            raise socket.gaierror(f"fake resolver: no entry for {hostname}")
        return list(mapping[hostname])
    return resolver


def fake_transport_factory(responses: list[FakeResponse]):
    """Return a fake transport that cycles through canned responses."""
    call_count = [0]
    def transport(target):
        idx = call_count[0]
        call_count[0] += 1
        if idx >= len(responses):
            raise RuntimeError("fake transport exhausted")
        return responses[idx]
    return transport


class AddressClassificationTests(SimpleTestCase):
    """Address classification must reject all private/local ranges."""

    def test_loopback_v4(self):
        self.assertEqual(policy._classify_address("127.0.0.1"), (False, policy.REASON_LOOPBACK))

    def test_loopback_v6(self):
        self.assertEqual(policy._classify_address("::1"), (False, policy.REASON_LOOPBACK))

    def test_private_v4_rfc1918(self):
        for addr in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with self.subTest(addr=addr):
                is_global, reason = policy._classify_address(addr)
                self.assertFalse(is_global)
                self.assertEqual(reason, policy.REASON_PRIVATE)

    def test_private_v6_unique_local(self):
        is_global, reason = policy._classify_address("fd00::1")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_PRIVATE)

    def test_link_local_v4(self):
        is_global, reason = policy._classify_address("169.254.169.254")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_LINK_LOCAL)

    def test_link_local_v6(self):
        is_global, reason = policy._classify_address("fe80::1")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_LINK_LOCAL)

    def test_multicast_v4(self):
        is_global, reason = policy._classify_address("224.0.0.1")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_MULTICAST)

    def test_reserved_v4_is_also_private(self):
        # 240.0.0.0/4 is both reserved and private; is_private wins in order
        is_global, reason = policy._classify_address("240.0.0.1")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_PRIVATE)

    def test_not_global_cgnat(self):
        # 100.64.0.0/10 shared address space (CGNAT)
        is_global, reason = policy._classify_address("100.64.0.1")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_NOT_GLOBAL)

    def test_unspecified_v4(self):
        is_global, reason = policy._classify_address("0.0.0.0")
        self.assertFalse(is_global)
        self.assertEqual(reason, policy.REASON_UNSPECIFIED_ADDRESS)

    def test_ipv6_embedded_ipv4_loopback(self):
        # ::ffff:127.0.0.1
        is_global, reason = policy._classify_address("::ffff:7f00:1")
        self.assertFalse(is_global)
        self.assertIn("embedded", reason)
        self.assertIn("loopback", reason)

    def test_ipv6_6to4_private(self):
        # 2002:0a00:0001:: embeds 10.0.0.1
        is_global, reason = policy._classify_address("2002:0a00:0001::")
        self.assertFalse(is_global)
        self.assertIn("embedded", reason)
        self.assertIn("private", reason)

    def test_ipv6_teredo_private(self):
        # Teredo with embedded 10.0.0.2
        is_global, reason = policy._classify_address("2001:0:5ef5:79fd:0:59d0:f5ff:fffd")
        self.assertFalse(is_global)
        self.assertIn("embedded", reason)
        self.assertIn("private", reason)

    def test_public_v4(self):
        for addr in ("8.8.8.8", "1.1.1.1", "157.240.241.63"):
            with self.subTest(addr=addr):
                is_global, reason = policy._classify_address(addr)
                self.assertTrue(is_global)
                self.assertEqual(reason, "")

    def test_public_v6(self):
        is_global, reason = policy._classify_address("2a03:2880:f003::1")
        self.assertTrue(is_global)
        self.assertEqual(reason, "")

    def test_video_mime_requires_a_matching_container_signature(self):
        mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00vide"
        webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 16 + b"V_VP9" + b"\x00" * 16

        self.assertTrue(policy._signature_matches("video/mp4", mp4))
        self.assertTrue(policy._signature_matches("video/webm", webm))
        self.assertFalse(policy._signature_matches("video/mp4", b"not a video"))


class HostAllowlistTests(SimpleTestCase):
    """Host allowlist must permit documented Meta CDN patterns only."""

    def test_meta_cdn_exact_match(self):
        self.assertTrue(policy._meta_cdn_match("scontent.cdninstagram.com"))
        self.assertTrue(policy._meta_cdn_match("lookaside.fbsbx.com"))
        self.assertTrue(policy._meta_cdn_match("scontent.xx.fbcdn.net"))

    def test_meta_cdn_subdomain_match(self):
        self.assertTrue(policy._meta_cdn_match("scontent-iad3-2.cdninstagram.com"))

    def test_host_match_is_case_insensitive(self):
        self.assertTrue(
            policy._hostname_allowed(
                "SCONTENT.CDNInstagram.COM", profile=policy.PROFILE_PROVIDER
            )
        )

    def test_trailing_dot_host_is_normalised(self):
        self.assertTrue(
            policy._hostname_allowed(
                "scontent.cdninstagram.com.", profile=policy.PROFILE_PROVIDER
            )
        )

    def test_suffix_confusion_host_rejected(self):
        for host in ("cdninstagram.com.evil.tld", "fbcdn.net.attacker.example"):
            with self.subTest(host=host):
                self.assertFalse(
                    policy._hostname_allowed(host, profile=policy.PROFILE_PROVIDER)
                )

    @override_settings(IG_MEDIA_URL_ALLOWED_HOSTS=("reviewed-proxy.example.net",))
    def test_reviewed_fetch_proxy_is_allowed(self):
        self.assertTrue(
            policy._hostname_allowed(
                "reviewed-proxy.example.net", profile=policy.PROFILE_PROVIDER
            )
        )

    @override_settings(SITE_BASE_URL="https://twocomms.shop")
    def test_own_origin_rejects_any_other_host(self):
        self.assertFalse(
            policy._hostname_allowed(
                "scontent.cdninstagram.com", profile=policy.PROFILE_OWN_ORIGIN
            )
        )

    def test_suffix_wildcard_prevents_prefix_spoofing(self):
        self.assertFalse(policy._meta_cdn_match("evilcdninstagram.com"))

    def test_non_meta_host_rejected(self):
        self.assertFalse(policy._meta_cdn_match("example.com"))

    @override_settings(SITE_BASE_URL="https://twocomms.shop")
    def test_own_origin_matches_site_base(self):
        self.assertTrue(
            policy._hostname_allowed("twocomms.shop", profile=policy.PROFILE_OWN_ORIGIN)
        )


class UrlValidationTests(SimpleTestCase):
    """URL shape and content validation."""

    def test_empty_url(self):
        v = policy.validate_media_url("")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_EMPTY)

    def test_too_long_url(self):
        v = policy.validate_media_url("https://example.com/" + "x" * 2100)
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_TOO_LONG)

    def test_control_characters(self):
        v = policy.validate_media_url("https://example.com/\x00foo")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_CONTROL_CHARS)

    def test_non_https_scheme_rejected_for_provider(self):
        v = policy.validate_media_url("http://scontent.cdninstagram.com/foo")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_SCHEME)

    def test_userinfo_rejected(self):
        v = policy.validate_media_url("https://user:pass@scontent.cdninstagram.com/foo")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_USERINFO)

    def test_ip_literal_rejected(self):
        v = policy.validate_media_url("https://157.240.241.63/foo")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_IP_LITERAL)

    def test_non_standard_port_rejected(self):
        v = policy.validate_media_url("https://scontent.cdninstagram.com:8443/foo")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_PORT)

    def test_host_not_in_allowlist(self):
        v = policy.validate_media_url("https://evil.com/foo")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_HOST_NOT_ALLOWED)

    def test_dns_returns_loopback(self):
        fake_resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["127.0.0.1"]})
        v = policy.validate_media_url(
            "https://scontent.cdninstagram.com/foo",
            resolver=fake_resolver,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_LOOPBACK)

    def test_dns_returns_private_ip(self):
        fake_resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["10.0.0.1"]})
        v = policy.validate_media_url(
            "https://scontent.cdninstagram.com/foo",
            resolver=fake_resolver,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, policy.REASON_PRIVATE)

    def test_dns_all_public_allowed_and_pinned(self):
        fake_resolver = fake_resolver_factory({
            "scontent.cdninstagram.com": ["157.240.1.1", "157.240.1.2"]
        })
        v = policy.validate_media_url(
            "https://scontent.cdninstagram.com/foo",
            resolver=fake_resolver,
        )
        self.assertTrue(v.allowed)
        self.assertEqual(v.target.pinned_address, "157.240.1.1")

    @override_settings(SITE_BASE_URL="http://localhost:8000")
    def test_own_origin_profile_permits_localhost(self):
        # localhost is allowed when it's our configured origin
        v = policy.validate_media_url(
            "http://localhost:8000/media/foo.jpg",
            profile=policy.PROFILE_OWN_ORIGIN,
        )
        self.assertTrue(v.allowed)
        self.assertEqual(v.target.pinned_address, "")


class FetchMediaTests(SimpleTestCase):
    """Bounded media fetch with per-hop validation."""

    def test_successful_fetch(self):
        fake_resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        fake_transport = fake_transport_factory([
            FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg"},
                body=valid_jpeg(),
            )
        ])
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.mime_type, "image/jpeg")
        self.assertEqual(outcome.status_code, 200)

    def test_redirect_followed_and_revalidated(self):
        fake_resolver = fake_resolver_factory({
            "scontent.cdninstagram.com": ["157.240.1.1"],
            "video.xx.fbcdn.net": ["157.240.2.1"],
        })
        fake_transport = fake_transport_factory([
            FakeResponse(
                status=302,
                headers={"Location": "https://video.xx.fbcdn.net/redirected.jpg"},
                body=b"",
            ),
            FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg"},
                body=valid_jpeg(),
            ),
        ])
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertTrue(outcome.success)

    def test_redirect_to_non_allowlisted_host_rejected(self):
        # redirect-target.local not in resolver, triggers host_not_allowed first
        fake_resolver = fake_resolver_factory({
            "scontent.cdninstagram.com": ["157.240.1.1"],
        })
        fake_transport = fake_transport_factory([
            FakeResponse(
                status=302,
                headers={"Location": "https://redirect-target.local/evil"},
                body=b"",
            ),
        ])
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertFalse(outcome.success)
        self.assertIn("host_not_allowed", outcome.reason)

    def test_redirect_limit_enforced(self):
        fake_resolver = fake_resolver_factory({
            f"cdn{i}.fbcdn.net": ["157.240.1.1"] for i in range(10)
        })
        redirects = [
            FakeResponse(
                status=302,
                headers={"Location": f"https://cdn{i+1}.fbcdn.net/foo.jpg"},
                body=b"",
            )
            for i in range(5)
        ]
        fake_transport = fake_transport_factory(redirects)
        outcome = policy.fetch_media(
            "https://cdn0.fbcdn.net/foo.jpg",
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_REDIRECT_LIMIT)

    def test_content_type_rejected(self):
        fake_resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        fake_transport = fake_transport_factory([
            FakeResponse(
                status=200,
                headers={"Content-Type": "text/html"},
                body=b"<html>...</html>",
            ),
        ])
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_CONTENT_TYPE)

    def test_declared_size_too_large(self):
        fake_resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        fake_transport = fake_transport_factory([
            FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg", "Content-Length": "10000000"},
                body=b"x" * 100,
            ),
        ])
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            max_bytes=5000,
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_DECLARED_TOO_LARGE)

    def test_stream_too_large(self):
        fake_resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        fake_transport = fake_transport_factory([
            FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg"},
                body=b"x" * 10000,
            ),
        ])
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            max_bytes=5000,
            resolver=fake_resolver,
            transport=fake_transport,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_STREAM_TOO_LARGE)

    @patch("management.services.ig_media_url_policy.fetch_media")
    def test_live_adapter_uses_provider_profile(self, fetch_media):
        from management.services.instagram_bot import download_image

        fetch_media.return_value = policy.FetchOutcome(
            success=True, mime_type="audio/ogg", body_bytes=b"OggSvoice",
        )

        self.assertEqual(
            download_image("https://lookaside.fbsbx.com/voice"),
            ("audio/ogg", b"OggSvoice"),
        )
        self.assertEqual(fetch_media.call_args.kwargs["profile"], policy.PROFILE_PROVIDER)


class PreferredMediaSourceTests(SimpleTestCase):
    """Task 6: prefer owned bytes > provider object ID > URL."""

    def test_owned_bytes_first(self):
        item = {"storage_name": "abc.jpg", "status": "owned", "url": "https://cdn/x"}
        self.assertEqual(policy.preferred_media_source(item), "owned")

    def test_provider_id_second(self):
        item = {"provider_object_key": "story_mention:123", "url": "https://cdn/x"}
        self.assertEqual(policy.preferred_media_source(item), "provider_id")

    def test_url_third(self):
        item = {"url": "https://cdn/x"}
        self.assertEqual(policy.preferred_media_source(item), "url")

    def test_none_when_no_source(self):
        item = {}
        self.assertEqual(policy.preferred_media_source(item), "none")


class DnsRebindingTests(SimpleTestCase):
    """A host that resolves public now and private later must not get through."""

    def test_rebound_answer_is_rejected_before_any_connection(self):
        answers = [["157.240.1.1"], ["127.0.0.1"]]
        calls = []

        def resolver(hostname, port):
            calls.append(hostname)
            return answers[min(len(calls) - 1, len(answers) - 1)]

        url = "https://scontent.cdninstagram.com/foo.jpg"

        first = policy.validate_media_url(url, resolver=resolver)
        self.assertTrue(first.allowed)

        second = policy.validate_media_url(url, resolver=resolver)
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason, policy.REASON_LOOPBACK)

    def test_connection_is_pinned_to_the_validated_address(self):
        """The socket must target the checked address, not a re-resolved one."""
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        seen = {}

        def transport(target):
            seen["pinned"] = target.pinned_address
            seen["hostname"] = target.hostname
            return FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg"},
                body=valid_jpeg(),
            )

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(seen["pinned"], "157.240.1.1")
        # The hostname is preserved so TLS still verifies the certificate.
        self.assertEqual(seen["hostname"], "scontent.cdninstagram.com")

    def test_dns_deadline_is_a_typed_fetch_failure(self):
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=lambda _hostname, _port: (_ for _ in ()).throw(TimeoutError()),
            transport=lambda _target: self.fail("transport must not run"),
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_DEADLINE)

    def test_pinned_connector_dials_the_pinned_address(self):
        target = policy.MediaTarget(
            url="https://scontent.cdninstagram.com/foo.jpg",
            scheme="https",
            hostname="scontent.cdninstagram.com",
            port=443,
            path_and_query="/foo.jpg",
            all_addresses=("157.240.1.1",),
            pinned_address="157.240.1.1",
        )
        dialled = {}

        def fake_create_connection(address, timeout=None, source_address=None):
            dialled["address"] = address
            return "socket"

        with patch.object(policy.socket, "create_connection", fake_create_connection):
            connector = policy._pinned_connector(target)
            result = connector(("scontent.cdninstagram.com", 443), 10, None)

        self.assertEqual(result, "socket")
        self.assertEqual(dialled["address"], ("157.240.1.1", 443))


class RedirectAddressPolicyTests(SimpleTestCase):
    """Every hop is re-validated, including the address behind the host."""

    def test_redirect_to_allowlisted_host_resolving_private_is_rejected(self):
        resolver = fake_resolver_factory({
            "scontent.cdninstagram.com": ["157.240.1.1"],
            "lookaside.fbsbx.com": ["10.1.2.3"],
        })
        transport = fake_transport_factory([
            FakeResponse(
                status=302,
                headers={"Location": "https://lookaside.fbsbx.com/internal"},
                body=b"",
            ),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(
            outcome.reason,
            policy.REDIRECT_REASON_PREFIX + policy.REASON_PRIVATE,
        )

    def test_redirect_to_link_local_metadata_address_is_rejected(self):
        resolver = fake_resolver_factory({
            "scontent.cdninstagram.com": ["157.240.1.1"],
            "metadata.fbcdn.net": ["169.254.169.254"],
        })
        transport = fake_transport_factory([
            FakeResponse(
                status=302,
                headers={"Location": "https://metadata.fbcdn.net/latest/meta-data/"},
                body=b"",
            ),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(
            outcome.reason,
            policy.REDIRECT_REASON_PREFIX + policy.REASON_LINK_LOCAL,
        )

    def test_redirect_downgrade_to_http_is_rejected(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        transport = fake_transport_factory([
            FakeResponse(
                status=302,
                headers={"Location": "http://scontent.cdninstagram.com/foo.jpg"},
                body=b"",
            ),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(
            outcome.reason,
            policy.REDIRECT_REASON_PREFIX + policy.REASON_SCHEME,
        )

    def test_every_hop_is_resolved_again(self):
        resolved = []

        def resolver(hostname, port):
            resolved.append(hostname)
            return ["157.240.1.1"]

        transport = fake_transport_factory([
            FakeResponse(
                status=302,
                headers={"Location": "https://video.xx.fbcdn.net/second.jpg"},
                body=b"",
            ),
            FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg"},
                body=valid_jpeg(),
            ),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/first.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(
            resolved,
            ["scontent.cdninstagram.com", "video.xx.fbcdn.net"],
        )

    def test_missing_location_header_is_rejected(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        transport = fake_transport_factory([FakeResponse(status=302, headers={}, body=b"")])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_REDIRECT_NO_LOCATION)


class HeaderGateBeforeStreamingTests(SimpleTestCase):
    """Content-type and size are decided from headers, before the body is read."""

    def _recording_response(self, headers, body=b"x" * 32):
        reads = []

        class Recording(FakeResponse):
            def read(self, amt=-1):
                reads.append(amt)
                return super().read(amt)

        return Recording(status=200, headers=headers, body=body), reads

    def test_disallowed_content_type_is_rejected_without_reading_body(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        response, reads = self._recording_response({"Content-Type": "text/html"})

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=lambda target: response,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_CONTENT_TYPE)
        self.assertEqual(reads, [])

    def test_declared_length_over_limit_is_rejected_without_reading_body(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        response, reads = self._recording_response(
            {"Content-Type": "image/jpeg", "Content-Length": "9999999"}
        )

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            max_bytes=1024,
            resolver=resolver,
            transport=lambda target: response,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_DECLARED_TOO_LARGE)
        self.assertEqual(reads, [])

    def test_content_type_parameters_are_ignored(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        transport = fake_transport_factory([
            FakeResponse(
                status=200,
                headers={"Content-Type": "IMAGE/JPEG; charset=binary"},
                body=valid_jpeg(),
            ),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.mime_type, "image/jpeg")

    def test_dishonest_content_length_still_bounded(self):
        """A small declared length cannot unlock an oversized body."""
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        transport = fake_transport_factory([
            FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg", "Content-Length": "10"},
                body=b"x" * 9000,
            ),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            max_bytes=1024,
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_STREAM_TOO_LARGE)

    def test_content_type_without_a_matching_signature_is_rejected(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/claimed.jpg",
            resolver=resolver,
            transport=lambda _target: FakeResponse(
                status=200,
                headers={"Content-Type": "image/jpeg"},
                body=b"not-an-image",
            ),
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_SIGNATURE)

    def test_read_error_is_a_typed_fetch_failure(self):
        class BrokenResponse(FakeResponse):
            def read(self, _amt=-1):
                raise OSError("connection reset")

        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/broken.jpg",
            resolver=resolver,
            transport=lambda _target: BrokenResponse(
                status=200, headers={"Content-Type": "image/jpeg"}, body=b""
            ),
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_TRANSPORT)

    def test_decoded_image_pixel_limit_is_enforced(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        with patch.object(policy, "MAX_IMAGE_PIXELS", 1):
            outcome = policy.fetch_media(
                "https://scontent.cdninstagram.com/two-pixels.jpg",
                resolver=resolver,
                transport=lambda _target: FakeResponse(
                    status=200,
                    headers={"Content-Type": "image/jpeg"},
                    body=valid_jpeg(),
                ),
            )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_IMAGE_PIXELS)

    def test_empty_body_is_rejected(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        transport = fake_transport_factory([
            FakeResponse(status=200, headers={"Content-Type": "image/jpeg"}, body=b""),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_EMPTY_BODY)

    def test_non_success_status_is_rejected(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        transport = fake_transport_factory([
            FakeResponse(status=404, headers={}, body=b"missing"),
        ])

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_STATUS)
        self.assertEqual(outcome.status_code, 404)


class StrictnessModeTests(SimpleTestCase):
    """The rollback lever adjusts strictness; it never turns the policy off."""

    def test_default_mode_is_enforce(self):
        self.assertEqual(policy.configured_mode(), policy.MODE_ENFORCE)
        self.assertTrue(policy.is_enforced())

    @override_settings(IG_MEDIA_URL_POLICY_MODE="off")
    def test_unknown_mode_falls_back_to_enforce(self):
        self.assertEqual(policy.configured_mode(), policy.MODE_ENFORCE)

    @override_settings(IG_MEDIA_URL_POLICY_MODE="relaxed")
    def test_relaxed_mode_lets_an_unlisted_host_through(self):
        resolver = fake_resolver_factory({"new-cdn.example.com": ["157.240.1.1"]})

        verdict = policy.validate_media_url(
            "https://new-cdn.example.com/foo.jpg",
            resolver=resolver,
        )

        self.assertTrue(verdict.allowed)

    @override_settings(IG_MEDIA_URL_POLICY_MODE="relaxed")
    def test_relaxed_mode_still_blocks_private_addresses(self):
        resolver = fake_resolver_factory({"new-cdn.example.com": ["127.0.0.1"]})

        verdict = policy.validate_media_url(
            "https://new-cdn.example.com/foo.jpg",
            resolver=resolver,
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_LOOPBACK)

    @override_settings(IG_MEDIA_URL_POLICY_MODE="relaxed")
    def test_relaxed_mode_still_blocks_plain_http(self):
        verdict = policy.validate_media_url("http://new-cdn.example.com/foo.jpg")

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_SCHEME)


class RejectionMetricTests(SimpleTestCase):
    """Rejections are counted by reason (Э3.16 metric)."""

    def test_guard_counts_the_reason_it_rejected_for(self):
        before = policy.rejection_count(policy.REASON_HOST_NOT_ALLOWED)

        allowed, reason = policy.guard_media_url("https://evil.example.com/foo.jpg")

        self.assertFalse(allowed)
        self.assertEqual(reason, policy.REASON_HOST_NOT_ALLOWED)
        self.assertEqual(
            policy.rejection_count(policy.REASON_HOST_NOT_ALLOWED),
            before + 1,
        )

    def test_guard_allows_a_documented_cdn_host(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        verdict = policy.validate_media_url(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
        )
        self.assertTrue(verdict.allowed)


class NoLiveRequestTests(SimpleTestCase):
    """Э3.16 forbids live SSRF probes: the suite must never touch the network."""

    def test_validation_uses_the_injected_resolver_only(self):
        def exploding_getaddrinfo(*args, **kwargs):
            raise AssertionError("the suite must not resolve real hostnames")

        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})

        with patch.object(policy.socket, "getaddrinfo", exploding_getaddrinfo):
            verdict = policy.validate_media_url(
                "https://scontent.cdninstagram.com/foo.jpg",
                resolver=resolver,
            )

        self.assertTrue(verdict.allowed)

    def test_dns_failure_is_fail_closed(self):
        def failing_getaddrinfo(*args, **kwargs):
            raise socket.gaierror("no such host")

        with patch.object(policy.socket, "getaddrinfo", failing_getaddrinfo):
            verdict = policy.validate_media_url(
                "https://scontent.cdninstagram.com/foo.jpg"
            )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_DNS_FAILED)

    def test_empty_dns_answer_is_rejected(self):
        verdict = policy.validate_media_url(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=lambda hostname, port: [],
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_DNS_EMPTY)

    def test_fetch_never_dials_a_socket_when_the_url_is_rejected(self):
        def exploding_create_connection(*args, **kwargs):
            raise AssertionError("the suite must not open sockets")

        with patch.object(policy.socket, "create_connection", exploding_create_connection):
            outcome = policy.fetch_media("https://169.254.169.254/latest/meta-data/")

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_IP_LITERAL)


class LoaderSourceGapTests(SimpleTestCase):
    """The parser accepts more shapes than Meta documents; the policy is the gate.

    This is the mock-only end-to-end proof for ADD-SEC-001: whatever the webhook
    parser hands to the loader still has to pass the policy, and a payload that
    points at an internal address does not.
    """

    def test_policy_rejects_internal_urls_the_parser_would_accept(self):
        from management.services import instagram_bot as bot

        message = {
            "attachments": [
                {"type": "image", "payload": {"url": "http://169.254.169.254/latest/meta-data/"}},
                {"type": "image", "payload": {"url": "https://10.0.0.7/internal.jpg"}},
                {"type": "image", "link_data": {"url": "http://127.0.0.1:8000/admin/"}},
            ]
        }

        extracted = bot._extract_media_urls(message)
        self.assertTrue(extracted, "the parser is expected to accept these shapes")

        for url in extracted:
            with self.subTest(url=url):
                verdict = policy.validate_media_url(url)
                self.assertFalse(verdict.allowed)

    def test_policy_accepts_a_documented_cdn_attachment(self):
        from management.services import instagram_bot as bot

        message = {
            "attachments": [
                {"type": "image", "payload": {"url": "https://scontent.cdninstagram.com/v/ok.jpg"}}
            ]
        }

        extracted = bot._extract_media_urls(message)
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})

        self.assertEqual(len(extracted), 1)
        self.assertTrue(
            policy.validate_media_url(extracted[0], resolver=resolver).allowed
        )


class ShouldFetchRemoteUrlTests(SimpleTestCase):
    """Owned bytes and provider object ids outrank an arbitrary URL."""

    def test_owned_media_is_not_fetched_over_the_network(self):
        item = {"storage_name": "abc.jpg", "status": "owned", "url": "https://cdn/x"}
        self.assertFalse(policy.should_fetch_remote_url(item))

    def test_provider_object_is_not_fetched_by_url(self):
        item = {"ig_post_media_id": "post-1", "url": "https://cdn/x"}
        self.assertFalse(policy.should_fetch_remote_url(item))

    def test_url_only_item_is_fetched(self):
        self.assertTrue(policy.should_fetch_remote_url({"url": "https://cdn/x"}))

    def test_item_without_any_source_is_not_fetched(self):
        self.assertFalse(policy.should_fetch_remote_url({}))


class MalformedInputTests(SimpleTestCase):
    """Junk input must produce a verdict, never an exception."""

    def test_unclosed_ipv6_bracket_is_malformed(self):
        verdict = policy.validate_media_url("https://[::1/foo.jpg")

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_MALFORMED)

    def test_non_numeric_port_is_rejected_not_raised(self):
        # urlsplit accepts this and only raises when .port is read, so the
        # validator has to catch it instead of letting it escape.
        verdict = policy.validate_media_url(
            "https://scontent.cdninstagram.com:notaport/foo.jpg"
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_PORT)

    def test_missing_host_is_rejected(self):
        verdict = policy.validate_media_url("https:///foo.jpg")

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_NO_HOST)

    def test_non_ascii_host_is_rejected(self):
        verdict = policy.validate_media_url("https://привет.cdninstagram.com/foo.jpg")

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_NON_ASCII_HOST)

    def test_unparseable_resolver_answer_is_rejected(self):
        verdict = policy.validate_media_url(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=lambda hostname, port: ["not-an-address"],
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, policy.REASON_BAD_ADDRESS)

    def test_transport_failure_is_reported_as_a_reason(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})

        def failing_transport(target):
            raise OSError("connection reset")

        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/foo.jpg",
            resolver=resolver,
            transport=failing_transport,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_TRANSPORT)


class ProviderMimeSniffTests(SimpleTestCase):
    def test_meta_audio_mp4_is_accepted_with_audio_allowlist(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        body = b"\x00\x00\x00\x18ftypisom" + b"soun"
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/audio",
            resolver=resolver,
            allowed_mime_types=policy.SUPPORTED_INLINE_AUDIO_MIMES,
            transport=fake_transport_factory([FakeResponse(
                status=200, headers={"Content-Type": "video/mp4"}, body=body,
            )]),
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.mime_type, "audio/m4a")

    def test_generic_provider_mime_with_valid_jpeg_is_accepted(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/photo",
            resolver=resolver,
            transport=fake_transport_factory([FakeResponse(
                status=200, headers={"Content-Type": "application/octet-stream"}, body=valid_jpeg(),
            )]),
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.mime_type, "image/jpeg")

    def test_provider_video_mp4_is_accepted_with_video_allowlist(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        body = b"\x00\x00\x00\x18ftypisom" + b"vide"
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/video", resolver=resolver,
            allowed_mime_types=(
                policy.SUPPORTED_INLINE_IMAGE_MIMES
                | policy.SUPPORTED_INLINE_AUDIO_MIMES
                | policy.SUPPORTED_INLINE_VIDEO_MIMES
            ),
            transport=fake_transport_factory([FakeResponse(
                status=200, headers={"Content-Type": "video/mp4"}, body=body,
            )]),
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.mime_type, "video/mp4")

    def test_video_mp4_is_rejected_when_caller_allows_images_only(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        body = b"\x00\x00\x00\x18ftypisom" + b"vide"
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/video",
            allowed_mime_types=policy.SUPPORTED_INLINE_IMAGE_MIMES,
            resolver=resolver,
            transport=fake_transport_factory([FakeResponse(
                status=200, headers={"Content-Type": "video/mp4"}, body=body,
            )]),
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_CONTENT_TYPE)

    def test_sniffed_audio_is_rejected_when_caller_allows_images_only(self):
        resolver = fake_resolver_factory({"scontent.cdninstagram.com": ["157.240.1.1"]})
        body = b"\x00\x00\x00\x18ftypisom" + b"soun"
        outcome = policy.fetch_media(
            "https://scontent.cdninstagram.com/audio",
            allowed_mime_types=policy.SUPPORTED_INLINE_IMAGE_MIMES,
            resolver=resolver,
            transport=fake_transport_factory([FakeResponse(
                status=200, headers={"Content-Type": "video/mp4"}, body=body,
            )]),
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, policy.REASON_CONTENT_TYPE)
