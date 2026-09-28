"""Тести Phase 3 / Task 10 — пам'ять діалогу (rolling summary) + ретеншн.

Щоб бот «пам'ятав» клієнта на 6 місяців без перегріву токенів: стара історія
стискається у memory_summary (management-модель), у контекст іде summary +
свіже вікно. purge_stale_clients чистить картки, неактивні понад 180 днів.
"""
import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from management.models import (
    IgAnalysisMaterialityEvent,
    IgClient,
    IgCommercialEpisode,
    IgFunnelResetAudit,
    InstagramBotMessage,
)
from management.services import bot_memory


class MemoryNoteTests(TestCase):
    def test_none_when_empty(self):
        c = IgClient.get_or_create_for_sender("m1")
        self.assertIsNone(bot_memory.memory_note(c))

    def test_text_when_set(self):
        c = IgClient.get_or_create_for_sender("m2")
        c.memory_summary = "хоче худі Kharkiv, розмір M, 950 грн"
        c.memory_updated_at = timezone.now()
        c.save()
        note = bot_memory.memory_note(c)
        self.assertIsNotNone(note)
        self.assertIn("Kharkiv", note)

    def test_summary_older_than_current_episode_is_not_injected(self):
        c = IgClient.get_or_create_for_sender("m2-stale-episode")
        c.memory_summary = "old narrative"
        c.memory_updated_at = timezone.now()
        c.save(update_fields=["memory_summary", "memory_updated_at", "updated_at"])
        episode = IgCommercialEpisode.objects.create(
            client=c,
            sequence=1,
            materialization_key="memory-stale-episode",
        )
        c.current_commercial_episode = episode
        c.save(update_fields=["current_commercial_episode", "updated_at"])

        self.assertIsNone(bot_memory.memory_note(c))

    def test_fresh_summary_inside_current_episode_is_still_available(self):
        c = IgClient.get_or_create_for_sender("m2-fresh-episode")
        episode = IgCommercialEpisode.objects.create(
            client=c,
            sequence=1,
            materialization_key="memory-fresh-episode",
        )
        c.current_commercial_episode = episode
        c.memory_summary = "current narrative"
        c.memory_updated_at = timezone.now()
        c.save(update_fields=[
            "current_commercial_episode", "memory_summary",
            "memory_updated_at", "updated_at",
        ])

        self.assertIn("current narrative", bot_memory.memory_note(c) or "")

    def test_summary_at_or_before_reset_is_not_injected(self):
        c = IgClient.get_or_create_for_sender("m2-stale-reset")
        c.memory_summary = "pre-reset narrative"
        c.memory_updated_at = timezone.now()
        c.save(update_fields=["memory_summary", "memory_updated_at", "updated_at"])
        IgFunnelResetAudit.objects.create(
            client=c,
            reason="test boundary",
            resulting_stage=IgClient.Stage.NEW,
        )

        self.assertIsNone(bot_memory.memory_note(c))


class UpdateMemoryTests(TestCase):
    def test_erasure_blocks_memory_generation(self):
        client = IgClient.objects.create(igsid="erasing-memory", privacy_erasure_started_at=timezone.now())
        with patch("management.services.bot_memory.gemini_generate_text") as generate:
            self.assertFalse(bot_memory.update_client_memory(client))
        generate.assert_not_called()

    def test_erasure_during_generation_blocks_summary_write(self):
        client = IgClient.objects.create(igsid="erasing-memory-late")
        InstagramBotMessage.objects.create(client=client, sender_id=client.igsid, role="user", text="Synthetic preference")

        def erase_during_generation(*args, **kwargs):
            IgClient.objects.filter(pk=client.pk).update(privacy_erasure_started_at=timezone.now())
            return {"parsed": "A summary that must not be stored"}

        with patch("management.services.bot_memory.gemini_generate_text", side_effect=erase_during_generation):
            self.assertFalse(bot_memory.update_client_memory(client))
        client.refresh_from_db()
        self.assertFalse(client.memory_summary)
    @patch("management.services.bot_memory.gemini_generate_text")
    def test_update_sets_summary_and_timestamp(self, mock_gen):
        mock_gen.return_value = {"parsed": "Клієнт хоче худі Kharkiv розмір M за 950 грн."}
        c = IgClient.get_or_create_for_sender("m3")
        InstagramBotMessage.objects.create(sender_id="m3", client=c, role="user", text="скільки худі Kharkiv?")
        self.assertTrue(bot_memory.update_client_memory(c))
        c.refresh_from_db()
        self.assertIn("Kharkiv", c.memory_summary)
        self.assertIsNotNone(c.memory_updated_at)

    @patch("management.services.bot_memory.update_client_memory")
    def test_maybe_update_triggers_on_threshold(self, mock_upd):
        c = IgClient.get_or_create_for_sender("m4")
        for i in range(8):
            InstagramBotMessage.objects.create(sender_id="m4", client=c, role="user", text=f"t{i}")
        bot_memory.maybe_update_memory(c, every=8)
        self.assertEqual(mock_upd.call_count, 1)

    @patch("management.services.bot_memory.update_client_memory")
    def test_maybe_update_skips_below_threshold(self, mock_upd):
        c = IgClient.get_or_create_for_sender("m5")
        InstagramBotMessage.objects.create(sender_id="m5", client=c, role="user", text="hi")
        bot_memory.maybe_update_memory(c, every=8)
        self.assertEqual(mock_upd.call_count, 0)


