"""Unified analytics services used by the custom admin dashboard."""

from __future__ import annotations

import json
import logging
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.core.cache import cache
from django.db.models import Avg, Case, CharField, Count, DurationField, Exists, ExpressionWrapper, F, Min, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, NullIf
from django.utils import timezone
from django.utils.dateparse import parse_date

from orders.models import Order, OrderItem

from ..models import (
    Category,
    CustomPrintLead,
    CustomPrintModerationStatus,
    PageView,
    Product,
    SiteSession,
    SurveySession,
    UserAction,
    UTMSession,
)
from ..analytics_exclusions import (
    action_exclusion_q,
    order_exclusion_q,
    pageview_exclusion_q,
    session_exclusion_q,
    utm_session_exclusion_q,
)
from ..models import AnalyticsExclusion
from ..analytics_noise import analytics_noise_q
from ..utm_utils import get_client_ip
from .external_analytics import (
    fetch_clarity_overview,
    fetch_clarity_problem_urls,
    fetch_ga4_acquisition_snapshot,
    get_clarity_status,
    get_ga4_status,
)

logger = logging.getLogger(__name__)

WIDGET_CACHE_TTL = 60 * 5
TRACKED_FROM_CACHE_TTL = 60 * 10

SOURCE_CLASS_CHOICES = [
    ("all", "Усі джерела"),
    ("bot", "Боти"),
    ("paid", "Платна реклама"),
    ("organic_search", "Органічний пошук"),
    ("instagram_referral", "Instagram"),
    ("other_referral", "Інші посилання"),
    ("direct_unknown", "Direct / unknown"),
]

DEVICE_CHOICES = [
    ("all", "Усі пристрої"),
    ("desktop", "Desktop"),
    ("mobile", "Mobile"),
    ("tablet", "Tablet"),
    ("unknown", "Unknown"),
]

COMPARE_CHOICES = [
    ("none", "Без порівняння"),
    ("previous_period", "Попередній період"),
    ("previous_year", "Рік до року"),
]

PERIOD_CHOICES = [
    ("today", "Сьогодні"),
    ("week", "7 днів"),
    ("month", "30 днів"),
    ("quarter", "90 днів"),
    ("all_time", "Весь час"),
]

CUSTOM_PRINT_STEPS = ["mode", "product", "config", "zones", "artwork", "quantity", "gift", "contact"]
SEARCH_DOMAINS = ("google.", "bing.", "search.yahoo.", "duckduckgo.", "uk.search.yahoo.", "yandex.")
INSTAGRAM_DOMAINS = ("instagram.com", "l.instagram.com", "lm.instagram.com", "instagr.am")
PAID_MEDIUM_TOKENS = (
    "cpc",
    "ppc",
    "paid",
    "paid_social",
    "paid-social",
    "paidsearch",
    "cpm",
    "display",
    "banner",
    "retargeting",
    "remarketing",
    "affiliate",
)
FIRST_PARTY_REFERRERS = ("twocomms", "localhost", "127.0.0.1")


@dataclass(frozen=True)
class AnalyticsFilters:
    period: str
    start_at: datetime | None
    end_at: datetime | None
    compare_to: str
    source_class: str
    device_type: str
    utm_source: str
    utm_medium: str
    campaign: str
    product_id: int | None
    include_bots: bool
    date_from: str
    date_to: str

    def cache_key(self) -> str:
        return json.dumps(
            {
                "period": self.period,
                "start_at": self.start_at.isoformat() if self.start_at else "",
                "end_at": self.end_at.isoformat() if self.end_at else "",
                "compare_to": self.compare_to,
                "source_class": self.source_class,
                "device_type": self.device_type,
                "utm_source": self.utm_source,
                "utm_medium": self.utm_medium,
                "campaign": self.campaign,
                "product_id": self.product_id,
                "include_bots": self.include_bots,
            },
            sort_keys=True,
        )

    def as_query_params(self) -> dict[str, str]:
        payload = {
            "period": self.period,
            "compare_to": self.compare_to,
            "source_class": self.source_class,
            "device_type": self.device_type,
            "utm_source": self.utm_source,
            "utm_medium": self.utm_medium,
            "campaign": self.campaign,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }
        if self.product_id:
            payload["product_id"] = str(self.product_id)
        if self.include_bots:
            payload["include_bots"] = "1"
        return {key: value for key, value in payload.items() if value}

    def build_compare_filters(self) -> AnalyticsFilters | None:
        if self.compare_to == "none" or not self.start_at or not self.end_at:
            return None
        if self.period == "all_time":
            return None

        duration = self.end_at - self.start_at
        if self.compare_to == "previous_period":
            start_at = self.start_at - duration
            end_at = self.start_at
        else:
            start_at = self.start_at - timedelta(days=365)
            end_at = self.end_at - timedelta(days=365)

        return replace(
            self,
            compare_to="none",
            start_at=start_at,
            end_at=end_at,
            date_from=start_at.date().isoformat(),
            date_to=(end_at - timedelta(seconds=1)).date().isoformat(),
        )


@dataclass
class AnalyticsScope:
    site_qs: Any
    utm_qs: Any
    session_keys: set[str] | None


def _cache_get_or_set(prefix: str, payload: dict[str, Any], builder, ttl: int = WIDGET_CACHE_TTL):
    serialized = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha1(serialized.encode("utf-8")).hexdigest()
    key = f"admin_analytics:{prefix}:{digest}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    result = builder()
    cache.set(key, result, ttl)
    return result


def _widget_result(
    *,
    source: str,
    data: Any = None,
    error_state: str | None = None,
    tracked_from: str | None = None,
    empty_state: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "freshness": timezone.now().isoformat(),
        "data": data if data is not None else {},
        "error_state": error_state,
        "tracked_from": tracked_from,
        "empty_state": empty_state,
        "meta": meta or {},
    }


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _local_day_range(day_value: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(day_value, time.min), tz)
    end_at = timezone.make_aware(datetime.combine(day_value + timedelta(days=1), time.min), tz)
    return start_at, end_at


def _resolve_period_range(period: str) -> tuple[datetime | None, datetime | None]:
    today = timezone.localdate()
    if period == "today":
        return _local_day_range(today)
    if period == "week":
        start_day = today - timedelta(days=6)
        start_at, _ = _local_day_range(start_day)
        _, end_at = _local_day_range(today)
        return start_at, end_at
    if period == "month":
        start_day = today - timedelta(days=29)
        start_at, _ = _local_day_range(start_day)
        _, end_at = _local_day_range(today)
        return start_at, end_at
    if period == "quarter":
        start_day = today - timedelta(days=89)
        start_at, _ = _local_day_range(start_day)
        _, end_at = _local_day_range(today)
        return start_at, end_at
    return None, None


