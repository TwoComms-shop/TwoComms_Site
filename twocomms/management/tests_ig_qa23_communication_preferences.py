"""QA23: direct style choices keep their own targets in ordinary chat."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from management.services import ig_communication_preferences as preferences


class CommunicationPreferenceTargetTests(SimpleTestCase):
    def choices(self, text):
        return [(row["slot"], row["value"], row["scope"])
                for row in preferences.explicit_preferences(text)]

    def test_style_and_product_requests_keep_independent_targets(self):
        for text, expected in (
            ("Пиши коротко і покажи принти", [("verbosity", "short", "persistent")]),
            ("Отвечайте кратко и покажите принты", [("verbosity", "short", "persistent")]),
            ("Reply briefly and show the prints", [("verbosity", "short", "persistent")]),
            ("Без смайлов, а футболка есть?", [("emoji", "omit", "persistent")]),
            ("Покажи принти і відповідай коротко", [("verbosity", "short", "persistent")]),
            ("Пиши коротко без смайлов а худи тёплое?", [
                ("verbosity", "short", "persistent"), ("emoji", "omit", "persistent")]),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), expected)

    def test_negation_does_not_leak_between_style_targets(self):
        for text, expected in (
            ("Не коротко а подробно", [("verbosity", "detailed", "persistent")]),
            ("Not short but detailed", [("verbosity", "detailed", "persistent")]),
            ("Не пиши коротко но эмодзи можно", [("emoji", "allow", "persistent")]),
            ("Не пиши коротко, но эмодзи можно", [("emoji", "allow", "persistent")]),
            ("Пишіть без емодзі докладніше", [
                ("emoji", "omit", "persistent"), ("verbosity", "detailed", "persistent")]),
            ("Please reply without emojis in detail", [
                ("emoji", "omit", "persistent"), ("verbosity", "detailed", "persistent")]),
            ("Do not reply briefly but allow emojis", [("emoji", "allow", "persistent")]),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), expected)

    def test_positive_emoji_suffix_belongs_only_to_adjacent_direct_style_request(self):
        for text in ("Пиши коротко и с эмодзи", "Пишіть коротко і з емодзі",
                     "Reply briefly and with emojis"):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), [
                    ("verbosity", "short", "persistent"), ("emoji", "allow", "persistent")])
        self.assertEqual(self.choices("Зараз коротко і зі смайлами"), [
            ("verbosity", "short", "current_turn"), ("emoji", "allow", "current_turn")])
        self.assertEqual(self.choices("Reply briefly and without emojis"), [
            ("verbosity", "short", "persistent"), ("emoji", "omit", "persistent")])
        for text in ("with emojis", "Пишіть на принті коротко і з емодзі",
                     "Print briefly and with emojis", "Раньше кратко и с эмодзи",
                     "Менеджер каже: коротко і з емодзі", '«Reply briefly and with emojis»',
                     "Do not reply briefly and with emojis"):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), [])
        for text in ("Reply briefly and show the hoodie and with emojis",
                     "Пиши коротко и покажи принты и с эмодзи",
                     "Reply briefly and with emojis on the shirt"):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), [("verbosity", "short", "persistent")])

    def test_art_quotes_reports_and_historical_style_do_not_grant_preferences(self):
        for text in (
            'Принт «Без смайлов», коротко',
            'Пишіть на принті коротко і без емодзі',
            'Print the slogan briefly and without emojis',
            '«Не коротко а подробно»',
            '> Reply briefly and show prints',
            'Менеджер каже: пишіть коротко, без емодзі',
            'Раньше: кратко и без эмодзи',
            'Раніше: коротко, без емодзі',
            'I write short answers and without emojis',
            'Reply to me and show short sleeves',
            'Please show a print and short sleeves',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), [])

    def test_temporary_and_later_correction_keep_existing_scope(self):
        self.assertEqual(self.choices("Зараз коротко і покажи принти без емодзі"),
                         [("verbosity", "short", "current_turn")])
        self.assertEqual(self.choices("Пиши коротко а тепер пиши докладніше"), [
            ("verbosity", "short", "persistent"), ("verbosity", "detailed", "persistent")])
        self.assertEqual(self.choices("Пиши без смайлов. Теперь эмодзи можно."), [
            ("emoji", "omit", "persistent"), ("emoji", "allow", "persistent")])
        self.assertEqual(self.choices("Please reply, as usual"), [
            ("verbosity", "default", "persistent"), ("emoji", "default", "persistent")])

    def test_corrected_choices_keep_source_proof_and_ignore_older_or_temporary_updates(self):
        at = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        scope = {"client_id": 91, "namespace": "qa23-local", "reset_id": None,
                 "reset_floor": 1, "erasure_at": ""}

        def source(identity, text, seconds):
            return SimpleNamespace(pk=identity, text=text, provider_created_at=at + timedelta(seconds=seconds),
                created_at=at + timedelta(seconds=seconds), client_id=91, role="user", source="webhook",
                sender_id="qa23-customer", mid=f"qa23-{identity}", provider_message_id="",
                reply_to_provider_message_id="", quick_reply_payload="")

        def update(state, row):
            return preferences.update_communication_preferences(state, source=row,
                namespace=scope["namespace"], scope=scope)

        old = update({}, source(11, "Пиши коротко і покажи принти", 1))
        corrected = update({"communication_preferences": old},
                           source(12, "Не коротко а подробно. Эмодзи можно.", 2))
        self.assertEqual(corrected["slots"]["verbosity"]["value"], "detailed")
        self.assertEqual(corrected["slots"]["verbosity"]["source_message_id"], 12)
        self.assertEqual(corrected["slots"]["verbosity"]["supersedes_source_message_id"], 11)
        self.assertEqual(corrected["slots"]["emoji"]["value"], "allow")
        self.assertEqual(len(corrected["slots"]["emoji"]["source_digest"]), 64)
        for row in (source(13, "Пиши коротко без эмодзи", 0),
                    source(14, "Зараз коротко без емодзі", 3)):
            with self.subTest(text=row.text):
                self.assertEqual(update({"communication_preferences": corrected}, row), corrected)
