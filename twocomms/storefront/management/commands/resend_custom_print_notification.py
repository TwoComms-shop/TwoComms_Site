"""
Повторна відправка Telegram-сповіщення про кастомну заявку всім адмінам.

Використання:
    # Останній створений лід
    python manage.py resend_custom_print_notification --latest

    # Конкретна заявка по lead_number
    python manage.py resend_custom_print_notification --lead CP14052026L007

    # По внутрішньому ID
    python manage.py resend_custom_print_notification --id 42

    # Тип сповіщення (за замовчуванням moderation_request — найповніший формат)
    python manage.py resend_custom_print_notification --latest --kind new_lead

Команда починає нову спробу вибраного типу, зберігаючи історію результатів,
і надсилає спільну заявку з усіма її виробами всім налаштованим отримувачам.
"""

from django.core.management.base import BaseCommand, CommandError

from storefront.models import CustomPrintLead


class Command(BaseCommand):
    help = "Повторно надіслати Telegram-сповіщення про кастомну заявку всім адмінам."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--latest",
            action="store_true",
            help="Надіслати останню створену заявку.",
        )
        group.add_argument(
            "--lead",
            dest="lead_number",
            help="Номер заявки (наприклад CP14052026L007).",
        )
        group.add_argument(
            "--id",
            dest="lead_id",
            type=int,
            help="Внутрішній PK заявки.",
        )
        parser.add_argument(
            "--kind",
            choices=["moderation_request", "new_lead"],
            default="moderation_request",
            help=(
                "Тип сповіщення. moderation_request — повне з кнопками "
                "Погодити/Відхилити/Звʼязатись (за замовчуванням). "
                "new_lead — як «нова заявка від менеджеру»."
            ),
        )

    def handle(self, *args, **options):
        # Lazy import — не тягнемо notifier при manage.py help
        from storefront.custom_print_notifications import (
            notify_custom_print_moderation_request,
            notify_new_custom_print_lead,
            notify_custom_print_creation,
            reset_notification_delivery,
        )

        lead = self._resolve_lead(options)
        if lead is None:
            raise CommandError("Заявку не знайдено.")

        meta = (lead.config_draft_json or {}).get("creation") or {}
        group = list(CustomPrintLead.objects.filter(pk__in=meta.get("lead_ids") or [lead.pk]))
        group.sort(key=lambda item: ((item.config_draft_json or {}).get("creation") or {}).get("item_index", 0))
        owner = group[0] if group else lead
        # Explicit operator resend resets this delivery gate, not confirmed timestamps.
        scope = ("creation_lead" if options["kind"] == "new_lead" else "creation_cart") if meta else options["kind"]
        reset_notification_delivery(owner, scope=scope)

        attachments_count = lead.attachments.count()
        self.stdout.write(
            self.style.NOTICE(
                f"Знайдено заявку {lead.lead_number} (id={lead.pk}, файлів: "
                f"{attachments_count}, статус модерації: {lead.moderation_status})."
            )
        )

        kind = options["kind"]
        if meta:
            success = notify_custom_print_creation(group, submission_type="lead" if kind == "new_lead" else "cart")
        elif kind == "new_lead":
            success = notify_new_custom_print_lead(lead)
        else:
            success = notify_custom_print_moderation_request(lead)

        targets = self._describe_targets()

        if success:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Сповіщення надіслано. Тип: {kind}. {targets}"
                )
            )
        else:
            raise CommandError(
                "Telegram повернув помилку. Перевірте DTF_TG_BOT_TOKEN / "
                "DTF_TG_ADMIN_ID / DTF_TG_CHAT_ID у середовищі."
            )

    # ----- helpers -----

    def _resolve_lead(self, options):
        if options.get("latest"):
            return CustomPrintLead.objects.order_by("-created_at").first()
        if options.get("lead_number"):
            return (
                CustomPrintLead.objects.filter(lead_number=options["lead_number"])
                .order_by("-created_at")
                .first()
            )
        if options.get("lead_id"):
            return CustomPrintLead.objects.filter(pk=options["lead_id"]).first()
        return None

    def _describe_targets(self) -> str:
        from storefront.custom_print_notifications import _build_notifier

        notifier = _build_notifier()
        admins = list(getattr(notifier, "admin_ids", []) or [])
        chats = list(getattr(notifier, "chat_ids", []) or [])
        actual = (
            notifier._resolve_targets(admin=True)
            if hasattr(notifier, "_resolve_targets")
            else []
        )
        return (
            f"Отримувачі: {len(actual)}."
        )
