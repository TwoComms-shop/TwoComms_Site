"""Розсилка фінансових push-звітів за розкладом (cron).

Запускати раз на 5–15 хв через cron. Команда сама вирішує, кому що слати:
  • щоденний звіт — якщо ввімкнено і поточний час співпав із push_daily_time;
  • тижневий звіт — якщо ввімкнено, сьогодні push_weekly_day і час push_weekly_time;
  • зведення по боргах — щодня о 08:00 (лише непогашені);
  • сповіщення про ризики — якщо ввімкнено і є critical alerts (раз на день).

Прив'язка до часу — у вікні ±DELTA хвилин, щоб не залежати від точного cron-тіку.

ДЕДУПЛІКАЦІЯ — за наявністю NotificationLog із відповідним ``dedup_key`` (а НЕ
за success). Це ключове проти спаму: якщо доставка не вдалась (мертва підписка,
таймаут), ми НЕ повторюємо ту саму розсилку щотіку. Один субʼєкт за період —
одна спроба. Користувачів без активних підписок взагалі пропускаємо, щоб не
засмічувати лог і не блокувати майбутні розсилки.

Приклад cron (кожні 10 хв):
    */10 * * * * cd /path/twocomms && python manage.py finance_send_reports
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from finance.models import get_default_company
from finance.models_settings import UserSettings, NotificationLog
from finance.services import push as push_service


WINDOW_MIN = 10  # вікно збігу за часом (хв)
DEBTS_TIME = dt.time(8, 0)  # зведення по боргах — зранку
PLANNED_TIME = dt.time(9, 0)  # нагадування про планові


class Command(BaseCommand):
    help = 'Надсилає фінансові push-звіти за розкладом користувачів'

    def add_arguments(self, parser):
        parser.add_argument('--force-daily', action='store_true',
                            help='Примусово надіслати щоденний звіт усім (тест)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Не слати, лише показати що було б надіслано')

    def handle(self, *args, **opts):
        if not push_service.is_configured():
            self.stdout.write(self.style.WARNING('Web Push не налаштований — вихід'))
            return

        company = get_default_company()
        now = timezone.localtime()
        today = now.date()
        force_daily = opts.get('force_daily')
        dry = opts.get('dry_run')

        sent_total = 0
        for st in UserSettings.objects.filter(push_enabled=True).select_related('user'):
            user = st.user
            # Без активних підписок слати нікому — пропускаємо (без логів).
            if not force_daily and not push_service.has_active_subscription(user):
                continue

            jobs = []  # (notification_type, dedup_key, report)

            # Щоденний звіт.
            if force_daily or (st.push_daily_enabled and self._time_match(now, st.push_daily_time)):
                key = push_service.dedup_daily(today)
                if force_daily or not self._already(user, key):
                    jobs.append(('daily', key, push_service.build_daily_report(company)))

            # Зведення по боргах — щодня о 08:00, рівно раз.
            if st.push_debts_enabled and self._time_match(now, DEBTS_TIME):
                key = push_service.dedup_debts(today)
                if not self._already(user, key):
                    jobs.append(('debts', key, push_service.build_debts_report(company)))

            # Тижневий звіт.
            if (st.push_weekly_enabled
                    and self._weekday_match(today, st.push_weekly_day)
                    and self._time_match(now, st.push_weekly_time)):
                key = push_service.dedup_weekly(today)
                if not self._already(user, key):
                    jobs.append(('weekly', key, push_service.build_weekly_report(company)))

            # Сповіщення про ризики (раз на день максимум).
            if st.push_health_alerts:
                key = push_service.dedup_health(today)
                if not self._already(user, key):
                    jobs.append(('health', key, push_service.build_health_alert_report(company)))

            # Нагадування про планові платежі (раз на день, зранку).
            if st.push_planned_reminders and self._time_match(now, PLANNED_TIME):
                key = push_service.dedup_planned(today)
                if not self._already(user, key):
                    jobs.append(('planned', key, push_service.build_planned_reminder_report(company)))

            # Reviewable ledger alerts: one deduplicated actionable reminder per day.
            key = f'ledger-alert:{today.isoformat()}'
            if st.push_health_alerts and not self._already(user, key):
                jobs.append(('custom', key, push_service.build_ledger_alert_report(company)))

            for ntype, dedup_key, report in jobs:
                if report is None:
                    # Нема контенту (нема боргів/ризиків) — нічого не шлемо й не
                    # логуємо. Дедуп спрацює лише коли реально щось відправили,
                    # тож health-алерт лишається чутливим протягом дня.
                    continue
                if dry:
                    self.stdout.write(f'[dry] {user.username}: {ntype} — {report["title"]}')
                    continue
                res = push_service.send_to_user(
                    user, report['title'], report['body'],
                    url='/', tag=f'finance-{ntype}',
                    notification_type=ntype, report_data=report.get('data'),
                    dedup_key=dedup_key)
                sent_total += res.get('sent', 0)
                if st.telegram_notifications:
                    # Telegram credentials and chat ids stay in the deployment
                    # environment; no secrets are persisted in finance data.
                    try:
                        from management.services.notify import admin_chat_ids, send_message
                        for chat_id in admin_chat_ids():
                            send_message(chat_id, f'{report["title"]}\n\n{report["body"]}')
                    except Exception as exc:  # pragma: no cover - optional adapter
                        self.stderr.write(f'Telegram delivery failed for {user.username}: {exc}')
                self.stdout.write(f'{user.username}: {ntype} → sent={res.get("sent")} '
                                  f'failed={res.get("failed")}')

        self.stdout.write(self.style.SUCCESS(f'Готово. Надіслано push: {sent_total}'))

    def _time_match(self, now, target_time) -> bool:
        if not target_time:
            return False
        target = now.replace(hour=target_time.hour, minute=target_time.minute,
                             second=0, microsecond=0)
        delta = abs((now - target).total_seconds()) / 60.0
        return delta <= WINDOW_MIN

    def _weekday_match(self, today, weekly_day) -> bool:
        # push_weekly_day: 0=Неділя..6=Субота (як у моделі). Python weekday(): 0=Пн..6=Нд.
        py_wd = today.weekday()  # 0=Mon..6=Sun
        model_to_py = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
        return model_to_py.get(weekly_day) == py_wd

    def _already(self, user, dedup_key) -> bool:
        """Чи вже була спроба за цим ключем (незалежно від success) — антиспам."""
        return NotificationLog.objects.filter(user=user, dedup_key=dedup_key).exists()
