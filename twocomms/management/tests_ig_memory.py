from types import SimpleNamespace

from django.test import SimpleTestCase

from management.services.bot_sales_classifier import _record_context_provenance


class CommercialMemoryProvenanceTests(SimpleTestCase):
    def test_context_keeps_source_and_bounded_conflict_history(self):
        memory = {}
        first = SimpleNamespace(pk=10, role="user")
        second = SimpleNamespace(pk=11, role="manager")
        _record_context_provenance(memory, {"size": "M"}, message=first, role="user")
        _record_context_provenance(memory, {"size": "L"}, message=second, role="manager")

        self.assertEqual(memory["size"], "L")
        record = memory["_provenance"]["size"]
        self.assertTrue(record["conflict"])
        self.assertEqual(record["source_message_id"], 11)
        self.assertEqual(record["source_role"], "manager")
        self.assertEqual(record["history"][0]["value"], "M")

    def test_legacy_flat_context_remains_available(self):
        memory = {"size": "M"}
        _record_context_provenance(memory, {"color": "black"}, message=None, role="user")
        self.assertEqual(memory["size"], "M")
        self.assertEqual(memory["color"], "black")
