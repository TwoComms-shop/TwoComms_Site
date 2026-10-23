"""Hosted beneficiary labels consume the frozen native item map, not SKU guesses."""
from copy import deepcopy

from django.test import TestCase, override_settings
from django.urls import reverse

from management.models import IgCheckoutRevision
from management.services import ig_revision_cart_binding as binding_contract
from storefront.tests import test_ig_checkout_view as view_fixtures


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class PublicCheckoutRecipientLabelsTests(TestCase):
    setUp = view_fixtures.InstagramCheckoutViewTests.setUp
    _open = view_fixtures.InstagramCheckoutViewTests._open

    def attach_binding(self, recipients=("self", "boyfriend"), *, corrupt_map=False):
        proposal = self.proposal
        scope = {"client_id": proposal.client_id, "episode_id": proposal.commercial_episode_id,
            "order_id": None, "reset_id": None, "reset_floor": 1, "source_namespace": "ig:label-fixture:1"}
        lines, readiness, quoted = [], {}, []
        for index, (item, recipient) in enumerate(zip(proposal.items.order_by("position", "id"), recipients, strict=True)):
            line_id = f"source-line-{index}"
            choices = {"product_id": item.product_id, "quantity": item.quantity, "size": item.size,
                "fit_option_code": item.fit_code, "option_values": item.option_values or {}}
            proof = {"source_message_id": index + 20, "source_digest": "a" * 64, "transition_id": index + 40}
            selection = {"schema": "source-selection.v1", "scope": {**scope, "line_id": line_id,
                "recipient_id": recipient, "session_id": 9, "generation": 1, "revision": 2},
                "fields": {key: {"value": value, "status": "confirmed", "authority": "customer_source", "source": deepcopy(proof)}
                    for key, value in choices.items()}}
            lines.append({"line_id": line_id, "recipient_id": recipient, "index": index, "source_selection": selection})
            quoted.append({"product_id": item.product_id, "qty": item.quantity, "size": item.size,
                "fit_option_code": item.fit_code, "color_variant_id": item.color_variant_id,
                "option_values": item.option_values or {}, "unit_price": str(item.quoted_unit_price),
                "line_total": str(item.quoted_line_total)})
            readiness[line_id] = {"scope": {**scope, "line_id": line_id, "recipient_id": recipient},
                "capture_digest": "b" * 64, "readiness": {"applicability_known": True, "can_issue_link": True,
                    "missing": [], "product": {"id": item.product_id}, "quantity": item.quantity,
                    "size": {"selected": item.size}, "fit": {"selected": item.fit_code},
                    "color": {"selected_variant_id": item.color_variant_id}, "options": {"selected": item.option_values or {}}}}
        capture = {"schema": "source-selections.v1", "status": "captured", "scope": scope, "session_id": 9,
            "generation": 1, "selection_revision": 2, "active_line_id": lines[0]["line_id"], "active_index": 0,
            "capture_digest": "b" * 64, "lines": lines}
        binding = binding_contract.build_cart_binding(captured_cart=capture, expected_scope=scope)
        bound = binding_contract.bind_quote_lines(binding=binding, quoted_items=quoted, readiness_by_line=readiness)
        self.assertEqual(bound.status, "ready", bound.reason)
        frozen_binding = binding_contract.frozen_cart_binding_payload(bound)
        if corrupt_map:
            frozen_binding["quote_line_map"][0]["recipient_id"] = "boyfriend"
        return IgCheckoutRevision.objects.create(proposal=proposal, revision=proposal.revision,
            digest=proposal.items_digest, snapshot={"items": quoted,
                "source_cart_binding": frozen_binding}, source="bot_create")

    def test_actual_public_page_labels_duplicate_sku_by_proposal_item_mapping(self):
        second = self.proposal.items.order_by("position", "id")[1]
        second.product = self.products[0]
        second.product_title = self.products[0].title
        second.save(update_fields=["product", "product_title"])
        self.attach_binding()
        before = (self.proposal.quoted_total, self.proposal.requested_payment_amount)
        response = self._open()
        self.assertContains(response, "data-beneficiary-label", count=2)
        self.assertEqual([row["recipient_label"] for row in response.context["items"]], ["Для вас", "Для хлопця"])
        self.assertEqual([row["size"] for row in response.context["items"]], ["M", "L"])
        self.assertContains(response, 'name="np_settlement_ref"')
        self.assertContains(response, 'name="full_name"')
        self.proposal.refresh_from_db()
        self.assertEqual((self.proposal.quoted_total, self.proposal.requested_payment_amount), before)

    def test_localized_known_labels_and_unknown_role_omit_arbitrary_text(self):
        self.attach_binding(("sister", "brother"))
        self._open()
        base = reverse("ig_checkout_proposal", kwargs={"proposal_id": self.proposal.public_id})
        for language, expected in (("uk", ["Для сестри", "Для брата"]), ("ru", ["Для сестры", "Для брата"]),
                ("en", ["For your sister", "For your brother"])):
            response = self.client.get(base, {"lang": language})
            self.assertEqual([row["recipient_label"] for row in response.context["items"]], expected)

    def test_legacy_offer_without_source_map_has_no_inferred_labels(self):
        response = self._open()
        self.assertNotContains(response, "data-beneficiary-label")

    def test_broken_quote_map_omits_all_labels(self):
        self.attach_binding(corrupt_map=True)
        response = self._open()
        self.assertNotContains(response, "data-beneficiary-label")

    def test_unknown_role_has_no_arbitrary_customer_label(self):
        self.attach_binding(("self", "<script>arbitrary recipient</script>"))
        response = self._open()
        self.assertEqual([row["recipient_label"] for row in response.context["items"]], ["Для вас", ""])
        self.assertContains(response, "data-beneficiary-label", count=1)

    def test_latest_proposal_revision_controls_labels_on_existing_clean_url(self):
        self.attach_binding()
        self._open()
        self.proposal.revision += 1
        self.proposal.save(update_fields=["revision"])
        self.attach_binding(("boyfriend", "self"))
        response = self.client.get(reverse("ig_checkout_proposal", kwargs={"proposal_id": self.proposal.public_id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["recipient_label"] for row in response.context["items"]], ["Для хлопця", "Для вас"])
