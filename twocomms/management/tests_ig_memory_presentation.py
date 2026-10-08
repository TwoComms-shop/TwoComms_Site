"""Pure frozen-read presentation contract; no database or providers."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.services.ig_memory_presentation import memory_presentation, SCHEMA
from management.services.ig_memory_producer import _digest
from management.services.ig_memory_timeline import VERSION, build_timeline, render_timeline


def frozen_read(*, time_basis="provider", topic="gift", quote="Хочу на подарунок."):
    stamp = "2026-10-08T12:00:00+00:00"
    boundary = {"client_id": 351, "source_namespace": "instagram_login:owner",
        "reset_id": None, "reset_floor": 1, "erasure_epoch": "",
        "watermark": {"message_id": 20, "event_at": stamp}}
    source = {"source_message_id": 20, "text": quote, "event_at": stamp,
        "time_basis": time_basis, "source_digest": "a" * 64, "role": "user", "scope": None}
    timeline = build_timeline({"version": VERSION, "events": [
        {"source_message_id": 20, "quote": quote, "topic": topic}]}, sources=[source])
    text = render_timeline(timeline)
    proof_source = {"message_id": 20, "event_at": stamp, "time_basis": time_basis,
        "source_digest": "a" * 64, "role": "user", "scope": None,
        "chars_sent": len(quote), "chars_omitted": 0,
        "provider_created_at": stamp if time_basis == "provider" else None,
        "observed_created_at": stamp}
    proof = {"version": "captured-memory.timeline.v2", "head_version": 1,
        "generated_at": stamp, "summary_digest": hashlib.sha256(text.encode()).hexdigest(),
        "capture": {"version": "captured-memory.timeline.v2", "generation_input_digest": "b" * 64,
            "scope": {"client_id": 351, "namespace": boundary["source_namespace"],
                "reset_id": None, "reset_floor": 1, "erasure_at": ""},
            "target": dict(boundary["watermark"]), "sources": [proof_source]}, "timeline": timeline}
    proof["digest"] = _digest(proof)
    proof.update(read_delta=[], read_boundary=deepcopy(boundary))
    proof["read_digest"] = _digest({"head_digest": proof["digest"], "boundary": boundary, "delta": []})
    return {"text": text, "reason": "historical_as_of", "provenance": proof}, boundary


class MemoryPresentationTests(SimpleTestCase):
    def test_frozen_read_yields_exact_dated_quote_and_finite_hint(self):
        memory, boundary = frozen_read()
        before = deepcopy(memory)
        with patch(
            "management.services.ig_memory_producer.read_memory_timeline", side_effect=AssertionError("head reread")):
            view = memory_presentation(memory, boundary=boundary, visible_source_ids=[20])
        self.assertEqual(view["schema"], SCHEMA)
        self.assertEqual(view["status"], "timeline")
        self.assertEqual(view["events"], [{"event_at": boundary["watermark"]["event_at"],
            "time_basis": "provider", "topic_label": "Подарунок", "quote": "Хочу на подарунок.",
            "source_id": 20, "scope_label": "Розмова клієнта"}])
        self.assertEqual(view["coverage"]["event_count"], 1)
        self.assertEqual(memory, before)
        encoded = json.dumps(view, ensure_ascii=False)
        for machine in ("source_digest", "head_version", "HISTORICAL SOURCE OBSERVATIONS", "provenance"):
            self.assertNotIn(machine, encoded)

    def test_ingestion_time_is_preserved_and_unloaded_source_has_no_link(self):
        memory, boundary = frozen_read(time_basis="local_ingest", topic="self_purchase")
        view = memory_presentation(memory, boundary=boundary, visible_source_ids=[19, True, "20"])
        self.assertEqual(view["events"][0]["time_basis"], "local_ingest")
        self.assertEqual(view["events"][0]["topic_label"], "Для себе")
        self.assertIsNone(view["events"][0]["source_id"])

    def test_changed_boundary_or_proof_fail_closed_without_raw_fallback(self):
        memory, boundary = frozen_read()
        invalid = deepcopy(memory)
        invalid["provenance"]["timeline"]["events"][0]["quote"] = "Інший текст."
        changed = dict(boundary, client_id=999)
        for payload, capture in ((invalid, boundary), (memory, changed), (memory, None)):
            with self.subTest(capture=capture):
                view = memory_presentation(payload, boundary=capture)
                self.assertEqual(view["status"], "unavailable")
                self.assertEqual(view["events"], [])
                self.assertEqual(view["legacy_text"], "")
                self.assertNotIn("Хочу", json.dumps(view, ensure_ascii=False))

    def test_hidden_and_erasing_memory_do_not_return_head_or_dates(self):
        memory, boundary = frozen_read()
        for flags in ({"hidden": True}, {"erasing": True}):
            view = memory_presentation(memory, boundary=boundary, **flags)
            self.assertEqual(view["status"], "unavailable")
            self.assertEqual(view["events"], [])
            self.assertEqual(view["updated_at"], "")
            self.assertEqual(view["as_of"], "")

    def test_legacy_requires_verified_reader_and_never_gets_event_authority(self):
        memory = {"text": "Попередній огляд.\n" * 200, "reason": "current",
            "provenance": {"version": "captured-memory.v1"}}
        view = memory_presentation(memory, updated_at=datetime(2026, 10, 8, tzinfo=timezone.utc))
        self.assertEqual(view["status"], "legacy")
        self.assertEqual(len(view["legacy_text"]), 2000)
        self.assertEqual(view["events"], [])
        self.assertTrue(view["updated_at"])
        for invalid in ({"text": memory["text"]}, dict(memory, reason="narrative_source_stale"),
                        dict(memory, text="[HISTORICAL SOURCE OBSERVATIONS; topic_hint=gift]")):
            view = memory_presentation(invalid)
            self.assertEqual(view["legacy_text"], "")
            self.assertEqual(view["events"], [])

    def test_failed_timeline_read_and_unknown_reasons_are_finite_copy(self):
        for reason in ("timeline_source_changed", "timeline_delta_count_budget", "<script>alert(1)</script>"):
            view = memory_presentation({"reason": reason, "text": "RAW_PRIVATE_HEAD"})
            encoded = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("RAW_PRIVATE_HEAD", encoded)
            self.assertNotIn("<script>", encoded)
            self.assertEqual(view["events"], [])

    def test_privacy_omissions_are_human_counts_without_private_source_text(self):
        memory, boundary = frozen_read(quote="Телефон +380991234567.")
        view = memory_presentation(memory, boundary=boundary)
        self.assertEqual(view["status"], "empty")
        self.assertEqual(view["reason_label"], "У перевіреному огляді немає збережених цитат. Причини пропусків наведено нижче.")
        self.assertNotIn("Датовані цитати", view["reason_label"])
        self.assertEqual(view["events"], [])
        self.assertEqual(view["coverage"]["omissions"], [{"label": "Контактні дані приховано", "count": 1}])
        self.assertNotIn("380991234567", json.dumps(view, ensure_ascii=False))

    def test_card_reads_canonical_memory_once_then_projects_the_same_capture(self):
        from management.bot_views import _client_memory_view
        memory, boundary = frozen_read()
        client = SimpleNamespace(pk=351, igsid="customer", hidden_at=None,
            privacy_erasure_started_at=None, memory_updated_at=None,
            memory_summary="RAW_PRIVATE_MACHINE_HEAD", memory_provenance={"version": "captured-memory.timeline.v2"})
        source = SimpleNamespace(pk=20, client_id=351, sender_id="customer",
            provider_namespace=boundary["source_namespace"], role="user",
            source="webhook", status="done", provider_created_at=datetime.fromisoformat(boundary["watermark"]["event_at"]),
            created_at=datetime.fromisoformat(boundary["watermark"]["event_at"]))
        captured = SimpleNamespace(text=memory["text"], reason=memory["reason"], provenance=memory["provenance"])
        with patch("management.services.ig_admin_state_capture._namespace", return_value=boundary["source_namespace"]), \
                patch("management.services.ig_memory_producer._scope", return_value={"reset_floor": 1, "reset_id": None, "erasure_at": ""}), \
                patch("management.services.ig_memory_producer.read_memory_timeline", return_value=captured) as reader:
            view = _client_memory_view(client, [source], current_floor=1)
        reader.assert_called_once_with(client, boundary=boundary)
        self.assertEqual(view["status"], "timeline")
        self.assertEqual(view["events"][0]["source_id"], 20)
        self.assertNotIn("RAW_PRIVATE_MACHINE_HEAD", json.dumps(view))

    def test_latest_manager_echo_after_user_is_the_memory_boundary(self):
        from management.bot_views import _client_memory_view
        memory, boundary = frozen_read(topic="availability_inquiry", quote="Дореч на які він параметри?")
        later = "2026-10-08T12:01:00+00:00"
        boundary["watermark"] = {"message_id": 21, "event_at": later}
        proof = memory["provenance"]
        proof["capture"]["target"] = deepcopy(boundary["watermark"])
        proof["capture"]["sources"].append({"message_id": 21, "event_at": later,
            "time_basis": "provider", "source_digest": "c" * 64, "role": "manager",
            "scope": None, "chars_sent": 10, "chars_omitted": 0,
            "provider_created_at": later, "observed_created_at": later})
        publication = {key: value for key, value in proof.items() if key not in {"read_boundary", "read_delta", "read_digest", "digest"}}
        proof["digest"] = _digest(publication)
        proof["read_boundary"] = deepcopy(boundary)
        proof["read_digest"] = _digest({"head_digest": proof["digest"], "boundary": boundary, "delta": []})
        client = SimpleNamespace(pk=351, igsid="customer", hidden_at=None,
            privacy_erasure_started_at=None, memory_updated_at=None,
            memory_summary="PRIVATE_HEAD", memory_provenance={"version": "captured-memory.timeline.v2"})
        user = SimpleNamespace(pk=20, client_id=351, sender_id="customer", role="user",
            source="webhook", status="done", provider_namespace=boundary["source_namespace"],
            provider_created_at=datetime.fromisoformat("2026-10-08T12:00:00+00:00"),
            created_at=datetime.fromisoformat("2026-10-08T12:00:00+00:00"))
        manager = SimpleNamespace(pk=21, client_id=351, sender_id="customer", role="manager",
            source="echo", status="done", provider_namespace=boundary["source_namespace"],
            provider_created_at=datetime.fromisoformat(later), created_at=datetime.fromisoformat(later))
        captured = SimpleNamespace(text=memory["text"], reason=memory["reason"], provenance=proof)
        with patch("management.services.ig_admin_state_capture._namespace", return_value=boundary["source_namespace"]), \
                patch("management.services.ig_memory_producer._scope", return_value={"reset_floor": 1, "reset_id": None, "erasure_at": ""}), \
                patch("management.services.ig_memory_producer.read_memory_timeline", return_value=captured) as reader:
            view = _client_memory_view(client, [user, manager], current_floor=1)
        reader.assert_called_once_with(client, boundary=boundary)
        self.assertEqual(view["status"], "timeline")
        self.assertEqual(view["events"][0]["source_id"], 20)
        self.assertEqual(view["events"][0]["topic_label"], "Питання про товар")

    def test_invalid_human_receipt_cannot_authorize_raw_namespace_as_boundary(self):
        from management.bot_views import _client_memory_view
        memory, boundary = frozen_read()
        client = SimpleNamespace(pk=351, igsid="customer", hidden_at=None,
            privacy_erasure_started_at=None, memory_updated_at=None,
            memory_summary="PRIVATE_HEAD", memory_provenance={"version": "captured-memory.timeline.v2"})
        user = SimpleNamespace(pk=20, client_id=351, sender_id="customer", role="user",
            source="webhook", status="done", provider_namespace=boundary["source_namespace"],
            provider_created_at=datetime.fromisoformat(boundary["watermark"]["event_at"]),
            created_at=datetime.fromisoformat(boundary["watermark"]["event_at"]))
        invalid = SimpleNamespace(pk=21, client_id=351, sender_id="customer", role="manager",
            source="human_reply", status="done", provider_namespace=boundary["source_namespace"],
            provider_created_at=datetime.fromisoformat("2026-10-08T12:01:00+00:00"),
            created_at=datetime.fromisoformat("2026-10-08T12:01:00+00:00"))
        command = SimpleNamespace(reply_message_id=21, client_id=351, recipient_igsid="foreign")
        captured = SimpleNamespace(text=memory["text"], reason=memory["reason"], provenance=memory["provenance"])
        with patch("management.services.ig_admin_state_capture._namespace", return_value=boundary["source_namespace"]), \
                patch("management.services.ig_memory_producer._scope", return_value={"reset_floor": 1, "reset_id": None, "erasure_at": ""}), \
                patch("management.models.HumanReplyCommand.objects.filter") as receipt_query, \
                patch("management.services.ig_memory_producer.read_memory_timeline", return_value=captured) as reader:
            receipt_query.return_value.order_by.return_value.__getitem__.return_value = [command]
            view = _client_memory_view(client, [user, invalid], current_floor=1)
        reader.assert_called_once_with(client, boundary=boundary)
        self.assertEqual(view["status"], "timeline")
        self.assertEqual(view["events"][0]["source_id"], 20)
