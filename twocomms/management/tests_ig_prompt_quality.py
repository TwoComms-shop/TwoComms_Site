"""W5 — качество промпта. Механизмы переопределены по данным разведки.

Разведка прода изменила три из шести формулировок волны:

**IMP-025.** «Версионирование `system_prompt` с diff и откатом» покрывает 11.7%
собранного промпта (3136 из ~26 900 символов). Поле дословно равно константе
кода, `InstagramBotLog(event="settings_saved")` — 0 записей: форму настроек не
сохраняли ни разу. Остальные 88% — код и git-версионированный `brand.md`.
Версионировать надо тот слой, который действительно правят через интерфейс и
которого нет в git: `BotInstruction` и `knowledge_base`.

**IMP-026.** «Диалоги с ожидаемыми свойствами ответа» непроверяемы: выход Gemini
во всех тестах замокан константой, то есть проверялась бы наша же строка.
Проверяемы свойства **промпта** — что нужный блок физически присутствует для
заданного состояния клиента.

**IMP-029.** «тип + последнее значение + давность» — значения нет: `value` пуст
в 149 из 150 сигналов, `payload` в 150 из 150. Доступно «тип + давность».
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from management.ig_bot_models import IgClient, IgConversationSignal
from management.models import BotInstruction, InstagramBotMessage, InstagramBotSettings


class EditableLayerVersioningTests(TestCase):
    """IMP-025 переопределён: версионируем то, что правят руками."""

    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            "prompt-version-actor", password="x", is_staff=True
        )

    def test_instruction_change_is_recorded_with_author_and_diff(self):
        from management.ig_bot_models import BotPromptRevision
        from management.services.bot_prompt_versions import record_instruction_revision

        instruction = BotInstruction.objects.create(
            title="FAQ", body="Доставка 1-3 дні.", is_active=True
        )
        instruction.body = "Доставка Новою Поштою 1-3 робочі дні."
        instruction.save(update_fields=["body", "updated_at"])

        revision = record_instruction_revision(
            instruction, actor=self.actor, previous_body="Доставка 1-3 дні."
        )

        self.assertIsInstance(revision, BotPromptRevision)
        self.assertEqual(revision.actor_id, self.actor.pk)
        self.assertIn("Новою Поштою", revision.body)
        self.assertIn("Доставка 1-3 дні.", revision.previous_body)

    def test_revision_can_be_rolled_back(self):
        from management.services.bot_prompt_versions import (
            record_instruction_revision,
            rollback_revision,
        )

        instruction = BotInstruction.objects.create(
            title="FAQ", body="перша версія", is_active=True
        )
        instruction.body = "друга версія"
        instruction.save(update_fields=["body", "updated_at"])
        revision = record_instruction_revision(
            instruction, actor=self.actor, previous_body="перша версія"
        )

        rollback_revision(revision, actor=self.actor)

        instruction.refresh_from_db()
        self.assertEqual(instruction.body, "перша версія")

    def test_rollback_is_itself_recorded(self):
        """Иначе история врёт: откат — тоже изменение."""
        from management.ig_bot_models import BotPromptRevision
        from management.services.bot_prompt_versions import (
            record_instruction_revision,
            rollback_revision,
        )

        instruction = BotInstruction.objects.create(
            title="FAQ", body="перша", is_active=True
        )
        instruction.body = "друга"
        instruction.save(update_fields=["body", "updated_at"])
        revision = record_instruction_revision(
            instruction, actor=self.actor, previous_body="перша"
        )

        rollback_revision(revision, actor=self.actor)

        self.assertEqual(
            BotPromptRevision.objects.filter(
                target=BotPromptRevision.Target.INSTRUCTION
            ).count(),
            2,
        )

    def test_knowledge_base_change_is_recorded(self):
        from management.ig_bot_models import BotPromptRevision
        from management.services.bot_prompt_versions import (
            record_knowledge_base_revision,
        )

        settings_obj = InstagramBotSettings.load()

        revision = record_knowledge_base_revision(
            settings_obj,
            actor=self.actor,
            previous_body="",
            body="Нова директива",
        )

        self.assertEqual(revision.target, BotPromptRevision.Target.KNOWLEDGE_BASE)
        self.assertEqual(revision.body, "Нова директива")

    def test_revision_history_is_append_only(self):
        from management.ig_bot_models import BotPromptRevision
        from management.services.bot_prompt_versions import record_instruction_revision

        instruction = BotInstruction.objects.create(
            title="FAQ", body="текст", is_active=True
        )
        revision = record_instruction_revision(
            instruction, actor=self.actor, previous_body=""
        )

        with self.assertRaises(ValueError):
            revision.delete()

    def test_unchanged_body_creates_no_revision(self):
        from management.services.bot_prompt_versions import record_instruction_revision

        instruction = BotInstruction.objects.create(
            title="FAQ", body="той самий текст", is_active=True
        )

        self.assertIsNone(
            record_instruction_revision(
                instruction, actor=self.actor, previous_body="той самий текст"
            )
        )


class PromptPropertyTests(TestCase):
    """IMP-026 переопределён: проверяем свойства промпта, а не выход модели."""

    def setUp(self):
        InstagramBotSettings.objects.update_or_create(
            pk=1, defaults={"is_enabled": True}
        )
        from management.tests_ig_policy_helpers import ensure_test_instruction_publication
        ensure_test_instruction_publication()
        self.client_row = IgClient.get_or_create_for_sender("prompt-property-client")

    def _prompt(self, client):
        from management.services.instagram_bot import build_prompt_snapshot

        return build_prompt_snapshot(client)

    def test_prompt_states_the_conversation_stage(self):
        self.client_row.stage = IgClient.Stage.CHECKOUT
        self.client_row.save(update_fields=["stage", "updated_at"])

        self.assertIn("checkout", self._prompt(self.client_row).lower())

    def test_prompt_states_the_conversation_language(self):
        self.client_row.language = "en"
        self.client_row.save(update_fields=["language", "updated_at"])

        self.assertIn("en", self._prompt(self.client_row).lower())

    def test_prompt_carries_recorded_signals(self):
        """F-AI-006: 987 сигналов писались и не читались при генерации."""
        message = InstagramBotMessage.objects.create(
            client=self.client_row,
            role=InstagramBotMessage.Role.USER,
            text="який розмір?",
        )
        IgConversationSignal.objects.create(
            client=self.client_row,
            message=message,
            signal_type=IgConversationSignal.Type.SIZE_CONCERN,
            confidence=Decimal("0.8"),
        )

        prompt = self._prompt(self.client_row)

        self.assertIn("СИГНАЛИ", prompt)
        self.assertIn("size_concern", prompt)

    def test_manager_takeover_signals_are_not_shown_as_customer_signals(self):
        """85% сигналов на проде — manager_takeover, это шум, не поведение клиента."""
        message = InstagramBotMessage.objects.create(
            client=self.client_row,
            role=InstagramBotMessage.Role.USER,
            text="текст",
        )
        IgConversationSignal.objects.create(
            client=self.client_row,
            message=message,
            signal_type=IgConversationSignal.Type.MANAGER_TAKEOVER,
            confidence=Decimal("1.0"),
        )

        self.assertNotIn("manager_takeover", self._prompt(self.client_row))

    def test_signal_block_is_absent_when_there_are_no_signals(self):
        """Пустой заголовок блока — тоже шум: он занимает бюджет и ничего не говорит."""
        self.assertNotIn("СИГНАЛИ", self._prompt(self.client_row))

    def test_signals_carry_recency_not_a_missing_value(self):
        """`value` пуст в 149 из 150 сигналов, поэтому показываем давность."""
        message = InstagramBotMessage.objects.create(
            client=self.client_row,
            role=InstagramBotMessage.Role.USER,
            text="текст",
        )
        signal = IgConversationSignal.objects.create(
            client=self.client_row,
            message=message,
            signal_type=IgConversationSignal.Type.CHECKOUT_STARTED,
            confidence=Decimal("0.8"),
        )
        IgConversationSignal.objects.filter(pk=signal.pk).update(
            created_at=timezone.now() - timedelta(hours=5)
        )

        prompt = self._prompt(self.client_row)

        self.assertIn("checkout_started", prompt)
        self.assertRegex(prompt, r"checkout_started[^\n]*год")

    def test_prompt_states_an_open_service_case(self):
        from management.ig_bot_models import IgPostSaleCase
        from orders.models import Order

        order = Order.objects.create(
            order_number="TWC-PROMPT-CASE",
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal("2100.00"),
            payment_status="paid",
            status="ship",
        )
        IgPostSaleCase.objects.create(
            client=self.client_row,
            order=order,
            source_message=InstagramBotMessage.objects.create(
                client=self.client_row,
                role=InstagramBotMessage.Role.USER,
                text="хочу обмін",
            ),
            case_type=IgPostSaleCase.CaseType.EXCHANGE,
            status=IgPostSaleCase.Status.IN_TRANSIT,
            requested_size="XL",
        )

        prompt = self._prompt(self.client_row)

        self.assertIn("XL", prompt)
        self.assertIn("обмін", prompt.lower())

    def test_prompt_does_not_offer_discounts_during_a_service_case(self):
        from management.ig_bot_models import IgPostSaleCase
        from management.tests_ig_policy_helpers import publish_current_instructions
        from orders.models import Order

        rescue_marker = "SERVICE-RESCUE-DISCOUNT-OFFER"
        BotInstruction.objects.create(title="Rescue offer fixture", is_active=True,
            intent_tags="sales, price, discount",
            body=f"{rescue_marker}: запропонуй rescue-знижку 5% або 10%.")
        publish_current_instructions()
        self.assertIn(rescue_marker, self._prompt(self.client_row))

        order = Order.objects.create(
            order_number="TWC-PROMPT-NO-DISCOUNT",
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal("2100.00"),
            payment_status="paid",
            status="ship",
        )
        IgPostSaleCase.objects.create(
            client=self.client_row,
            order=order,
            source_message=InstagramBotMessage.objects.create(
                client=self.client_row,
                role=InstagramBotMessage.Role.USER,
                text="хочу обмін",
            ),
            case_type=IgPostSaleCase.CaseType.EXCHANGE,
            status=IgPostSaleCase.Status.OPEN,
        )

        prompt = self._prompt(self.client_row)

        self.assertIn("[POST-SALE SERVICE MODE", prompt)
        self.assertIn("Зараз це не продаж. Не пропонуй знижок", prompt)
        self.assertNotIn(rescue_marker, prompt)
        self.assertNotIn("запропонуй rescue-знижку 5% або 10%", prompt)
        # Fabric percentages are legitimate facts, not discount offers.
        self.assertIn("95% бавовни / 5% еластану", prompt)
        self.assertIn("90% бавовни / 10% поліестеру", prompt)

    def test_buyer_is_named_in_the_prompt(self):
        IgClient.objects.filter(pk=self.client_row.pk).update(purchases_count=2)
        self.client_row.refresh_from_db()

        self.assertIn("постійний клієнт", self._prompt(self.client_row))


class CanonicalPromptPolicyTests(TestCase):
    def setUp(self):
        self.settings_row = InstagramBotSettings.load()
        self.settings_row.system_prompt = "Стара стилістика згадує лише українську та російську."
        self.settings_row.save(update_fields=["system_prompt"])
        from management.tests_ig_policy_helpers import ensure_test_instruction_publication
        ensure_test_instruction_publication()
        self.settings_row.refresh_from_db()
        self.client_row = IgClient.get_or_create_for_sender("canonical-prompt-policy")

    def test_runtime_policy_resolves_price_discount_language_and_reply_conflicts(self):
        from management.services.instagram_bot import assemble_system_instruction

        prompt = assemble_system_instruction(
            self.settings_row,
            client=self.client_row,
            turn_text="Could you reply in English?",
        )

        self.assertIn("[ЄДИНИЙ ПОРЯДОК ІСТИНИ", prompt)
        self.assertIn("(1) підтверджені системою факти про оплату", prompt)
        self.assertIn("(2) [СТАН ОФОРМЛЕННЯ]", prompt)
        self.assertIn("UA/RU/EN", prompt)
        self.assertIn("не називай одну точну суму", prompt)
        self.assertIn("лише факт вже порахованої ціни", prompt)
        self.assertIn("не більше одного запитання", prompt)
        self.assertNotIn("ОПЕРАТИВНІ ДИРЕКТИВИ — найвищий пріоритет", prompt)

    def test_mandatory_live_directives_are_not_cut_by_the_old_optional_limit(self):
        from management.services.instagram_bot import (
            assemble_system_instruction,
        )

        first = "DIRECTIVE-ONE " + "a" * 1200
        second = "DIRECTIVE-TWO " + "b" * 1200
        third = "DIRECTIVE-THREE " + "c" * 1200
        self.settings_row.knowledge_base = "\n\n".join((first, second, third))
        self.settings_row.save(update_fields=["knowledge_base"])

        prompt = assemble_system_instruction(self.settings_row, client=self.client_row)

        self.assertIn(first, prompt)
        self.assertIn(second, prompt)
        self.assertIn(third, prompt)
        self.assertNotIn("оперативних директив: 1 блок(ів) не вмістилися", prompt)


class ClientProfileTests(TestCase):
    """IMP-030 переопределён: профиль — первая память, а не замена резюме."""

    def setUp(self):
        self.client_row = IgClient.get_or_create_for_sender("profile-client")

    def test_profile_records_confirmed_facts(self):
        from management.services.bot_memory import update_client_profile

        self.client_row.current_size = "XL"
        self.client_row.language = "uk"
        self.client_row.save(update_fields=[
            "current_size", "language", "updated_at",
        ])

        profile = update_client_profile(self.client_row)

        self.assertEqual(profile["fit"]["size"], "XL")
        self.assertEqual(profile["comms"]["lang"], "uk")

    def test_objections_are_appended_not_overwritten(self):
        """`primary_objection` — одно перезаписываемое поле; история терялась."""
        from management.services.bot_memory import update_client_profile

        self.client_row.primary_objection = IgClient.Objection.PRICE
        self.client_row.save(update_fields=["primary_objection", "updated_at"])
        update_client_profile(self.client_row)

        self.client_row.primary_objection = IgClient.Objection.SIZE
        self.client_row.save(update_fields=["primary_objection", "updated_at"])
        profile = update_client_profile(self.client_row)

        self.assertEqual(
            [row["type"] for row in profile["objections"]], ["price", "size"]
        )

    def test_the_same_objection_is_not_appended_twice(self):
        from management.services.bot_memory import update_client_profile

        self.client_row.primary_objection = IgClient.Objection.PRICE
        self.client_row.save(update_fields=["primary_objection", "updated_at"])
        update_client_profile(self.client_row)
        profile = update_client_profile(self.client_row)

        self.assertEqual(len(profile["objections"]), 1)

    def test_profile_survives_a_funnel_reset(self):
        """`ig_funnel_reset` обнуляет весь `sales_context`; профиль — не догадка."""
        from management.services.bot_memory import update_client_profile

        update_client_profile(self.client_row)
        from django.contrib.auth import get_user_model as _gum

        actor = _gum().objects.create_user("profile-reset-actor", is_staff=True)
        from management.services.ig_funnel_reset import reset_funnel

        reset_funnel(client_id=self.client_row.pk, actor=actor, reason="test")

        self.client_row.refresh_from_db()
        self.assertIn("_profile", self.client_row.sales_context or {})

    def test_profile_lives_under_a_service_key(self):
        """Служебное соглашение уже есть: `_provenance`, `_media_evidence`."""
        from management.services.bot_memory import update_client_profile

        update_client_profile(self.client_row)

        self.client_row.refresh_from_db()
        self.assertIn("_profile", self.client_row.sales_context)
        self.assertNotIn("profile", self.client_row.sales_context)
