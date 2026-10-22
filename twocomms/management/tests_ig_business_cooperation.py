"""Current business questions stay distinct from creator and fulfillment work."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.services.ig_business_cooperation import (
    business_cooperation_for_sources, current_cooperation_sources,
    extract_business_cooperation,
)


SHOP_REQUEST = (
    "Добрий день! Хочу запустити Instagram-магазин URBAN DROP в Україні. "
    "Цікавить співпраця з продажу чорних oversize-футболок. Підкажіть: "
    "1 Яка ціна для партнера? 2 Які є розміри та кольори? "
    "3 Чи можна почати без закупівлі партії товару? "
    "4 Чи можна продавати через Instagram і які умови доставки? "
    "5 Чи дозволяєте використовувати ваші фото товарів у магазині? Дякую!"
)


def sources(*texts):
    return [{"message_id": index, "role": "user", "text": text,
             "source_digest": f"source-{index}"} for index, text in enumerate(texts, 1)]


def revision(*texts, routes=None):
    return SimpleNamespace(pk=901, client=SimpleNamespace(intent=""),
        snapshot_digest="sealed-business", bundle_snapshot={"sources": sources(*texts)},
        generation_proposal={"customer_routes": routes} if routes else {})


class BusinessCooperationEvidenceTests(SimpleTestCase):
    def test_real_shop_request_keeps_all_five_questions_without_creator_inputs(self):
        brief = extract_business_cooperation(SHOP_REQUEST)
        self.assertTrue(brief["shop_launch"])
        self.assertIn("wholesale_store", brief["subtypes"])
        self.assertEqual(set(brief["requested_topics"]), {
            "partner_price", "catalog_options", "start_without_stock",
            "delivery_terms", "resale_channel", "photo_usage",
        })
        self.assertEqual(brief["decision_owner"], "manager")
        self.assertNotIn("creator", brief["subtypes"])
        self.assertNotIn("portfolio_review", brief)

    def test_generic_shop_opening_is_business_in_all_supported_languages(self):
        for text in (
            "Хочу відкрити магазин, хочу задати кілька питань",
            "Хочу открыть магазин, хочу задать несколько вопросов",
            "I want to open a shop and ask a few questions",
            "We are launching an Instagram store and have some questions",
            "Цікавить опт", "Интересует дропшиппинг", "Interested in dropshipping",
            "Can I resell your shirts?", "Do you offer a partnership?",
        ):
            with self.subTest(text=text):
                self.assertTrue(extract_business_cooperation(text))

    def test_greetings_questions_and_trailing_reaction_keep_burst_evidence(self):
        bundle = sources("Добрий день", "Хочу відкрити магазин", "Яка ціна для партнера?", "Дякую 💛")
        brief = business_cooperation_for_sources(bundle)
        self.assertTrue(brief["shop_launch"])
        self.assertIn("partner_price", brief["requested_topics"])
        self.assertIn(2, brief["evidence_message_ids"])
        self.assertIn(3, brief["evidence_message_ids"])
        self.assertEqual(bundle[1]["text"], "Хочу відкрити магазин")

    def test_quotes_reports_history_and_normal_store_questions_do_not_open_business(self):
        for text in (
            '«Хочу відкрити магазин, цікавить співпраця» — цитата.',
            '> Interested in wholesale\nWhat does this mean?',
            'My friend said: I want to open a store and discuss a partnership.',
            'Друг сказав: хочу відкрити магазин, цікавить співпраця.',
            'Раніше хотів відкрити магазин, цікавила співпраця.',
            'This is not my offer: partnership.',
            'This is a quote: I want to open a shop and discuss a partnership.',
            'Я не пропоную співпрацю', 'I am not proposing a partnership',
            'В якому магазині ви знаходитесь?', 'Ваш магазин працює сьогодні?',
            'I want a shirt from your store', 'Покажіть фото вашого магазину',
            'https://example.invalid/wholesale-partnership', 'Привіт', 'Дякую 💛',
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_business_cooperation(text), {})

    def test_withdrawal_clears_earlier_bundle_then_current_reentry_can_reopen(self):
        for withdrawal in ("Співпраця вже не цікавить", "Не хочу відкривати магазин",
                           "I no longer want a partnership", "Сотрудничество не актуально",
                           "Більше не актуально", "Never mind"):
            with self.subTest(withdrawal=withdrawal):
                bundle = sources("Хочу відкрити магазин. Цікавить співпраця", withdrawal, "Дякую")
                self.assertEqual(business_cooperation_for_sources(bundle), {})
                bundle.extend(sources("Привіт! Тепер знову хочу відкрити магазин"))
                self.assertTrue(business_cooperation_for_sources(bundle))
        self.assertTrue(extract_business_cooperation(
            "Раніше хотів співпрацю, але зараз хочу відкрити магазин"))
        self.assertTrue(extract_business_cooperation(
            "Співпраця вже не цікавить, а тепер хочу відкрити магазин"))

    def test_creator_and_unrelated_print_paths_remain_distinct(self):
        from management.services.bot_sales_classifier import extract_collaboration_brief

        creator = "Я фотограф, можу створити фото й відео для вашого бренду. Telegram @photo_pro"
        self.assertEqual(extract_business_cooperation(creator), {})
        brief = extract_collaboration_brief(creator)
        self.assertEqual(brief["primary_subtype"], "creator")
        self.assertIn("@photo_pro", brief["contact_values"])
        self.assertEqual(extract_business_cooperation("Хочу футболку зі своїм принтом"), {})

    def test_classification_and_required_policy_follow_current_business_signal(self):
        from management.services.bot_instruction_routing import turn_triggers
        from management.services.bot_sales_classifier import _interaction_type
        from management.services.ig_required_policy import derive_required_scenarios

        client = SimpleNamespace(stage="new", is_blocked=False)
        for text in (SHOP_REQUEST, "Хочу відкрити магазин, маю кілька питань", "Interested in dropshipping"):
            with self.subTest(text=text), patch(
                "management.services.bot_payment_truth.client_has_confirmed_purchase", return_value=False
            ):
                self.assertEqual(_interaction_type(client, {}, text, "user"), "wholesale_b2b")
                self.assertIn("collaboration", derive_required_scenarios(semantic_triggers=turn_triggers(text)))
        for text in ('«Цікавить опт та співпраця»', "Співпраця вже не цікавить"):
            with self.subTest(text=text):
                self.assertNotIn("collaboration", turn_triggers(text))

    def test_bundle_manager_reason_and_brief_preserve_mixed_retail_question(self):
        from management.services.ig_revision_intents import collaboration_brief_for_revision, manager_case_reason

        value = revision("Хочу відкрити магазин", "Які умови співпраці?", "І собі хочу худі L — яка ціна?")
        self.assertEqual(manager_case_reason(value), "collaboration_review")
        brief = collaboration_brief_for_revision(value)
        self.assertEqual(brief["cooperation_kind"], "business")
        self.assertIn("partner_price", brief["business_cooperation"]["requested_topics"])
        self.assertEqual(value.bundle_snapshot["sources"][2]["text"], "І собі хочу худі L — яка ціна?")
        withdrawn = revision("Цікавить співпраця", "Співпраця вже не цікавить")
        with patch("management.services.ig_product_inquiries.product_inquiries_for_revision", return_value={}):
            self.assertNotEqual(manager_case_reason(withdrawn), "collaboration_review")

    def test_non_user_sources_cannot_open_business(self):
        self.assertEqual(business_cooperation_for_sources([
            {"role": "model", "message_id": 1, "text": SHOP_REQUEST},
            {"role": "manager", "message_id": 2, "text": SHOP_REQUEST},
        ]), {})

    def test_unspecified_partnership_holding_asks_one_format_question_without_creator_requirements(self):
        from management.services.ig_revision_holding import _collaboration_holding_reply

        for text in ("Добрий день! Можна щодо співпраці?", "Хочу співпрацювати, можна кілька запитань?",
                     "Можно насчёт сотрудничества?", "May I ask about a partnership?"):
            with self.subTest(text=text):
                brief = extract_business_cooperation(text)
                reply = _collaboration_holding_reply("uk", business_cooperation=brief)
                self.assertIn("Який формат співпраці вас цікавить?", reply)
                self.assertEqual(reply.count("?"), 1)
                self.assertNotIn("ваші запитання", reply)
                self.assertNotIn("додаткові", reply)
                self.assertNotRegex(reply.lower(), r"портфол|результат|фото|магазин")

    def test_store_question_invitation_and_existing_five_questions_are_different_replies(self):
        from management.services.ig_revision_holding import _collaboration_holding_reply

        for text in ("Хочу відкрити магазин, можна поставити запитання?",
                     "Хочу запустити Instagram-магазин, маю кілька запитань"):
            with self.subTest(text=text):
                reply = _collaboration_holding_reply("uk", business_cooperation=extract_business_cooperation(text))
                self.assertIn("Напишіть, будь ласка, свої запитання тут", reply)
                self.assertNotIn("Зафіксувала ваші запитання", reply)
                self.assertNotIn("Який формат", reply)
        reply = _collaboration_holding_reply("uk", business_cooperation=extract_business_cooperation(SHOP_REQUEST))
        self.assertIn("Зафіксувала ваші запитання", reply)
        self.assertNotIn("Напишіть, будь ласка, свої запитання тут", reply)
        self.assertNotRegex(reply.lower(), r"портфол|фото-|результат|який формат")


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class BusinessCooperationManagerCaseTests(TransactionTestCase):
    def setUp(self):
        from management.models import IgClient, IgCustomerTurn, IgTurnMessage, InstagramBotMessage, InstagramBotSettings
        from management.services.ig_turn_revisions import (
            claim_revision_preparation, create_collecting_revision, seal_revision,
        )

        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True)
        self.customer = IgClient.objects.create(igsid="business-case", language="uk")
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", mid="business-source", text=SHOP_REQUEST,
            provider_namespace="instagram_login:business-owner")
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        self.revision = create_collecting_revision(turn, [source], bypass_quiet=True).revision
        preparation = claim_revision_preparation(self.revision.pk)
        sealed = seal_revision(self.revision.pk, preparation.token)
        self.assertTrue(sealed.sealed, sealed.reason)
        self.revision = sealed.revision
        proposal = {"response": {"reply_text": "Дякую, ставте запитання.", "controls": []},
            "authority": {"allowed_actions": ["manager_escalation_intent"]},
            "execution_binding": {"settings_id": self.settings.pk, "settings_permission_epoch": 1},
            "policy_manifest": {"instruction_publication": {"id": 1, "version": 1, "hash": "bound"}},
            "sources": [{"message_id": source.pk, "source_digest": self.revision.bundle_snapshot["sources"][0]["source_digest"]}]}
        self.proposal = proposal
        self.source_id = source.pk

    def ensure(self):
        from management.services.ig_revision_intents import _digest, ensure_revision_manager_case

        if not self.revision.generation_proposal_digest:
            self.revision.generation_proposal = self.proposal
            self.revision.generation_proposal_digest = _digest(self.proposal)
            self.revision.save(update_fields=["generation_proposal", "generation_proposal_digest"])

        with patch("management.services.ig_revision_intents._readiness", return_value=SimpleNamespace(ready=True)), patch(
            "requests.sessions.Session.request", side_effect=AssertionError("network forbidden")
        ):
            return ensure_revision_manager_case(self.revision.pk, "local-test-token", settings_id=self.settings.pk)

    def test_business_case_has_real_queued_notification_source_and_business_decisions(self):
        from management.models import IgBotNotification, IgFollowUpTask

        result = self.ensure()
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        notification = IgBotNotification.objects.get(pk=result.notification_id)
        self.assertEqual(task.reason, "revision_case:collaboration_review")
        self.assertEqual(task.manager_context["cooperation_kind"], "business")
        self.assertEqual(task.manager_context["sources"][0]["message_id"], self.source_id)
        self.assertEqual(task.manager_context["required_decisions"], [
            "business_cooperation_review", "partnership_terms", "customer_questions"])
        self.assertNotIn("портфоліо", task.message_text)
        self.assertEqual(notification.payload["manager_task_id"], task.pk)
        self.assertEqual(notification.status, "pending")
        self.assertEqual(notification.attempts, 0)
        self.assertTrue(self.ensure().replayed)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_missing_manager_authority_cannot_create_case(self):
        from management.models import IgFollowUpTask
        self.proposal["authority"]["allowed_actions"] = []
        self.assertEqual(self.ensure().reason, "manager_action_not_authorized")
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_notification_failure_rolls_back_case_and_handoff_receipt(self):
        from management.models import IgFollowUpTask

        with patch("management.services.instagram_bot.notify_manager", return_value=False):
            with self.assertRaisesMessage(RuntimeError, "manager_notification_not_recorded"):
                self.ensure()
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.revision.refresh_from_db()
        self.assertNotIn("manager_handoff", self.revision.action_receipts)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=False)
class BusinessCooperationPublishedPolicyTests(TestCase):
    def setUp(self):
        from management.models import InstagramBotSettings
        from management.knowledge.scenarios import reviewed_scenario_values
        from management.services.ig_policy_publication import draft_state, save_instruction_draft
        from management.tests_ig_policy_helpers import (
            ensure_test_instruction_publication, publish_current_instructions,
        )

        ensure_test_instruction_publication()
        self.settings = InstagramBotSettings.load()
        state = draft_state()
        self.values = reviewed_scenario_values("collaboration")
        self.instruction, _state = save_instruction_draft(expected_revision=state.revision,
            expected_snapshot_hash=state.snapshot_hash, values=self.values, note="Disposable business policy test")
        self.publication = publish_current_instructions()
        self.settings.refresh_from_db()

    def assert_business_policy(self, prompt):
        # These are behavioral constraints, checked in the actually selected
        # compiled policy rather than only in the source proposal dictionary.
        self.assertIn("Для магазину/опту/dropshipping не проси портфоліо", prompt)
        self.assertIn("не проси повторити запитання", prompt)
        self.assertIn("партнерську ціну, розміри/кольори, старт без закупівлі партії", prompt)
        self.assertIn("Instagram-продажі/доставку та право використання фото", prompt)
        self.assertIn("Для creator/дизайнерської/фото-відео пропозиції постав щонайбільше одне питання лише про відсутню необхідну деталь", prompt)
        self.assertIn("Спочатку відповідай на вже відомі актуальні питання", prompt)
        self.assertIn("Якщо даних достатньо, відповідь без запитань є повною", prompt)
        self.assertIn("підтвердженим сервером case/receipt", prompt)
        self.assertIn("не стверджуй, що менеджер уже прочитав", prompt)
        self.assertIn("не обіцяй оплату, бартер, безкоштовні речі чи строк відповіді", prompt)
        self.assertIn("не призначай автоматичний процент", prompt)
        self.assertIn("не припускай ані creator, ані магазин", prompt)
        self.assertIn("Який формат співпраці вас цікавить?", prompt)
        self.assertIn("не стверджуй, що запитання вже надані", prompt)

    def test_selected_required_module_reaches_real_compiler_with_business_and_creator_boundaries(self):
        from management.services.bot_playbooks import active_instruction_selection
        from management.services.instagram_bot import assemble_system_instruction

        selection = active_instruction_selection(turn_text=SHOP_REQUEST,
            captured_tags={"uk", "collaboration"}, publication_snapshot=self.publication)
        identity = f"instruction:{self.instruction.pk}"
        self.assertEqual(selection.required_ids, (identity,))
        self.assertEqual(selection.required_policy["covered_scenarios"], ["collaboration"])
        metadata = {}
        prompt = assemble_system_instruction(self.settings, turn_text=SHOP_REQUEST,
            captured_policy_tags={"uk", "collaboration"}, compiled_metadata=metadata,
            instruction_publication=self.publication)
        self.assert_business_policy(prompt)
        self.assertEqual(prompt.count(self.values["body"]), 1)
        self.assertIn(identity, metadata["required_ids"])
        self.assertEqual(metadata["instruction_selection"]["required_policy"]["required_ids"], [identity])
        self.assertEqual(metadata["instruction_publication"]["hash"], self.publication.snapshot_hash)

    def test_real_gemini_payload_contains_reviewed_business_policy_and_original_questions(self):
        from management.services.instagram_bot import gemini_generate
        from management.services.gemini_accounting_contract import sanitize_request_policy_manifest

        response = {"parsed": {"reply_text": "Дякую! Питання щодо партнерської ціни, розмірів і кольорів, "
            "старту без закупівлі, Instagram-продажів, доставки та використання фото збережені для розгляду адміністрацією.",
            "controls": []}, "model": "gemini-test", "usage": {},
            "meta": {"key": "test", "reasoning_task": "customer_chat"}}
        with patch("management.services.call_ai_analysis.gemini_generate_text", return_value=response) as provider, patch(
            "requests.sessions.Session.request", side_effect=AssertionError("network forbidden")
        ):
            result = gemini_generate(self.settings, [{"role": "user", "text": SHOP_REQUEST}],
                captured_policy_tags={"uk", "collaboration"})
        self.assertTrue(result.valid)
        self.assertEqual(provider.call_count, 1)
        payload = provider.call_args.args[0]
        prompt = payload["system_instruction"]["parts"][0]["text"]
        self.assert_business_policy(prompt)
        self.assertIn(SHOP_REQUEST, str(payload["contents"]))
        manifest = provider.call_args.kwargs["request_policy_manifest"]
        identity = f"instruction:{self.instruction.pk}"
        self.assertIn(identity, manifest["required_ids"])
        self.assertEqual(manifest["instruction_selection"]["required_policy"]["required_ids"], [identity])
        self.assertEqual(manifest, sanitize_request_policy_manifest(manifest))

    def test_reviewed_publisher_updates_existing_scenario_id_by_body_without_version_adapter(self):
        from management.models import BotInstruction
        from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values
        from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
        from management.services.ig_policy_publication import draft_state, save_instruction_draft
        from management.tests_ig_policy_helpers import publish_current_instructions

        state = draft_state()
        old_values = {**self.values, "body": "Лише на власну актуальну пропозицію співпраці: попроси портфоліо."}
        _instruction, state = save_instruction_draft(expected_revision=state.revision,
            expected_snapshot_hash=state.snapshot_hash, values=old_values,
            instruction_id=self.instruction.pk, note="Disposable old published collaboration body")
        for row in SCENARIO_INSTRUCTIONS:
            if row["key"] == "collaboration":
                continue
            _instruction, state = save_instruction_draft(expected_revision=state.revision,
                expected_snapshot_hash=state.snapshot_hash, values=reviewed_scenario_values(row["key"]),
                note="Disposable unchanged owner scenario")
        unrelated = BotInstruction.objects.create(title="Unrelated reviewed instruction", body="Keep this exact body",
            intent_tags="delivery", is_active=True)
        publish_current_instructions()
        before = publish_reviewed_owner_scenarios()
        changes = {row["key"]: row for row in before["changes"]}
        self.assertEqual(changes["collaboration"]["action"], "update")
        self.assertEqual(changes["collaboration"]["instruction_id"], self.instruction.pk)
        self.assertTrue(all(row["action"] == "unchanged" for key, row in changes.items() if key != "collaboration"))
        result = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=before["draft_hash"], expected_head_hash=before["head_hash"])
        self.assertTrue(result["changed"])
        self.instruction.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(self.instruction.body, self.values["body"])
        self.assertEqual(unrelated.body, "Keep this exact body")
        self.assertTrue(all(row["action"] == "unchanged" for row in publish_reviewed_owner_scenarios()["changes"]))
