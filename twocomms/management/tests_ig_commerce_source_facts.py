"""Retained synthetic client352 source regressions, with no external I/O."""
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from unittest.mock import patch
import os

from management.models import IgCommerceTurnDecision
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn, understand_turn
from management.services.ig_commerce_source_identity import resolve_source_product_request
from management.services.ig_revision_commerce import synchronize_selected_session
from management.tests_ig_commerce_state import CommerceStateFixture


class SourceSizeSemanticsTests(SimpleTestCase):
    def test_multilingual_aliases_and_explicit_corrections(self):
        for text, expected in (("L", "L"), ("l", "L"), ("л", "L"),
            ("Хочу футболку розмір л", "L"), ("розмір хл", "XL"),
            ("не L, а XL", "XL"), ("Not L, but XL", "XL"), ("2xl", "XXL")):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).field_updates.get("size"), expected)

    def test_unaffirmed_sizes_cannot_be_promoted_by_model_hint(self):
        for text in ("M або L", "M or L", "M/L", "L?", "«L» написано в описі", "L size guide", "не L", "which size fits?"):
            with self.subTest(text=text):
                self.assertNotIn("size", parse_turn(text).field_updates)
                self.assertNotIn("size", understand_turn(text, model_payload={"size": "L"}).field_updates)

    def test_word_apostrophes_do_not_end_quoted_source_preferences(self):
        from management.services.ig_commerce_turns import _is_quoted_preference
        for text in ("Він написав 'I'd like to buy tshirt size L'.",
            "Він написав ‘I’m buying tshirt size L’.", "Він написав ‘I’d like to buy tshirt size L’.",
            "Він написав \"I'd like to buy tshirt size L\".", "Він написав «I'd like to buy tshirt size L»."):
            with self.subTest(text=text):
                size_start = text.rindex("L")
                self.assertTrue(_is_quoted_preference(text, size_start, size_start + 1))
                for request in (parse_turn(text), understand_turn(text, model_payload={"size": "L"})):
                    self.assertNotIn("size", request.field_updates)
                    self.assertFalse(request.purchase_requested)
        request = parse_turn("I'd like to buy tshirt size L")
        self.assertEqual(request.field_updates.get("size"), "L")
        self.assertTrue(request.purchase_requested)

    def test_contracted_size_negation_abstains_or_withdraws_by_source_meaning(self):
        for text in ("I don't want size L", "I don’t want size L"):
            with self.subTest(text=text):
                for request in (parse_turn(text), understand_turn(text, model_payload={"size": "L"})):
                    self.assertNotIn("size", request.field_updates)
                    self.assertEqual(request.preference_withdrawals.get("size"), "L")
        for text in ("I haven't requested size L", "I haven’t requested size L"):
            with self.subTest(text=text):
                for request in (parse_turn(text), understand_turn(text, model_payload={"size": "L"})):
                    self.assertNotIn("size", request.field_updates)
                    self.assertNotIn("size", request.preference_withdrawals)
        for text in ("I don't want size L, but XL", "I don’t want size L, choose XL"):
            with self.subTest(text=text):
                request = understand_turn(text, model_payload={"size": "L"})
                self.assertEqual(request.field_updates.get("size"), "XL")
                self.assertEqual(request.preference_withdrawals.get("size"), "L")

    def test_purchase_intent_is_source_bound_and_separate_from_checkout(self):
        for text in ("Хочу замовити", "Хочу заказать", "I want to order", "I'd like to buy"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertTrue(request.purchase_requested)
                self.assertFalse(request.checkout_requested)
        for text in ("Да", "Не хочу заказать", "«хочу заказать» написано в описании", "I want to order?"):
            self.assertFalse(parse_turn(text).purchase_requested)
        self.assertFalse(understand_turn("Да", model_payload={"checkout_requested": True}).purchase_requested)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class SourceFactStateTests(CommerceStateFixture, TestCase):
    def setUp(self):
        super().setUp()
        from management.models import InstagramBotSettings
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings_row = InstagramBotSettings.objects.create(pk=1, ig_user_id="source-state-owner")

    def source(self, text):
        from management.services.instagram_bot import ingress_provider_namespace
        self._source_counter = getattr(self, "_source_counter", 0) + 1
        source = self.message(f"{text} #{self._source_counter}")
        source.text = text
        source.source = "webhook"
        source.provider_namespace = ingress_provider_namespace(self.settings_row)
        source.save(update_fields=["source", "text", "provider_namespace"])
        return source

    def test_size_before_identity_preserves_original_transition_and_episode(self):
        size_source = self.source("Хочу футболку розмір л")
        first = apply_turn(self.client, size_source, parse_turn(size_source.text), reply_payload={})
        session = first.session
        line_id = session.lines[0]["line_id"]
        name_source = self.source("Reality Bends")
        request = resolve_source_product_request(self.client, name_source, parse_turn(name_source.text))
        selected = apply_turn(self.client, name_source, request, reply_payload={})
        session.refresh_from_db()
        self.client.refresh_from_db()
        self.assertEqual(session.lines[0]["product_id"], self.reality.pk)
        self.assertEqual(session.lines[0]["size"], "L")
        self.assertEqual(session.lines[0]["line_id"], line_id)
        self.assertEqual(session.commercial_episode_id, self.client.current_commercial_episode_id)
        self.assertEqual(session.revision, 2)
        self.assertEqual(selected.transition.previous_snapshot["lines"][0]["size"], "L")
        self.assertEqual(first.result_payload["source_facts"]["source_message_id"], size_source.pk)

    def _legacy_category_then_first_size(self):
        from copy import deepcopy
        from management.models import IgCommercialEpisode, IgCommerceSelectionSession, IgCommerceSelectionTransition
        from management.services.ig_commerce_state import _apply_snapshot, _create_decision, _request_payload
        category = self.source("Хочу футболку")
        # Historical reducers kept the category in constraints before creating
        # a position. Build that old append-only receipt at INSERT, then use
        # the current reducer for the size and subsequent real operations.
        episode = IgCommercialEpisode.objects.create(client=self.client, sequence=1, open_slot=1)
        self.client.current_commercial_episode = episode
        self.client.save(update_fields=['current_commercial_episode'])
        session = IgCommerceSelectionSession.objects.create(client=self.client, commercial_episode=episode,
            generation=1, open_slot=1, state='open')
        def historical(source, *, accepted, action, garment=None):
            before = session.snapshot();after = deepcopy(before)
            if garment:after['query_constraints']['garment_type'] = garment
            after['revision'] = before['revision'] + 1
            after['last_provider_message_id'] = source.mid
            transition = IgCommerceSelectionTransition.objects.create(session=session, source_message=source,
                from_revision=before['revision'], to_revision=after['revision'], action=action,
                previous_snapshot=before, next_snapshot=after)
            _apply_snapshot(session, after, event_at=source.provider_created_at, event_id=source.mid)
            return _create_decision(source_message=source, session=session, transition=transition,
                request_payload=_request_payload(parse_turn(source.text)), result_payload={'reason':action},
                accepted=accepted, is_stale=False, delivery_required=False, delivery_state='not_required')
        historical(category, accepted=True, action='query_constraints_updated', garment='tshirt')
        unresolved = self.source("Допоможіть визначити модель")
        historical(unresolved, accepted=False, action='turn_unresolved')
        size = self.source("L")
        apply_turn(self.client, size, parse_turn(size.text), reply_payload={})
        self.client.refresh_from_db()
        return category, size

    def test_legacy_category_before_first_line_retains_its_own_source_through_unresolved_turns(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        category, size = self._legacy_category_then_first_size()
        captured = capture_current_selection_lines(self.client.pk)
        self.assertTrue(captured['coverage_complete'], captured)
        row = captured['lines'][0]
        self.assertEqual(row['fields']['garment_type']['value'], 'tshirt')
        self.assertEqual(row['evidence']['garment_type']['source_message_id'], category.pk)
        self.assertEqual(row['fields']['size']['value'], 'L')
        self.assertEqual(row['evidence']['size']['source_message_id'], size.pk)

    def test_old_accepted_receipts_without_source_fact_extension_keep_exact_category_source(self):
        from management.services import ig_commerce_state as reducer
        from management.services.ig_commerce_projection import capture_current_selection_lines
        create = reducer._create_decision
        def older_receipt(*args, **kwargs):
            payload = dict(kwargs['result_payload'])
            payload.pop('source_facts', None)
            return create(*args, **{**kwargs, 'result_payload': payload})
        with patch.object(reducer, '_create_decision', side_effect=older_receipt):
            category, size = self._legacy_category_then_first_size()
        captured = capture_current_selection_lines(self.client.pk)
        row = captured['lines'][0]
        self.assertEqual(row['fields']['garment_type']['value'], 'tshirt')
        self.assertEqual(row['evidence']['garment_type']['source_message_id'], category.pk)
        self.assertEqual(row['evidence']['size']['source_message_id'], size.pk)

    def test_legacy_first_category_remains_on_first_position_when_hoodie_is_added(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        category, _ = self._legacy_category_then_first_size()
        sibling = self.source('добавьте худи размер M для друга')
        apply_turn(self.client, sibling, parse_turn(sibling.text), reply_payload={})
        captured = capture_current_selection_lines(self.client.pk)
        self.assertTrue(captured['coverage_complete'], captured)
        self.assertEqual([r['fields']['garment_type']['value'] for r in captured['lines']], ['tshirt','hoodie'])
        self.assertEqual(captured['lines'][0]['evidence']['garment_type']['source_message_id'], category.pk)
        self.assertEqual(captured['lines'][1]['evidence']['garment_type']['source_message_id'], sibling.pk)

    def test_superseded_legacy_category_is_not_restored_by_later_sibling(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        old, _ = self._legacy_category_then_first_size()
        replacement = self.source('Хочу худі')
        apply_turn(self.client, replacement, parse_turn(replacement.text), reply_payload={})
        sibling = self.source('добавьте футболку размер M для друга')
        apply_turn(self.client, sibling, parse_turn(sibling.text), reply_payload={})
        captured = capture_current_selection_lines(self.client.pk)
        self.assertTrue(captured['coverage_complete'], captured)
        self.assertEqual(captured['lines'][0]['fields']['garment_type']['value'], 'hoodie')
        self.assertEqual(captured['lines'][0]['evidence']['garment_type']['source_message_id'], replacement.pk)
        self.assertNotEqual(captured['lines'][0]['evidence']['garment_type']['source_message_id'], old.pk)

    def test_named_ambiguity_keeps_size_and_requires_one_identity_question(self):
        source = self.source("Reality Bends або Classic, розмір L")
        request = resolve_source_product_request(self.client, source, parse_turn(source.text))
        self.assertIsNone(request.exact_product_id)
        self.assertNotIn("fit", request.field_updates)
        decision = apply_turn(self.client, source, request, reply_payload={})
        self.assertEqual(decision.session.lines[0]["size"], "L")
        self.assertNotIn("fit_option_code", decision.session.lines[0])
        self.assertNotIn("fit", decision.result_payload["source_facts"]["values"])
        self.assertEqual(decision.session.pending_clarification, "which_product")

    def test_named_choice_with_independent_price_question_selects_current_source(self):
        original = self.source("Third розмір M")
        apply_turn(self.client, original,
            resolve_source_product_request(self.client, original, parse_turn(original.text)), reply_payload={})
        source = self.source("Беру Classic розмір L. Скільки коштує?")
        request = resolve_source_product_request(self.client, source, parse_turn(source.text))
        self.assertEqual(request.exact_product_id, self.classic.pk)
        self.assertNotIn("fit", request.field_updates)
        decision = apply_turn(self.client, source, request, reply_payload={})
        self.assertEqual(decision.session.lines[0]["product_id"], self.classic.pk)
        self.assertEqual(decision.session.lines[0]["size"], "L")
        self.assertNotIn("fit_option_code", decision.session.lines[0])
        self.assertTrue(decision.session.query_constraints["purchase_requested"])
        self.assertEqual(decision.result_payload["source_facts"]["source_message_id"], source.pk)
        self.assertEqual(decision.result_payload["source_facts"]["binding"]["product_resolution"], "exact_name")

    def test_title_overlap_preserves_explicit_fit_and_non_title_fit(self):
        for text, expected in (("Classic, крій Classic розмір L", "classic"),
            ("Classic, крій oversize розмір L", "oversize")):
            with self.subTest(text=text):
                source = self.source(text)
                request = resolve_source_product_request(self.client, source, parse_turn(source.text))
                self.assertEqual(request.field_updates.get("fit"), expected)
                decision = apply_turn(self.client, source, request, reply_payload={})
                self.assertEqual(decision.session.lines[0]["fit_option_code"], expected)
        original = self.source("Third")
        apply_turn(self.client, original,
            resolve_source_product_request(self.client, original, parse_turn(original.text)), reply_payload={})
        fit_source = self.source("крій classic")
        request = resolve_source_product_request(self.client, fit_source, parse_turn(fit_source.text))
        self.assertIsNone(request.exact_product_id)
        self.assertEqual(request.field_updates.get("fit"), "classic")
        decision = apply_turn(self.client, fit_source, request, reply_payload={})
        self.assertEqual(decision.session.lines[0]["fit_option_code"], "classic")
        self.assertEqual(decision.session.lines[0]["product_id"], self.third.pk)

    def test_named_questions_and_reports_leave_current_identity_unchanged(self):
        original = self.source("Third розмір L")
        apply_turn(self.client, original, resolve_source_product_request(self.client, original, parse_turn(original.text)), reply_payload={})
        for text in ("У рекламі написано «Reality Bends», це назва принта?", "Classic?", "«Reality Bends» написано в описі", "«Reality Bends»"):
            with self.subTest(text=text):
                source = self.source(text)
                request = resolve_source_product_request(self.client, source, parse_turn(source.text))
                self.assertIsNone(request.exact_product_id)
                decision = apply_turn(self.client, source, request, reply_payload={})
                self.assertEqual(decision.session.lines[0]["product_id"], self.third.pk)
                self.assertEqual(decision.session.lines[0]["size"], "L")
                self.assertEqual(decision.session.pending_clarification, "which_product")
        chosen = self.source("Reality Bends")
        request = resolve_source_product_request(self.client, chosen, parse_turn(chosen.text))
        self.assertEqual(request.exact_product_id, self.reality.pk)

    def test_quoted_affirmatives_and_negative_contractions_do_not_choose_named_identity(self):
        original = self.source("Third розмір L")
        apply_turn(self.client, original,
            resolve_source_product_request(self.client, original, parse_turn(original.text)), reply_payload={})
        for text in ("Він написав 'I want Classic'", "'I want Classic'", "‘I'd like to buy Classic’", "He said ‘I'd like to buy Classic’",
            "Він написав ‘I’m choosing Classic’", "I don't want Classic", "I don’t want Classic",
            "I haven't requested Classic", "I haven’t requested Classic"):
            with self.subTest(text=text):
                source = self.source(text)
                request = resolve_source_product_request(self.client, source,
                    understand_turn(text, model_payload={"size": "XL"}))
                self.assertIsNone(request.exact_product_id)
                self.assertFalse(request.purchase_requested)
                decision = apply_turn(self.client, source, request, reply_payload={})
                self.assertEqual(decision.session.lines[0]["product_id"], self.third.pk)
                self.assertEqual(decision.session.lines[0]["size"], "L")
        source = self.source("Хочу «Classic»")
        request = resolve_source_product_request(self.client, source, parse_turn(source.text))
        self.assertEqual(request.exact_product_id, self.classic.pk)
        decision = apply_turn(self.client, source, request, reply_payload={})
        self.assertEqual(decision.session.lines[0]["product_id"], self.classic.pk)

    def test_reported_unrequested_size_preserves_choice_and_rejection_withdraws_it(self):
        source = self.source("L")
        apply_turn(self.client, source, parse_turn(source.text), reply_payload={})
        report = self.source("I haven’t requested size L")
        decision = apply_turn(self.client, report, understand_turn(report.text, model_payload={"size": "XL"}), reply_payload={})
        self.assertEqual(decision.session.lines[0]["size"], "L")
        self.assertNotIn("preference_withdrawal", decision.result_payload)
        rejected = self.source("I don’t want size L")
        decision = apply_turn(self.client, rejected, understand_turn(rejected.text, model_payload={"size": "L"}), reply_payload={})
        self.assertNotIn("size", decision.session.lines[0])
        self.assertEqual(decision.result_payload["preference_withdrawal"]["values"], {"size": "L"})

    def test_product_switch_and_recipient_change_do_not_inherit_size_or_intent(self):
        first = self.source("Хочу заказать Classic размер L")
        decision = apply_turn(self.client, first, resolve_source_product_request(self.client, first, parse_turn(first.text)), reply_payload={})
        second = self.source("Reality Bends")
        switched = apply_turn(self.client, second, resolve_source_product_request(self.client, second, parse_turn(second.text)), reply_payload={})
        self.assertNotIn("size", switched.session.lines[0])
        self.assertNotIn("purchase_requested", switched.session.query_constraints)
        third = self.source("Для друга размер M")
        recipient = apply_turn(self.client, third, parse_turn(third.text), reply_payload={})
        self.assertEqual(recipient.session.lines[0]["recipient_id"], "friend")
        self.assertEqual(recipient.session.lines[0]["size"], "M")
        self.assertNotIn("product_id", recipient.session.lines[0])

    def test_current_explicit_recipient_survives_product_switch(self):
        for recipient_text, previous_recipient in (("Для друга", "friend"), ("Для мами", "mother")):
            with self.subTest(previous_recipient=previous_recipient):
                first = self.source(f"{recipient_text} Classic розмір L")
                before = apply_turn(self.client, first,
                    resolve_source_product_request(self.client, first, parse_turn(first.text)), reply_payload={})
                self.assertEqual(before.session.lines[0]["recipient_id"], previous_recipient)
                second = self.source("Для друга Reality Bends розмір M")
                switched = apply_turn(self.client, second,
                    resolve_source_product_request(self.client, second, parse_turn(second.text)), reply_payload={})
                self.assertEqual(switched.session.lines[0]["product_id"], self.reality.pk)
                self.assertEqual(switched.session.lines[0]["recipient_id"], "friend")
                self.assertEqual(switched.session.lines[0]["size"], "M")
                self.assertEqual(switched.result_payload["source_facts"]["values"]["recipient_id"], "friend")
                self.assertEqual(switched.result_payload["source_facts"]["source_message_id"], second.pk)
                self.assertEqual(switched.transition.next_snapshot["lines"][0]["recipient_id"], "friend")

    def test_replay_keeps_one_decision_and_continuous_transition(self):
        source = self.source("Хочу замовити розмір L")
        first = apply_turn(self.client, source, parse_turn(source.text), reply_payload={})
        second = apply_turn(self.client, source, parse_turn(source.text), reply_payload={})
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.session.transitions.count(), 1)
        self.assertTrue(first.session.query_constraints["purchase_requested"])
        self.assertFalse(first.delivery_required)

    def test_size_rejection_clears_choice_and_explicit_correction_replaces_it(self):
        first = self.source("L")
        apply_turn(self.client, first, parse_turn(first.text), reply_payload={})
        rejected = self.source("не L")
        decision = apply_turn(self.client, rejected, parse_turn(rejected.text), reply_payload={})
        self.assertNotIn("size", decision.session.lines[0])
        self.assertEqual(decision.result_payload["preference_withdrawal"]["values"], {"size": "L"})
        corrected = self.source("не L а XL")
        decision = apply_turn(self.client, corrected, parse_turn(corrected.text), reply_payload={})
        self.assertEqual(decision.session.lines[0]["size"], "XL")

    def test_additional_unpaid_item_preserves_previous_episode_and_distinct_size(self):
        first = self.source("Classic размер L")
        old = apply_turn(self.client, first, resolve_source_product_request(self.client, first, parse_turn(first.text)), reply_payload={})
        second = self.source("Хочу ще одну розмір M")
        repeat = apply_turn(self.client, second, parse_turn(second.text), reply_payload={})
        self.assertEqual(old.session.commercial_episode_id, repeat.session.commercial_episode_id)
        self.assertEqual(old.session_id, repeat.session_id)
        self.assertEqual(repeat.session.lines[0]["product_id"], self.classic.pk)
        self.assertEqual(repeat.session.lines[0]["size"], "L")
        self.assertEqual(repeat.session.lines[1]["product_id"], self.classic.pk)
        self.assertEqual(repeat.session.lines[1]["size"], "M")
        self.assertTrue(repeat.session.query_constraints["purchase_requested"])
        self.assertFalse(repeat.session.commercial_episode.repeat_evidence_message_ids)
        old.session.refresh_from_db()
        self.assertEqual(old.session.open_slot, 1)

    def test_legacy_selection_synchronization_noop_or_visible_source_refusal(self):
        source = self.source("Classic размер L")
        request = resolve_source_product_request(self.client, source, parse_turn(source.text))
        decision = apply_turn(self.client, source, request, reply_payload={})
        self.client.refresh_from_db()
        with transaction.atomic():
            result = synchronize_selected_session(self.client, source_message_id=source.pk)
        self.assertEqual(result["before"], result["after"])
        self.client.current_size = "XL"
        self.client.save(update_fields=["current_size"])
        with transaction.atomic():
            authorized = synchronize_selected_session(self.client, source_message_id=source.pk)
        self.assertEqual(authorized["before"]["revision"] + 1, authorized["after"]["revision"])
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)
        self.assertEqual(decision.session.transitions.filter(source_message=source).count(), 2)
        self.client.current_size = "M"
        self.client.save(update_fields=["current_size"])
        with self.assertRaisesRegex(ValueError, "selection_source_already_reduced_or_missing"):
            with transaction.atomic():
                synchronize_selected_session(self.client, source_message_id=source.pk)
        decision.session.refresh_from_db()
        self.assertEqual(decision.session.revision, 2)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class SourceAdmissionTests(TransactionTestCase):
    def fixture(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        fixture = RevisionLiveTests(methodName="runTest")
        fixture.setUp()
        fixture._prepare()
        return fixture

    def reduce(self, fixture):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_revision_outbox import PublicationBinding
        return reduce_revision_commerce(fixture.revision.pk, fixture.token,
            settings_id=fixture.settings.pk, settings_permission_epoch=fixture.settings.reply_permission_epoch,
            publication=PublicationBinding(fixture.publication.pk, fixture.publication.version, fixture.publication.snapshot_hash))

    def test_erasure_and_reset_fence_incoming_fact_admission(self):
        from management.models import IgFunnelResetAudit
        fixture = self.fixture()
        fixture.customer.privacy_erasure_started_at = timezone.now()
        fixture.customer.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.reduce(fixture).reason, "client_erasure_changed")
        self.assertFalse(IgCommerceTurnDecision.objects.exists())
        fixture.customer.privacy_erasure_started_at = None
        fixture.customer.save(update_fields=["privacy_erasure_started_at"])
        IgFunnelResetAudit.objects.create(client=fixture.customer, reset_after_message_id=fixture.source.pk, reason="synthetic source reset")
        self.assertEqual(self.reduce(fixture).reason, "commerce_source_before_scope")
        self.assertFalse(IgCommerceTurnDecision.objects.exists())

    def test_disabled_ai_and_manager_pause_still_admit_incoming_facts(self):
        fixture = self.fixture()
        fixture.customer.bot_paused = True
        fixture.customer.manager_takeover = True
        fixture.customer.save(update_fields=["bot_paused", "manager_takeover"])
        fixture.settings.ai_enabled = False
        fixture.settings.is_enabled = False
        fixture.settings.save(update_fields=["ai_enabled", "is_enabled"])
        result = self.reduce(fixture)
        self.assertTrue(result.ready, result.reason)
        decision = IgCommerceTurnDecision.objects.get(pk=result.decisions[0]["decision_id"])
        self.assertFalse(decision.delivery_required)
        fixture.customer.refresh_from_db()
        self.assertTrue(fixture.customer.bot_paused)
        self.assertTrue(fixture.customer.manager_takeover)

    def test_sealed_receipt_replay_rechecks_original_source_binding(self):
        from datetime import timedelta
        fixture = self.fixture()
        fixture._replace_bundle(["Хочу футболку розмір L"])
        source = fixture.revision.sources.get().message
        first = self.reduce(fixture)
        self.assertTrue(first.ready, first.reason)
        decision = IgCommerceTurnDecision.objects.get(pk=first.decisions[0]["decision_id"])
        before = decision.session.snapshot()
        for field, replacement in (("text", "не L"), ("provider_namespace", "foreign-owner"),
            ("reply_to_provider_message_id", "foreign-reply"), ("mid", "changed-mid"),
            ("provider_created_at", (source.provider_created_at or timezone.now()) + timedelta(seconds=1))):
            with self.subTest(field=field):
                original = getattr(source, field)
                setattr(source, field, replacement)
                source.save(update_fields=[field])
                replay = self.reduce(fixture)
                self.assertFalse(replay.ready)
                self.assertEqual(replay.reason, "commerce_source_changed")
                decision.session.refresh_from_db()
                self.assertEqual(decision.session.snapshot(), before)
                self.assertEqual(IgCommerceTurnDecision.objects.count(), 1)
                setattr(source, field, original)
                source.save(update_fields=[field])
        replay = self.reduce(fixture)
        self.assertTrue(replay.ready, replay.reason)
        self.assertTrue(replay.replayed)

    def assert_repeat_bundle_replay(self, *, intake=False):
        from management.models import IgCommerceSelectionSession
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from storefront.models import Category, Product
        fixture = self.fixture()
        category = Category.objects.create(name="Repeat replay", slug="repeat-replay")
        Product.objects.create(title="Classic", slug="repeat-classic", category=category, price=790, status="published")
        fixture._replace_bundle(["Classic размер L", "Хочу ще одну розмір M"])
        sources = list(fixture.revision.sources.select_related("message").order_by("ordinal"))
        if intake:
            for row in sources:
                fixture.customer.refresh_from_db()
                with transaction.atomic():
                    admitted = reduce_inbound_commerce_source(fixture.customer, row.message,
                        expected_provider_namespace=row.message.provider_namespace)
                self.assertTrue(admitted.ready, admitted.reason)
        first = self.reduce(fixture)
        self.assertTrue(first.ready, first.reason)
        before = list(IgCommerceSelectionSession.objects.filter(client=fixture.customer).order_by("generation"))
        snapshots = [(row.pk, row.commercial_episode_id, row.snapshot()) for row in before]
        count = IgCommerceTurnDecision.objects.count()
        replay = self.reduce(fixture)
        self.assertTrue(replay.ready, replay.reason)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.decisions, replay.decisions)
        self.assertEqual(IgCommerceTurnDecision.objects.count(), count)
        after = list(IgCommerceSelectionSession.objects.filter(client=fixture.customer).order_by("generation"))
        self.assertEqual(snapshots, [(row.pk, row.commercial_episode_id, row.snapshot()) for row in after])
        self.assertEqual(len(after), 1)
        self.assertEqual([row["size"] for row in after[0].lines], ["L", "M"])
        self.assertEqual(after[0].lines[0]["product_id"], after[0].lines[1]["product_id"])

    def test_sealed_repeat_bundle_replay_keeps_original_source_episodes(self):
        self.assert_repeat_bundle_replay()

    def test_intake_then_sealed_repeat_bundle_reuses_same_decisions(self):
        self.assert_repeat_bundle_replay(intake=True)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_ANALYSIS_V2_MODE="off")