class RetentionTests(TransactionTestCase):
    def test_purge_stale_clients(self):
        old = IgClient.get_or_create_for_sender("old1")
        old.last_message_at = timezone.now() - datetime.timedelta(days=200)
        old.save()
        fresh = IgClient.get_or_create_for_sender("fresh1")
        fresh.last_message_at = timezone.now()
        fresh.save()
        n = bot_memory.purge_stale_clients(days=180)
        self.assertEqual(n, 1)
        self.assertFalse(IgClient.objects.filter(igsid="old1").exists())
        self.assertTrue(IgClient.objects.filter(igsid="fresh1").exists())

    def test_purge_stale_blank_igsid_uses_pk_fence_and_shared_analysis_purge(self):
        old = IgClient.objects.create(
            igsid="",
            last_message_at=timezone.now() - datetime.timedelta(days=200),
        )
        message = InstagramBotMessage.objects.create(
            client=old,
            sender_id="",
            role=InstagramBotMessage.Role.USER,
            text="stale",
        )
        event = IgAnalysisMaterialityEvent.objects.create(
            client=old,
            source_message=message,
            source_role=IgAnalysisMaterialityEvent.SourceRole.USER,
            event_kind=IgAnalysisMaterialityEvent.Kind.CUSTOMER_TURN,
            event_key="materiality:" + "a" * 64,
            event_digest="b" * 64,
            relevant_at=timezone.now() - datetime.timedelta(days=200),
        )

        self.assertEqual(bot_memory.purge_stale_clients(days=180), 1)
        self.assertFalse(IgClient.objects.filter(pk=old.pk).exists())
        self.assertFalse(InstagramBotMessage.objects.filter(pk=message.pk).exists())
        self.assertFalse(IgAnalysisMaterialityEvent.objects.filter(pk=event.pk).exists())


class MemoryNoteInjectionTests(TestCase):
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_memory_note_injected_into_system(self, mock_gen):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot as bot

        captured = {}

        def _fake(payload, role="chat", manual_key=None, **kwargs):
            captured["payload"] = payload
            return {"parsed": "ок", "model": "x", "meta": {}}

        mock_gen.side_effect = _fake
        s = InstagramBotSettings.load()
        bot.gemini_generate(
            s, [{"role": "user", "text": "ще раз?"}], memory_note="ПАМ-ЯТЬ-XYZ"
        )
        sysi = captured["payload"].get("system_instruction", {}).get("parts", [{}])[0].get("text", "")
        self.assertIn("ПАМ-ЯТЬ-XYZ", sysi)