def parse_analytics_filters(params) -> AnalyticsFilters:
    period = params.get("period", "month")
    if period not in {item[0] for item in PERIOD_CHOICES}:
        period = "month"

    requested_date_from = (params.get("date_from") or "").strip()
    requested_date_to = (params.get("date_to") or "").strip()
    parsed_date_from = parse_date(requested_date_from) if requested_date_from else None
    parsed_date_to = parse_date(requested_date_to) if requested_date_to else None

    if parsed_date_from and parsed_date_to:
        start_at, _ = _local_day_range(parsed_date_from)
        _, end_at = _local_day_range(parsed_date_to)
        date_from = parsed_date_from.isoformat()
        date_to = parsed_date_to.isoformat()
    else:
        start_at, end_at = _resolve_period_range(period)
        date_from = start_at.date().isoformat() if start_at else ""
        date_to = (end_at - timedelta(seconds=1)).date().isoformat() if end_at else ""

    product_id = None
    try:
        raw_product_id = (params.get("product_id") or "").strip()
        product_id = int(raw_product_id) if raw_product_id else None
    except (TypeError, ValueError):
        product_id = None

    compare_to = params.get("compare_to", "none")
    if compare_to not in {item[0] for item in COMPARE_CHOICES}:
        compare_to = "none"

    source_class = params.get("source_class", "all")
    if source_class not in {item[0] for item in SOURCE_CLASS_CHOICES}:
        source_class = "all"

    device_type = params.get("device_type", "all")
    if device_type not in {item[0] for item in DEVICE_CHOICES}:
        device_type = "all"

    return AnalyticsFilters(
        period=period,
        start_at=start_at,
        end_at=end_at,
        compare_to=compare_to,
        source_class=source_class,
        device_type=device_type,
        utm_source=(params.get("utm_source") or "").strip(),
        utm_medium=(params.get("utm_medium") or "").strip(),
        campaign=(params.get("campaign") or "").strip(),
        product_id=product_id,
        include_bots=str(params.get("include_bots") or "").lower() in {"1", "true", "yes", "on"},
        date_from=date_from,
        date_to=date_to,
    )


def _apply_range(qs, field_name: str, filters: AnalyticsFilters):
    if filters.start_at is not None:
        qs = qs.filter(**{f"{field_name}__gte": filters.start_at})
    if filters.end_at is not None:
        qs = qs.filter(**{f"{field_name}__lt": filters.end_at})
    return qs


def _is_search_referrer(referrer: str) -> bool:
    text = (referrer or "").lower()
    return any(domain in text for domain in SEARCH_DOMAINS)


def _is_instagram_referrer(referrer: str) -> bool:
    text = (referrer or "").lower()
    return any(domain in text for domain in INSTAGRAM_DOMAINS)


def _is_internal_referrer(referrer: str) -> bool:
    text = (referrer or "").lower()
    return any(domain in text for domain in FIRST_PARTY_REFERRERS)


def _extract_referrer_query(referrer: str) -> str:
    if not referrer:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(referrer)
        params = parse_qs(parsed.query or "")
        for key in ("q", "query", "p", "text", "wd"):
            values = params.get(key) or []
            if values:
                return str(values[0]).strip()
    except Exception:
        return ""
    return ""


def _session_utm(session: SiteSession):
    try:
        return getattr(session, "utm_data", None)
    except Exception:
        return None


def classify_session_source(session: SiteSession) -> tuple[str, dict[str, Any]]:
    utm = _session_utm(session)
    first_touch = session.first_touch_data or {}

    utm_source = (
        (getattr(utm, "utm_source", "") or "")
        or str(first_touch.get("utm_source") or "")
    ).strip()
    utm_medium = (
        (getattr(utm, "utm_medium", "") or "")
        or str(first_touch.get("utm_medium") or "")
    ).strip()
    utm_campaign = (
        (getattr(utm, "utm_campaign", "") or "")
        or str(first_touch.get("utm_campaign") or "")
    ).strip()
    referrer = (
        (getattr(utm, "referrer", "") or "")
        or str(first_touch.get("referrer") or "")
    ).strip()
    browser_source = f"{utm_source} {utm_medium} {utm_campaign} {referrer}".lower()
    user_agent = (session.user_agent or getattr(utm, "user_agent", "") or "").lower()

    if session.is_bot or "bot" in user_agent or "spider" in user_agent or "crawler" in user_agent:
        source_class = "bot"
    elif any(token in utm_medium.lower() for token in PAID_MEDIUM_TOKENS) or any(
        token in browser_source for token in ("gclid", "fbclid", "ttclid")
    ):
        source_class = "paid"
    elif utm_medium.lower() == "organic" or _is_search_referrer(referrer) or utm_source.lower() in {
        "google",
        "bing",
        "duckduckgo",
        "yahoo",
    }:
        source_class = "organic_search"
    elif _is_instagram_referrer(referrer) or utm_source.lower() in {"instagram", "insta", "ig"}:
        source_class = "instagram_referral"
    elif (referrer and not _is_internal_referrer(referrer)) or utm_medium.lower() in {
        "referral",
        "social",
        "link",
    }:
        source_class = "other_referral"
    else:
        source_class = "direct_unknown"

    return source_class, {
        "utm_source": utm_source or "direct",
        "utm_medium": utm_medium or "none",
        "utm_campaign": utm_campaign or "",
        "referrer": referrer,
        "search_query": _extract_referrer_query(referrer),
        "device_type": (getattr(utm, "device_type", "") or "unknown").strip() or "unknown",
        "browser_name": (getattr(utm, "browser_name", "") or "").strip(),
        "os_name": (getattr(utm, "os_name", "") or "").strip(),
        "country_name": (getattr(utm, "country_name", "") or "").strip(),
        "city": (getattr(utm, "city", "") or "").strip(),
        "landing_page": (
            (getattr(utm, "landing_page", "") or "")
            or str(first_touch.get("landing_path") or "")
            or str(getattr(session, "first_human_path", "") or "")
        ).strip() or session.last_path,
        "is_returning": bool(getattr(utm, "is_returning_visitor", False)),
    }


def classify_order_source(order: Order) -> str:
    utm_source = (order.utm_source or "").lower()
    utm_medium = (order.utm_medium or "").lower()
    referrer = ""
    try:
        referrer = (order.utm_session.referrer or "").lower() if order.utm_session else ""
    except Exception:
        referrer = ""

    fake_session = SiteSession(
        is_bot=False,
        user_agent="",
        first_touch_data={"referrer": referrer, "utm_source": utm_source, "utm_medium": utm_medium},
    )
    if order.utm_session:
        setattr(fake_session, "utm_data", order.utm_session)
    source_class, _ = classify_session_source(fake_session)
    return source_class


def _resolve_scope(filters: AnalyticsFilters) -> AnalyticsScope:
    site_qs = SiteSession.objects.select_related("utm_data").all()
    site_qs = _apply_range(site_qs, "first_seen", filters)
    # Drop everything that admins explicitly excluded (offices, staff, bot agents…)
    site_exclusion = session_exclusion_q()
    if site_exclusion:
        site_qs = site_qs.exclude(site_exclusion)
    human_pageviews = (
        PageView.objects.filter(session_id=OuterRef("pk"))
        .exclude(analytics_noise_q("path"))
        .order_by("when")
    )
    site_qs = site_qs.annotate(
        has_human_pageview=Exists(human_pageviews),
        first_human_path=Subquery(human_pageviews.values("path")[:1]),
    ).filter(has_human_pageview=True)
    if not filters.include_bots:
        site_qs = site_qs.filter(is_bot=False)
    if filters.device_type != "all":
        site_qs = site_qs.filter(utm_data__device_type=filters.device_type)
    if filters.utm_source:
        site_qs = site_qs.filter(utm_data__utm_source=filters.utm_source)
    if filters.utm_medium:
        site_qs = site_qs.filter(utm_data__utm_medium=filters.utm_medium)
    if filters.campaign:
        site_qs = site_qs.filter(utm_data__utm_campaign=filters.campaign)

    session_keys: set[str] | None = None
    if filters.source_class != "all":
        session_keys = set()
        for session in site_qs.iterator(chunk_size=500):
            source_class, _ = classify_session_source(session)
            if source_class == filters.source_class:
                session_keys.add(session.session_key)
        site_qs = site_qs.filter(session_key__in=session_keys) if session_keys else site_qs.none()

    utm_qs = UTMSession.objects.select_related("session").all()
    utm_qs = _apply_range(utm_qs, "first_seen", filters)
    utm_exclusion = utm_session_exclusion_q()
    if utm_exclusion:
        utm_qs = utm_qs.exclude(utm_exclusion)
    if filters.device_type != "all":
        utm_qs = utm_qs.filter(device_type=filters.device_type)
    if filters.utm_source:
        utm_qs = utm_qs.filter(utm_source=filters.utm_source)
    if filters.utm_medium:
        utm_qs = utm_qs.filter(utm_medium=filters.utm_medium)
    if filters.campaign:
        utm_qs = utm_qs.filter(utm_campaign=filters.campaign)
    if session_keys is not None:
        utm_qs = utm_qs.filter(session_key__in=session_keys)

    return AnalyticsScope(site_qs=site_qs, utm_qs=utm_qs, session_keys=session_keys)