class SourceActualIngressTests(TestCase):
    @override_settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00")
    def test_accepted_webhook_source_survives_output_policy_gates(self):
        from management.models import IgClient, InstagramBotSettings, InstagramBotMessage
        from management.services.instagram_bot import enqueue_inbound
        settings = InstagramBotSettings.load()
        settings.allowed_senders = ""
        settings.reply_after = None
        settings.ig_user_id = "source-ingress-owner"
        settings.page_id = "source-ingress-page"
        settings.save(update_fields=["allowed_senders", "reply_after", "ig_user_id", "page_id"])
        for ordinal, gate in enumerate(("disabled", "ai_disabled", "pause", "no_reply")):
            with self.subTest(gate=gate):
                settings.is_enabled = gate != "disabled"
                settings.ai_enabled = gate != "ai_disabled"
                settings.save(update_fields=["is_enabled", "ai_enabled"])
                client = IgClient.get_or_create_for_sender(f"source-intake-{ordinal}")
                if gate == "pause":
                    client.bot_paused = True
                    client.manager_takeover = True
                    client.save(update_fields=["bot_paused", "manager_takeover"])
                with patch("management.services.bot_sales_classifier.classify_message", return_value={"interaction_type": "reaction_only" if gate == "no_reply" else "retail"}), patch("management.services.instagram_bot._schedule_inbound_analysis"), patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
                    admitted = enqueue_inbound(settings, sender_id=client.igsid,
                        text="Дякую. Розмір L", mid=f"source-intake-mid-{ordinal}", source="webhook")
                self.assertTrue(admitted)
                provider.assert_not_called()
                source = InstagramBotMessage.objects.get(mid=f"source-intake-mid-{ordinal}")
                decision = IgCommerceTurnDecision.objects.get(source_message=source)
                self.assertEqual(decision.session.lines[0]["size"], "L")
                self.assertFalse(decision.delivery_required)
                self.assertEqual(decision.delivery_state, "not_required")

    @override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00")
    def test_legacy_ingress_keeps_durable_reply_owner_in_both_materialization_modes(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services import instagram_bot
        from management.tests_ig_commerce_delivery import CommerceWorkerDeliveryTests
        fixture = CommerceWorkerDeliveryTests(methodName="test_receipted_durable_reply_skips_gemini_and_is_not_sent_twice")
        fixture.setUp()
        settings = fixture.settings
        settings.allowed_senders = ""
        settings.ig_user_id = "legacy-source-owner"
        settings.page_id = "legacy-source-page"
        settings.save(update_fields=["allowed_senders", "ig_user_id", "page_id"])
        for ordinal, persistence_only in enumerate((False, True)):
            with self.subTest(persistence_only=persistence_only):
                fixture.client = IgClient.get_or_create_for_sender(f"legacy-intake-{ordinal}")
                fixture.client.profile_fetched_at = timezone.now()
                fixture.client.save(update_fields=["profile_fetched_at"])
                with patch("management.services.bot_sales_classifier.classify_message", return_value={"interaction_type": "retail"}), patch.object(instagram_bot, "_schedule_inbound_analysis"):
                    admitted = instagram_bot.enqueue_inbound(settings, sender_id=fixture.client.igsid,
                        text=f"https://twocomms.shop/product/{fixture.product.slug}/",
                        mid=f"legacy-source-mid-{ordinal}", source="webhook", persistence_only=persistence_only)
                self.assertTrue(admitted)
                source = InstagramBotMessage.objects.get(mid=f"legacy-source-mid-{ordinal}")
                self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())
                source.status = "processing"
                source.processing_started_at = timezone.now()
                source.save(update_fields=["status", "processing_started_at"])
                with fixture._worker_patches(), patch("management.services.bot_sales_classifier.ensure_rule_classification", return_value=None) as classifier, patch.object(instagram_bot, "send_text", return_value=instagram_bot.ProviderDeliveryReceipt(True, "", "", f"legacy-receipt-{ordinal}")) as send:
                    self.assertTrue(instagram_bot._process_one(settings, source))
                decision = IgCommerceTurnDecision.objects.get(source_message=source)
                self.assertTrue(decision.delivery_required)
                self.assertEqual(decision.delivery_state, "sent")
                self.assertEqual(decision.provider_message_ids, [f"legacy-receipt-{ordinal}"])
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[2], decision.reply_payload["text"][0])
                classifier.assert_not_called()


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_ANALYSIS_V2_MODE="off")
class SourceRolloutOwnershipTests(TransactionTestCase):
    def assert_claimed_source_rollback(self, *, proposal_saved):
        import json
        from datetime import timedelta
        from management.models import IgCustomerTurnRevision, InstagramBotMessage
        from management.services import instagram_bot, ig_revision_live
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.services.ig_revision_execution import prepare_revision
        from management.tests_ig_revision_live import RevisionLiveTests
        from management.tests_ig_revision_rollout import RevisionRolloutTests

        fixture = RevisionLiveTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with self.settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00"):
            with transaction.atomic():
                admitted = reduce_inbound_commerce_source(fixture.customer, fixture.source,
                    expected_provider_namespace=fixture.source.provider_namespace)
            self.assertTrue(admitted.ready, admitted.reason)
            fixture._prepare()
            if proposal_saved:
                # Crash after the real proposal CAS and before any outbox effects.
                with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=fixture._generate), patch("management.services.ig_revision_live._prepare_effects", side_effect=RuntimeError("synthetic crash before effect planning")), patch.object(instagram_bot, "_provider_http") as initial_transport:
                    with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                        ig_revision_live.execute_claimed_revision(fixture.revision.pk, fixture.token, fixture.settings)
                initial_transport.assert_not_called()
        fixture.revision.refresh_from_db()
        self.assertEqual(fixture.revision.state, "claimed")
        self.assertEqual(bool(fixture.revision.generation_proposal_digest), proposal_saved)
        self.assertFalse(fixture.revision.delivery_effects.exists())
        snapshot_digest = fixture.revision.snapshot_digest
        proposal_digest = fixture.revision.generation_proposal_digest
        original_token = fixture.token
        ordinary = RevisionRolloutTests(methodName="runTest")
        _client, ordinary_source, _turn, ordinary_revision = ordinary._revision("source-claimed-unowned")
        self.assertTrue(prepare_revision(ordinary_revision.pk, lambda **kwargs: None).ready)
        IgCustomerTurnRevision.objects.filter(pk=ordinary_revision.pk).update(lease_until=timezone.now() - timedelta(seconds=1))

        with self.settings(IG_REVISION_EXECUTION_ENABLED=False), patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=fixture._generate) as generation, patch.object(instagram_bot, "get_page_token", return_value="memory-token"), patch.object(instagram_bot, "_provider_http", return_value=(200, json.dumps({"message_id": "source-rollback-claimed-receipt"}))) as transport, patch.object(instagram_bot, "_register_outgoing_message"), patch("management.services.ig_revision_live.execute_claimed_revision", wraps=ig_revision_live.execute_claimed_revision) as execute:
            # A live execution lease is never reclaimed on rollout rollback.
            self.assertEqual(ig_revision_live.process_pending_revisions(fixture.settings, max_items=5, create_new=False), 0)
            execute.assert_not_called()
            generation.assert_not_called()
            transport.assert_not_called()
            self.assertFalse(ig_revision_live.legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=fixture.source.pk)).exists())
            IgCustomerTurnRevision.objects.filter(pk=fixture.revision.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
            self.assertEqual(ig_revision_live.process_pending_revisions(fixture.settings, max_items=5, create_new=False), 1)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0], fixture.revision.pk)
            self.assertNotEqual(execute.call_args.args[1], original_token)
            self.assertEqual(generation.call_count, 0 if proposal_saved else 1)
            self.assertEqual(transport.call_count, 1)
            # Completion cannot re-enter the canonical or legacy sender.
            self.assertEqual(ig_revision_live.process_pending_revisions(fixture.settings, max_items=5, create_new=False), 0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(transport.call_count, 1)
        fixture.revision.refresh_from_db()
        ordinary_revision.refresh_from_db()
        self.assertEqual(fixture.revision.state, IgCustomerTurnRevision.State.PROCESSED)
        self.assertEqual(fixture.revision.snapshot_digest, snapshot_digest)
        if proposal_saved:
            self.assertEqual(fixture.revision.generation_proposal_digest, proposal_digest)
        self.assertEqual(ordinary_revision.state, "claimed")
        self.assertFalse(ordinary_revision.delivery_effects.exists())

    def test_rollback_reclaims_expired_source_owned_claim_before_proposal_or_effects(self):
        self.assert_claimed_source_rollback(proposal_saved=False)

    def test_rollback_reclaims_expired_source_owned_claim_with_saved_proposal(self):
        self.assert_claimed_source_rollback(proposal_saved=True)

    def test_fact_admitted_before_seal_keeps_exact_revision_owner_across_rollback(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from management.models import IgCustomerTurnRevision, InstagramBotMessage, InstagramBotSettings
        from management.services import instagram_bot
        from management.services.ig_revision_execution import due_revision_ids
        from management.services.ig_revision_live import (
            legacy_claimable_messages, process_pending_revisions, revision_execution_enabled, revision_owned_turn_ids,
        )
        from management.services.ig_revision_commerce import INGRESS_SOURCE_PRODUCER
        from management.tests_ig_revision_rollout import RevisionRolloutTests

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.allowed_senders = ""
        settings.ig_user_id = "rollback-source-owner"
        settings.page_id = "rollback-source-page"
        settings.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders", "ig_user_id", "page_id"])
        with self.settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00"), patch.object(instagram_bot, "_schedule_inbound_analysis"):
            self.assertTrue(instagram_bot.enqueue_inbound(settings, sender_id="source-rollback-client",
                text="Хочу футболку розмір L", mid="source-rollback-mid", persistence_only=True))
        source = InstagramBotMessage.objects.get(mid="source-rollback-mid")
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        revision = IgCustomerTurnRevision.objects.get(sources__message=source, active_slot=1)
        self.assertEqual(revision.state, "collecting")
        self.assertFalse(revision.snapshot_digest)
        self.assertIsNone(revision.media_prepare_deadline)
        self.assertEqual(decision.request_payload["source_binding"]["producer"], INGRESS_SOURCE_PRODUCER)
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(quiet_deadline=timezone.now() - timedelta(seconds=1))
        fixture = RevisionRolloutTests(methodName="runTest")
        _client, unowned_source, _turn, unowned = fixture._revision("source-rollback-unowned")
        legacy_observation = apply_turn(_client, unowned_source, parse_turn(unowned_source.text), reply_payload={})
        self.assertFalse(legacy_observation.delivery_required)
        self.assertNotIn("producer", legacy_observation.request_payload["source_binding"])

        with self.settings(IG_REVISION_EXECUTION_ENABLED=False):
            self.assertFalse(revision_execution_enabled())
            self.assertFalse(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=source.pk)).exists())
            self.assertTrue(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=unowned_source.pk)).exists())
            self.assertTrue(revision_owned_turn_ids().filter(pk=revision.turn_id).exists())
            due = due_revision_ids(source_producer_only=True)
            self.assertIn(revision.pk, due)
            self.assertNotIn(unowned.pk, due)
            self.assertIn(revision.pk, due_revision_ids(cutover_at=timezone.now() + timedelta(days=1)))
            with patch("management.services.ig_revision_live._claim_preparation", return_value=SimpleNamespace(token="source-prepare")), patch("management.services.ig_revision_execution.prepare_revision", return_value=SimpleNamespace(ready=True, execution_token="source-execute")) as prepare, patch("management.services.ig_revision_live.execute_claimed_revision", return_value=SimpleNamespace(state="completed", reasons=())) as execute, patch.object(instagram_bot, "gemini_generate") as provider:
                self.assertEqual(process_pending_revisions(settings, max_items=3, create_new=False), 1)
            self.assertEqual(prepare.call_args.args[0], revision.pk)
            self.assertEqual(execute.call_args.args[0], revision.pk)
            self.assertEqual(execute.call_count, 1)
            provider.assert_not_called()

    def test_requested_revision_flag_without_effective_cutover_preserves_legacy_owner(self):
        from management.models import InstagramBotMessage, InstagramBotSettings
        from management.services import instagram_bot
        from management.services.ig_revision_live import legacy_claimable_messages
        settings = InstagramBotSettings.load()
        settings.allowed_senders = ""
        settings.ig_user_id = "cutover-source-owner"
        settings.page_id = "cutover-source-page"
        settings.save(update_fields=["allowed_senders", "ig_user_id", "page_id"])
        with self.settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="invalid"), patch.object(instagram_bot, "_schedule_inbound_analysis"):
            self.assertTrue(instagram_bot.enqueue_inbound(settings, sender_id="source-cutover-client",
                text="Розмір L", mid="source-cutover-mid", persistence_only=True))
        source = InstagramBotMessage.objects.get(mid="source-cutover-mid")
        self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())
        self.assertTrue(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=source.pk)).exists())


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class SourcePresentationTests(TransactionTestCase):
    def setUp(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_revision_outbox import PublicationBinding
        from storefront.models import Category, Product
        self.fixture = RevisionLiveTests(methodName="runTest")
        self.fixture.setUp()
        self.fixture._prepare()
        result = reduce_revision_commerce(self.fixture.revision.pk, self.fixture.token,
            settings_id=self.fixture.settings.pk, settings_permission_epoch=self.fixture.settings.reply_permission_epoch,
            publication=PublicationBinding(self.fixture.publication.pk, self.fixture.publication.version, self.fixture.publication.snapshot_hash))
        self.assertTrue(result.ready, result.reason)
        self.fixture.revision.refresh_from_db()
        self.fixture.customer.refresh_from_db()
        category = Category.objects.create(name="Presentation", slug="presentation")
        self.product = Product.objects.create(title="Model Alpha", slug="model-alpha", category=category, price=790, status="published")
        self.other = Product.objects.create(title="Model Beta", slug="model-beta", category=category, price=790, status="published")

    def present(self, products, *, states=None, foreign_episode=False):
        from management.models import IgRevisionDeliveryEffect
        from management.services.ig_revision_outbox import _digest
        fixture = self.fixture
        admission = {"episode_id": fixture.customer.current_commercial_episode_id + int(foreign_episode),
            "revision_id": fixture.revision.pk, "client_id": fixture.customer.pk,
            "snapshot_digest": fixture.revision.snapshot_digest, "plan_digest": "a" * 64}
        fixture.revision.action_receipts = {**fixture.revision.action_receipts,
            "reply_projection_admission": {"funnel": {**admission, "digest": _digest(admission)}}}
        fixture.revision.save(update_fields=["action_receipts"])
        for index, product in enumerate(products):
            metadata = {"part_index": index, "product_id": product.pk, "title": product.title}
            payload = {"recipient": {"id": fixture.customer.igsid}, "message": {"attachment": {"type": "image", "payload": {"url": "https://example.invalid/photo.jpg"}}}}
            IgRevisionDeliveryEffect.objects.create(revision=fixture.revision, source_message=fixture.source,
                effect_key=f"test-presentation:{fixture.revision.pk}:{index}",
                group="catalog_media", kind="image", order_index=index, part_index=index, part_count=len(products),
                plan_digest="a" * 64, payload=payload, payload_digest=_digest(payload),
                projection_metadata=metadata, projection_digest=_digest(metadata),
                recipient_igsid=fixture.customer.igsid, provider_namespace=fixture.source.provider_namespace,
                settings_id_snapshot=fixture.settings.pk, settings_permission_epoch=fixture.settings.reply_permission_epoch,
                client_permission_epoch=fixture.customer.reply_permission_epoch,
                revision_snapshot_digest=fixture.revision.snapshot_digest,
                publication_id=fixture.publication.pk, publication_version=fixture.publication.version,
                publication_hash=fixture.publication.snapshot_hash, authority_context_digest="b" * 64,
                state=(states[index] if states else "sent"), provider_message_id=f"shown:{index}")

    def resolve(self, text, *, reply_to=""):
        source = self.fixture._message(text, "new-presentation-source")
        source.reply_to_provider_message_id = reply_to
        source.save(update_fields=["reply_to_provider_message_id"])
        return resolve_source_product_request(self.fixture.customer, source, parse_turn(text))

    def test_single_sent_presentation_and_size_bind_exact_identity_only(self):
        self.present([self.product])
        request = self.resolve("л")
        self.assertEqual(request.exact_product_id, self.product.pk)
        self.assertEqual(request.field_updates, {"size": "L"})
        self.assertFalse(request.purchase_requested)
        self.assertNotIn("color", request.field_updates)
        self.assertNotIn("fit", request.field_updates)
        self.assertEqual(request.source_binding["product_resolution"], "exact_presentation")

    def test_yes_to_photo_is_not_purchase_or_product_selection(self):
        self.present([self.product])
        request = self.resolve("Да", reply_to="shown:0")
        self.assertIsNone(request.exact_product_id)
        self.assertFalse(request.purchase_requested)

    def test_multiple_presentation_keeps_size_without_arbitrary_identity(self):
        self.present([self.product, self.other])
        request = self.resolve("Хочу заказать размер L")
        self.assertIsNone(request.exact_product_id)
        self.assertEqual(request.field_updates.get("size"), "L")
        self.assertEqual(request.pending_clarification, "which_product")

    def test_partial_or_unknown_presentation_cannot_select_sent_half(self):
        self.present([self.product, self.other], states=["sent", "unknown"])
        request = self.resolve("Беру L", reply_to="shown:0")
        self.assertIsNone(request.exact_product_id)
        self.assertEqual(request.field_updates.get("size"), "L")

    def test_foreign_episode_presentation_cannot_supply_current_identity(self):
        self.present([self.product], foreign_episode=True)
        self.assertIsNone(self.resolve("L").exact_product_id)
