"""Prompt publication/compilation contracts; these do not prove model behavior."""
from django.test import SimpleTestCase

from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values
from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY, CORE_POLICY_SHA256, core_policy_hash


class CommunicationPolicyContracts(SimpleTestCase):
    def test_answer_first_and_optional_question_do_not_depend_on_another_pending_answer(self):
        for clause in (
            "Спочатку відповідай на вже відомі актуальні питання",
            "лише про відсутню необхідну деталь",
            "Якщо даних достатньо, відповідь без запитань є повною",
            "нове невідоме питання додай до чинного brief очікування",
            "очікування відповіді на друге питання не блокує третє",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, CANONICAL_IG_CORE_POLICY)

    def test_style_uses_explicit_evidence_without_sensitive_inference_or_artificial_delay(self):
        for clause in (
            "прямо висловлені побажання, поточний зміст і збережені communication.preferences",
            "Не виводь військовий статус, характер, дохід чи інші чутливі риси з фото, принта або мови",
            "Емодзі й гумор необов'язкові",
            "про оплату, скаргу чи критичний строк відповідай спокійно й предметно",
            "не затримуй готову відповідь заради природності",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, CANONICAL_IG_CORE_POLICY)

    def test_direct_service_offer_is_truthful_and_does_not_require_external_contact(self):
        body = reviewed_scenario_values("collaboration")["body"]
        for clause in (
            "Подякуй за конкретно запропоновану допомогу",
            "віртуальна помічниця, доступна 24/7",
            "люди команди розглядають нестандартні питання",
            "Не стверджуй, що самостійно навчаєшся чи вдосконалюєшся",
            "Не оголошуй вакансію відкритою чи закритою",
            "Після підтвердженого передавання",
            "якщо після розгляду знадобляться уточнення, команда може написати тут",
            "відсутність іншого контакту не блокує handoff",
            "лише за підтвердженим сервером case/receipt",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, body)

    def test_direct_service_routing_is_source_owned_without_service_identifiers_in_the_playbook(self):
        from management.services.bot_sales_classifier import extract_collaboration_brief
        from management.services.ig_business_cooperation import recruitment_service_for_sources

        body = reviewed_scenario_values("collaboration")["body"]
        # Routing remains native code's responsibility. The customer-prose
        # playbook must not supply internal identifiers that can be echoed.
        for text in (
            "Можу відповідати на повідомлення клієнтів вашого бренду в Instagram Direct.",
            "Хочу работать оператором Instagram Direct. Могу отвечать на сообщения ваших клиентов.",
            "I can answer your customers' messages in Instagram Direct.",
        ):
            with self.subTest(text=text):
                native = recruitment_service_for_sources([{"role": "user", "message_id": 1, "text": text}])
                brief = extract_collaboration_brief(text)
                self.assertTrue(native["employment_service_offer"])
                self.assertEqual(native["hiring_status"], "unknown")
                self.assertEqual(brief["primary_subtype"], native["subtype"])
                self.assertNotIn(native["subtype"], body)
        self.assertNotIn("collaboration_review", body)
        self.assertIn("повідомлення, товари, розміри чи доставка", body)
        self.assertIn("не описуй внутрішні сервіси", body)
        from management.services.ig_policy_publication import (
            DEFAULT_INSTRUCTION_BUDGET_CHARS, SNAPSHOT_SCHEMA_VERSION, select_policy_snapshot,
        )
        values = reviewed_scenario_values("collaboration")
        snapshot = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "instructions": [
            {**values, "id": "instruction:1", "source_id": 1},
        ]}
        selected = select_policy_snapshot(snapshot, client_tags={"collaboration"},
            active_triggers={"collaboration"}, required_scenarios=("collaboration",))
        self.assertEqual(selected["required_ids"], ["instruction:1"])
        self.assertLessEqual(len(selected["rendered_text"]), DEFAULT_INSTRUCTION_BUDGET_CHARS)

    def test_objection_stays_specific_and_approved_fact_authority_is_preserved(self):
        body = reviewed_scenario_values("objection")["body"]
        for clause in (
            "один доречний доказ чи уточнення з підтверджених фактів саме щодо цього сумніву",
            "не знецінюй конкурентів і не пропонуй непрошену знижку",
            "передай менеджеру точне запитання",
            "Ручна поступка потребує чинного manager approval",
            "понад 100 прань — оцінка засновника при рекомендованому догляді",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, body)

    def test_all_reviewed_scenarios_fit_with_runtime_authority_core_and_knowledge_in_every_locale(self):
        from management.services import instagram_bot as bot
        from management.services.bot_knowledge import read_knowledge_manifest
        from management.services.ig_policy_compiler import (
            DEFAULT_POLICY_BUDGET_CHARS, PolicyModule, compile_policy,
        )
        from management.services.ig_policy_publication import SNAPSHOT_SCHEMA_VERSION, select_policy_snapshot

        self.assertEqual(CORE_POLICY_SHA256, core_policy_hash(CANONICAL_IG_CORE_POLICY))
        original_keys = ("custom_print", "collaboration", "ugc", "size", "objection")
        instructions = []
        for index, row in enumerate(SCENARIO_INSTRUCTIONS, 1):
            values = reviewed_scenario_values(row["key"])
            expected_scenario = row["key"] if row["key"] in original_keys else "business_cooperation"
            self.assertEqual(values["programme_metadata"]["required_for_scenarios"], [expected_scenario])
            self.assertEqual(values["allowed_actions"], [])
            instructions.append({**values, "id": "scenario:" + row["key"], "source_id": index})
        snapshot = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "instructions": instructions}
        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                selected = select_policy_snapshot(snapshot, locale=locale,
                    active_triggers=("business_cooperation",),
                    required_scenarios=(*original_keys, "business_cooperation"),
                    budget_chars=DEFAULT_POLICY_BUDGET_CHARS)
                expected_ids = {"scenario:" + key for key in
                    (*original_keys, "public_cooperation_" + locale)}
                self.assertEqual(set(selected["selected_ids"]), expected_ids)
                self.assertEqual(set(selected["required_ids"]), expected_ids)
                localized = [item for item in selected["selected"] if item["locale"] != "all"]
                self.assertEqual([item["locale"] for item in localized], [locale])
                playbooks = [PolicyModule(item["id"], item["rendered_body"], required=item["required"])
                    for item in selected["selected"]]
                creator = select_policy_snapshot(snapshot, locale=locale,
                    client_tags={"creator", "collaboration"}, active_triggers=("creator",),
                    required_scenarios=("collaboration",))
                self.assertEqual(creator["selected_ids"], ["scenario:collaboration"])
                self.assertEqual(creator["required_ids"], ["scenario:collaboration"])
                knowledge = read_knowledge_manifest(locale)
                compiled = compile_policy(
                    immutable_authority=[PolicyModule("authority:server", bot.CANONICAL_PROMPT_AUTHORITY_POLICY)],
                    published_core=bot._policy_core_modules(CANONICAL_IG_CORE_POLICY),
                    verified_dynamic_facts=(), playbooks=playbooks,
                    knowledge=knowledge.policy_inputs(),
                    budget_chars=DEFAULT_POLICY_BUDGET_CHARS,
                )
                self.assertLessEqual(len(compiled.text), DEFAULT_POLICY_BUDGET_CHARS)
                self.assertFalse(compiled.omitted)
                for module in playbooks:
                    self.assertIn(module.id, compiled.required_ids)
                    self.assertEqual(compiled.text.count(module.body), 1)
                self.assertEqual(compiled.text.count(CANONICAL_IG_CORE_POLICY), 1)
