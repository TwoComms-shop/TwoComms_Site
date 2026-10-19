"""Opt-in asynchronous, read-only Marketing API metadata importer.

Admission/drain are separate: reply construction may read a saved observation
but never performs Graph I/O. Absence of credentials/rights is named unknown.
"""
from __future__ import annotations

from datetime import timedelta
import re
import uuid
from urllib.parse import urlsplit, unquote

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from management.services.ig_ad_context import digest, MAX_CANDIDATES, _url

API_VERSION = re.compile(r"^v\d{1,2}\.\d{1,2}$")
AD_ID = re.compile(r"^[0-9]{1,64}$")
FIELDS = "id,name,creative{id,name,object_story_id,effective_object_story_id,instagram_permalink_url,object_story_spec,asset_feed_spec,product_set_id}"
MAX_RESPONSE_BYTES = 128 * 1024


def metadata_capability():
    if not getattr(settings, "IG_AD_METADATA_ENABLED", False):
        return dict(available=False, reason="metadata_import_disabled")
    if not str(getattr(settings, "IG_AD_METADATA_READ_TOKEN", "") or "").strip():
        return dict(available=False, reason="metadata_read_token_unconfigured")
    if not API_VERSION.fullmatch(str(getattr(settings, "IG_AD_METADATA_API_VERSION", "") or "")):
        return dict(available=False, reason="metadata_api_version_unconfigured")
    return dict(available=True, reason="configured_not_permission_verified")


def enqueue_ad_metadata(namespace, ad_id):
    """DB-only deduplicated admission; never inspect token/Graph remotely."""
    capability = metadata_capability()
    if not capability["available"]:
        return capability
    namespace, ad_id = str(namespace or "")[:128], str(ad_id or "")[:64]
    if not namespace or not AD_ID.fullmatch(ad_id):
        return dict(available=False, reason="metadata_ad_identity_invalid")
    from management.models import IgAdMetadataRecord
    # This queue is bounded per namespace. Existing observations are never
    # overwritten/requeued on every customer reply, regardless of cache age.
    with transaction.atomic():
        # Singleton account settings serialize the bounded queue admission.
        from management.models import InstagramBotSettings
        InstagramBotSettings.objects.select_for_update().order_by("pk").first()
        row = IgAdMetadataRecord.objects.filter(namespace=namespace, ad_id=ad_id).first()
        if row is not None:
            return dict(available=True, reason="metadata_already_admitted", record_id=row.pk, state=row.state)
        if IgAdMetadataRecord.objects.filter(namespace=namespace, state__in=["pending", "fetching"]).count() >= 100:
            return dict(available=False, reason="metadata_queue_capacity")
        row, _ = IgAdMetadataRecord.objects.get_or_create(namespace=namespace, ad_id=ad_id,
            defaults=dict(api_version=str(settings.IG_AD_METADATA_API_VERSION)))
    return dict(available=True, reason="metadata_queued", record_id=row.pk, state=row.state)


def read_ad_metadata(namespace, ad_id):
    """Read saved current observation only; no implicit revalidation/fetch."""
    from management.models import IgAdMetadataRecord
    row = IgAdMetadataRecord.objects.filter(namespace=str(namespace or "")[:128], ad_id=str(ad_id or "")[:64]).first()
    if row is None:
        return dict(status="unavailable", reason=metadata_capability()["reason"] if not metadata_capability()["available"] else "metadata_not_imported")
    result = dict(status="available" if row.state == "ready" else "unavailable",
        reason=row.reason or ("metadata_observed" if row.state == "ready" else "metadata_" + row.state),
        record_id=row.pk, state=row.state, fetched_at=row.fetched_at.isoformat() if row.fetched_at else "",
        observation="current_observation_not_historical_proof", freshness="unknown")
    if row.state == "ready" and row.snapshot_digest == digest(row.snapshot):
        result.update(snapshot=row.snapshot, snapshot_digest=row.snapshot_digest)
    elif row.state == "ready":
        result.update(status="unavailable", reason="metadata_snapshot_digest_invalid")
    return result


def _read_graph(ad_id, api_version, token):
    """Exactly one bounded GET. No redirects, permission probes or retries."""
    import json
    import requests
    response = requests.get(f"https://graph.facebook.com/{api_version}/{ad_id}",
        headers={"Authorization": "Bearer " + token}, params={"fields": FIELDS},
        timeout=(3, 8), stream=True, allow_redirects=False)
    try:
        if response.status_code in {401, 403}:
            return None, "metadata_permission_unavailable"
        if response.status_code != 200:
            return None, "metadata_http_unavailable"
        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                return None, "metadata_response_budget"
        data = json.loads(bytes(body))
        if not isinstance(data, dict) or str(data.get("id") or "") != ad_id or data.get("error"):
            return None, "metadata_response_identity_invalid"
        return data, ""
    finally:
        response.close()


