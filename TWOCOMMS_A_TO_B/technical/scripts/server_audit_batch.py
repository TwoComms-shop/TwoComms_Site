"""Batch read-only audit for CRO-051 + AEO-001 + DB-001/003/004/006.

Run on the production server via:
    python manage.py shell < server_audit_batch.py

Output: JSON between ===AUDIT_JSON_START=== / ===AUDIT_JSON_END=== markers.
All queries are READ-ONLY (SELECT / SHOW only).
"""
import json
import re
from datetime import timedelta

from django.db import connection
from django.db.models import Count
from django.utils import timezone

from storefront.models import SiteSession, UTMSession, UserAction

OUT = {}

FUNNEL = ["page_view", "product_view", "add_to_cart", "initiate_checkout", "lead", "purchase"]

# ---------- 1. CRO-051: totals by action_type ----------
totals = dict(
    UserAction.objects.values_list("action_type").annotate(c=Count("id")).order_by()
)
OUT["action_totals"] = totals
OUT["funnel_raw"] = {a: totals.get(a, 0) for a in FUNNEL}

# ---------- 2. product_view characteristics ----------
pv = UserAction.objects.filter(action_type="product_view")
OUT["product_view"] = {
    "total": pv.count(),
    "no_site_session": pv.filter(site_session__isnull=True).count(),
    "no_utm_session": pv.filter(utm_session__isnull=True).count(),
    "no_user": pv.filter(user__isnull=True).count(),
    "distinct_session_product_pairs": pv.exclude(site_session__isnull=True)
        .values("site_session_id", "product_id").distinct().count(),
    "distinct_products": pv.values("product_id").distinct().count(),
}

# ---------- 3. bot estimation on sessions linked to product_view ----------
BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python-requests|httpx|scrapy|headless|"
    r"phantom|lighthouse|pagespeed|gtmetrix|pingdom|uptime|monitor|ahrefs|semrush|"
    r"mj12|dotbot|petalbot|bytespider|gptbot|claudebot|ccbot|amazonbot|facebookexternalhit",
    re.I,
)
sess_ids = list(pv.exclude(site_session__isnull=True).values_list("site_session_id", flat=True).distinct())
bot_sessions = 0
empty_ua = 0
checked = 0
bot_ua_samples = {}
for chunk_start in range(0, len(sess_ids), 500):
    chunk = sess_ids[chunk_start:chunk_start + 500]
    for sid, ua in SiteSession.objects.filter(id__in=chunk).values_list("id", "user_agent"):
        checked += 1
        if not ua:
            empty_ua += 1
        elif BOT_RE.search(ua):
            bot_sessions += 1
            key = ua[:80]
            bot_ua_samples[key] = bot_ua_samples.get(key, 0) + 1
OUT["bot_estimate"] = {
    "pv_sessions_checked": checked,
    "bot_ua_sessions": bot_sessions,
    "empty_ua_sessions": empty_ua,
    "is_bot_flag_true_total": SiteSession.objects.filter(is_bot=True).count(),
    "site_sessions_total": SiteSession.objects.count(),
    "top_bot_ua": sorted(bot_ua_samples.items(), key=lambda x: -x[1])[:15],
}

# product_view volume coming from bot-UA sessions
bot_ids = set()
for chunk_start in range(0, len(sess_ids), 500):
    chunk = sess_ids[chunk_start:chunk_start + 500]
    for sid, ua in SiteSession.objects.filter(id__in=chunk).values_list("id", "user_agent"):
        if ua and BOT_RE.search(ua):
            bot_ids.add(sid)
pv_from_bots = pv.filter(site_session_id__in=list(bot_ids)).count() if bot_ids else 0
OUT["bot_estimate"]["product_view_from_bot_ua_sessions"] = pv_from_bots

# ---------- 4. monthly funnel (8 x 30-day windows) ----------
now = timezone.now()
monthly = []
for i in range(8):
    end = now - timedelta(days=30 * i)
    start = end - timedelta(days=30)
    window = UserAction.objects.filter(timestamp__gte=start, timestamp__lt=end)
    counts = dict(window.values_list("action_type").annotate(c=Count("id")).order_by())
    monthly.append({
        "window": f"{start.date()}..{end.date()}",
        **{a: counts.get(a, 0) for a in FUNNEL},
    })
OUT["monthly_funnel"] = monthly

# ---------- 5. funnel by unique sessions ----------
uniq = {}
for a in FUNNEL:
    uniq[a] = (
        UserAction.objects.filter(action_type=a)
        .exclude(site_session__isnull=True)
        .values("site_session_id").distinct().count()
    )
OUT["funnel_unique_sessions"] = uniq

# ---------- 6. top products by views ----------
OUT["top_products_by_views"] = list(
    pv.values("product_id", "product_name").annotate(c=Count("id")).order_by("-c")[:10]
)

# ---------- 7. AEO-001: chatgpt.com landing pages ----------
ai_sources = ["chatgpt.com", "chatgpt", "openai", "perplexity", "perplexity.ai",
              "gemini", "copilot", "bing", "claude.ai"]
aeo = {}
for src in ai_sources:
    qs = UTMSession.objects.filter(utm_source__iexact=src)
    n = qs.count()
    if n:
        aeo[src] = {
            "sessions": n,
            "landing_pages": list(
                qs.values("landing_page").annotate(c=Count("id")).order_by("-c")[:20]
            ),
            "device_split": list(
                qs.values("device_type").annotate(c=Count("id")).order_by("-c")
            ),
            "converted": qs.filter(is_converted=True).count(),
            "first_seen_min": str(qs.order_by("first_seen").values_list("first_seen", flat=True).first()),
            "last_seen_max": str(qs.order_by("-last_seen").values_list("last_seen", flat=True).first()),
        }