class PurgeCommandTests(TransactionTestCase):
    def test_command_runs(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("purge_ig_clients", stdout=out)
        self.assertIn("Видалено карток", out.getvalue())


class ClientContextNoteTests(TestCase):
    def test_none_when_nothing(self):
        c = IgClient.get_or_create_for_sender("cc0")
        self.assertIsNone(bot_memory.client_context_note(c))

    def test_ad_attribution_with_mapped_product(self):
        from management.models import BotAdCampaign
        from storefront.models import Category, Product, ProductStatus

        cat = Category.objects.create(name="Худі", slug="h-cc")
        p = Product.objects.create(
            title="Худі Kharkiv", slug="hk-cc", category=cat, price=950, status=ProductStatus.PUBLISHED
        )
        BotAdCampaign.objects.create(ad_id="555", title="Промо худі", theme="hoodie", product=p)
        c = IgClient.get_or_create_for_sender("cc1")
        c.ad_id = "555"
        c.ad_title = "Промо худі"
        c.save()
        note = bot_memory.client_context_note(c)
        self.assertIn("Худі Kharkiv", note)
        self.assertIn("950", note)

    def test_ad_attribution_uses_sellable_variant_price_not_product_base(self):
        from management.models import BotAdCampaign
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Футболки", slug="cc-priced")
        product = Product.objects.create(
            title="Футболка Бойова квіточка",
            slug="cc-priced-flower",
            category=category,
            price=1090,
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Термо-зелена", primary_hex="#A2AB92")
        ProductColorVariant.objects.create(
            product=product,
            color=color,
            price_override=1450,
            is_default=True,
        )
        BotAdCampaign.objects.create(
            ad_id="priced-555",
            title="Бойова квіточка",
            product=product,
        )
        client = IgClient.get_or_create_for_sender("cc-priced-client")
        client.ad_id = "priced-555"
        client.save(update_fields=["ad_id", "updated_at"])

        note = bot_memory.client_context_note(client)

        self.assertIn("1450", note)
        self.assertNotIn("ціна 1090 грн", note)

    @patch(
        "management.services.ig_catalog_pricing.resolve_product_pricing",
        return_value={"display": "", "exact": False},
    )
    def test_ad_attribution_does_not_quote_base_when_variant_price_is_unresolved(
        self, _pricing
    ):
        from management.models import BotAdCampaign
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Футболки", slug="cc-unresolved")
        product = Product.objects.create(
            title="Футболка з опціями",
            slug="cc-unresolved-shirt",
            category=category,
            price=1090,
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Тестовий", primary_hex="#654321")
        ProductColorVariant.objects.create(product=product, color=color)
        BotAdCampaign.objects.create(
            ad_id="unresolved-555",
            title="Футболка з опціями",
            product=product,
        )
        client = IgClient.get_or_create_for_sender("cc-unresolved-client")
        client.ad_id = "unresolved-555"
        client.save(update_fields=["ad_id", "updated_at"])

        note = bot_memory.client_context_note(client)

        self.assertIn("ціна залежить від конфігурації", note)
        self.assertNotIn("1090 грн", note)

    def test_ad_title_only(self):
        c = IgClient.get_or_create_for_sender("cc2")
        c.ad_title = "Розпродаж футболок"
        c.save()
        note = bot_memory.client_context_note(c)
        self.assertIn("реклам", note.lower())
        self.assertIn("Розпродаж футболок", note)

    def test_returning_customer(self):
        c = IgClient.get_or_create_for_sender("cc3")
        c.purchases_count = 2
        c.total_spent = 1900
        c.save()
        note = bot_memory.client_context_note(c)
        self.assertIn("постій", note.lower())

    def test_payment_context_uses_current_source_qualified_amounts(self):
        from management.models import IgDeal, IgPaymentProjection

        c = IgClient.get_or_create_for_sender("cc-payment-truth")
        deal = IgDeal.objects.create(
            client=c,
            amount=Decimal("2680.00"),
            pay_type=IgDeal.PayType.PREPAYMENT,
            requested_payment_amount=Decimal("880.00"),
        )
        IgPaymentProjection.objects.create(
            deal=deal,
            client=c,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("880.00"),
            paid_at=timezone.now(),
        )

        note = bot_memory.client_context_note(c)

        self.assertIn("2680.00", note)
        self.assertIn("880.00", note)
        self.assertIn("1800.00", note)
        self.assertIn("Monobank", note)
        self.assertIn("не замінюй", note.lower())


class ContextNoteInjectionTests(TestCase):
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_context_note_injected(self, mock_gen):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot as bot

        captured = {}

        def _fake(payload, role="chat", manual_key=None, **kwargs):
            captured["p"] = payload
            return {"parsed": "ок", "model": "x", "meta": {}}

        mock_gen.side_effect = _fake
        bot.gemini_generate(
            InstagramBotSettings.load(), [{"role": "user", "text": "привіт"}],
            context_note="КОНТЕКСТ-XYZ",
        )
        sysi = captured["p"].get("system_instruction", {}).get("parts", [{}])[0].get("text", "")
        self.assertIn("КОНТЕКСТ-XYZ", sysi)

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_authoritative_context_is_after_historical_memory(self, mock_gen):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot as bot

        captured = {}

        def _fake(payload, role="chat", manual_key=None, **kwargs):
            captured["p"] = payload
            return {"parsed": "ок", "model": "x", "meta": {}}

        mock_gen.side_effect = _fake
        bot.gemini_generate(
            InstagramBotSettings.load(),
            [{"role": "user", "text": "яка сума?"}],
            memory_note="ІСТОРИЧНА-ПАМЯТЬ-950",
            context_note="ПОТОЧНА-ІСТИНА-2680",
        )

        sysi = captured["p"]["system_instruction"]["parts"][0]["text"]
        self.assertLess(
            sysi.index("ІСТОРИЧНА-ПАМЯТЬ-950"),
            sysi.index("ПОТОЧНА-ІСТИНА-2680"),
        )


class UnresolvedAdvertisingContextTests(TestCase):
    def test_ad_identifier_without_mapping_does_not_invent_product_or_price(self):
        client = IgClient.get_or_create_for_sender('unmapped-ad-price')
        client.ad_id = 'new-ad'
        note = bot_memory.client_context_note(client)
        self.assertIn('конкретний товар не підтверджено', note)
        self.assertIn('Не вгадуй товар чи ціну', note)

    def test_ambiguous_mapping_requires_clarification(self):
        from management.models import BotAdCampaign
        client = IgClient.get_or_create_for_sender('ambiguous-ad-price')
        client.ad_id = 'duplicate'
        for theme in ('hoodie', 'tshirt'):
            BotAdCampaign.objects.create(ad_id='duplicate', theme=theme)
        note = bot_memory.client_context_note(client)
        self.assertIn('не підтверджено однозначним мапінгом', note)
        self.assertNotIn('найімовірніше цікавить', note)
