"""Медиа сообщения строится из транскрипта, а не из телеметрии скоринга.

Найдено заказчиком в живой работе: к сообщению «Дякую» приклеились два битых
изображения «Зображення товару», которых там быть не может.

Диагноз на данных прода (клиент #59):

- `InstagramBotMessage#2398` («Дякую») имеет `attachments=""` — вложений нет;
- при этом в `IgClient.sales_context["_media_evidence"]` **две** записи с
  `source_message_id=2398` и одинаковым `asset_id=180587876067577`;
- URL — подписанные ссылки `lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=…
  &signature=…` с коротким TTL, поэтому в браузере остаётся только alt-текст.

Три независимых дефекта в одном симптоме:

1. **Привязка.** `_media_evidence` — производная телеметрия скоринга, а не
   транскрипт. `source_message_id` берётся из `message`, переданного в
   `classify_message`, поэтому при переанализе медиа приписывается тому
   сообщению, которое обрабатывалось, а не тому, к которому вложение
   действительно относится.
2. **Дубли.** Дедуп сравнивает URL целиком, а подписанная ссылка на один и тот
   же ассет каждый раз новая: `signature` меняется.
3. **Битые ссылки.** Показывается прямой CDN-URL Meta без локальной копии
   (F-DATA-011: 100% HTTP 404 при скачивании), и менеджер видит сломанную
   картинку вместо понятного «зображення недоступне».

Источником истины о вложениях сообщения является `InstagramBotMessage.attachments` —
иммутабельный транскрипт. `_media_evidence` остаётся источником роли и intent,
но не источником принадлежности.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from management.models import IgClient, InstagramBotMessage

ASSET_A = (
    "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=111111"
    "&signature=AAAAsignature-one"
)
ASSET_A_RESIGNED = (
    "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=111111"
    "&signature=BBBBsignature-two"
)
ASSET_B = (
    "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=222222"
    "&signature=CCCCsignature-three"
)


class MessageMediaPayloadTests(TestCase):
    def setUp(self):
        self.c = IgClient.get_or_create_for_sender("message-media-client")

    def _message(self, text, *, attachments=None, role=None):
        return InstagramBotMessage.objects.create(
            client=self.c,
            role=role or InstagramBotMessage.Role.USER,
            text=text,
            attachments=json.dumps(attachments) if attachments else "",
        )

    def test_message_without_attachments_gets_no_media(self):
        """Дословный кейс заказчика: «Дякую» без вложений."""
        from management.bot_views import _message_media_rows

        with_image = self._message("ось фото", attachments=[ASSET_A])
        plain = self._message("Дякую")
        evidence = [
            {
                "url": ASSET_A,
                "role": "product",
                "intent": "interest",
                # Телеметрия приписала медиа последнему обработанному сообщению.
                "source_message_id": plain.pk,
            }
        ]

        self.assertEqual(_message_media_rows(plain, evidence), [])
        self.assertEqual(len(_message_media_rows(with_image, evidence)), 1)

    def test_role_is_taken_from_the_evidence_by_asset(self):
        from management.bot_views import _message_media_rows

        message = self._message("ось фото", attachments=[ASSET_A])
        evidence = [
            {
                "url": ASSET_A_RESIGNED,
                "role": "product",
                "intent": "purchase_candidate",
                "source_message_id": 999,
            }
        ]

        rows = _message_media_rows(message, evidence)

        self.assertEqual(rows[0]["role"], "product")
        self.assertEqual(rows[0]["intent"], "purchase_candidate")

    def test_unknown_asset_falls_back_to_a_neutral_role(self):
        from management.bot_views import _message_media_rows

        message = self._message("ось фото", attachments=[ASSET_B])

        rows = _message_media_rows(message, [])

        self.assertEqual(rows[0]["role"], "other")
        self.assertEqual(rows[0]["intent"], "unknown")

    def test_the_same_asset_signed_twice_is_shown_once(self):
        from management.bot_views import _message_media_rows

        message = self._message(
            "два рази те саме", attachments=[ASSET_A, ASSET_A_RESIGNED]
        )

        self.assertEqual(len(_message_media_rows(message, [])), 1)

    def test_distinct_assets_are_both_shown(self):
        from management.bot_views import _message_media_rows

        message = self._message("два різні", attachments=[ASSET_A, ASSET_B])

        self.assertEqual(len(_message_media_rows(message, [])), 2)

    def test_short_lived_provider_link_is_flagged(self):
        """Менеджер мусить бачити причину, а не зламану картинку."""
        from management.bot_views import _message_media_rows

        message = self._message("ось фото", attachments=[ASSET_A])

        self.assertTrue(_message_media_rows(message, [])[0]["provider_link"])

    def test_local_copy_is_not_flagged_as_provider_link(self):
        from management.bot_views import _message_media_rows

        message = self._message("ось фото", attachments=["/media/ig/abc.jpg"])

        rows = _message_media_rows(message, [])

        self.assertFalse(rows[0]["provider_link"])

    def test_malformed_attachments_do_not_break_the_payload(self):
        from management.bot_views import _message_media_rows

        message = self._message("битий json")
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            attachments="not a json at all"
        )
        message.refresh_from_db()

        self.assertEqual(_message_media_rows(message, []), [])

    def test_insecure_url_is_dropped(self):
        from management.bot_views import _message_media_rows

        message = self._message(
            "http", attachments=["http://insecure.example/asset.jpg"]
        )

        self.assertEqual(_message_media_rows(message, []), [])

    def test_media_count_is_bounded(self):
        from management.bot_views import _message_media_rows

        many = [
            f"https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id={index}"
            for index in range(30)
        ]
        message = self._message("багато", attachments=many)

        self.assertLessEqual(len(_message_media_rows(message, [])), 8)


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class MessageMediaApiTests(TestCase):
    def setUp(self):
        from django.test import Client as DjangoClient

        self.manager = get_user_model().objects.create_user(
            "message-media-manager", password="x", is_staff=True, is_superuser=True
        )
        self.http = DjangoClient()
        self.http.force_login(self.manager)
        self.c = IgClient.get_or_create_for_sender("message-media-api-client")

    def test_detail_api_does_not_attach_media_to_a_plain_message(self):
        with_image = InstagramBotMessage.objects.create(
            client=self.c,
            role=InstagramBotMessage.Role.USER,
            text="ось фото",
            attachments=json.dumps([ASSET_A]),
        )
        plain = InstagramBotMessage.objects.create(
            client=self.c,
            role=InstagramBotMessage.Role.USER,
            text="Дякую",
        )
        self.c.sales_context = {
            "_media_evidence": [
                {
                    "url": ASSET_A,
                    "role": "product",
                    "intent": "interest",
                    "source_message_id": plain.pk,
                },
                {
                    "url": ASSET_A_RESIGNED,
                    "role": "product",
                    "intent": "interest",
                    "source_message_id": plain.pk,
                },
            ]
        }
        self.c.save(update_fields=["sales_context", "updated_at"])

        data = self.http.get(
            reverse("management_bot_client_detail_api", args=[self.c.pk])
        ).json()
        rows = {row["id"]: row for row in data["messages"]}

        self.assertEqual(rows[plain.pk]["media"], [])
        self.assertEqual(len(rows[with_image.pk]["media"]), 1)
        self.assertEqual(rows[with_image.pk]["media"][0]["role"], "product")


class MessageMediaTemplateTests(TestCase):
    def _template(self):
        from pathlib import Path

        from django.conf import settings

        return (
            Path(settings.BASE_DIR)
            / "management"
            / "templates"
            / "management"
            / "bot.html"
        ).read_text(encoding="utf-8")

    def test_broken_image_is_replaced_by_an_explanation(self):
        template = self._template()

        self.assertTrue(
            "bot-media-unavailable" in template,
            "зламане зображення мусить перетворюватися на зрозумілу плашку",
        )

    def test_image_has_an_error_handler(self):
        self.assertIn("addEventListener('error'", self._template())

    def test_private_preview_uses_the_thumbnail_rendering_contract(self):
        template = self._template()
        from pathlib import Path
        from django.conf import settings

        scroll_script = (
            Path(settings.BASE_DIR)
            / "management"
            / "static"
            / "management"
            / "bot_conversation_scroll.js"
        ).read_text(encoding="utf-8")

        self.assertIn("safeHttpUrl(item.public_url)", template)
        self.assertIn("item.media_label", template)
        self.assertIn("bot_conversation_scroll.js", template)
        self.assertIn("ResizeObserver", scroll_script)
        self.assertIn("image.decode", scroll_script)

    def test_unavailable_placeholder_has_a_style(self):
        self.assertTrue(".bot-media-unavailable{" in self._template())


class MediaEvidenceDeduplicationTests(TestCase):
    """Дедуп телеметрии должен работать по ассету, а не по подписанному URL."""

    def test_resigned_url_of_the_same_asset_is_not_appended_twice(self):
        from management.services.bot_sales_classifier import classify_message

        client = IgClient.get_or_create_for_sender("media-dedup-client")
        message = InstagramBotMessage.objects.create(
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="ось фото",
            attachments=json.dumps([ASSET_A]),
        )

        classify_message(
            client,
            message=message,
            media_context=[{"url": ASSET_A, "role": "product", "intent": "interest"}],
        )
        classify_message(
            client,
            message=message,
            media_context=[
                {"url": ASSET_A_RESIGNED, "role": "product", "intent": "interest"}
            ],
        )

        client.refresh_from_db()
        rows = (client.sales_context or {}).get("_media_evidence", [])
        self.assertEqual(len(rows), 1)