def _orders_queryset(filters: AnalyticsFilters, scope: AnalyticsScope):
    qs = Order.objects.select_related("utm_session", "user").prefetch_related("items", "items__product")
    qs = _apply_range(qs, "created", filters)
    order_exclusion = order_exclusion_q()
    if order_exclusion:
        qs = qs.exclude(order_exclusion)
    if filters.utm_source:
        qs = qs.filter(utm_source=filters.utm_source)
    if filters.utm_medium:
        qs = qs.filter(utm_medium=filters.utm_medium)
    if filters.campaign:
        qs = qs.filter(utm_campaign=filters.campaign)
    if filters.device_type != "all":
        qs = qs.filter(utm_session__device_type=filters.device_type)
    if scope.session_keys is not None:
        qs = qs.filter(Q(session_key__in=scope.session_keys) | Q(utm_session__session_key__in=scope.session_keys))
    return qs


def _actions_queryset(filters: AnalyticsFilters, scope: AnalyticsScope):
    qs = UserAction.objects.select_related("site_session", "utm_session")
    qs = _apply_range(qs, "timestamp", filters)
    action_exclusion = action_exclusion_q()
    if action_exclusion:
        qs = qs.exclude(action_exclusion)
    if filters.product_id:
        qs = qs.filter(product_id=filters.product_id)
    if filters.utm_source:
        qs = qs.filter(utm_session__utm_source=filters.utm_source)
    if filters.utm_medium:
        qs = qs.filter(utm_session__utm_medium=filters.utm_medium)
    if filters.campaign:
        qs = qs.filter(utm_session__utm_campaign=filters.campaign)
    if filters.device_type != "all":
        qs = qs.filter(utm_session__device_type=filters.device_type)
    if not filters.include_bots:
        qs = qs.filter(Q(site_session__is_bot=False) | Q(site_session__isnull=True))
    # F-076: quarantine legacy/residual product-view rows that cannot be tied
    # to an actual page navigation, even in the opt-in bot diagnostic view.
    # Keep null-session lead and purchase actions: their linkage is repaired
    # by separate order flows. A linked bot PV remains visible with
    # include_bots=1.
    qs = qs.exclude(
        Q(action_type="product_view")
        & (Q(site_session__isnull=True) | Q(site_session__pageviews__lte=0))
    )
    if scope.session_keys is not None:
        qs = qs.filter(
            Q(site_session__session_key__in=scope.session_keys)
            | Q(utm_session__session_key__in=scope.session_keys)
        )
    return qs


def _pageviews_queryset(filters: AnalyticsFilters, scope: AnalyticsScope):
    qs = PageView.objects.select_related("session")
    qs = _apply_range(qs, "when", filters)
    qs = qs.exclude(analytics_noise_q("path"))
    pv_exclusion = pageview_exclusion_q()
    if pv_exclusion:
        qs = qs.exclude(pv_exclusion)
    if not filters.include_bots:
        qs = qs.filter(is_bot=False)
    if scope.session_keys is not None:
        qs = qs.filter(session__session_key__in=scope.session_keys)
    return qs


def _custom_print_queryset(filters: AnalyticsFilters):
    qs = CustomPrintLead.objects.select_related("order")
    qs = _apply_range(qs, "created_at", filters)
    return qs


def _survey_queryset(filters: AnalyticsFilters):
    qs = SurveySession.objects.select_related("user", "awarded_promocode")
    qs = _apply_range(qs, "started_at", filters)
    return qs


def _build_daily_labels(filters: AnalyticsFilters) -> list[date]:
    if not filters.start_at or not filters.end_at:
        return []
    labels = []
    current = filters.start_at.date()
    end_date = (filters.end_at - timedelta(seconds=1)).date()
    while current <= end_date:
        labels.append(current)
        current += timedelta(days=1)
    return labels


def _local_day_key(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).date().isoformat()
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _count_queryset_by_local_day(qs, datetime_field: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for value in qs.values_list(datetime_field, flat=True).iterator(chunk_size=2000):
        day_key = _local_day_key(value)
        if day_key:
            counter[day_key] += 1
    return dict(counter)


def _sum_queryset_by_local_day(qs, datetime_field: str, value_field: str) -> dict[str, float]:
    totals: defaultdict[str, float] = defaultdict(float)
    for value, amount in qs.values_list(datetime_field, value_field).iterator(chunk_size=2000):
        day_key = _local_day_key(value)
        if day_key:
            totals[day_key] += _as_float(amount)
    return dict(totals)


def _delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 2)


def _comparison_summary(current_value: float, previous_value: float) -> dict[str, Any]:
    return {
        "current": current_value,
        "previous": previous_value,
        "delta_percent": _delta(current_value, previous_value),
    }


def _count_distinct_visitors(site_qs) -> int:
    """
    Count distinct visitors falling back from ``visitor_id`` (twc_vid cookie) to
    ``session_key`` when the cookie is missing. ``Coalesce`` alone is not enough
    because empty strings are non-NULL, so we route them through ``NullIf`` to
    treat them as missing.
    """
    return (
        site_qs.annotate(
            visitor_key=Coalesce(
                NullIf(F("visitor_id"), Value("")),
                F("session_key"),
                output_field=CharField(),
            )
        )
        .values("visitor_key")
        .distinct()
        .count()
    )


SESSION_DURATION_CAP_SECONDS = 30 * 60  # 30 minutes — GA4-style inactivity timeout


