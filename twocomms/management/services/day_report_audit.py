"""
Комплексний ШІ-аудит робочого дня менеджера (при відправці звіту).

ШІ отримує повний контекст дня:
  - кожен оброблений клієнт (що менеджер ввів у CRM),
  - кожен дзвінок цього дня з готовим ШІ-розбором (вердикт, бал, резюме,
    розбіжності, витягнуті факти),
  - дисципліна передзвонів (чи виконані домовлені дзвінки),
  - стаж менеджера, кількість і історія звітів, тренд MOSAIC.

На цьому фоні робить точний аудит: чи сходиться введене з розмовами, що
пропущено, чи прогресує менеджер, перспективи, конкретні недоробки. Результат
іде адміну (Telegram + адмін-UI). Менеджеру бали/аудит не показуємо.

Текстовий запит (агрегує вже проаналізовані дзвінки) — швидко й дешево.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from management.lead_services import client_points_value
from management.models import (
    CallAIAnalysis,
    CallRecord,
    Client,
    ClientFollowUp,
    DayReportAudit,
    Report,
)

logger = logging.getLogger("binotel")

CONVERSION_RESULTS = {Client.CallResult.ORDER, Client.CallResult.TEST_BATCH}


def _system_instruction() -> str:
    return (
        "Ти — досвідчений керівник відділу продажів бренду TwoComms (мілітарі/стрітстайл одяг). "
        "Менеджери роблять переважно ХОЛОДНІ ВИХІДНІ дзвінки у фізичні торгові точки, щоб продати "
        "ОПТову партію одягу та представити бренд. Тобі дають повний контекст робочого дня менеджера: "
        "оброблені клієнти (що він зафіксував у CRM), розбір кожного дзвінка (вердикт, бал, розбіжності, "
        "домовленості), дисципліну передзвонів, стаж та історію звітів, тренд рейтингу.\n\n"
        "Зроби ЧЕСНИЙ, конкретний аудит дня для АДМІНА (не для менеджера):\n"
        "- чи сходиться те, що менеджер ввів у CRM, з тим, що реально було в розмовах;\n"
        "- чи виконані домовлені передзвони, чи нічого не пропущено;\n"
        "- чи прогресує менеджер (порівняй з історією/трендом), чи є перспектива;\n"
        "- конкретні недоробки (не загальні слова, а що саме і де);\n"
        "- практичні рекомендації, що покращити.\n\n"
        "Памʼятай специфіку: дотискати клієнта до останнього; 'неконверсійний' доречний лише за "
        "реальною твердою відмовою. Якщо аналізів дзвінків мало — зважай на це й не роби надмірних висновків.\n\n"
        "Відповідай СУВОРО валідним JSON (без markdown), українською, за схемою:\n"
        "{\n"
        '  "summary": "стислий підсумок дня (3-6 речень)",\n'
        '  "day_score": <number 0..100 — загальна якість дня>,\n'
        '  "verdict": "strong|ok|weak",\n'
        '  "matches": ["що сходиться між CRM і розмовами"],\n'
        '  "mismatches": ["конкретні розбіжності CRM vs розмови"],\n'
        '  "missed_callbacks": ["пропущені/прострочені домовлені передзвони"],\n'
        '  "growth": {"tone": "good|neutral|bad", "label": "коротко", "text": "оцінка прогресу і динаміки"},\n'
        '  "weaknesses": ["конкретні недоробки"],\n'
        '  "recommendations": ["що покращити завтра"],\n'
        '  "prospects": "перспективи менеджера для адміна (1-3 речення)"\n'
        "}"
    )


def _day_bounds(day: date) -> tuple:
    """[day 00:00, day+1 00:00) як aware datetimes у локальній TZ.
    Не використовуємо __date (на проді MySQL без tz-таблиць CONVERT_TZ→NULL)."""
    tz = timezone.get_current_timezone()
    from datetime import datetime as _dt
    start_dt = timezone.make_aware(_dt.combine(day, _dt.min.time()), tz)
    end_dt = timezone.make_aware(_dt.combine(day + timedelta(days=1), _dt.min.time()), tz)
    return start_dt, end_dt


def _gather_context(manager, day: date) -> dict:
    start_dt, end_dt = _day_bounds(day)
    # Клієнти дня
    clients = list(
        Client.objects.filter(owner=manager, created_at__gte=start_dt, created_at__lt=end_dt)
        .order_by("-created_at")[:200]
    )
    client_rows = []
    conversions = 0
    points = 0
    for c in clients:
        points += client_points_value(c)
        if c.call_result in CONVERSION_RESULTS:
            conversions += 1
        ctx = c.call_result_context or {}
        client_rows.append({
            "shop": c.shop_name,
            "result": c.get_call_result_display(),
            "manager_note": (c.manager_note or "")[:400],
            "reason": (c.call_result_reason_note or "")[:300],
            "details": (c.call_result_details or "")[:300],
            "next_call": timezone.localtime(c.next_call_at).strftime("%Y-%m-%d %H:%M") if c.next_call_at else None,
            "xml": bool(ctx.get("xml_platform") or ctx.get("xml_resource_url")),
        })

    # Дзвінки дня + ШІ-розбір
    records = list(
        CallRecord.objects.filter(manager=manager, created_at__gte=start_dt, created_at__lt=end_dt)
        .select_related("matched_client")
        .prefetch_related("ai_analyses")
        .order_by("-started_at", "-created_at")[:200]
    )
    call_rows = []
    analyzed = 0
    for r in records:
        analysis = None
        for an in r.ai_analyses.all():
            if an.status == CallAIAnalysis.Status.DONE:
                analysis = an
                break
        row = {
            "client": r.matched_client.shop_name if r.matched_client_id else (r.phone or "—"),
            "duration_sec": r.duration_seconds,
        }
        if analysis:
            analyzed += 1
            row.update({
                "verdict": analysis.verdict,
                "score": float(analysis.overall_score),
                "summary": (analysis.summary or "")[:500],
                "discrepancies": analysis.discrepancies or [],
                "facts": analysis.extracted_facts or {},
            })
        else:
            row["analysis"] = "немає (не значущий або ще в черзі)"
        call_rows.append(row)

    # Дисципліна передзвонів (домовлені раніше, дедлайн до сьогодні включно)
    horizon = day - timedelta(days=14)
    fus = list(
        ClientFollowUp.objects.filter(
            owner=manager, due_date__lte=day, due_date__gte=horizon
        ).select_related("client").order_by("-due_at")[:60]
    )
    fu_done = sum(1 for f in fus if f.status == ClientFollowUp.Status.DONE)
    fu_missed = [
        {"shop": (f.client.shop_name if f.client_id else "—"),
         "due": timezone.localtime(f.due_at).strftime("%Y-%m-%d %H:%M") if f.due_at else ""}
        for f in fus if f.status in (ClientFollowUp.Status.OPEN, ClientFollowUp.Status.MISSED)
    ][:20]

    # Мета менеджера
    profile = getattr(manager, "userprofile", None)
    started = getattr(profile, "manager_started_at", None)
    if not started:
        try:
            started = timezone.localtime(manager.date_joined).date() if manager.date_joined else None
        except Exception:
            started = None
    tenure_days = max(0, (day - started).days) if started else 0
    reports = list(Report.objects.filter(owner=manager).order_by("-created_at")[:6])
    reports_count = Report.objects.filter(owner=manager).count()
    report_history = [
        {"date": timezone.localtime(rp.created_at).strftime("%Y-%m-%d"), "points": rp.points, "processed": rp.processed}
        for rp in reports
    ]
    try:
        from management.services.manager_review import build_mosaic_trend
        mosaic = build_mosaic_trend(manager, days=10)
    except Exception:
        mosaic = {"series": [], "latest": None, "direction": {}}

    return {
        "context": {
            "day": day.isoformat(),
            "manager": manager.get_full_name() or manager.username,
            "tenure_days": tenure_days,
            "reports_count": reports_count,
            "report_history": report_history,
            "mosaic_trend": mosaic,
            "day_totals": {"processed": len(clients), "points": points, "conversions": conversions},
            "callbacks": {"due_considered": len(fus), "done": fu_done, "missed_or_open": fu_missed},
            "clients": client_rows,
            "calls": call_rows,
        },
        "tenure_days": tenure_days,
        "reports_count": reports_count,
        "calls_total": len(records),
        "calls_analyzed": analyzed,
    }


def _to_score(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")
    return max(Decimal("0"), min(Decimal("100"), d)).quantize(Decimal("0.01"))


def _norm_verdict(v) -> str:
    v = (str(v or "")).strip().lower()
    return v if v in {"strong", "ok", "weak"} else DayReportAudit.Verdict.UNKNOWN


def _as_list(v) -> list:
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def build_day_report_audit(report: Report) -> DayReportAudit:
    """Будує ШІ-аудит дня для звіту. Безпечний: при будь-якій помилці лишає
    запис зі статусом error/skipped, не кидає виняток у потік відправки звіту."""
    manager = report.owner
    day = timezone.localtime(report.created_at).date()
    ctx = _gather_context(manager, day)
    audit = DayReportAudit.objects.create(
        report=report, owner=manager, day=day,
        status=DayReportAudit.Status.RUNNING,
        tenure_days=ctx["tenure_days"], reports_count=ctx["reports_count"],
        calls_total=ctx["calls_total"], calls_analyzed=ctx["calls_analyzed"],
    )
    started = time.monotonic()
    try:
        from management.services.call_ai_analysis import gemini_generate_json, _resolve_gemini_key
        if not _resolve_gemini_key():
            audit.status = DayReportAudit.Status.SKIPPED
            audit.error = "Немає ключа Gemini."
            audit.save()
            return audit
        user_text = json.dumps(ctx["context"], ensure_ascii=False)
        out = gemini_generate_json(
            _system_instruction(),
            user_text,
            max_output_tokens=4096,
            reasoning_task="reporting_summary",
        )
        parsed = out["parsed"]
        usage = out.get("usage") or {}
        audit.status = DayReportAudit.Status.DONE
        audit.model = out.get("model") or ""
        audit.summary = str(parsed.get("summary") or "")
        audit.day_score = _to_score(parsed.get("day_score"))
        audit.verdict = _norm_verdict(parsed.get("verdict"))
        audit.matches = _as_list(parsed.get("matches"))
        audit.mismatches = _as_list(parsed.get("mismatches"))
        audit.missed_callbacks = _as_list(parsed.get("missed_callbacks"))
        audit.growth = parsed.get("growth") if isinstance(parsed.get("growth"), dict) else {}
        audit.weaknesses = _as_list(parsed.get("weaknesses"))
        audit.recommendations = _as_list(parsed.get("recommendations"))
        audit.prospects = str(parsed.get("prospects") or "")
        audit.result = parsed if isinstance(parsed, dict) else {"_raw": parsed}
        audit.prompt_tokens = int(usage.get("promptTokenCount") or 0)
        audit.output_tokens = int(usage.get("candidatesTokenCount") or usage.get("totalTokenCount") or 0)
        audit.elapsed_ms = int((time.monotonic() - started) * 1000)
        audit.save()
    except Exception as exc:
        audit.status = DayReportAudit.Status.ERROR
        audit.error = str(exc)[:2000]
        audit.elapsed_ms = int((time.monotonic() - started) * 1000)
        audit.save()
        logger.info("day-audit failed for report %s: %s", report.id, exc)
    return audit


def telegram_audit_block(audit: DayReportAudit) -> list[str]:
    """Рядки для Telegram-звіту адміну (короткий ШІ-аудит дня)."""
    if not audit or audit.status != DayReportAudit.Status.DONE:
        return []
    from html import escape
    lines = ["", "🤖 <b>ШІ-аудит дня</b>"]
    if audit.summary:
        lines.append(escape(audit.summary[:600]))
    g = audit.growth or {}
    if g.get("label") or g.get("text"):
        lines.append(f"📈 <b>Динаміка:</b> {escape(str(g.get('label') or ''))} — {escape(str(g.get('text') or ''))}".rstrip(" —"))
    if audit.mismatches:
        lines.append("⚠️ <b>Розбіжності:</b>")
        for m in audit.mismatches[:3]:
            lines.append(f"• {escape(str(m))}")
    if audit.missed_callbacks:
        lines.append("📞 <b>Пропущені домовленості:</b>")
        for m in audit.missed_callbacks[:3]:
            lines.append(f"• {escape(str(m))}")
    if audit.prospects:
        lines.append(f"🎯 <b>Перспектива:</b> {escape(audit.prospects[:300])}")
    lines.append(f"⏱ стаж {audit.tenure_days} дн · звітів {audit.reports_count} · дзвінків {audit.calls_analyzed}/{audit.calls_total}")
    return lines
