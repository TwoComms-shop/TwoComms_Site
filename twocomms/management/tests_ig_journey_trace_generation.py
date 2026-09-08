"""Explicit reconstruction uses existing provider admission without normal work."""
from io import StringIO
import hashlib
import json
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from management.models import IgClient, IgConversationAnalysisJob, IgJourneyTraceSnapshot, InstagramBotMessage, InstagramBotSettings
from management.services.ig_journey_trace_generation import generate_journey_trace, rebuild_journey_traces


MODULE = "management.services.ig_journey_trace_generation"


class JourneyTraceGenerationTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.get_or_create_for_sender("trace-generation-buyer")
        self.settings = InstagramBotSettings.objects.create(pk=1, analysis_backfill_enabled=True, is_enabled=False)
        self.message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="trace-generation-buyer", role="user", status="done", text="PRIVATE хочу худі",
        )
        self.job = IgConversationAnalysisJob.objects.create(
            client=self.buyer, status="done", watermark_message_id=self.message.pk, analyzed_watermark_message_id=self.message.pk,
            revision=7, analyzed_revision=7, trigger="message",
        )
        for target, value in (("management.services.gemini_keys.ALL_KEYS", ["test-key"]),):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mapping = self.enterContext(patch("management.services.gemini_keys.key_project_groups", return_value={"test-key": "test-project"}))
        self.enterContext(patch(f"{MODULE}._sender_allowlist_skip_reason", return_value=""))
        self.waiting = self.enterContext(patch(f"{MODULE}._customer_reply_work_waiting", return_value=False))
        self.provider = self.enterContext(patch(f"{MODULE}.gemini_generate_json", side_effect=self.reply))

    @staticmethod
    def reply(prompt, user_text, **kwargs):
        from management.services.ig_turn_lineage import current_context
        lineage = current_context()
        assert lineage["lane"] == "analysis" and lineage["client_id"]
        assert lineage["source_message_id"] is None
        assert lineage["logical_turn_id"].startswith("jt:")
        rows = json.loads(user_text)["conversation"]
        source = next(row for row in reversed(rows) if row["role"] in {"user", "manager"} and row["text"])
        return {"model": "gemini-test", "parsed": {"schema_version": 1, "steps": [{
            "from_node": "inbound", "to_node": "catalog_discovery", "kind": "progress", "reason_code": "entered",
            "confidence": 0.9, "evidence": [{"message_id": source["message_id"], "quote": source["text"][-12:]}],
        }], "current_node": "catalog_discovery"}}

    def test_dry_default_is_read_only_and_command_rejects_unbounded_or_implicit_apply(self):
        with CaptureQueriesContext(connection) as queries:
            result = generate_journey_trace(self.buyer.pk)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.provider.assert_not_called()
        output = StringIO()
        call_command("rebuild_ig_journey_traces", client_id=[self.buyer.pk], stdout=output)
        self.assertEqual(json.loads(output.getvalue())["mode"], "dry_run")
        self.assertNotIn("PRIVATE", output.getvalue())
        with self.assertRaises(CommandError):
            call_command("rebuild_ig_journey_traces", client_id=[self.buyer.pk], allow_historical=True)
        for values in ([], [True], [self.buyer.pk] * 2, list(range(1, 8))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                rebuild_journey_traces(values)

    def test_explicit_historical_admission_overrides_only_toggle_not_key_mapping(self):
        InstagramBotSettings.objects.filter(pk=1).update(analysis_backfill_enabled=False)
        default = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(default["reason"], "historical_backfill_gate")
        self.provider.assert_not_called()
        self.mapping.return_value = {}
        denied = generate_journey_trace(self.buyer.pk, apply=True, allow_historical=True)
        self.assertEqual(denied["reason"], "key_project_mapping_missing")
        self.provider.assert_not_called()
        self.mapping.return_value = {"test-key": "test-project"}
        accepted = generate_journey_trace(self.buyer.pk, apply=True, allow_historical=True)
        self.assertEqual(accepted["status"], "recorded")
        self.assertEqual(accepted["admission"], "explicit_bounded_rebuild")
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.analysis_backfill_enabled)

    def test_busy_normal_analysis_or_customer_reply_prevents_provider_work(self):
        for status in ("pending", "processing"):
            IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(status=status)
            with self.subTest(status=status):
                self.assertEqual(generate_journey_trace(self.buyer.pk, apply=True)["reason"], "ordinary_analysis_busy")
        IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(status="done")
        self.waiting.return_value = True
        self.assertEqual(generate_journey_trace(self.buyer.pk, apply=True)["reason"], "customer_reply_busy")
        self.provider.assert_not_called()

    def test_apply_writes_only_trace_and_reuses_same_input_without_provider_replay(self):
        before_job = IgConversationAnalysisJob.objects.filter(pk=self.job.pk).values().get()
        before_client = IgClient.objects.filter(pk=self.buyer.pk).values().get()
        before_settings = InstagramBotSettings.objects.filter(pk=1).values().get()
        result = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(self.provider.call_count, 1)
        kwargs = self.provider.call_args.kwargs
        self.assertEqual(kwargs, {"role": "management", "reasoning_task": "journey_trace_reconstruction", "max_output_tokens": 12288, "timeout": (8, 45), "deadline_seconds": 90})
        self.assertEqual(IgJourneyTraceSnapshot.objects.get(pk=result["snapshot_id"]).prompt_version, "journey-trace.text.v2.medium")
        repeated = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(repeated["status"], "existing")
        self.assertEqual(repeated["snapshot_id"], result["snapshot_id"])
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(IgConversationAnalysisJob.objects.filter(pk=self.job.pk).values().get(), before_job)
        self.assertEqual(IgClient.objects.filter(pk=self.buyer.pk).values().get(), before_client)
        self.assertEqual(InstagramBotSettings.objects.filter(pk=1).values().get(), before_settings)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_new_message_or_new_ordinary_work_after_provider_discards_trace(self):
        def new_message(*args, **kwargs):
            response = self.reply(*args, **kwargs)
            InstagramBotMessage.objects.create(client=self.buyer, sender_id="trace-generation-buyer", role="manager", status="done", text="fresh")
            return response
        self.provider.side_effect = new_message
        self.assertEqual(generate_journey_trace(self.buyer.pk, apply=True)["reason"], "sources_changed")
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())
        def busy(*args, **kwargs):
            response = self.reply(*args, **kwargs)
            IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(status="pending")
            return response
        self.provider.side_effect = busy
        self.assertEqual(generate_journey_trace(self.buyer.pk, apply=True)["reason"], "ordinary_analysis_busy")
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())

    def test_invalid_trace_and_provider_failure_are_sanitized_without_custom_retry(self):
        self.provider.side_effect = RuntimeError("PRIVATE provider credential and raw transcript")
        failed = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(failed["reason"], "provider_failed")
        self.assertEqual(self.provider.call_count, 1)
        self.assertNotIn("PRIVATE", json.dumps(failed))
        self.provider.side_effect = None
        self.provider.return_value = {"model": "gemini-test", "parsed": {"paid": True, "raw": "PRIVATE"}}
        invalid = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(invalid["reason"], "invalid_trace")
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())

    def test_newest_text_budget_keeps_full_source_hash_and_omits_media_bytes(self):
        text = "x" * 40_000 + " НОВИЙ хочу худі"
        latest = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="trace-generation-buyer", role="user", status="done", text=text,
            attachments="PRIVATE MEDIA URL", attachment_media=[{"url": "PRIVATE MEDIA URL"}],
        )
        result = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(result["status"], "recorded")
        coverage = result["coverage"]
        self.assertEqual(coverage["messages"], 1)
        self.assertEqual(coverage["text_characters"], 30_000)
        self.assertTrue(coverage["text_truncated"])
        self.assertEqual(coverage["media_omitted"], 1)
        body = json.loads(self.provider.call_args.args[1])
        self.assertEqual(body["conversation"][0]["message_id"], latest.pk)
        self.assertTrue(body["conversation"][0]["text"].endswith("НОВИЙ хочу худі"))
        self.assertNotIn("PRIVATE MEDIA URL", self.provider.call_args.args[1])
        saved = IgJourneyTraceSnapshot.objects.get(pk=result["snapshot_id"])
        self.assertEqual(saved.trace["steps"][0]["evidence"][0]["source_text_sha256"], hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest())

    def test_message_count_bound_and_chronological_output(self):
        InstagramBotMessage.objects.bulk_create([
            InstagramBotMessage(client=self.buyer, sender_id="trace-generation-buyer", role="user", status="done", text=f"хочу худі {index}")
            for index in range(165)
        ])
        result = generate_journey_trace(self.buyer.pk, apply=True)
        self.assertEqual(result["coverage"]["messages"], 160)
        self.assertTrue(result["coverage"]["message_limit_reached"])
        body = json.loads(self.provider.call_args.args[1])
        ids = [row["message_id"] for row in body["conversation"]]
        self.assertEqual(ids, sorted(ids))
        self.assertNotIn(self.message.pk, ids)
