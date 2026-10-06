# -*- coding: utf-8 -*-
"""Інструкції за тригером поточного ходу, а не «завжди».

Прямий запит заказника. Виміряно на проді до правки: 202 клієнти з 289 (70%)
отримували рівно одну інструкцію з семи, а відбір ішов по зрізу CRM-полів і не
знав ні тексту повідомлення, ні сигналів. У клієнта #5 стояв `objection=size` при
`intent=payment`, тому розмірний playbook підмішувався в повідомлення про оплату.
"""
from django.test import TestCase

from management.models import BotInstruction, IgClient


def _client(igsid="instruction-routing"):
    return IgClient.get_or_create_for_sender(igsid)


class TurnTriggerTests(TestCase):
    def test_size_question_triggers_only_on_a_size_question(self):
        from management.services.bot_instruction_routing import turn_triggers

        self.assertIn("size_question", turn_triggers("а який розмір мені підійде?"))
        self.assertIn("size_question", turn_triggers("скиньте размерную сетку"))
        self.assertNotIn("size_question", turn_triggers("дайте посилання на оплату"))

    def test_price_question_and_price_objection_are_different_triggers(self):
        """«Скільки коштує» — питання, «дорого» — заперечення. Раніше це збігалось."""
        from management.services.bot_instruction_routing import turn_triggers

        question = turn_triggers("скільки коштує ця футболка?")
        objection = turn_triggers("щось дорого як на футболку")

        self.assertIn("price_question", question)
        self.assertNotIn("price_objection", question)
        self.assertIn("price_objection", objection)

    def test_empty_message_has_no_triggers(self):
        """Хід без тексту (лише фото) не має підмішувати тригерні інструкції."""
        from management.services.bot_instruction_routing import turn_triggers

        self.assertEqual(turn_triggers(""), set())
        self.assertEqual(turn_triggers("   "), set())

    def test_price_objection_requires_own_current_semantic_concern(self):
        from management.services.bot_instruction_routing import turn_triggers

        for text in ("Це не дорого.", '«Дорого» — цитата.', "Друг сказав, що дорого.",
                     "Раніше було дорого.", "Мені дорого. Це не дорого."):
            with self.subTest(text=text):
                self.assertNotIn("price_objection", turn_triggers(text))
        self.assertIn("price_objection", turn_triggers("Мені дорого."))
        self.assertIn("price_objection", turn_triggers("В іншому магазині дешевше."))

    def test_first_person_contrast_keeps_real_objection_after_reported_history(self):
        from management.services.bot_instruction_routing import turn_triggers

        self.assertIn("price_objection", turn_triggers("Друг сказав, що дорого, але мені дорого."))
        self.assertIn("price_objection", turn_triggers("Раніше було дорого, але зараз мені дорого."))

    def test_hesitation_requires_current_own_concern(self):
        from management.services.bot_instruction_routing import turn_triggers

        self.assertIn("hesitation", turn_triggers("Ще подумаю."))
        for text in ("Не подумаю.", '«Подумаю» — цитата.', "Друг сказав: подумаю.",
                     "Раніше думав: подумаю."):
            with self.subTest(text=text):
                self.assertNotIn("hesitation", turn_triggers(text))

    def test_greeting_thanks_and_ordinary_price_keep_existing_question_contract(self):
        from management.services.bot_instruction_routing import turn_triggers

        for text in ("Привіт", "Дякую"):
            self.assertEqual(turn_triggers(text), set())
        for text in ("Скільки коштує футболка?", "What is the price?", "Какая цена футболки?"):
            with self.subTest(text=text):
                triggers = turn_triggers(text)
                self.assertIn("price_question", triggers)
                self.assertNotIn("price_objection", triggers)


class TagMarkupTests(TestCase):
    def test_markup_splits_plain_triggers_and_excludes(self):
        from management.services.bot_instruction_routing import split_instruction_tags

        parts = split_instruction_tags("size, on:size_question, not:paid")

        self.assertEqual(parts["plain"], {"size"})
        self.assertEqual(parts["triggers"], {"size_question"})
        self.assertEqual(parts["excludes"], {"paid"})

    def test_unknown_tag_is_reported(self):
        from management.services.bot_instruction_routing import validate_instruction_tags

        issues = validate_instruction_tags("globl, size")

        self.assertEqual(issues["unknown_tags"], ["globl"])
        self.assertEqual(issues["unknown_triggers"], [])

    def test_unknown_trigger_is_reported(self):
        from management.services.bot_instruction_routing import validate_instruction_tags

        issues = validate_instruction_tags("on:razmer_vopros")

        self.assertEqual(issues["unknown_triggers"], ["razmer_vopros"])

    def test_real_production_tags_are_all_known(self):
        """Сім прод-інструкцій не мають раптом стати невалідними."""
        from management.services.bot_instruction_routing import validate_instruction_tags

        for raw in (
            "global,core,sales",
            "product,catalog,product_matched,checkout",
            "size,fit",
            "prepayment,payment",
            "price,discount",
            "custom_print",
            "no_buy,stop,cold,spam",
        ):
            with self.subTest(raw=raw):
                issues = validate_instruction_tags(raw)
                self.assertEqual(issues["unknown_tags"], [], raw)