def _extract_observation(data, *, api_version, fetched_at):
    creative = data.get("creative") if isinstance(data.get("creative"), dict) else {}
    strings, retailer_ids, links, assets = [], set(), set(), []
    # Depth/count bounded: configuration is not the customer's shown carousel.
    def visit(value, depth=0):
        if depth > 7 or len(strings) >= 200:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:40]:
                if key in {"retailer_id", "retailer_product_id"} and isinstance(item, (str, int)):
                    retailer_ids.add(str(item)[:128])
                if isinstance(item, str):
                    strings.append(item[:2048])
                    if key in {"link", "url", "website_url", "link_url"} and _url(item):
                        links.add(item[:2048])
                    if key in {"image_hash", "video_id", "image_url", "picture"} and len(assets) < 24:
                        assets.append(dict(kind=key, identity=item[:2048]))
                elif isinstance(item, (dict, list)):
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:24]:
                visit(item, depth + 1)
    visit(creative)
    from storefront.models import Product, ProductStatus
    hosts = {"twocomms.shop", "www.twocomms.shop"}
    slugs = []
    for link in links:
        parsed = urlsplit(link)
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname in hosts and len(parts) == 2 and parts[0] == "product":
            slugs.append(unquote(parts[1])[:255])
    # Retailer IDs are ambiguous across catalogues; only canonical site links
    # are auto-bound. Numeric IDs remain evidence unless explicitly namespaced.
    products = Product.objects.filter(status=ProductStatus.PUBLISHED, slug__in=slugs[:24]).only("pk", "slug", "title").order_by("pk")[:MAX_CANDIDATES]
    candidates = [dict(product_id=p.pk, title=p.title, canonical_path=f"/product/{p.slug}/",
        position=None, evidence="canonical_site_link") for p in products]
    return dict(schema_version=1, ad_id=str(data["id"]), title=str(data.get("name") or "")[:512],
        creative_id=str(creative.get("id") or "")[:255],
        post_id=str(creative.get("effective_object_story_id") or creative.get("object_story_id") or "")[:255],
        api_version=api_version, fields_requested=FIELDS,
        fields_received=sorted(str(key) for key in data)[:24], fetched_at=fetched_at.isoformat(),
        candidates=candidates, retailer_ids=sorted(retailer_ids)[:24], assets=assets,
        creative_configuration_digest=digest(creative), shown_order="unknown", observation="current_observation_not_historical_proof",
        freshness="unknown", intent_status="not_inferred", selection_status="not_selected")


def drain_ad_metadata_imports(*, limit=3):
    """Dedicated worker lane only, bounded attempts; no reply/health caller."""
    if not metadata_capability()["available"]:
        return 0
    from management.models import IgAdMetadataRecord
    completed = 0
    # A dead worker must not leave a permanent fetching state or silently retry.
    IgAdMetadataRecord.objects.filter(state="fetching", lease_until__lt=timezone.now()).update(
        state="unavailable", reason="metadata_worker_lease_expired", next_attempt_at=None, lease_until=None, claim_token="")
    for _ in range(max(0, min(int(limit), 5))):
        now = timezone.now()
        with transaction.atomic():
            row = IgAdMetadataRecord.objects.select_for_update(skip_locked=True).filter(state="pending").order_by("pk").first()
            if row is None:
                break
            row.state, row.attempts = "fetching", row.attempts + 1
            row.claim_token, row.lease_until = uuid.uuid4().hex, now + timedelta(minutes=2)
            row.save(update_fields=["state", "attempts", "claim_token", "lease_until"])
        observed_at = timezone.now()
        try:
            if not AD_ID.fullmatch(row.ad_id) or not API_VERSION.fullmatch(row.api_version):
                data, reason = None, "metadata_record_identity_invalid"
            else:
                data, reason = _read_graph(row.ad_id, row.api_version, str(settings.IG_AD_METADATA_READ_TOKEN))
            observed_at = timezone.now()
            if data is None:
                snapshot = {}
            else:
                snapshot = _extract_observation(data, api_version=row.api_version, fetched_at=observed_at)
        except Exception:
            # Do not persist exception strings: requests errors may contain URLs
            # and authorization material. Operators receive a named reason.
            snapshot, reason = {}, "metadata_fetch_unavailable"
        IgAdMetadataRecord.objects.filter(pk=row.pk, state="fetching", claim_token=row.claim_token, attempts=row.attempts).update(
            state="ready" if snapshot else "unavailable", reason=reason,
            fetched_at=observed_at, next_attempt_at=None, lease_until=None, claim_token="", snapshot=snapshot,
            snapshot_digest=digest(snapshot) if snapshot else "")
        completed += 1
    return completed
