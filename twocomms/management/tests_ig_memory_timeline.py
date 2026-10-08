"""Pure timeline contract: exact historical quotes, never commerce authority."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest import TestCase

from management.services.ig_memory_timeline import (
    MAX_EVENTS, MAX_RENDER_CHARS, TOPIC_HINTS, VERSION, TimelineError,
    build_timeline, parse_timeline_selection, render_timeline, validate_timeline,
)


class MemoryTimelineTests(TestCase):
    def source(self, identity=1, text="Хочу футболку на подарунок другу.", **updates):
        row = {"source_message_id": identity, "text": text,
            "event_at": datetime(2026, 10, 8, 10, tzinfo=timezone.utc) + timedelta(minutes=identity),
            "time_basis": "provider", "source_digest": hashlib.sha256(text.encode()).hexdigest(),
            "role": "user", "scope": None}
        row.update(updates)
        return row

    def selection(self, *sources, topic="other"):
        return {"version": VERSION, "events": [{"source_message_id": source["source_message_id"],
            "quote": source["text"], "topic": topic} for source in sources]}

    def timeline(self, *sources, **kwargs):
        return build_timeline(self.selection(*sources), sources=list(sources), **kwargs)

    def assert_error(self, code, callback):
        with self.assertRaises(TimelineError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_backend_dates_quote_and_nullable_scope_without_inferred_authority(self):
        source = self.source()
        timeline = build_timeline(self.selection(source, topic="gift"), sources=[source])
        event = timeline["events"][0]
        self.assertEqual(event["event_at"], "2026-10-08T10:01:00+00:00")
        self.assertEqual(event["quote"], source["text"])
        self.assertEqual(event["topic_hint"], "gift")
        self.assertIsNone(event["scope"])
        self.assertFalse({"recipient_id", "gift", "purchase_confirmed", "supersedes"}.intersection(event))
        text = render_timeline(timeline)
        self.assertIn("HISTORICAL SOURCE OBSERVATIONS", text)
        self.assertIn("topic_hint=gift", text)
        self.assertIn("time_basis=provider", text)
        self.assertLessEqual(len(text), MAX_RENDER_CHARS)

    def test_multilingual_unicode_and_negation_are_not_paraphrased(self):
        for text in ("Не хочу подарунок, обираю для себе.", "Не подарок, беру себе.",
                "I do not want a gift; this is for me.", "Ne podarok, khochu dlia sebe.",
                "Подарунок? Ні, це для мене!", "Хочу худі для себе 🙂."):
            with self.subTest(text=text):
                # One exact complete own sentence is selected from the source.
                quote = "Ні, це для мене!" if "Подарунок?" in text else text
                source = self.source(text=text)
                pick = self.selection(source, topic="gift")
                pick["events"][0]["quote"] = quote
                timeline = build_timeline(pick, sources=[source])
                self.assertEqual(timeline["events"][0]["quote"], quote)
                self.assertNotIn("gift", timeline["events"][0].get("scope") or {})

    def test_prefix_suffix_or_negation_cut_has_named_omission(self):
        source = self.source(text="Не хочу подарунок другу, хочу футболку для себе.")
        for quote in ("хочу подарунок другу", "хочу футболку для себе.",
                "Не хочу подарунок другу"):
            pick = self.selection(source)
            pick["events"][0]["quote"] = quote
            timeline = build_timeline(pick, sources=[source])
            self.assertEqual(timeline["events"], [])
            self.assertEqual(timeline["coverage"]["omissions"][0]["reason"], "quote_context_loss")

    def test_quoted_source_and_blockquote_are_conservative_omissions(self):
        for text in ('"Хочу подарунок."', "«Не хочу подарунок.»", "> I want a gift."):
            source = self.source(text=text)
            timeline = self.timeline(source)
            self.assertEqual(timeline["events"], [])
            self.assertIn(timeline["coverage"]["omissions"][0]["reason"],
                {"quoted_source_context", "quote_context_loss"})

    def test_nonexact_quote_rejects_provider_paraphrase(self):
        source = self.source()
        pick = self.selection(source)
        pick["events"][0]["quote"] = "Клієнт купив подарунок для друга."
        self.assert_error("quote_not_source_span", lambda: build_timeline(pick, sources=[source]))

    def test_date_mention_is_only_raw_quote_and_time_is_backend_owned(self):
        source = self.source(text="Подарунок потрібен 15.10.2026 о 12:30.",
            time_basis="local_ingest", event_at="2026-10-08T13:01:00+03:00")
        event = self.timeline(source)["events"][0]
        self.assertEqual(event["event_at"], "2026-10-08T10:01:00+00:00")
        self.assertEqual(event["quote"], source["text"])
        self.assertNotIn("planned_date", event)
        self.assertEqual(event["time_basis"], "local_ingest")

    def test_extra_keys_wrong_version_ids_and_duplicate_selections_reject(self):
        source = self.source()
        for mutate, code in (
                (lambda row: row.update(authority=True), "selection_shape_invalid"),
                (lambda row: row.update(version="unknown"), "selection_shape_invalid"),
                (lambda row: row["events"][0].update(event_at="invented"), "selection_event_invalid"),
                (lambda row: row["events"][0].update(source_message_id=True), "source_id_invalid"),
                (lambda row: row["events"][0].update(source_message_id=0), "source_id_invalid"),
                (lambda row: row["events"].append(deepcopy(row["events"][0])), "duplicate_source_selection"),
                (lambda row: row["events"][0].update(topic="payment_confirmed"), "topic_hint_invalid")):
            pick = self.selection(source)
            mutate(pick)
            self.assert_error(code, lambda: build_timeline(pick, sources=[source]))

    def test_json_duplicate_keys_nan_and_nonobject_reject(self):
        self.assert_error("duplicate_json_key", lambda: parse_timeline_selection(
            '{"version":"x","version":"y","events":[]}'))
        self.assert_error("invalid_json", lambda: parse_timeline_selection('{"version":NaN,"events":[]}'))
        self.assert_error("selection_shape_invalid", lambda: parse_timeline_selection("[]"))
        self.assert_error("invalid_json", lambda: parse_timeline_selection("not JSON"))
        source = self.source()
        self.assertEqual(parse_timeline_selection(json.dumps(self.selection(source))), self.selection(source))

    def test_foreign_sources_and_assistant_claims_reject(self):
        source = self.source()
        self.assert_error("source_not_admitted", lambda: build_timeline(self.selection(source), sources=[]))
        for role in ("assistant", "model", "manager", "system"):
            other = self.source(role=role)
            self.assert_error("source_role_untrusted", lambda: self.timeline(other))

    def test_source_metadata_and_duplicate_sources_reject(self):
        for updates in ({"event_at": "2026-10-08"}, {"event_at": datetime(2026, 10, 8)},
                {"time_basis": "inferred"}, {"source_digest": "bad"}, {"scope": "self"}):
            source = self.source(**updates)
            with self.assertRaises(TimelineError):
                self.timeline(source)
        source = self.source()
        self.assert_error("source_identity_invalid", lambda: build_timeline(
            self.selection(source), sources=[source, source]))

    def test_record_identity_binds_source_digest_id_and_quote(self):
        source = self.source(text="Хочу футболку. Хочу подарунок.")
        pick = self.selection(source)
        pick["events"][0]["quote"] = "Хочу футболку."
        one = build_timeline(pick, sources=[source])["events"][0]
        changed = self.source(text=source["text"], source_digest="a" * 64)
        two = build_timeline(pick, sources=[changed])["events"][0]
        pick["events"][0]["quote"] = "Хочу подарунок."
        three = build_timeline(pick, sources=[source])["events"][0]
        self.assertEqual(len({one["event_id"], two["event_id"], three["event_id"]}), 3)

    def test_verified_retained_record_keeps_identity_timestamp_scope_and_hint(self):
        source = self.source(scope={"episode_id": 4, "recipient_id": "friend"})
        retained = self.timeline(source)["events"]
        pick = self.selection(source, topic="self_purchase")
        current = build_timeline(pick, sources=[source], retained_events=retained)
        self.assertEqual(current["events"], retained)
        self.assertEqual(current["events"][0]["topic_hint"], "other")

    def test_unselected_retained_record_is_explicitly_omitted(self):
        old, new = self.source(), self.source(2, "Цікавить інша футболка.")
        retained = self.timeline(old)["events"]
        timeline = build_timeline(self.selection(new), sources=[old, new], retained_events=retained)
        self.assertEqual([row["source_message_id"] for row in timeline["events"]], [2])
        self.assertEqual(timeline["coverage"]["omissions"],
            [{"source_message_id": 1, "reason": "retained_not_selected"}])

    def test_empty_selection_preserves_verified_retained_records(self):
        source = self.source()
        retained = self.timeline(source)["events"]
        timeline = build_timeline({"version": VERSION, "events": []},
            sources=[source], retained_events=retained)
        self.assertEqual(timeline["events"], retained)
        self.assertEqual(timeline["coverage"]["omitted_count"], 0)

    def test_changed_or_missing_retained_source_cannot_restore_old_record(self):
        source = self.source()
        retained = self.timeline(source)["events"]
        for sources, reason in (([], "retained_source_unavailable"),
                ([dict(source, source_digest="b" * 64)], "retained_source_changed"),
                ([dict(source, text="Інший текст.")], "retained_source_changed"),
                ([dict(source, event_at="2026-10-09T10:01:00+00:00")], "retained_source_changed"),
                ([dict(source, scope={"recipient_id": "self"})], "retained_source_changed")):
            timeline = build_timeline({"version": VERSION, "events": []},
                sources=sources, retained_events=retained)
            self.assertEqual(timeline["events"], [])
            self.assertEqual(timeline["coverage"]["omissions"][0]["reason"], reason)

    def test_retained_identity_forgery_extra_authority_and_conflicting_selection_reject(self):
        source = self.source(text="Хочу футболку. Хочу подарунок.")
        pick = self.selection(source)
        pick["events"][0]["quote"] = "Хочу футболку."
        retained = build_timeline(pick, sources=[source])["events"]
        for mutation in ({"event_id": "forged"}, {"purchase_confirmed": True}):
            forged = [dict(retained[0], **mutation)]
            self.assert_error("retained_event_invalid", lambda: build_timeline(
                {"version": VERSION, "events": []}, sources=[source], retained_events=forged))
        pick["events"][0]["quote"] = "Хочу подарунок."
        self.assert_error("retained_selection_conflict", lambda: build_timeline(
            pick, sources=[source], retained_events=retained))

    def test_privacy_contacts_are_named_omissions_without_redaction(self):
        for text in ("Мій телефон +38 (067) 123-45-67.", "Call me at +1 202 555 0199.",
                "Мій email person@example.com.", "Відділення №15.", "НП №7.",
                "Parcel locker 123.", "Адреса: вул. Шевченка 12.", "Напишіть @client_name.",
                "Мій контакт https://t.me/client_name."):
            source = self.source(text=text)
            timeline = self.timeline(source)
            self.assertEqual(timeline["events"], [], text)
            self.assertEqual(timeline["coverage"]["omissions"][0]["reason"], "contact_details", text)
        price = self.source(text="Ціна 1234567 грн.")
        self.assertEqual(self.timeline(price)["events"][0]["quote"], price["text"])

    def test_quote_event_source_and_render_limits_do_not_truncate_rows(self):
        source = self.source(text="а" * 240 + ".")
        timeline = self.timeline(source)
        self.assertEqual(timeline["events"], [])
        self.assertEqual(timeline["coverage"]["omissions"][0]["reason"], "quote_too_long")
        sources = [self.source(index) for index in range(1, MAX_EVENTS + 2)]
        self.assert_error("event_limit_exceeded", lambda: self.timeline(*sources))
        self.assert_error("source_limit_invalid", lambda: build_timeline(
            {"version": VERSION, "events": []}, sources=[self.source(index) for index in range(1, 66)]))
        source = self.source(text="\\" * 239 + ".")
        sources = [self.source(index, source["text"]) for index in range(1, 9)]
        timeline = self.timeline(*sources)
        self.assert_error("render_budget_exceeded", lambda: render_timeline(timeline))

    def test_chronology_is_backend_time_not_provider_selection_order(self):
        early, late = self.source(2), self.source(1, event_at="2026-10-09T10:00:00+00:00")
        timeline = self.timeline(late, early)
        self.assertEqual([row["source_message_id"] for row in timeline["events"]], [2, 1])
        for event in timeline["events"]:
            self.assertNotIn("supersedes", event)

    def test_all_interfaces_leave_input_unchanged_and_return_detached_values(self):
        source = self.source(scope={"episode_id": None, "recipient_id": None})
        pick = self.selection(source)
        original = deepcopy((source, pick))
        timeline = build_timeline(pick, sources=[source])
        valid = validate_timeline(timeline)
        render_timeline(timeline)
        self.assertEqual((source, pick), original)
        valid["events"][0]["scope"]["recipient_id"] = "self"
        self.assertIsNone(timeline["events"][0]["scope"]["recipient_id"])
        self.assertIsNone(source["scope"]["recipient_id"])

    def test_finite_hint_list_does_not_contain_business_authority(self):
        self.assertFalse({"paid", "fulfilled", "stock_available", "order_created", "discount"}.intersection(TOPIC_HINTS))