class InstructionMatchTests(TestCase):
    def test_plain_tag_still_matches_by_intersection(self):
        from management.services.bot_instruction_routing import instruction_matches

        self.assertTrue(instruction_matches("size,fit", {"size", "checkout"}))
        self.assertFalse(instruction_matches("size,fit", {"checkout"}))

    def test_untagged_instruction_reaches_everyone(self):
        from management.services.bot_instruction_routing import instruction_matches

        self.assertTrue(instruction_matches("", {"checkout"}))

    def test_trigger_requires_the_trigger_to_fire(self):
        from management.services.bot_instruction_routing import instruction_matches

        self.assertFalse(instruction_matches("on:size_question", {"checkout"}))
        self.assertTrue(instruction_matches(
            "on:size_question", {"checkout"}, active_triggers={"size_question"}
        ))

    def test_exclusion_vetoes_even_a_matching_tag(self):
        from management.services.bot_instruction_routing import instruction_matches

        self.assertFalse(instruction_matches(
            "sales, not:paid", {"sales", "paid"}
        ))
        self.assertTrue(instruction_matches("sales, not:paid", {"sales"}))


class InstructionBlockTests(TestCase):
    def setUp(self):
        BotInstruction.objects.all().delete()
        self.client_row = _client()
        self.client_row.stage = IgClient.Stage.CHECKOUT
        self.client_row.intent = IgClient.Intent.PAYMENT
        self.client_row.save(update_fields=["stage", "intent", "updated_at"])

    def test_size_instruction_arrives_only_with_a_size_question(self):
        BotInstruction.objects.create(
            title="Size And Fit", body="Поясни різницю classic/oversize.",
            intent_tags="on:size_question", priority=30,
        )
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        from management.services.bot_playbooks import active_instruction_block

        silent = active_instruction_block(self.client_row, turn_text="дай посилання")
        asked = active_instruction_block(self.client_row, turn_text="який розмір брати?")

        self.assertNotIn("classic/oversize", silent)
        self.assertIn("classic/oversize", asked)

    def test_excluded_instruction_is_dropped_for_a_paid_client(self):
        BotInstruction.objects.create(
            title="Rescue", body="Знижка 5% як останній аргумент.",
            intent_tags="sales, not:paid", priority=50,
        )
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        from management.services.bot_playbooks import active_instruction_block

        self.assertIn("Знижка", active_instruction_block(self.client_row))

        self.client_row.stage = IgClient.Stage.PAID
        self.client_row.save(update_fields=["stage", "updated_at"])
        self.assertNotIn("Знижка", active_instruction_block(self.client_row))

    def test_block_is_capped_by_whole_instructions(self):
        """Обрізана посередині інструкція гірша за відсутню."""
        from management.services.bot_playbooks import (
            MAX_INSTRUCTION_BLOCK_CHARS,
            active_instruction_block,
        )

        for index in range(12):
            BotInstruction.objects.create(
                title=f"Rule {index}", body="х" * 800, intent_tags="global",
                priority=index,
            )
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        block = active_instruction_block(self.client_row)

        self.assertLessEqual(len(block), MAX_INSTRUCTION_BLOCK_CHARS + 200)
        self.assertIn("не вміщено в бюджет", block)
        # Жодна інструкція не обрізана посередині: кожен рядок або цілий, або відсутній.
        for line in block.split("\n"):
            if line.startswith("• Rule"):
                self.assertEqual(len(line.split(": ", 1)[1]), 800)

    def test_admin_preview_without_client_shows_everything(self):
        BotInstruction.objects.create(
            title="Narrow", body="Тільки для обміну.", intent_tags="exchange",
        )
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        from management.services.bot_playbooks import active_instruction_block

        self.assertIn("Тільки для обміну", active_instruction_block(None))


class DeadTagMappingTests(TestCase):
    """Явна таблиця, яка дублює цикл вище, вводить в оману — і вже підвела W3."""

    def test_enum_values_are_tags_without_an_explicit_branch(self):
        from management.services.bot_playbooks import tags_for_client

        client = _client("dead-mapping")
        client.stage = IgClient.Stage.PAYMENT_PENDING
        client.intent = IgClient.Intent.CUSTOM_PRINT
        client.primary_objection = IgClient.Objection.PREPAYMENT
        client.save(update_fields=["stage", "intent", "primary_objection", "updated_at"])

        tags = tags_for_client(client)

        # Ці теги приходять із значень enum'ів, а не з окремих гілок.
        self.assertIn("payment_pending", tags)
        self.assertIn("custom_print", tags)
        self.assertIn("prepayment", tags)
        # А ці — справді додаються окремо, бо серед значень їх немає.
        self.assertIn("payment", tags)

    def test_price_objection_still_adds_discount(self):
        from management.services.bot_playbooks import tags_for_client

        client = _client("dead-mapping-price")
        client.primary_objection = IgClient.Objection.PRICE
        client.save(update_fields=["primary_objection", "updated_at"])

        tags = tags_for_client(client)
        self.assertIn("price", tags)
        self.assertIn("discount", tags)
