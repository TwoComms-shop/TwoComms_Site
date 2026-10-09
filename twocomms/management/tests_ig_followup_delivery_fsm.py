"""IMP-102: durable, operator-resolvable follow-up delivery boundary."""

from contextlib import nullcontext
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.urls import reverse

from management.ig_bot_models import IgClient, IgFollowUpTask
from management.models import (
    AdminAuditLog,
    IgBotNotification,
    IgDeal,
    IgPaymentProjection,
    InstagramBotSettings,
)
from management.services.instagram_bot import ProviderDeliveryReceipt


KYIV = ZoneInfo("Europe/Kyiv")
MGMT = override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    SECURE_SSL_REDIRECT=False,
)


class FollowupDeliveryFsmTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 5, 11, 0, tzinfo=KYIV)
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.save(update_fields=["is_enabled", "updated_at"])
        self.client_record = self._client("imp102")

    def _client(self, suffix):
        client = IgClient.get_or_create_for_sender(f"imp102-{suffix}")
        client.stage = IgClient.Stage.QUALIFYING
        client.last_message_at = self.now
        client.save(update_fields=["stage", "last_message_at", "updated_at"])
        return client

    def _task(self, *, client=None, **overrides):
        values = {
            "client": client or self.client_record,
            "due_at": self.now,
            "status": IgFollowUpTask.Status.PENDING,
            "kind": IgFollowUpTask.Kind.QUALIFICATION,
            "reason": "imp102_delivery_boundary",
            "message_text": "Чи актуальне ще замовлення?",
            "meta_window_deadline": self.now + timedelta(hours=20),
        }
        values.update(overrides)
        return IgFollowUpTask.objects.create(**values)

    def test_task_is_processing_with_lease_at_exact_provider_boundary(self):
        from management.services.bot_followups import (
            FOLLOWUP_CLAIM_TTL,
            process_due_followups,
        )

        task = self._task()
        observed = {}
        boundary_now = self.now + timedelta(minutes=2)
        clock = iter((self.now, boundary_now))

        def send_text(*_args, **kwargs):
            task.refresh_from_db()
            observed.update(
                status=task.status,
                claim_token=task.claim_token,
                claim_until=task.claim_until,
                attempt_count=task.attempt_count,
                return_receipt=kwargs.get("return_receipt"),
            )
            return ProviderDeliveryReceipt(True, "", "", "mid-imp102-success")

        with (
            patch(
                "management.services.bot_followups._now",
                side_effect=lambda: next(clock, boundary_now),
            ),
            patch(
                "management.services.instagram_bot.send_text", side_effect=send_text
            ),
        ):
            self.assertEqual(
                process_due_followups(self.settings, limit=1), 1
            )

        self.assertEqual(observed["status"], IgFollowUpTask.Status.PROCESSING)
        self.assertTrue(observed["claim_token"])
        self.assertEqual(observed["claim_until"], boundary_now + FOLLOWUP_CLAIM_TTL)
        self.assertEqual(observed["attempt_count"], 1)
        self.assertTrue(observed["return_receipt"])
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertEqual(task.provider_message_id, "mid-imp102-success")
        self.assertEqual(task.sent_message.provider_message_id, "mid-imp102-success")
        self.assertEqual(task.claim_token, "")
        self.assertIsNone(task.claim_until)

    def test_uncertain_provider_outcomes_are_ambiguous_and_never_retried(self):
        from management.services.bot_followups import process_due_followups

        outcomes = {
            "timeout": TimeoutError("provider timeout"),
            "http_503": ProviderDeliveryReceipt(False, "transient", "http_503", ""),
            "unknown": ProviderDeliveryReceipt(False, "unknown", "unknown", ""),
            "missing_receipt": ProviderDeliveryReceipt(True, "", "", ""),
            "numeric_receipt": ProviderDeliveryReceipt(True, "", "", 123),
            "overlong_receipt": ProviderDeliveryReceipt(True, "", "", "m" * 256),
        }
        for suffix, outcome in outcomes.items():
            with self.subTest(outcome=suffix):
                client = self._client(suffix)
                task = self._task(client=client)
                first = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("management.services.instagram_bot.send_text", **first) as send:
                    self.assertEqual(
                        process_due_followups(self.settings, now=self.now, limit=1),
                        0,
                    )
                    task.refresh_from_db()
                    self.assertEqual(task.status, IgFollowUpTask.Status.AMBIGUOUS)
                    self.assertEqual(task.provider_message_id, "")
                    self.assertFalse(task.sent_message_id)
                    self.assertEqual(task.attempt_count, 1)
                    self.assertEqual(task.claim_token, "")
                    self.assertIsNone(task.claim_until)
                    review = IgFollowUpTask.objects.get(
                        delivery_review_for=task,
                        kind=IgFollowUpTask.Kind.MANAGER_TASK,
                    )
                    self.assertEqual(review.status, IgFollowUpTask.Status.PENDING)

                    self.assertEqual(
                        process_due_followups(self.settings, now=self.now, limit=1),
                        0,
                    )
                    self.assertEqual(send.call_count, 1)

    def test_legacy_provider_unknown_and_success_without_receipt_are_ambiguous(self):
        from management.services.bot_followups import process_due_followups

        for suffix, outcome in (
            ("legacy-unknown", (False, "unknown", "provider_503")),
            ("legacy-success", (True, "", "")),
        ):
            with self.subTest(suffix=suffix):
                task = self._task(client=self._client(suffix))
                with patch(
                    "management.services.instagram_bot.send_text",
                    return_value=outcome,
                ) as send:
                    self.assertEqual(
                        process_due_followups(self.settings, now=self.now, limit=1),
                        0,
                    )
                send.assert_called_once()
                task.refresh_from_db()
                self.assertEqual(task.status, IgFollowUpTask.Status.AMBIGUOUS)
                self.assertTrue(
                    IgFollowUpTask.objects.filter(delivery_review_for=task).exists()
                )
                self.assertFalse(task.sent_message_id)

    def test_worker_rechecks_task_claim_after_start_before_provider_io(self):
        from management.services import bot_followups
        from management.services.bot_followups import process_due_followups

        task = self._task()
        real_start = bot_followups._start_followup_attempt

        def expire_after_start(*args, **kwargs):
            claimed = real_start(*args, **kwargs)
            IgFollowUpTask.objects.filter(pk=claimed.pk).update(
                status=IgFollowUpTask.Status.AMBIGUOUS,
                claim_token="",
                claim_until=None,
            )
            return claimed

        with (
            patch(
                "management.services.bot_followups._start_followup_attempt",
                side_effect=expire_after_start,
            ),
            patch(
                "management.services.instagram_bot.send_text",
                return_value=ProviderDeliveryReceipt(True, "", "", "late-send"),
            ) as send,
        ):
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1),
                0,
            )

        send.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.AMBIGUOUS)

    def test_stale_processing_lease_becomes_ambiguous_without_provider_call(self):
        from management.services.bot_followups import process_due_followups

        task = self._task(
            status=IgFollowUpTask.Status.PROCESSING,
            claim_token="dead-worker",
            claim_until=self.now - timedelta(seconds=1),
            attempt_count=1,
        )

        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1), 0
            )

        send.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.AMBIGUOUS)
        self.assertTrue(
            IgFollowUpTask.objects.filter(delivery_review_for=task).exists()
        )

    def test_send_claim_samples_clock_after_lock_and_rejects_expiry_during_wait(self):
        from management.services.bot_followups import _recheck_followup_send_claim

        task = self._task(
            status=IgFollowUpTask.Status.PROCESSING,
            claim_token="lock-race",
            claim_until=self.now + timedelta(minutes=1),
            attempt_count=1,
        )
        events = []
        real_first = QuerySet.first

        def delayed_first(queryset):
            events.append("lock")
            locked = real_first(queryset)
            locked.claim_until = self.now - timedelta(seconds=1)
            return locked

        def fresh_clock():
            events.append("clock")
            return self.now

        with (
            patch.object(QuerySet, "first", autospec=True, side_effect=delayed_first),
            patch("management.services.bot_followups._now", side_effect=fresh_clock),
        ):
            renewed = _recheck_followup_send_claim(
                task.pk,
                claim_token="lock-race",
            )

        self.assertIsNone(renewed)
        self.assertLess(events.index("lock"), events.index("clock"))

    def test_receipt_committed_before_worker_crash_is_recovered_without_resend(self):
        from management.services.bot_followups import process_due_followups

        task = self._task(
            status=IgFollowUpTask.Status.SENT,
            provider_message_id="mid-recovery",
            message_text="Збережений текст доставки",
        )

        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1), 0
            )

        send.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertIsNotNone(task.sent_message_id)
        self.assertEqual(task.sent_message.text, "Збережений текст доставки")

    def test_due_followup_for_allowlist_excluded_client_is_skipped_without_send(self):
        from management.services.bot_followups import process_due_followups

        self.settings.allowed_senders = "permitted-sender"
        self.settings.save(update_fields=["allowed_senders", "updated_at"])
        task = self._task()

        with patch(
            "management.services.instagram_bot.send_text",
            return_value=ProviderDeliveryReceipt(True, "", "", "mid-not-allowed"),
        ) as send_text:
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1),
                0,
            )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.skip_reason, "sender_not_allowed")
        send_text.assert_not_called()

    def test_receipt_recovery_replays_fulfillment_escalation_once(self):
        from management.services.bot_followups import process_due_followups

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=self.now,
        )
        IgPaymentProjection.objects.create(
            client=self.client_record,
            deal=deal,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount="1090.00",
            paid_at=self.now,
        )
        task = self._task(
            client=self.client_record,
            deal=deal,
            status=IgFollowUpTask.Status.SENT,
            kind=IgFollowUpTask.Kind.FULFILLMENT,
            reason="paid_missing_delivery",
            level=2,
            provider_message_id="mid-recovery-fulfillment",
        )

        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1),
            0,
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"fulfillment_missing_delivery:{deal.pk}:g3",
                event_type="fulfillment_missing_delivery",
            ).exists()
        )
        self.assertEqual(
            IgBotNotification.objects.filter(
                dedupe_key=f"fulfillment_missing_delivery:{deal.pk}:g3",
            ).count(),
            1,
        )
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)

    def test_stale_pending_claim_is_safely_reclaimed_before_provider_io(self):
        from management.services.bot_followups import process_due_followups

        task = self._task(
            claim_token="safe-dead-worker",
            claim_until=self.now - timedelta(seconds=1),
        )

        with patch(
            "management.services.instagram_bot.send_text",
            return_value=ProviderDeliveryReceipt(True, "", "", "mid-reclaimed"),
        ) as send:
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1), 1
            )

        send.assert_called_once()
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertEqual(task.provider_message_id, "mid-reclaimed")
        self.assertEqual(task.attempt_count, 1)

    def test_finalize_failure_preserves_receipt_and_queues_review(self):
        from management.services.bot_followups import process_due_followups

        task = self._task()
        with (
            patch(
                "management.services.instagram_bot.send_text",
                return_value=ProviderDeliveryReceipt(
                    True, "", "", "mid-finalize-failed"
                ),
            ),
            patch(
                "management.services.bot_followups._finalize_confirmed_followup",
                side_effect=RuntimeError("local transaction failed"),
            ),
        ):
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1), 0
            )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.AMBIGUOUS)
        self.assertEqual(task.provider_message_id, "mid-finalize-failed")
        self.assertIn("local transaction failed", task.last_error)
        self.assertTrue(
            IgFollowUpTask.objects.filter(delivery_review_for=task).exists()
        )

    def test_finalization_is_idempotent_when_recovery_races_sender(self):
        from management.services.bot_followups import _finalize_confirmed_followup

        task = self._task(
            status=IgFollowUpTask.Status.SENT,
            provider_message_id="mid-finalize-race",
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
        )
        with patch(
            "management.services.bot_followups._schedule_next_policy_step",
            return_value=True,
        ) as schedule:
            _finalize_confirmed_followup(task.pk, text=task.message_text, now=self.now)
            _finalize_confirmed_followup(task.pk, text=task.message_text, now=self.now)

        self.assertEqual(schedule.call_count, 1)
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertIsNotNone(task.sent_message_id)

    def test_sender_exception_after_concurrent_finalization_does_not_reopen_delivery(self):
        from management.services import bot_followups
        from management.services.bot_followups import process_due_followups

        task = self._task()
        real_finalize = bot_followups._finalize_confirmed_followup

        def finalize_then_fail(*args, **kwargs):
            result = real_finalize(*args, **kwargs)
            raise RuntimeError("sender observed stale finalization error")

        with (
            patch(
                "management.services.instagram_bot.send_text",
                return_value=ProviderDeliveryReceipt(True, "", "", "mid-race-finalized"),
            ),
            patch(
                "management.services.bot_followups._finalize_confirmed_followup",
                side_effect=finalize_then_fail,
            ),
        ):
            self.assertEqual(
                process_due_followups(self.settings, now=self.now, limit=1),
                0,
            )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertIsNotNone(task.sent_message_id)

    def test_recovery_exception_after_concurrent_finalization_does_not_reopen_delivery(self):
        from management.services import bot_followups

        task = self._task(
            status=IgFollowUpTask.Status.SENT,
            provider_message_id="mid-recovery-race",
        )
        real_finalize = bot_followups._finalize_confirmed_followup

        def finalize_then_fail(*args, **kwargs):
            result = real_finalize(*args, **kwargs)
            raise RuntimeError("recovery observed stale finalization error")

        with patch(
            "management.services.bot_followups._finalize_confirmed_followup",
            side_effect=finalize_then_fail,
        ):
            self.assertEqual(
                bot_followups.recover_sent_followup_receipts(
                    now=self.now,
                    limit=1,
                ),
                0,
            )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertIsNotNone(task.sent_message_id)


