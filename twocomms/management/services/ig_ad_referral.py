"""One bounded resolver shared by capture, map, prompt and guards."""
from dataclasses import dataclass, field
from types import SimpleNamespace
from copy import deepcopy

from django.db.models import Q


@dataclass(frozen=True)
class AdReferralResolution:
    status: str
    reason_codes: tuple[str, ...] = ()
    campaign: object | None = None
    context: dict = field(default_factory=dict)

    @property
    def product_id(self) -> int | None:
        candidates = self.context.get("candidates", [])
        if candidates:
            return candidates[0]["product_id"] if len(candidates) == 1 else None
        if self.context:
            return None
        raw = getattr(self.campaign, "product_id", None)
        return int(raw) if raw and self.status == "resolved" else None

    def snapshot(self):
        output = deepcopy(self.context)
        output.update(status=self.status, reason_codes=list(self.reason_codes),
            mapping_active=self.campaign is not None or self.context.get("metadata_resolved") is True,
            mapping_unique=(self.campaign is not None and "duplicate_active_mapping" not in self.reason_codes) or self.context.get("metadata_resolved") is True,
            mapping_id=getattr(self.campaign, "pk", None), product_id=self.product_id,
            theme=str(getattr(self.campaign, "theme", "") or "")[:120],
            intent_status="not_inferred", selection_status="not_selected")
        return output


def resolve_referral_evidence(referral, *, namespace="") -> AdReferralResolution:
    from management.services.ig_ad_context import referral_identity
    ref = referral if isinstance(referral, dict) else {}
    identity = referral_identity(ref)
    resolution = resolve_ad_referral(SimpleNamespace(ad_id=identity["ad_id"], ad_ref=identity["ref"],
        referral_payload=ref))
    if not namespace or not identity["ad_id"]:
        return resolution
    from management.services.ig_ad_metadata import read_ad_metadata
    try:
        metadata = read_ad_metadata(namespace, identity["ad_id"])
    except Exception:
        metadata = dict(status="unavailable", reason="metadata_store_unavailable")
    context = deepcopy(resolution.context)
    context["metadata"] = metadata
    if resolution.reason_codes == ("mapping_missing",) and metadata.get("status") == "available":
        saved = metadata.get("snapshot") or {}
        rows = saved.get("candidates") or []
        if identity["post_id"] and saved.get("post_id") and identity["post_id"] != saved["post_id"]:
            return AdReferralResolution("unavailable", ("metadata_post_conflict",), None, context)
        if rows:
            from storefront.models import Product, ProductStatus
            ids = [row["product_id"] for row in rows]
            published = set(Product.objects.filter(pk__in=ids, status=ProductStatus.PUBLISHED).values_list("pk", flat=True))
            if set(ids) == published:
                context.update(candidates=deepcopy(rows), kind="single_product" if len(rows) == 1 else "product_set",
                    revision=metadata["snapshot_digest"], metadata_resolved=True,
                    freshness="unknown", observation="current_observation_not_historical_proof")
                return AdReferralResolution("resolved", ("metadata_candidates_resolved",), None, context)
            return AdReferralResolution("unavailable", ("metadata_candidate_unpublished",), None, context)
        return AdReferralResolution("unavailable", ("metadata_candidates_unresolved",), None, context)
    if resolution.reason_codes == ("mapping_missing",) and metadata.get("status") == "unavailable":
        return AdReferralResolution("unavailable", ("mapping_missing", metadata.get("reason") or "metadata_unavailable"), None, context)
    return AdReferralResolution(resolution.status, resolution.reason_codes, resolution.campaign, context)


def resolve_ad_referral(client) -> AdReferralResolution:
    """Resolve published candidates; current mapping never proves click history."""
    from management.services.ig_ad_context import campaign_editor_context, digest, referral_identity

    ad_id = str(getattr(client, "ad_id", "") or "").strip()[:255]
    ref = str(getattr(client, "ad_ref", "") or "").strip()[:255]
    payload = getattr(client, "referral_payload", {}) or {}
    identity = referral_identity(payload)
    if not ad_id and not ref:
        return AdReferralResolution("unavailable", ("referral_absent",))
    from management.models import BotAdCampaign

    query = Q()
    if ad_id:
        query |= Q(ad_id=ad_id)
    if ref:
        query |= Q(ref=ref)
    try:
        matches = list(BotAdCampaign.objects.filter(query, is_active=True)
            .select_related("product").order_by("id")[:3])
        if not matches:
            return AdReferralResolution("unavailable", ("mapping_missing",))
        if len(matches) > 1:
            return AdReferralResolution("ambiguous", ("duplicate_active_mapping",))
        campaign = matches[0]
        if any(request and stored and request != stored for request, stored in
            ((ad_id, campaign.ad_id), (ref, campaign.ref))):
            return AdReferralResolution("ambiguous", ("mapping_identity_conflict",), campaign)
        try:
            authored = campaign_editor_context(getattr(campaign, "context", {}) or {},
                product_id=campaign.product_id, theme=campaign.theme)
        except ValueError:
            return AdReferralResolution("unavailable", ("mapping_context_invalid",), campaign)
        evidence = dict(mapping_id=campaign.pk, ad_id=campaign.ad_id, ref=campaign.ref,
            theme=campaign.theme, title=campaign.title, landing_note=campaign.landing_note[:1024],
            context=authored, updated_at=campaign.updated_at.isoformat())
        revision = digest(evidence)
        captured = dict(kind=authored["kind"], candidates=[], revision=revision,
            mapping_digest=revision, mapping_evidence=evidence,
            review=deepcopy(authored["review"]), freshness="unknown",
            observation="current_mapping_at_capture")
        reviewed_post = authored["creative"].get("post_id", "")
        if identity["post_id"] and reviewed_post and identity["post_id"] != reviewed_post:
            return AdReferralResolution("unavailable", ("creative_post_conflict",), campaign, captured)
        if authored["kind"] in {"single_product", "product_set"}:
            from storefront.models import Product, ProductStatus
            ids = [row["product_id"] for row in authored["candidates"]]
            products = {row.pk: row for row in Product.objects.filter(pk__in=ids,
                status=ProductStatus.PUBLISHED).only("pk", "title", "slug")}
            if len(products) != len(ids):
                return AdReferralResolution("unavailable", ("mapped_product_unpublished",), campaign, captured)
            captured["candidates"] = [dict(row, title=products[row["product_id"]].title,
                canonical_path="/product/" + products[row["product_id"]].slug + "/")
                for row in authored["candidates"]]
        elif authored["kind"] != "theme" or not campaign.theme.strip():
            return AdReferralResolution("unavailable", ("mapping_has_no_target",), campaign, captured)
        return AdReferralResolution("resolved", ("mapping_resolved",), campaign, captured)
    except Exception:
        # A DB/import failure is unavailable, never an exact candidate.
        return AdReferralResolution("unavailable", ("resolver_unavailable",))