def _format_duration_human(seconds: int | float | None) -> str:
    """Render a seconds value as a compact ``mm:ss`` / ``hh:mm:ss`` label."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "0с"
    if total <= 0:
        return "0с"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}год {minutes:02d}хв"
    if minutes:
        return f"{minutes}хв {secs:02d}с"
    return f"{secs}с"


def _average_session_seconds(site_qs) -> int:
    """
    Mean session duration in seconds.

    Two pitfalls we guard against:
    * Single-page sessions where ``last_seen == first_seen`` collapse the mean
      toward zero, so we drop ``duration <= 0`` from the aggregate.
    * Long-lived browser tabs can keep ``last_seen`` ticking for hours/days,
      producing absurd 9h+ averages. Like GA4, we cap any single session to
      ``SESSION_DURATION_CAP_SECONDS`` (30 min of inactivity).
    """
    cap = timedelta(seconds=SESSION_DURATION_CAP_SECONDS)
    qs_with_duration = site_qs.annotate(
        raw_duration=ExpressionWrapper(F("last_seen") - F("first_seen"), output_field=DurationField())
    ).filter(raw_duration__gt=timedelta(seconds=0))

    total_seconds = 0
    counted = 0
    for value in qs_with_duration.values_list("raw_duration", flat=True).iterator(chunk_size=2000):
        if value is None:
            continue
        seconds = int(value.total_seconds())
        if seconds <= 0:
            continue
        total_seconds += min(seconds, SESSION_DURATION_CAP_SECONDS)
        counted += 1

    return total_seconds // counted if counted else 0


def _tracked_from(*action_types: str) -> str | None:
    payload = {"action_types": action_types}

    def _builder():
        value = (
            UserAction.objects.filter(action_type__in=action_types)
            .order_by("timestamp")
            .values_list("timestamp", flat=True)
            .first()
        )
        return value.isoformat() if value else None

    return _cache_get_or_set("tracked_from", payload, _builder, ttl=TRACKED_FROM_CACHE_TTL)


def _build_overview_cards(filters: AnalyticsFilters) -> dict[str, Any]:
    scope = _resolve_scope(filters)
    site_qs = scope.site_qs
    orders_qs = _orders_queryset(filters, scope)
    actions_qs = _actions_queryset(filters, scope)
    custom_qs = _custom_print_queryset(filters)
    survey_qs = _survey_queryset(filters)
    pageviews_qs = _pageviews_queryset(filters, scope)

    paid_orders = orders_qs.filter(payment_status="paid")
    sessions_count = site_qs.count()
    unique_visitors = _count_distinct_visitors(site_qs)
    revenue = _as_float(paid_orders.aggregate(total=Sum("total_sum"))["total"])
    paid_orders_count = paid_orders.count()
    all_orders_count = orders_qs.count()
    aov = _as_float(paid_orders.aggregate(avg=Avg("total_sum"))["avg"])
    avg_duration = _average_session_seconds(site_qs)
    bounce_sessions = site_qs.annotate(
        clean_pageviews=Count("views", filter=~analytics_noise_q("views__path"))
    ).filter(clean_pageviews__lte=1).count()
    bounce_rate = round((bounce_sessions / sessions_count) * 100, 2) if sessions_count else 0
    conversion_rate = round((paid_orders_count / sessions_count) * 100, 2) if sessions_count else 0

    purchase_events = actions_qs.filter(action_type="purchase").count() or paid_orders_count
    cart_adds = actions_qs.filter(action_type="add_to_cart").count()
    checkout_starts = actions_qs.filter(action_type="initiate_checkout").count()
    custom_starts = actions_qs.filter(action_type="custom_print_start").count()
    survey_starts = actions_qs.filter(action_type="survey_start").count() or survey_qs.count()
    survey_completions = actions_qs.filter(action_type="survey_complete").count() or survey_qs.filter(status="completed").count()

    compare_filters = filters.build_compare_filters()
    compare = {}
    if compare_filters:
        compare_scope = _resolve_scope(compare_filters)
        compare_site_qs = compare_scope.site_qs
        compare_all_orders = _orders_queryset(compare_filters, compare_scope)
        compare_orders = compare_all_orders.filter(payment_status="paid")
        compare_actions = _actions_queryset(compare_filters, compare_scope)
        compare_survey = _survey_queryset(compare_filters)
        compare.update(
            {
                "sessions": _comparison_summary(sessions_count, compare_site_qs.count()),
                "revenue": _comparison_summary(
                    revenue,
                    _as_float(compare_orders.aggregate(total=Sum("total_sum"))["total"]),
                ),
            "orders": _comparison_summary(all_orders_count, compare_all_orders.count()),
                "checkout_starts": _comparison_summary(
                    checkout_starts,
                    compare_actions.filter(action_type="initiate_checkout").count(),
                ),
                "survey_completions": _comparison_summary(
                    survey_completions,
                    compare_actions.filter(action_type="survey_complete").count()
                    or compare_survey.filter(status="completed").count(),
                ),
            }
        )

    return {
        "headline": {
            "sessions": sessions_count,
            "unique_visitors": unique_visitors,
            "orders": all_orders_count,
            "paid_orders": paid_orders_count,
            "revenue": revenue,
            "aov": aov,
            "conversion_rate": conversion_rate,
            "avg_session_seconds": avg_duration,
            "avg_session_label": _format_duration_human(avg_duration),
            "bounce_rate": bounce_rate,
            "page_views": pageviews_qs.count(),
            "cart_adds": cart_adds,
            "checkout_starts": checkout_starts,
            "purchases": purchase_events,
            "custom_print_starts": custom_starts or custom_qs.count(),
            "survey_starts": survey_starts,
            "survey_completions": survey_completions,
        },
        "comparison": compare,
        "tracked_from": {
            "product_views": _tracked_from("product_view"),
            "search": _tracked_from("search"),
            "checkout": _tracked_from("initiate_checkout"),
            "purchase": _tracked_from("purchase"),
            "custom_print": _tracked_from("custom_print_start", "custom_print_step_enter"),
            "survey": _tracked_from("survey_start", "survey_answer"),
        },
    }


def build_overview_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "overview",
        {"filters": filters.cache_key()},
        lambda: _widget_result(source="internal", data=_build_overview_cards(filters)),
    )


def _timeseries_data(filters: AnalyticsFilters) -> dict[str, Any]:
    if not filters.start_at or not filters.end_at:
        return {
            "labels": [],
            "series": {
                "sessions": [],
                "orders": [],
                "revenue": [],
                "cart_adds": [],
                "checkout_starts": [],
                "purchases": [],
            },
            "comparison": None,
        }

    scope = _resolve_scope(filters)
    session_bucket = _count_queryset_by_local_day(scope.site_qs, "first_seen")
    order_bucket = _count_queryset_by_local_day(_orders_queryset(filters, scope), "created")
    revenue_bucket = _sum_queryset_by_local_day(
        _orders_queryset(filters, scope).filter(payment_status="paid"),
        "created",
        "total_sum",
    )
    actions_qs = _actions_queryset(filters, scope)
    cart_bucket = _count_queryset_by_local_day(actions_qs.filter(action_type="add_to_cart"), "timestamp")
    checkout_bucket = _count_queryset_by_local_day(actions_qs.filter(action_type="initiate_checkout"), "timestamp")
    purchase_bucket = _count_queryset_by_local_day(actions_qs.filter(action_type="purchase"), "timestamp")

    labels = _build_daily_labels(filters)
    label_strings = [item.isoformat() for item in labels]

    series = {
        "sessions": [_as_int(session_bucket.get(label, 0)) for label in label_strings],
        "orders": [_as_int(order_bucket.get(label, 0)) for label in label_strings],
        "revenue": [round(_as_float(revenue_bucket.get(label, 0)), 2) for label in label_strings],
        "cart_adds": [_as_int(cart_bucket.get(label, 0)) for label in label_strings],
        "checkout_starts": [_as_int(checkout_bucket.get(label, 0)) for label in label_strings],
        "purchases": [_as_int(purchase_bucket.get(label, 0)) for label in label_strings],
    }

    comparison = None
    compare_filters = filters.build_compare_filters()
    if compare_filters and compare_filters.start_at and compare_filters.end_at:
        compare_scope = _resolve_scope(compare_filters)
        compare_labels = _build_daily_labels(compare_filters)
        compare_label_strings = [item.isoformat() for item in compare_labels]
        compare_session_bucket = _count_queryset_by_local_day(compare_scope.site_qs, "first_seen")
        compare_revenue_bucket = _sum_queryset_by_local_day(
            _orders_queryset(compare_filters, compare_scope).filter(payment_status="paid"),
            "created",
            "total_sum",
        )
        comparison = {
            "label": "Попередній період" if filters.compare_to == "previous_period" else "Рік до року",
            "labels": compare_label_strings,
            "series": {
                "sessions": [_as_int(compare_session_bucket.get(label, 0)) for label in compare_label_strings],
                "revenue": [round(_as_float(compare_revenue_bucket.get(label, 0)), 2) for label in compare_label_strings],
            },
        }

    return {"labels": label_strings, "series": series, "comparison": comparison}


def build_timeseries_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "timeseries",
        {"filters": filters.cache_key()},
        lambda: _widget_result(source="internal", data=_timeseries_data(filters)),
    )


def _acquisition_data(filters: AnalyticsFilters) -> dict[str, Any]:
    scope = _resolve_scope(filters)
    sessions = list(scope.site_qs)
    source_class_counter = Counter()
    source_map: dict[tuple[str, str], dict[str, Any]] = {}
    landing_counter = Counter()
    referrer_counter = Counter()
    geo_counter = Counter()
    browser_counter = Counter()
    os_counter = Counter()
    device_counter = Counter()
    search_term_counter = Counter()
    new_vs_returning = Counter({"new": 0, "returning": 0})

    for session in sessions:
        source_class, meta = classify_session_source(session)
        source_class_counter[source_class] += 1
        source_key = (meta["utm_source"], meta["utm_medium"])
        source_entry = source_map.setdefault(
            source_key,
            {
                "utm_source": meta["utm_source"],
                "utm_medium": meta["utm_medium"],
                "sessions": 0,
                "source_class": source_class,
            },
        )
        source_entry["sessions"] += 1
        if meta["landing_page"]:
            landing_counter[meta["landing_page"]] += 1
        if meta["referrer"] and not _is_internal_referrer(meta["referrer"]):
            referrer_counter[meta["referrer"]] += 1
        if meta["country_name"]:
            geo_counter[f"{meta['country_name']} / {meta['city'] or '—'}"] += 1
        if meta["browser_name"]:
            browser_counter[meta["browser_name"]] += 1
        if meta["os_name"]:
            os_counter[meta["os_name"]] += 1
        if meta["device_type"]:
            device_counter[meta["device_type"]] += 1
        if meta["search_query"]:
            search_term_counter[meta["search_query"]] += 1
        if meta["is_returning"]:
            new_vs_returning["returning"] += 1
        else:
            new_vs_returning["new"] += 1

    actions_qs = _actions_queryset(filters, scope)
    internal_searches = (
        actions_qs.filter(action_type="search")
        .values("metadata__query")
        .annotate(total=Count("id"))
        .order_by("-total")[:20]
    )

    ga4_snapshot = None
    if filters.start_at and filters.end_at:
        ga4_snapshot = fetch_ga4_acquisition_snapshot(
            start_date=filters.start_at.date(),
            end_date=(filters.end_at - timedelta(seconds=1)).date(),
            device_type="" if filters.device_type == "all" else filters.device_type,
            utm_source=filters.utm_source,
            utm_medium=filters.utm_medium,
            campaign=filters.campaign,
        )

    return {
        "source_classes": [
            {
                "key": key,
                "label": dict(SOURCE_CLASS_CHOICES).get(key, key),
                "sessions": count,
            }
            for key, count in source_class_counter.most_common()
        ],
        "top_sources": sorted(source_map.values(), key=lambda item: item["sessions"], reverse=True)[:20],
        "landing_pages": [{"label": label, "sessions": count} for label, count in landing_counter.most_common(15)],
        "referrers": [{"label": label, "sessions": count} for label, count in referrer_counter.most_common(15)],
        "geo": [{"label": label, "sessions": count} for label, count in geo_counter.most_common(15)],
        "devices": [{"label": label, "sessions": count} for label, count in device_counter.most_common(10)],
        "browsers": [{"label": label, "sessions": count} for label, count in browser_counter.most_common(10)],
        "os": [{"label": label, "sessions": count} for label, count in os_counter.most_common(10)],
        "visitor_mix": dict(new_vs_returning),
        "organic_queries": [{"label": label, "sessions": count} for label, count in search_term_counter.most_common(20)],
        "internal_searches": [
            {
                "query": row["metadata__query"] or "—",
                "count": row["total"],
            }
            for row in internal_searches
            if row["metadata__query"]
        ],
        "ga4_snapshot": ga4_snapshot,
    }


def build_acquisition_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "acquisition",
        {"filters": filters.cache_key()},
        lambda: _widget_result(source="internal+ga4", data=_acquisition_data(filters)),
    )


def _sales_data(filters: AnalyticsFilters) -> dict[str, Any]:
    scope = _resolve_scope(filters)
    orders_qs = _orders_queryset(filters, scope)
    paid_orders = orders_qs.filter(payment_status="paid")
    paid_or_prepay = orders_qs.filter(payment_status__in=["paid", "prepaid", "partial"])
    order_items = OrderItem.objects.filter(order__in=paid_orders)

    customer_counter = Counter()
    for order in paid_orders:
        customer_key = f"user:{order.user_id}" if order.user_id else f"phone:{order.phone}"
        customer_counter[customer_key] += 1

    repeat_customers = sum(1 for value in customer_counter.values() if value > 1)
    total_customers = len(customer_counter)

    payment_split_rows = (
        orders_qs.values("payment_status")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    source_ltv_map: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": 0, "orders": 0, "revenue": 0.0})
    for order in paid_orders:
        source_class = classify_order_source(order)
        bucket = source_ltv_map[source_class]
        bucket["orders"] += 1
        bucket["revenue"] += _as_float(order.total_sum)

    for session in scope.site_qs.iterator(chunk_size=500):
        source_class, _ = classify_session_source(session)
        source_ltv_map[source_class]["sessions"] += 1

    daily_revenue = _sum_queryset_by_local_day(paid_orders, "created", "total_sum")
    daily_orders = _count_queryset_by_local_day(paid_orders, "created")
    daily_labels = sorted(set(daily_revenue.keys()) | set(daily_orders.keys()))

    top_products = (
        order_items.values("product_id", "product__title")
        .annotate(items_sold=Sum("qty"), revenue=Sum("line_total"))
        .order_by("-revenue")[:15]
    )

    return {
        "summary": {
            "paid_orders": paid_orders.count(),
            "paid_or_prepay_orders": paid_or_prepay.count(),
            "revenue": _as_float(paid_orders.aggregate(total=Sum("total_sum"))["total"]),
            "aov": _as_float(paid_orders.aggregate(avg=Avg("total_sum"))["avg"]),
            "items_sold": _as_int(order_items.aggregate(total=Sum("qty"))["total"]),
            "repeat_purchase_rate": round((repeat_customers / total_customers) * 100, 2) if total_customers else 0,
            "total_customers": total_customers,
        },
        "payment_split": [
            {"payment_status": row["payment_status"] or "unknown", "count": row["total"]}
            for row in payment_split_rows
        ],
        "daily_series": {
            "labels": daily_labels,
            "revenue": [round(_as_float(daily_revenue.get(label, 0)), 2) for label in daily_labels],
            "orders": [_as_int(daily_orders.get(label, 0)) for label in daily_labels],
        },
        "source_ltv": [
            {
                "source_class": key,
                "label": dict(SOURCE_CLASS_CHOICES).get(key, key),
                "sessions": values["sessions"],
                "orders": values["orders"],
                "revenue": round(values["revenue"], 2),
                "ltv": round(values["revenue"] / values["sessions"], 2) if values["sessions"] else 0,
            }
            for key, values in sorted(source_ltv_map.items(), key=lambda item: item[1]["revenue"], reverse=True)
        ],
        "top_products": [
            {
                "product_id": row["product_id"],
                "title": row["product__title"],
                "items_sold": row["items_sold"],
                "revenue": round(_as_float(row["revenue"]), 2),
            }
            for row in top_products
        ],
    }


def build_sales_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "sales",
        {"filters": filters.cache_key()},
        lambda: _widget_result(source="internal", data=_sales_data(filters)),
    )


def _action_identity(action: UserAction) -> str:
    metadata = action.metadata or {}
    visitor_id = metadata.get("visitor_id")
    if visitor_id:
        return f"visitor:{visitor_id}"
    if action.site_session_id:
        session = action.site_session
        if session and session.visitor_id:
            return f"visitor:{session.visitor_id}"
        if session:
            return f"session:{session.session_key}"
    return f"action:{action.pk}"


def _cart_data(filters: AnalyticsFilters) -> dict[str, Any]:
    scope = _resolve_scope(filters)
    actions_qs = _actions_queryset(filters, scope)
    orders_qs = _orders_queryset(filters, scope)

    add_actions = list(actions_qs.filter(action_type="add_to_cart"))
    remove_actions = list(actions_qs.filter(action_type="remove_from_cart"))
    checkout_actions = list(actions_qs.filter(action_type="initiate_checkout"))
    purchase_actions = list(actions_qs.filter(action_type="purchase"))

    add_entities = {_action_identity(action) for action in add_actions}
    remove_entities = {_action_identity(action) for action in remove_actions}
    checkout_entities = {_action_identity(action) for action in checkout_actions}
    purchase_entities = {_action_identity(action) for action in purchase_actions}

    top_removed = (
        actions_qs.filter(action_type="remove_from_cart")
        .values("product_id", "product_name")
        .annotate(total=Count("id"))
        .order_by("-total")[:15]
    )

    payment_methods = (
        orders_qs.values("pay_type")
        .annotate(total=Count("id"))
        .order_by("-total")
    )

    purchased_from_added = len(add_entities & purchase_entities) if purchase_entities else 0
    add_unique = len(add_entities)
    checkout_unique = len(checkout_entities)
    purchase_unique = len(purchase_entities) or orders_qs.filter(payment_status="paid").count()

    funnel = [
        {"key": "add_to_cart", "label": "Додали в кошик", "count": add_unique},
        {"key": "remove_from_cart", "label": "Видалили з кошика", "count": len(remove_entities)},
        {"key": "initiate_checkout", "label": "Почали checkout", "count": checkout_unique},
        {"key": "purchase", "label": "Купили", "count": purchase_unique},
    ]

    return {
        "summary": {
            "adds": len(add_actions),
            "removes": len(remove_actions),
            "checkout_starts": len(checkout_actions),
            "purchases": len(purchase_actions) or orders_qs.filter(payment_status="paid").count(),
            "abandonment_rate": round(((add_unique - purchase_unique) / add_unique) * 100, 2) if add_unique else 0,
            "add_to_checkout_rate": round((checkout_unique / add_unique) * 100, 2) if add_unique else 0,
            "add_to_purchase_rate": round((purchase_unique / add_unique) * 100, 2) if add_unique else 0,
            "remove_after_add": len(add_entities & remove_entities),
            "added_then_purchased": purchased_from_added,
        },
        "funnel": funnel,
        "payment_methods": [{"pay_type": row["pay_type"] or "unknown", "count": row["total"]} for row in payment_methods],
        "top_removed_products": [
            {
                "product_id": row["product_id"],
                "product_name": row["product_name"] or "—",
                "count": row["total"],
            }
            for row in top_removed
        ],
    }


def build_cart_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "cart",
        {"filters": filters.cache_key()},
        lambda: _widget_result(source="internal", data=_cart_data(filters)),
    )


def build_product_admin_metrics(product_ids: list[int]) -> dict[int, dict[str, int]]:
    if not product_ids:
        return {}
    product_views = UserAction.objects.filter(
        action_type="product_view",
        product_id__in=product_ids,
        site_session__is_bot=False,
        site_session__pageviews__gt=0,
    )
    action_exclusion = action_exclusion_q()
    if action_exclusion:
        product_views = product_views.exclude(action_exclusion)
    totals = (
        product_views
        .values("product_id")
        .annotate(
            total_views=Count("id"),
            unique_ip_views=Count("site_session__ip_address", distinct=True),
        )
    )
    return {
        row["product_id"]: {
            "total_views": row["total_views"],
            "unique_ip_views": row["unique_ip_views"],
        }
        for row in totals
    }


def _products_data(filters: AnalyticsFilters) -> dict[str, Any]:
    scope = _resolve_scope(filters)
    actions_qs = _actions_queryset(filters, scope)
    view_rows = (
        actions_qs.filter(action_type="product_view")
        .values("product_id", "product_name")
        .annotate(
            total_views=Count("id"),
            unique_ip_views=Count("site_session__ip_address", distinct=True),
        )
        .order_by("-total_views")
    )
    add_rows = {
        row["product_id"]: row["total"]
        for row in actions_qs.filter(action_type="add_to_cart")
        .values("product_id")
        .annotate(total=Count("id"))
    }
    purchase_rows = {
        row["product_id"]: {
            "purchases": row["orders"],
            "items_sold": row["items_sold"],
            "revenue": _as_float(row["revenue"]),
        }
        for row in OrderItem.objects.filter(order__in=_orders_queryset(filters, scope).filter(payment_status="paid"))
        .values("product_id")
        .annotate(
            orders=Count("order_id", distinct=True),
            items_sold=Sum("qty"),
            revenue=Sum("line_total"),
        )
    }

    top_viewed = []
    category_counter = Counter()
    viewed_product_ids = []
    for row in view_rows[:25]:
        product_id = row["product_id"]
        if not product_id:
            continue
        viewed_product_ids.append(product_id)
        product = Product.objects.filter(pk=product_id).select_related("category").first()
        add_count = add_rows.get(product_id, 0)
        purchase_meta = purchase_rows.get(product_id, {})
        total_views = row["total_views"]
        view_to_cart_rate = round((add_count / total_views) * 100, 2) if total_views else 0
        purchase_count = purchase_meta.get("purchases", 0)
        view_to_purchase_rate = round((purchase_count / total_views) * 100, 2) if total_views else 0
        if product and product.category:
            category_counter[product.category.name] += total_views
        top_viewed.append(
            {
                "product_id": product_id,
                "title": row["product_name"] or (product.title if product else "—"),
                "category": product.category.name if product and product.category else "—",
                "total_views": total_views,
                "unique_ip_views": row["unique_ip_views"],
                "adds_to_cart": add_count,
                "purchases": purchase_count,
                "items_sold": purchase_meta.get("items_sold", 0),
                "revenue": round(purchase_meta.get("revenue", 0), 2),
                "view_to_cart_rate": view_to_cart_rate,
                "view_to_purchase_rate": view_to_purchase_rate,
            }
        )

    low_view_threshold = 3
    low_viewed = []
    viewed_product_id_set = set(viewed_product_ids)
    totals_by_product = {row["product_id"]: row["total_views"] for row in view_rows}
    for product in Product.objects.select_related("category").filter(status="published").order_by("title")[:150]:
        total_views = totals_by_product.get(product.pk, 0)
        if total_views <= low_view_threshold:
            low_viewed.append(
                {
                    "product_id": product.pk,
                    "title": product.title,
                    "category": product.category.name if product.category else "—",
                    "total_views": total_views,
                }
            )
        if len(low_viewed) >= 20:
            break

    return {
        "summary": {
            "tracked_product_view_from": _tracked_from("product_view"),
            "tracked_add_to_cart_from": _tracked_from("add_to_cart"),
            "tracked_purchase_from": _tracked_from("purchase"),
        },
        "top_viewed": top_viewed,
        "low_viewed": low_viewed,
        "categories": [{"category": label, "views": count} for label, count in category_counter.most_common(12)],
    }


def build_products_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "products",
        {"filters": filters.cache_key()},
        lambda: _widget_result(
            source="internal",
            data=_products_data(filters),
            tracked_from=_tracked_from("product_view"),
        ),
    )


def _custom_print_data(filters: AnalyticsFilters) -> dict[str, Any]:
    custom_qs = _custom_print_queryset(filters)
    action_filters = replace(filters, product_id=None)
    scope = _resolve_scope(action_filters)
    actions_qs = _actions_queryset(action_filters, scope).filter(
        action_type__in=[
            "custom_print_start",
            "custom_print_step_enter",
            "custom_print_step_complete",
            "custom_print_add_to_cart",
            "custom_print_send_to_manager",
            "custom_print_safe_exit",
            "custom_print_moderation_result",
        ]
    )

    step_enter_counter = Counter()
    step_complete_counter = Counter()
    safe_exit_counter = Counter()
    for action in actions_qs:
        metadata = action.metadata or {}
        step_key = str(metadata.get("step_key") or "")
        if action.action_type == "custom_print_step_enter" and step_key:
            step_enter_counter[step_key] += 1
        elif action.action_type == "custom_print_step_complete" and step_key:
            step_complete_counter[step_key] += 1
        elif action.action_type == "custom_print_safe_exit":
            safe_exit_counter[step_key or str(metadata.get("exit_step") or "") or "unknown"] += 1

    if not safe_exit_counter:
        for row in custom_qs.exclude(exit_step="").values("exit_step").annotate(total=Count("id")):
            safe_exit_counter[row["exit_step"]] += row["total"]

    moderation_counts = (
        custom_qs.values("moderation_status")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    client_mix = (
        custom_qs.values("client_kind")
        .annotate(total=Count("id"))
        .order_by("-total")
    )

    estimate_required = sum(1 for lead in custom_qs if lead.estimate_required)
    priced_values = [_as_float(lead.final_price_value) for lead in custom_qs]

    step_funnel = []
    for step in CUSTOM_PRINT_STEPS:
        enters = step_enter_counter.get(step, 0)
        completes = step_complete_counter.get(step, 0)
        dropoff = max(0, enters - completes)
        step_funnel.append(
            {
                "step": step,
                "enters": enters,
                "completes": completes,
                "dropoff": dropoff,
            }
        )

    return {
        "summary": {
            "unique_starters": actions_qs.filter(action_type="custom_print_start").count() or custom_qs.count(),
            "leads": custom_qs.count(),
            "estimate_required": estimate_required,
            "add_to_cart": actions_qs.filter(action_type="custom_print_add_to_cart").count()
            or custom_qs.filter(source="custom_print_cart").count(),
            "send_to_manager": actions_qs.filter(action_type="custom_print_send_to_manager").count(),
            "linked_to_order": custom_qs.filter(order__isnull=False).count(),
            "linked_to_order_rate": round((custom_qs.filter(order__isnull=False).count() / custom_qs.count()) * 100, 2)
            if custom_qs.count()
            else 0,
            "avg_final_value": round(sum(priced_values) / len(priced_values), 2) if priced_values else 0,
        },
        "step_funnel": step_funnel,
        "safe_exits": [{"step": key, "count": count} for key, count in safe_exit_counter.most_common()],
        "moderation": [
            {"status": row["moderation_status"] or "unknown", "count": row["total"]}
            for row in moderation_counts
        ],
        "client_mix": [
            {"client_kind": row["client_kind"] or "unknown", "count": row["total"]}
            for row in client_mix
        ],
    }


def build_custom_print_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "custom_print",
        {"filters": filters.cache_key()},
        lambda: _widget_result(
            source="internal",
            data=_custom_print_data(filters),
            tracked_from=_tracked_from("custom_print_start", "custom_print_step_enter"),
        ),
    )


def _survey_data(filters: AnalyticsFilters) -> dict[str, Any]:
    survey_qs = _survey_queryset(filters)
    scope = _resolve_scope(filters)
    actions_qs = _actions_queryset(filters, scope).filter(
        action_type__in=["survey_start", "survey_answer", "survey_back", "survey_skip", "survey_close", "survey_complete"]
    )
    answer_rows = (
        actions_qs.filter(action_type="survey_answer")
        .values("metadata__question_id")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    dropoff_rows = (
        survey_qs.filter(status="in_progress")
        .values("current_question_id")
        .annotate(total=Count("id"))
        .order_by("-total")
    )

    downstream_purchases = 0
    completed_sessions = list(survey_qs.filter(status="completed", completed_at__isnull=False).select_related("user"))
    for session in completed_sessions:
        if Order.objects.filter(user=session.user, created__gte=session.completed_at).exists():
            downstream_purchases += 1

    return {
        "summary": {
            "starts": actions_qs.filter(action_type="survey_start").count() or survey_qs.count(),
            "completed": actions_qs.filter(action_type="survey_complete").count()
            or survey_qs.filter(status="completed").count(),
            "resumes": actions_qs.filter(action_type="survey_start", metadata__created=False).count(),
            "back_used": actions_qs.filter(action_type="survey_back").count() or survey_qs.filter(back_used=True).count(),
            "skip_used": actions_qs.filter(action_type="survey_skip").count(),
            "promo_issued": survey_qs.filter(awarded_promocode__isnull=False).count(),
            "completion_rate": round((survey_qs.filter(status="completed").count() / survey_qs.count()) * 100, 2)
            if survey_qs.count()
            else 0,
            "downstream_purchase_rate": round((downstream_purchases / len(completed_sessions)) * 100, 2)
            if completed_sessions
            else 0,
        },
        "question_answers": [
            {
                "question_id": row["metadata__question_id"] or "—",
                "count": row["total"],
            }
            for row in answer_rows
        ],
        "question_dropoff": [
            {
                "question_id": row["current_question_id"] or "unknown",
                "count": row["total"],
            }
            for row in dropoff_rows
        ],
    }


def build_survey_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "survey",
        {"filters": filters.cache_key()},
        lambda: _widget_result(
            source="internal",
            data=_survey_data(filters),
            tracked_from=_tracked_from("survey_start", "survey_answer"),
        ),
    )


def _ux_health_data(filters: AnalyticsFilters) -> dict[str, Any]:
    days = 1
    if filters.start_at and filters.end_at:
        days = max(1, min((filters.end_at.date() - filters.start_at.date()).days + 1, 3))

    tracking_freshness = [
        {
            "label": "Остання сесія",
            "timestamp": (
                SiteSession.objects.order_by("-last_seen").values_list("last_seen", flat=True).first()
            ),
        },
        {
            "label": "Останній pageview",
            "timestamp": (
                PageView.objects.order_by("-when").values_list("when", flat=True).first()
            ),
        },
        {
            "label": "Останній product_view",
            "timestamp": (
                UserAction.objects.filter(action_type="product_view")
                .order_by("-timestamp")
                .values_list("timestamp", flat=True)
                .first()
            ),
        },
        {
            "label": "Останній search",
            "timestamp": (
                UserAction.objects.filter(action_type="search")
                .order_by("-timestamp")
                .values_list("timestamp", flat=True)
                .first()
            ),
        },
        {
            "label": "Останній checkout start",
            "timestamp": (
                UserAction.objects.filter(action_type="initiate_checkout")
                .order_by("-timestamp")
                .values_list("timestamp", flat=True)
                .first()
            ),
        },
    ]

    clarity_overview = None
    clarity_urls = None
    clarity_error = None
    try:
        clarity_overview = fetch_clarity_overview(num_of_days=days)
        clarity_urls = fetch_clarity_problem_urls(num_of_days=days)
    except Exception as exc:
        clarity_error = str(exc)

    ip_total = SiteSession.objects.count()
    ip_with_value = SiteSession.objects.exclude(ip_address__isnull=True).count()
    visitor_total = SiteSession.objects.count()
    visitor_with_cookie = SiteSession.objects.exclude(visitor_id__isnull=True).exclude(visitor_id="").count()

    warnings = []
    if ip_total and (ip_with_value / ip_total) < 0.75:
        warnings.append("IP capture поки що неповний: unique-IP метрики треба перевірити на production.")
    if visitor_total and (visitor_with_cookie / visitor_total) < 0.75:
        warnings.append("Cookie stitching `twc_vid` ще не покриває більшість сесій.")
    if clarity_error:
        warnings.append("Clarity live insights недоступний, показуємо лише внутрішні метрики.")

    return {
        "tracking_freshness": [
            {
                "label": item["label"],
                "timestamp": item["timestamp"].isoformat() if item["timestamp"] else "",
            }
            for item in tracking_freshness
        ],
        "capture_health": {
            "ip_capture_ratio": round((ip_with_value / ip_total) * 100, 2) if ip_total else 0,
            "visitor_cookie_ratio": round((visitor_with_cookie / visitor_total) * 100, 2) if visitor_total else 0,
        },
        "clarity_overview": clarity_overview,
        "problem_urls": clarity_urls or [],
        "warnings": warnings,
    }


def build_ux_health_widget(filters: AnalyticsFilters) -> dict[str, Any]:
    return _cache_get_or_set(
        "ux_health",
        {"filters": filters.cache_key()},
        lambda: _widget_result(source="internal+clarity", data=_ux_health_data(filters)),
    )


def _integration_status_data() -> dict[str, Any]:
    last_session = SiteSession.objects.order_by("-last_seen").values_list("last_seen", flat=True).first()
    last_action = UserAction.objects.order_by("-timestamp").values_list("timestamp", flat=True).first()
    total_sessions = SiteSession.objects.count()
    ip_sessions = SiteSession.objects.exclude(ip_address__isnull=True).count()
    visitor_sessions = SiteSession.objects.exclude(visitor_id__isnull=True).exclude(visitor_id="").count()

    internal_status = {
        "key": "internal",
        "label": "Внутрішня БД",
        "status": "healthy" if total_sessions else "warning",
        "message": "Внутрішня аналітика є primary source для продажів, кошика, товарів і custom print.",
        "details": {
            "total_sessions": total_sessions,
            "last_session_at": last_session.isoformat() if last_session else "",
            "last_action_at": last_action.isoformat() if last_action else "",
            "ip_capture_ratio": round((ip_sessions / total_sessions) * 100, 2) if total_sessions else 0,
            "visitor_cookie_ratio": round((visitor_sessions / total_sessions) * 100, 2) if total_sessions else 0,
        },
    }
    ga4_status = get_ga4_status(test_connection=True)
    clarity_status = get_clarity_status(test_connection=True)

    warnings = []
    if internal_status["details"]["ip_capture_ratio"] < 75:
        warnings.append(
            "IP capture нижче 75%: перед використанням unique-IP KPI перевірити production reverse proxy."
        )
    if ga4_status["status"] != "healthy":
        # Surface the actual connector message (file missing / quota / etc.)
        # instead of a generic "недоступний" warning that hides the real cause.
        warnings.append(f"GA4: {ga4_status.get('message', 'недоступний')}")
    if clarity_status["status"] != "healthy":
        warnings.append(f"Clarity: {clarity_status.get('message', 'недоступний')}")

    return {
        "integrations": [internal_status, ga4_status, clarity_status],
        "warnings": warnings,
    }


def build_integration_status_widget(filters: AnalyticsFilters | None = None) -> dict[str, Any]:
    return _widget_result(source="internal", data=_integration_status_data())


def build_widget_by_name(name: str, filters: AnalyticsFilters) -> dict[str, Any]:
    mapping = {
        "overview": build_overview_widget,
        "timeseries": build_timeseries_widget,
        "acquisition": build_acquisition_widget,
        "sales": build_sales_widget,
        "cart": build_cart_widget,
        "products": build_products_widget,
        "custom-print": build_custom_print_widget,
        "survey": build_survey_widget,
        "ux-health": build_ux_health_widget,
        "integration-status": build_integration_status_widget,
    }
    builder = mapping.get(name)
    if builder is None:
        return _widget_result(source="internal", error_state=f"Unknown widget: {name}")
    try:
        return builder(filters) if name != "integration-status" else builder(filters)
    except Exception as exc:
        logger.error("Admin analytics widget %s failed: %s", name, exc, exc_info=True)
        return _widget_result(source="internal", error_state=str(exc))


def _top_distinct_choices(field_name: str, limit: int = 15) -> list[dict[str, Any]]:
    rows = (
        UTMSession.objects.exclude(**{f"{field_name}__isnull": True})
        .exclude(**{field_name: ""})
        .values(field_name)
        .annotate(total=Count("id"))
        .order_by("-total")[:limit]
    )
    return [{"value": row[field_name], "label": row[field_name], "count": row["total"]} for row in rows]


def build_admin_analytics_context(request) -> dict[str, Any]:
    filters = parse_analytics_filters(request.GET)
    overview = build_overview_widget(filters)
    timeseries = build_timeseries_widget(filters)
    integration_status = build_integration_status_widget(filters)
    product_options = [
        {"value": product.id, "label": product.title}
        for product in Product.objects.filter(status="published").order_by("title")[:100]
    ]
    config = {
        "apiBase": "/api/admin/analytics",
        "filters": filters.as_query_params(),
        "initialTab": (request.GET.get("analytics_tab") or "overview").strip() or "overview",
        "initialData": {
            "overview": overview,
            "timeseries": timeseries,
            "integrationStatus": integration_status,
        },
        "legacyDispatcherUrl": f"?section=dispatcher&period={filters.period}",
    }
    exclusions = list(
        AnalyticsExclusion.objects.order_by("-is_active", "kind", "value").values(
            "id", "kind", "value", "note", "is_active", "updated_at"
        )
    )
    exclusion_kinds = [
        {"value": choice.value, "label": choice.label}
        for choice in AnalyticsExclusion.Kind
    ]
    return {
        "analytics_dashboard": {
            "filters": filters,
            "overview": overview,
            "timeseries": timeseries,
            "integration_status": integration_status,
            "period_options": PERIOD_CHOICES,
            "compare_options": COMPARE_CHOICES,
            "source_class_options": SOURCE_CLASS_CHOICES,
            "device_options": DEVICE_CHOICES,
            "utm_source_options": _top_distinct_choices("utm_source"),
            "utm_medium_options": _top_distinct_choices("utm_medium"),
            "campaign_options": _top_distinct_choices("utm_campaign"),
            "product_options": product_options,
            "config": config,
            "legacy_dispatcher_url": config["legacyDispatcherUrl"],
            "exclusions": exclusions,
            "exclusion_kinds": exclusion_kinds,
            "exclusions_count": len(exclusions),
            "exclusions_active": sum(1 for e in exclusions if e["is_active"]),
        }
    }