@MGMT
class FollowupDeliveryResolutionTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=KYIV)
        self.admin = get_user_model().objects.create_user(
            "imp102-admin", password="x", is_staff=True
        )
        self.admin.user_permissions.add(*[
            Permission.objects.get(content_type__app_label="management", codename=codename)
            for codename in ("operate_ig_bot", "view_ig_conversation_pii")
        ])
        self.client.force_login(self.admin)
        self.client_record = IgClient.get_or_create_for_sender("imp102-resolution")
        self.client_record.stage = IgClient.Stage.QUALIFYING
        self.client_record.last_message_at = self.now
        self.client_record.save(
            update_fields=["stage", "last_message_at", "updated_at"]
        )

    def _ambiguous_pair(self, suffix):
        source = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.AMBIGUOUS,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason=f"imp102_{suffix}",
            message_text="Точний текст можливої доставки",
            attempt_count=1,
        )
        review = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="followup_delivery_review",
            event_key=f"followup_delivery_review:{source.pk}",
            delivery_review_for=source,
            message_text="Перевірте Direct, не надсилайте повторно.",
        )
        return source, review

    def _resolve_url(self, source):
        return reverse(
            "management_bot_client_followup_delivery_resolve_api",
            args=[self.client_record.pk, source.pk],
        )

    def _completed_event(self):
        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="imp103-continuation",
            invoice_url="https://pay.example/imp103-continuation",
            invoice_expires_at=self.now,
        )
        return IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            status=IgFollowUpTask.Status.COMPLETED,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=3,
            event_key=f"invoice_expired:{deal.pk}:imp103-continuation",
            trigger=IgFollowUpTask.Trigger.EVENT,
            event_occurred_at=self.now,
            event_payload={
                "event": "invoice_expired",
                "deal_id": deal.pk,
                "invoice_id": "imp103-continuation",
            },
            policy_started_at=self.now - timedelta(hours=24),
            policy_version="followup-v1",
        )

    def test_delivered_resolution_is_idempotent_audited_and_never_resends(self):
        source, review = self._ambiguous_pair("delivered")
        url = self._resolve_url(source)

        with patch("management.services.instagram_bot.send_text") as send:
            first = self.client.post(
                url,
                {"outcome": "delivered", "note": "Є у Meta Inbox"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            second = self.client.post(
                url,
                {"outcome": "delivered", "note": "Є у Meta Inbox"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertFalse(first.json()["idempotent"])
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()["idempotent"])
        send.assert_not_called()
        source.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(source.status, IgFollowUpTask.Status.SENT)
        self.assertEqual(review.status, IgFollowUpTask.Status.COMPLETED)
        self.assertEqual(
            AdminAuditLog.objects.filter(
                action="ig_followup_delivery_resolved",
                entity_id=str(source.pk),
            ).count(),
            1,
        )

    def test_generic_staff_cannot_read_or_resolve_delivery_without_capabilities(self):
        staff = get_user_model().objects.create_user("imp102-staff-without-capabilities", password="x", is_staff=True)
        self.client.force_login(staff)
        source, review = self._ambiguous_pair("unauthorized")
        with patch("management.services.instagram_bot.send_text") as send:
            response = self.client.post(self._resolve_url(source), {"outcome": "delivered", "note": "No authority"})
            detail = self.client.get(reverse("management_bot_client_detail_api", args=[self.client_record.pk]))
        self.assertEqual((response.status_code, detail.status_code), (403, 403))
        send.assert_not_called()
        source.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual((source.status, review.status), ("ambiguous", "pending"))
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_followup_delivery_resolved", entity_id=str(source.pk)).exists())

    def test_not_delivered_resolution_is_terminal_and_never_resends(self):
        source, review = self._ambiguous_pair("not-delivered")

        with patch("management.services.instagram_bot.send_text") as send:
            response = self.client.post(
                self._resolve_url(source),
                {"outcome": "not_delivered", "note": "У Direct немає"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200, response.content)
        send.assert_not_called()
        source.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(source.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(source.skip_reason, "manager_confirmed_not_delivered")
        self.assertEqual(review.status, IgFollowUpTask.Status.COMPLETED)

    def test_delivered_resolution_keeps_nonempty_transcript_when_source_text_missing(self):
        source, review = self._ambiguous_pair("missing-text")
        source.message_text = ""
        source.save(update_fields=["message_text", "updated_at"])
        review.message_text = "Текст, збережений для ручної перевірки"
        review.save(update_fields=["message_text", "updated_at"])

        response = self.client.post(
            self._resolve_url(source),
            {"outcome": "delivered", "note": "Підтверджено вручну"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        source.refresh_from_db()
        self.assertTrue(source.sent_message_id)
        self.assertEqual(
            source.sent_message.text,
            "Текст, збережений для ручної перевірки",
        )

    def test_resolution_audit_failure_rolls_back_state_transition(self):
        source, review = self._ambiguous_pair("audit-atomic")
        self.client.raise_request_exception = False

        with patch(
            "management.bot_views.AdminAuditLog.objects.create",
            side_effect=RuntimeError("audit persistence failed"),
        ):
            response = self.client.post(
                self._resolve_url(source),
                {"outcome": "not_delivered", "note": "Перевірено"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 500)
        source.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(source.status, IgFollowUpTask.Status.AMBIGUOUS)
        self.assertEqual(review.status, IgFollowUpTask.Status.PENDING)

    def test_resolution_surface_requires_login_staff_and_post(self):
        source, _review = self._ambiguous_pair("access")
        url = self._resolve_url(source)
        self.assertEqual(self.client.get(url).status_code, 405)

        ordinary = get_user_model().objects.create_user("imp102-user", password="x")
        self.client.force_login(ordinary)
        self.assertEqual(
            self.client.post(url, {"outcome": "delivered"}).status_code, 403
        )
        self.client.logout()
        self.assertEqual(
            self.client.post(url, {"outcome": "delivered"}).status_code, 302
        )

    def test_event_continuation_api_is_audited_and_preserves_boundary(self):
        event = self._completed_event()
        boundary = {
            "event_key": event.event_key,
            "event_occurred_at": event.event_occurred_at,
            "event_payload": dict(event.event_payload),
            "policy_started_at": event.policy_started_at,
            "policy_version": event.policy_version,
        }
        url = reverse(
            "management_bot_client_followup_continue_api",
            args=[self.client_record.pk, event.pk],
        )

        first = self.client.post(
            url,
            {"note": "Перевірено менеджером"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        second = self.client.post(
            url,
            {"note": "Перевірено менеджером"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        payload = first.json()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["idempotent"])
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(second.json()["next_task_id"], payload["next_task_id"])
        next_task = IgFollowUpTask.objects.get(pk=payload["next_task_id"])
        self.assertEqual(next_task.level, 4)
        self.assertEqual(next_task.policy_started_at, boundary["policy_started_at"])
        event.refresh_from_db()
        self.assertEqual(
            {
                "event_key": event.event_key,
                "event_occurred_at": event.event_occurred_at,
                "event_payload": event.event_payload,
                "policy_started_at": event.policy_started_at,
                "policy_version": event.policy_version,
            },
            boundary,
        )
        audit = AdminAuditLog.objects.get(
            action="ig_event_followup_continued",
            entity_id=str(event.pk),
        )
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.reason, "Перевірено менеджером")
        detail = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.client_record.pk])
        )
        row = next(
            item for item in detail.json()["followups"] if item["id"] == event.pk
        )
        self.assertEqual(row["continue_url"], "")

    def test_client_detail_exposes_review_without_resend_action(self):
        source, _review = self._ambiguous_pair("detail")

        response = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.client_record.pk])
        )

        self.assertEqual(response.status_code, 200, response.content)
        row = next(
            item
            for item in response.json()["followups"]
            if item["id"] == source.pk
        )
        self.assertEqual(row["status"], IgFollowUpTask.Status.AMBIGUOUS)
        self.assertEqual(row["allowed_outcomes"], ["delivered", "not_delivered"])
        self.assertTrue(row["resolution_url"])
        self.assertNotIn("resend", row["allowed_outcomes"])

    def test_client_detail_keeps_older_ambiguous_source_after_newer_followups(self):
        source, _review = self._ambiguous_pair("older-than-eight")
        for index in range(9):
            IgFollowUpTask.objects.create(
                client=self.client_record,
                due_at=self.now + timedelta(minutes=index + 1),
                kind=IgFollowUpTask.Kind.QUALIFICATION,
                reason=f"newer_followup_{index}",
                message_text=f"Нове повідомлення {index}",
            )

        response = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.client_record.pk])
        )

        self.assertEqual(response.status_code, 200, response.content)
        row = next(
            item
            for item in response.json()["followups"]
            if item["id"] == source.pk
        )
        self.assertEqual(row["allowed_outcomes"], ["delivered", "not_delivered"])
        self.assertTrue(row["resolution_url"])

    def test_management_bot_renders_delivery_resolution_actions(self):
        response = self.client.get(reverse("management_bot"))

        self.assertEqual(response.status_code, 200, response.content)
        template = response.content.decode()
        for contract in (
            "processing:'Надсилається'",
            "ambiguous:'Потрібна перевірка'",
            "completed:'Завершено'",
            "Array.isArray(item.allowed_outcomes)",
            "function isFollowupDeliveryReview(item)",
            "allFollowups.forEach(item=>",
            "item.resolution_url",
            "Підтвердити доставку",
            "Не доставлено",
            "body.append('outcome',outcome)",
            "body.append('note',note.trim())",
            "'X-CSRFToken':csrf",
            "'X-Requested-With':'XMLHttpRequest'",
            "await detail(id)",
        ):
            self.assertIn(contract, template)
        self.assertNotIn("addAction('resend'", template)

    def test_global_stop_preserves_delivery_review_task(self):
        from management.services.instagram_bot import stop_bot

        _source, review = self._ambiguous_pair("stop-preserves-review")
        with patch(
            "management.services.ig_reply_boundary.pause_reply_boundary",
            return_value=nullcontext(),
        ):
            stop_bot()

        review.refresh_from_db()
        self.assertEqual(review.status, IgFollowUpTask.Status.PENDING)

    def test_generic_cancellation_preserves_delivery_review_for_client_and_deal(self):
        from management.services.bot_followups import cancel_pending, cancel_pending_for_deal

        source, review = self._ambiguous_pair("cancel-preserves-review")
        self.assertEqual(cancel_pending(self.client_record, reason="manual_pause"), 0)
        review.refresh_from_db()
        self.assertEqual(review.status, IgFollowUpTask.Status.PENDING)

        deal = IgDeal.objects.create(client=self.client_record, status=IgDeal.Status.QUOTED)
        review.deal = deal
        review.save(update_fields=["deal", "updated_at"])
        self.assertEqual(cancel_pending_for_deal(deal, reason="deal_cancelled"), 0)
        review.refresh_from_db()
        self.assertEqual(review.status, IgFollowUpTask.Status.PENDING)
        source.refresh_from_db()
        self.assertEqual(source.status, IgFollowUpTask.Status.AMBIGUOUS)