OUT["aeo_ai_traffic"] = aeo
# also referrer-based AI detection (utm_source may be absent)
ref_ai = (
    UTMSession.objects.filter(referrer__icontains="chatgpt").count(),
    UTMSession.objects.filter(referrer__icontains="perplexity").count(),
    UTMSession.objects.filter(referrer__icontains="gemini.google").count(),
)
OUT["aeo_referrer_counts"] = {"chatgpt": ref_ai[0], "perplexity": ref_ai[1], "gemini": ref_ai[2]}

# ---------- 8. DB-003: Order <-> UTMSession integrity ----------
from orders.models import Order
orders_total = Order.objects.count()
db3 = {"orders_total": orders_total}
order_fields = {f.name for f in Order._meta.get_fields()}
if "utm_session" in order_fields:
    db3["orders_with_utm_session"] = Order.objects.filter(utm_session__isnull=False).count()
if "utm_source" in order_fields:
    db3["orders_with_utm_source"] = Order.objects.exclude(utm_source__isnull=True).exclude(utm_source="").count()
# orphaned UserAction.order_id
order_ids = set(Order.objects.values_list("id", flat=True))
ua_order_ids = set(
    UserAction.objects.exclude(order_id__isnull=True).values_list("order_id", flat=True).distinct()
)
db3["useraction_distinct_order_ids"] = len(ua_order_ids)
db3["useraction_orphan_order_ids"] = sorted(ua_order_ids - order_ids)[:50]
db3["utm_sessions_converted"] = UTMSession.objects.filter(is_converted=True).count()
OUT["db003_integrity"] = db3

# ---------- 9. DB-004: UserAction table size ----------
with connection.cursor() as cur:
    cur.execute("SHOW TABLE STATUS LIKE 'storefront_useraction'")
    row = cur.fetchone()
    cols = [c[0] for c in cur.description]
    st = dict(zip(cols, row)) if row else {}
OUT["db004_useraction_table"] = {
    "rows_estimate": st.get("Rows"),
    "data_length_mb": round((st.get("Data_length") or 0) / 1048576, 2),
    "index_length_mb": round((st.get("Index_length") or 0) / 1048576, 2),
    "engine": st.get("Engine"),
    "collation": st.get("Collation"),
}
oldest = UserAction.objects.order_by("timestamp").values_list("timestamp", flat=True).first()
newest = UserAction.objects.order_by("-timestamp").values_list("timestamp", flat=True).first()
OUT["db004_useraction_table"]["oldest"] = str(oldest)
OUT["db004_useraction_table"]["newest"] = str(newest)
cutoff = now - timedelta(days=365)
OUT["db004_useraction_table"]["rows_older_than_12m"] = UserAction.objects.filter(timestamp__lt=cutoff).count()
cutoff6 = now - timedelta(days=182)
OUT["db004_useraction_table"]["rows_older_than_6m"] = UserAction.objects.filter(timestamp__lt=cutoff6).count()

# top-15 biggest tables overall
with connection.cursor() as cur:
    cur.execute(
        "SELECT table_name, table_rows, ROUND((data_length+index_length)/1048576,2) AS mb "
        "FROM information_schema.tables WHERE table_schema = DATABASE() "
        "ORDER BY (data_length+index_length) DESC LIMIT 15"
    )
    OUT["db004_biggest_tables"] = [list(r) for r in cur.fetchall()]

# ---------- 10. DB-006: charset/collation ----------
with connection.cursor() as cur:
    cur.execute(
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
        "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()"
    )
    OUT["db006_database_charset"] = list(cur.fetchone())
    cur.execute(
        "SELECT CCSA.character_set_name, T.table_collation, COUNT(*) "
        "FROM information_schema.tables T "
        "JOIN information_schema.collation_character_set_applicability CCSA "
        "  ON CCSA.collation_name = T.table_collation "
        "WHERE T.table_schema = DATABASE() GROUP BY 1, 2 ORDER BY 3 DESC"
    )
    OUT["db006_table_charsets"] = [list(r) for r in cur.fetchall()]
    # columns NOT utf8mb4 in text-bearing tables
    cur.execute(
        "SELECT table_name, column_name, character_set_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND character_set_name IS NOT NULL "
        "AND character_set_name <> 'utf8mb4' LIMIT 40"
    )
    OUT["db006_non_utf8mb4_columns"] = [list(r) for r in cur.fetchall()]

# ---------- 11. DB-001: slow query log status ----------
with connection.cursor() as cur:
    try:
        cur.execute("SHOW VARIABLES WHERE Variable_name IN "
                    "('slow_query_log','slow_query_log_file','long_query_time','log_queries_not_using_indexes')")
        OUT["db001_slow_log_vars"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001 - audit script, report and continue
        OUT["db001_slow_log_vars"] = {"error": str(e)}
    try:
        cur.execute("SHOW GLOBAL STATUS LIKE 'Slow_queries'")
        r = cur.fetchone()
        OUT["db001_slow_queries_counter"] = r[1] if r else None
    except Exception as e:  # noqa: BLE001
        OUT["db001_slow_queries_counter"] = {"error": str(e)}

# ---------- 12. leftover CRO-050: audit utm_source cleanup check ----------
audit_utm = UTMSession.objects.filter(utm_source="audit")
OUT["cro050_audit_utm_sessions"] = {
    "count": audit_utm.count(),
    "action_count": UserAction.objects.filter(utm_session__in=audit_utm).count(),
}

print("===AUDIT_JSON_START===")
print(json.dumps(OUT, ensure_ascii=False, default=str))
print("===AUDIT_JSON_END===")
