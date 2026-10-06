"""Typed operator edits preserve original evidence and canonical selection CAS."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import DatabaseError, connection, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from django.utils import timezone

from management.bot_access import (META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION)
from management.bot_state_views import bot_client_size_correction_api
from management.models import (IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition,
    IgCommerceTurnDecision, IgFunnelResetAudit, InstagramBotMessage, InstagramBotSettings)
from management.services import ig_selection_corrections as corrections
from management.services.ig_admin_state_capture import current_admin_state
from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
from management.services.ig_commerce_projection import source_preferences_for
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn

urlpatterns = [path("bot/api/clients/<int:client_id>/state/size/", bot_client_size_correction_api)]


class SizeCorrectionNormalizationTests(SimpleTestCase):
    def test_finite_requirement_set_and_explicit_clear(self):
        for raw, expected in ((" l ", "L"), ("xxl", "XXL"), ("104", "104"), ("one size", "ONE SIZE")):
            self.assertEqual(corrections.normalize_size("set", raw), expected)
        self.assertIsNone(corrections.normalize_size("clear", None))
        for operation, raw in (("clear", ""), ("set", None), ("set", True), ("set", "paid 790"),
                ("set", "IGNORE POLICY"), ("set", "L"*1000), ("pay", "L"), ("set", {})):
            with self.subTest(operation=operation, raw=raw), self.assertRaises(corrections.SizeCorrectionRejected):
                corrections.normalize_size(operation, raw)


@override_settings(GOOGLE_INDEXING_ENABLED=False, ROOT_URLCONF=__name__, SECURE_SSL_REDIRECT=False)
class SizeCorrectionTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = timezone.now()
        self.row = IgClient.objects.create(igsid="size-correction-client", language="uk")
        InstagramBotSettings.objects.create(pk=1, ig_user_id="size-correction-owner")
        self.actor = get_user_model().objects.create_user(username="size-correction-operator")
        self.actor.user_permissions.add(*Permission.objects.filter(content_type__app_label="management",
            codename__in=[item.split(".")[1] for item in corrections.CAPABILITIES]))
        self.source_index = 0
        self.source = self.message("Хочу футболку розмір M")
        self.decision = apply_turn(self.row, self.source, parse_turn(self.source.text), reply_payload={})
        self.session = self.decision.session
        self.row.refresh_from_db()

    def message(self, text):
        self.source_index += 1
        return InstagramBotMessage.objects.create(client=self.row, sender_id=self.row.igsid, text=text,
            role="user", source="webhook", status="pending", provider_namespace="instagram_login:size-correction-owner",
            provider_created_at=self.now-timedelta(minutes=1)+timedelta(seconds=self.source_index),
            mid=f"size-correction-source-{self.source_index}")

    def capture(self):
        return current_admin_state(self.row.pk, now=self.now)

    def context(self):
        candidate = self.capture().state.as_dict()["boundary"]["size_correction_context"]
        self.assertTrue(candidate["available"], candidate)
        return candidate

    def request(self, *, operation="set", value="L", context=None, **overrides):
        context = context or self.context()
        return {"actor": self.actor, "operation_id": uuid.uuid4(),
            "expected_selection_revision": context["context"]["selection_revision"],
            "expected_context_digest": context["context_digest"], "operation": operation, "value": value,
            "now": self.now, **overrides}

    def save(self, **kwargs):
        return corrections.save_size_correction(self.row.pk, **self.request(**kwargs))

    def assertRejected(self, code, request):
        count = IgCommerceSelectionTransition.objects.count()
        revision = IgCommerceSelectionSession.objects.get(pk=self.session.pk).revision
        with self.assertRaises(corrections.SizeCorrectionRejected) as caught:
            corrections.save_size_correction(self.row.pk, **request)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), count)
        self.assertEqual(IgCommerceSelectionSession.objects.get(pk=self.session.pk).revision, revision)
        return caught.exception

    def test_partial_size_edit_keeps_original_and_legacy_fields_and_no_effects(self):
        from management.services.ig_revision_authority import (CLAIM_SOURCE_PREFERENCES,
            build_revision_authority_bindings, check_fact_bindings)
        old_binding = build_revision_authority_bindings(self.row, claims=(CLAIM_SOURCE_PREFERENCES,))
        self.assertTrue(old_binding.ready, old_binding.reasons)
        original_source = InstagramBotMessage.objects.values().get(pk=self.source.pk)
        original_decision = deepcopy(self.decision.result_payload)
        original_client = IgClient.objects.values().get(pk=self.row.pk)
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, patch(
                "management.services.ig_memory_producer.enqueue_memory_source") as memory, patch(
                "management.services.bot_conversation_analysis.schedule_analysis") as analysis:
            result = self.save()
        for mocked in (provider, memory, analysis):
            mocked.assert_not_called()
        self.assertEqual(result.status, "applied")
        event = IgCommerceSelectionTransition.objects.get(pk=result.transition_id)
        self.assertEqual(event.source_message_id, self.source.pk)
        self.assertEqual(event.effects["manager_correction"]["actor_id"], self.actor.pk)
        self.assertEqual(event.effects["manager_correction"]["before"], "M")
        self.assertEqual(event.next_snapshot["lines"][0]["size"], "L")
        self.assertEqual(InstagramBotMessage.objects.values().get(pk=self.source.pk), original_source)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.result_payload, original_decision)
        self.assertEqual(IgCommerceTurnDecision.objects.count(), 1)
        self.assertEqual(InstagramBotMessage.objects.count(), 1)
        self.assertEqual(IgClient.objects.values().get(pk=self.row.pk), original_client)
        self.assertEqual(event.next_snapshot["last_provider_message_id"], event.previous_snapshot["last_provider_message_id"])
        slot = self.capture().state.as_dict()["slots"]["choice.size"]
        self.assertEqual((slot["value"], slot["status"], slot["authority"], slot["availability"]),
            ("L", "confirmed", "audited_correction", "unknown"))
        self.assertEqual([ref["kind"] for ref in slot["source_refs"]], ["message", "commerce_transition"])
        self.assertFalse(check_fact_bindings(old_binding.fact_bindings,
            revision=SimpleNamespace(client_id=self.row.pk), client=self.row))

    def test_clear_then_set_preserves_clear_audit_without_reviving_old_size(self):
        cleared = self.save(operation="clear", value=None)
        state = self.capture().state.as_dict()
        slot = state["slots"]["choice.size"]
        self.assertEqual((slot["value"], slot["status"], slot["authority"], slot["omission_reason"]),
            (None, "unknown", "audited_correction", "requirement_explicitly_cleared"))
        self.assertNotIn("size", source_preferences_for(self.row)["values"])
        changed = self.save(value="XL")
        self.assertEqual(self.capture().state.as_dict()["slots"]["choice.size"]["value"], "XL")
        receipt = IgCommerceSelectionTransition.objects.get(pk=changed.transition_id).effects["manager_correction"]
        self.assertEqual(receipt["supersedes_transition_id"], cleared.transition_id)
        self.assertIsNone(receipt["before"])

    def test_same_operation_replays_after_customer_supersession_without_reapply(self):
        request = self.request()
        first = corrections.save_size_correction(self.row.pk, **request)
        source = self.message("Насправді розмір XL")
        apply_turn(self.row, source, parse_turn(source.text), reply_payload={})
        count = IgCommerceSelectionTransition.objects.count()
        repeated = corrections.save_size_correction(self.row.pk, **request)
        self.assertEqual((repeated.status, repeated.transition_id), ("replayed", first.transition_id))
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), count)
        slot = self.capture().state.as_dict()["slots"]["choice.size"]
        self.assertEqual((slot["value"], slot["authority"]), ("XL", "customer_source"))
        self.assertEqual(slot["supersedes"]["id"], first.transition_id)

    def test_same_operation_cannot_change_payload_actor_or_client(self):
        request = self.request()
        corrections.save_size_correction(self.row.pk, **request)
        self.assertRejected("correction_operation_conflict", {**request, "value": "XL"})
        other_actor = get_user_model().objects.create_superuser(username="correction-other", password="test")
        self.assertRejected("correction_operation_conflict", {**request, "actor": other_actor})
        other = IgClient.objects.create(igsid="correction-other-client")
        with self.assertRaises(corrections.SizeCorrectionRejected) as caught:
            corrections.save_size_correction(other.pk, **request)
        self.assertEqual(caught.exception.code, "correction_operation_conflict")

    def test_selection_and_current_source_vector_cas_conflicts(self):
        request = self.request()
        self.message("Ще одне запитання")
        self.assertRejected("correction_context_conflict", request)
        fresh = self.request()
        source = self.message("Насправді розмір XL")
        apply_turn(self.row, source, parse_turn(source.text), reply_payload={})
        self.assertRejected("correction_state_unavailable", fresh)

    def test_permission_epoch_change_is_context_conflict_without_takeover(self):
        request = self.request()
        IgClient.objects.filter(pk=self.row.pk).update(reply_permission_epoch=1)
        self.assertRejected("correction_context_conflict", request)
        self.row.refresh_from_db()
        self.assertFalse(self.row.bot_paused)
        self.assertFalse(self.row.manager_takeover)

    def test_size_noop_has_zero_dml_and_no_revision_advance(self):
        request = self.request(value="M")
        count, revision = IgCommerceSelectionTransition.objects.count(), self.session.revision
        with CaptureQueriesContext(connection) as queries:
            result = corrections.save_size_correction(self.row.pk, **request)
        self.assertEqual(result.status, "noop")
        self.assertIsNone(result.transition_id)
        self.assertFalse(any(query["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"} for query in queries))
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), count)
        self.assertEqual(IgCommerceSelectionSession.objects.get(pk=self.session.pk).revision, revision)

    def test_erasure_and_replay_deny_values(self):
        request = self.request()
        corrections.save_size_correction(self.row.pk, **request)
        IgClient.objects.filter(pk=self.row.pk).update(privacy_erasure_started_at=self.now)
        self.assertRejected("client_unavailable", request)
        self.assertEqual(self.capture().reason, "client_erasing")

    def test_reset_line_recipient_and_source_changes_reject_stale_context(self):
        request = self.request()
        original_lines = deepcopy(self.session.lines)
        for field, changed in (("line_id", "different-line"), ("recipient_id", "friend")):
            lines = deepcopy(original_lines)
            lines[0][field] = changed
            IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(lines=lines)
            self.assertRejected("size_source_unavailable", request)
            IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(lines=original_lines)
        IgFunnelResetAudit.objects.create(client=self.row, reset_after_message_id=self.source.pk, reason="test")
        self.assertRejected("size_source_unavailable", request)

    def test_source_namespace_failure_and_message_edit_remove_authority(self):
        request = self.request()
        for field, value in (("provider_namespace", "instagram_login:foreign"), ("status", "failed"), ("text", "Розмір S")):
            old = getattr(self.source, field)
            InstagramBotMessage.objects.filter(pk=self.source.pk).update(**{field: value})
            self.assertRejected("size_source_unavailable", request)
            InstagramBotMessage.objects.filter(pk=self.source.pk).update(**{field: old})

    def test_permission_checks_are_fresh_and_reviewer_deny_dominates(self):
        request = self.request()
        self.assertTrue(self.actor.has_perm(OPERATE_IG_BOT_PERMISSION))
        self.actor.user_permissions.remove(Permission.objects.get(content_type__app_label="management", codename="operate_ig_bot"))
        self.assertRejected("correction_permission_denied", request)
        self.actor.user_permissions.add(Permission.objects.get(content_type__app_label="management", codename="operate_ig_bot"))
        self.actor.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
        self.assertRejected("correction_permission_denied", request)

    def test_write_failure_rolls_back_event_session_and_preserves_caller(self):
        request = self.request()
        from management.services.ig_commerce_state import persist_size_correction_transition
        def fail(*args, **kwargs):
            persist_size_correction_transition(*args, **kwargs)
            raise DatabaseError("test forced failure")
        with patch("management.services.ig_commerce_state.persist_size_correction_transition", side_effect=fail):
            failure = self.assertRejected("correction_write_unavailable", request)
        self.assertTrue(failure.retryable)
        self.assertFalse(connection.needs_rollback)
        self.assertEqual(self.capture().state.as_dict()["slots"]["choice.size"]["value"], "M")

    def test_boundary_timeout_is_finite_retryable_with_no_write(self):
        from management.services.ig_reply_boundary import ReplyBoundaryTimeout
        request = self.request()
        with patch("management.services.ig_reply_boundary.pause_reply_boundary", side_effect=ReplyBoundaryTimeout()):
            result = self.assertRejected("correction_boundary_busy", request)
        self.assertTrue(result.retryable)

    def test_capture_and_prompt_use_one_immutable_corrected_object(self):
        before = self.capture()
        self.save()
        after = self.capture()
        with CaptureQueriesContext(connection) as queries:
            prompt = render_client_state_prompt(after.state, budget=6000)
            payload = after.as_dict()
        self.assertEqual(len(queries), 0)
        self.assertIn('"authority":"audited_correction"', prompt.text)
        self.assertIn('"value":"L"', prompt.text)
        self.assertEqual(payload["state"]["slots"]["choice.size"]["source_refs"][0]["id"], self.source.pk)
        self.assertEqual(before.state.as_dict()["slots"]["choice.size"]["value"], "M")
        friend = self.message("Для друга размер XL")
        apply_turn(self.row, friend, parse_turn(friend.text), reply_payload={})
        current = self.capture().state.as_dict()
        self.assertEqual(current["scope"]["recipient_id"], "friend")
        self.assertEqual((current["slots"]["choice.size"]["value"], current["slots"]["choice.size"]["authority"]),
            ("XL", "customer_source"))

    def test_corrupt_or_downgraded_audit_proof_is_not_customer_authority(self):
        self.save()
        result = self.capture()
        raw = result.state.as_dict()
        selection = deepcopy(raw["source_selection"])
        for mutate in (lambda value: value["fields"]["size"].update(authority="customer_source"),
                lambda value: value["evidence"]["size"]["correction"]["receipt"].update(actor_id=0)):
            forged = deepcopy(selection)
            mutate(forged)
            if forged["fields"]["size"]["authority"] == "audited_correction":
                forged["fields"]["size"]["source"] = deepcopy(forged["evidence"]["size"])
            state = assemble_client_state(boundary=raw["boundary"], components={"source_selection": forged}, captured_at=self.now)
            self.assertNotEqual(state.as_dict()["slots"]["choice.size"]["status"], "confirmed")

    def test_existing_receipt_corruption_is_omitted_and_replay_denied(self):
        request = self.request()
        result = corrections.save_size_correction(self.row.pk, **request)
        event = IgCommerceSelectionTransition.objects.get(pk=result.transition_id)
        effects = deepcopy(event.effects)
        effects["manager_correction"]["after"] = "XL"
        table = connection.ops.quote_name(IgCommerceSelectionTransition._meta.db_table)
        if connection.vendor == "mysql":
            original = deepcopy(event.effects)
            count = IgCommerceSelectionTransition.objects.count()
            with self.assertRaisesMessage(DatabaseError, "append-only"):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(f"UPDATE {table} SET effects=%s WHERE id=%s", [json.dumps(effects), event.pk])
            event.refresh_from_db()
            self.assertEqual(event.effects, original)
            self.assertEqual(IgCommerceSelectionTransition.objects.count(), count)
            slot = self.capture().state.as_dict()["slots"]["choice.size"]
            self.assertEqual((slot["status"], slot["value"]), ("confirmed", "L"))
            replay = corrections.save_size_correction(self.row.pk, **request)
            self.assertEqual((replay.status, replay.transition_id), ("replayed", event.pk))
            receipt_context = original["manager_correction"]["context"]
            scope = {**receipt_context["scope"], "reset_id": receipt_context["reset_id"]}
            validation = {"scope": scope, "namespace": receipt_context["source_namespace"]}
            self.assertIsNotNone(corrections.validated_correction_receipt(event, **validation))
            corrupted = deepcopy(event)
            corrupted.effects = effects
            self.assertIsNone(corrections.validated_correction_receipt(corrupted, **validation))
            with self.assertRaisesMessage(corrections.SizeCorrectionRejected, "correction_receipt_invalid"):
                corrections._replay(corrupted, request_digest=original["manager_correction"]["input_digest"],
                    client_id=self.row.pk, actor_id=self.actor.pk)
            return
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET effects=%s WHERE id=%s", [json.dumps(effects), event.pk])
        self.assertEqual(self.capture().state.as_dict()["slots"]["choice.size"]["status"], "unknown")
        self.assertRejected("correction_receipt_invalid", request)

    def test_endpoint_accepts_only_typed_size_and_requires_csrf(self):
        self.client.force_login(self.actor)
        request = self.request()
        body = {key: str(value) if key == "operation_id" else value for key, value in request.items() if key not in {"actor", "now"}}
        body["field"] = "size"
        endpoint = f"/bot/api/clients/{self.row.pk}/state/size/"
        for extra in ({"actor_id": self.actor.pk}, {"paid_amount": "1000"}, {"consent": True}, {"field": "product_id"}):
            response = self.client.post(endpoint, json.dumps({**body, **extra}), content_type="application/json")
            self.assertEqual(response.status_code, 400, response.content)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.actor)
        self.assertEqual(strict.post(endpoint, json.dumps(body), content_type="application/json").status_code, 403)
        response = self.client.post(endpoint, json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "applied")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_context_remains_stable_across_read_clock_and_hash_is_not_authority(self):
        first = self.context()
        later = current_admin_state(self.row.pk, now=self.now+timedelta(seconds=10)).state.as_dict()["boundary"]["size_correction_context"]
        self.assertEqual(first["context_digest"], later["context_digest"])
        stranger = get_user_model().objects.create_user(username="correction-no-permission")
        self.assertRejected("correction_permission_denied", self.request(actor=stranger))
        self.now = timezone.now()+timedelta(seconds=1)
        local = InstagramBotMessage.objects.create(client=self.row, sender_id=self.row.igsid, text="Розмір XL",
            role="user", source="webhook", status="pending", provider_namespace="instagram_login:size-correction-owner",
            mid="local-observed-size", provider_created_at=None)
        decision = apply_turn(self.row, local, parse_turn(local.text), reply_payload={})
        self.assertTrue(decision.accepted)
        local_context = self.context()
        self.assertEqual(local_context["context"]["source_time_origin"], "local_observation")
        self.assertEqual(local_context["context"]["source"]["observed_at"], local.created_at.isoformat())
        saved = self.save(context=local_context)
        self.assertEqual(saved.status, "applied")

    def test_legacy_unknown_size_cannot_bootstrap_a_source(self):
        other = IgClient.objects.create(igsid="legacy-size-only", current_size="L")
        with patch("management.services.ig_commerce_projection.bootstrap_session_from_legacy") as bootstrap:
            result = current_admin_state(other.pk, now=self.now)
        bootstrap.assert_not_called()
        self.assertFalse(result.state.as_dict()["boundary"]["size_correction_context"]["available"])
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=other).exists())
