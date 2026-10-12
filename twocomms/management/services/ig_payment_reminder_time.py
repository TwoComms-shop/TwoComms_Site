"""Conservative payment-reminder intent and exact Kyiv wall-clock parsing."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


@dataclass(frozen=True)
class RequestedReminderTime:
    handled: bool
    due_at: datetime | None = None
    locale: str = "uk"
    reason: str = ""


def quiet_time_allowed(value):
    local = value.astimezone(KYIV)
    return (10, 0) <= (local.hour, local.minute) <= (21, 30)


def parse_requested_reminder(text, provider_created_at):
    """No defaults, fuzzy periods, calendar guessing or local ingestion clocks."""
    value = str(text or "").strip().lower()
    reminder = re.search(r"\b(?:нагадай(?:те)?|нагадати|нагадайте|напомни(?:те)?|напомнить|remind(?:\s+me)?|reminder)\b", value)
    payment = re.search(r"\b(?:оплат\w*|сплат\w*|платеж\w*|платіж\w*|pay(?:ment)?)\b", value)
    locale = "en" if re.search(r"\b(?:remind|reminder)\b", value) else ("uk" if re.search(r"[іїєґ]|нагада|нагадати|сплат|платіж", value) else "ru")
    if not reminder or not payment:
        return RequestedReminderTime(False, locale=locale, reason="not_payment_reminder")
    if re.search(r"[\"«»“”]|\b(?:якщо|если|if|would|might|quoted|цитат\w*|не|not|don't|ніколи|никогда)\b", value):
        return RequestedReminderTime(False, locale=locale, reason="not_explicit_request")
    # Questions about merchandise, conditions or hypotheticals must reach the
    # ordinary reply path. A request phrased 'can you remind...' is permitted.
    clean = re.sub(r"^(?:can you|could you|можеш|можете|можешь)\s+", "", value)
    clean = re.sub(r"\b(?:please|будь ласка|пожалуйста|мені|мене|мне|me|про|об|о|за|оплат\w*|сплат\w*|платеж\w*|платіж\w*|to|the|pay(?:ment)?|нагадай(?:те)?|нагадати|напомни(?:те)?|напомнить|remind|reminder)\b", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .,!?;:")
    if provider_created_at is None or provider_created_at.tzinfo is None:
        return RequestedReminderTime(True, locale=locale, reason="provider_clock_missing")
    anchor = provider_created_at.astimezone(KYIV)
    relative = re.fullmatch(r"(?:через|in|after)\s+(\d{1,4})\s*(хв(?:илин[ауи]?)?|мин(?:ут[ауиы]?)?|minutes?|mins?|год(?:ин[ауи]?)?|час(?:а|ов)?|hours?|hrs?)", clean)
    due = None
    if relative:
        number = int(relative.group(1))
        unit = relative.group(2)
        seconds = number * (3600 if re.match(r"год|час|hour|hr", unit) else 60)
        due = (provider_created_at.astimezone(timezone.utc) + timedelta(seconds=seconds)).astimezone(KYIV)
    else:
        exact = re.fullmatch(r"(?:(сьогодні|сегодня|today|завтра|tomorrow|\d{4}-\d{2}-\d{2}|\d{2}[./]\d{2}[./]\d{4})\s+)?(?:(?:at|в|о)\s+)?(\d{1,2}):(\d{2})", clean)
        if exact:
            day, hour, minute = exact.groups()
            if day in {None, "сьогодні", "сегодня", "today"}:
                date = anchor.date()
            elif day in {"завтра", "tomorrow"}:
                date = anchor.date() + timedelta(days=1)
            else:
                try:
                    date = datetime.strptime(day, "%Y-%m-%d" if "-" in day else ("%d.%m.%Y" if "." in day else "%d/%m/%Y")).date()
                except ValueError:
                    return RequestedReminderTime(True, locale=locale, reason="invalid_date")
            try:
                due = datetime(date.year, date.month, date.day, int(hour), int(minute), tzinfo=KYIV)
                # A repeated/nonexistent civil time cannot specify an exact
                # instant. Quiet-hour rejection usually excludes DST hours.
                alternate = due.replace(fold=1)
                if alternate.utcoffset() != due.utcoffset():
                    return RequestedReminderTime(True, locale=locale, reason="ambiguous_clock")
            except ValueError:
                return RequestedReminderTime(True, locale=locale, reason="invalid_clock")
        elif clean and not re.fullmatch(r"(?:пізніше|позже|later|завтра|tomorrow|сьогодні|сегодня|today|(?:через|in|after)\s+\w+\s+\w+)", clean):
            return RequestedReminderTime(False, locale=locale, reason="mixed_request")
    if due is None:
        return RequestedReminderTime(True, locale=locale, reason="exact_time_required")
    if due <= provider_created_at:
        return RequestedReminderTime(True, locale=locale, reason="past_time")
    if not quiet_time_allowed(due):
        return RequestedReminderTime(True, locale=locale, reason="quiet_hours")
    return RequestedReminderTime(True, due.astimezone(timezone.utc), locale, "")


def format_kyiv_datetime(value):
    return value.astimezone(KYIV).strftime("%d.%m.%Y %H:%M")
