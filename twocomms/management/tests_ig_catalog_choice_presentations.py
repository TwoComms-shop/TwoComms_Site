"""Causal native-payload/receipt tests without fixtures or provider traffic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from management.services import ig_catalog_choice_presentations as choices
from management.services.ig_revision_outbox import _digest, _normalize_specs, _safe_bindings


@override_settings(SITE_BASE_URL="https://twocomms.test", SECRET_KEY="catalog-choice-causal-test")
class CatalogChoicePresentationTests(SimpleTestCase):
    def scope(self):
        return {"client_id": 7, "igsid": "customer-7", "episode_id": 9,
            "session_id": 11, "generation": 1, "selection_revision": 3,
            "candidate_generation": 2, "line_id": "line-1", "recipient_id": "self",
            "selection_digest": "a" * 64, "reset_floor": 1,
            "namespace": "instagram_login:owner-1", "revision_snapshot_digest": "b" * 64,
            "source_message_ids": [10], "settings_id": 4, "settings_epoch": 2,
            "client_epoch": 5, "publication_id": 6, "publication_version": 1,
            "publication_hash": "c" * 64, "locale": "uk", "privacy_clear": True,
            "ordered_product_ids": [101, 102, 103], "page": 0}

    def cards(self, count=3):
        return [{"product_id": 101 + index, "title": f"Model {index + 1}",
            "image_url": f"https://twocomms.test/media/products/choice_{index}.jpg",
            "asset_ref": {"asset_kind": "main_image", "asset_id": 101 + index,
                "product_id": 101 + index, "variant_id": 0, "mode": "presentation_generic",
                "part_index": 0, "url_digest": "a" * 64, "payload_digest": "b" * 64,
                "projection_digest": "c" * 64, "reference_digest": "d" * 64, "legacy_response": False},
            "asset_selected_variant_id": 0,
            "configuration_selector": {"variant_id": None, "option_values": None, "fit_code": None},
            "configuration_digest": "e" * 64} for index in range(count)]

    def prepare(self, count=3):
        scope = self.scope()
        scope["ordered_product_ids"] = [row["product_id"] for row in self.cards(count)]
        return choices.build_catalog_choice_effects(12, scope, self.cards(count))

    def source(self, payload):
        return SimpleNamespace(pk=20, client_id=7, sender_id="customer-7", role="user",
            source="webhook", status="done", provider_message_id="customer-mid", mid="customer-mid",
            provider_namespace="instagram_login:owner-1", quick_reply_payload=payload,
            text="Обрати", provider_created_at=datetime(2026, 10, 14, tzinfo=timezone.utc) + timedelta(minutes=1))

    def receipts(self, preparation, *, state="sent"):
        scope = preparation.manifest["scope"]
        rows = []
        for index, spec in enumerate(preparation.effects):
            rows.append(SimpleNamespace(pk=30 + index, revision_id=12, group=spec["group"], kind=spec["kind"],
                payload=spec["payload"], payload_digest=_digest(spec["payload"]),
                projection_metadata=spec["projection_metadata"], projection_digest=_digest(spec["projection_metadata"]),
                state=state if index == 0 else "cancelled", provider_message_id="native-mid" if index == 0 and state == "sent" else "",
                failure_code="", provider_http_status=200 if state == "sent" else None,
                activation_group="template" if index else "", activation_part_index=0 if index else None,
                activation_failure_code="provider_rejected" if index else "", part_index=index - 1 if index else 0,
                part_count=len(preparation.effects) - 1 if index else 1,
                revision_snapshot_digest=scope["revision_snapshot_digest"], revision=SimpleNamespace(snapshot_digest=scope["revision_snapshot_digest"]),
                client_permission_epoch=scope["client_epoch"], recipient_igsid=scope["igsid"], provider_namespace=scope["namespace"],
                settings_id_snapshot=scope["settings_id"], settings_permission_epoch=scope["settings_epoch"],
                publication_id=scope["publication_id"], publication_version=scope["publication_version"],
                publication_hash=scope["publication_hash"], plan_digest="f" * 64,
                terminal_at=datetime(2026, 10, 14, tzinfo=timezone.utc), purpose="normal_reply",
                generation_request_id="request", generation_model="model"))
        return rows

    def deliver(self, source, rows, *, fresh=False):
        with patch("management.models.IgRevisionDeliveryEffect.objects.filter") as queried:
            queried.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = rows
            return choices.delivered_catalog_choice(SimpleNamespace(pk=7, igsid="customer-7"), source, fresh=fresh)

    def payload(self, preparation, index=0):
        elements = preparation.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"]
        return elements[index]["buttons"][0]["payload"]

    def test_three_products_have_one_native_message_one_image_each_and_exact_order(self):
        prepared = self.prepare()
        elements = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"]
        self.assertEqual(len(prepared.effects), 5)
        self.assertEqual([element["image_url"] for element in elements], [card["image_url"] for card in self.cards()])
        self.assertEqual([row["product_id"] for row in prepared.manifest["cards"]], [101, 102, 103])
        self.assertTrue(all([button["title"] for button in row["buttons"]] == ["Обрати", "Таблиця розмірів"] for row in elements))
        self.assertNotIn("price", str(prepared.effects))
        self.assertNotIn("in_stock", str(prepared.effects))

    def test_one_product_is_one_card(self):
        prepared = self.prepare(1)
        elements = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"]
        self.assertEqual(len(elements), 1)

    def test_four_cards_are_refused_without_silent_truncation(self):
        with self.assertRaisesRegex(ValueError, "cardinality"):
            self.prepare(4)

    def test_native_and_fallback_preserve_same_short_signed_choices(self):
        prepared = self.prepare()
        native = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"]
        fallback = prepared.effects[-1]["payload"]["message"]
        self.assertNotIn("attachment", fallback)
        self.assertEqual([quick["payload"] for quick in fallback["quick_replies"]],
            [button["payload"] for card in native for button in card["buttons"]])
        self.assertTrue(all(len(quick["payload"]) < 100 for quick in fallback["quick_replies"]))

    def test_scope_order_and_image_mutation_change_action_signatures(self):
        original = self.prepare()
        scope = self.scope()
        scope["episode_id"] += 1
        changed = choices.build_catalog_choice_effects(12, scope, self.cards())
        self.assertNotEqual(self.payload(original), self.payload(changed))
        scope = self.scope()
        scope["ordered_product_ids"] = list(reversed(scope["ordered_product_ids"]))
        reordered = choices.build_catalog_choice_effects(12, scope, list(reversed(self.cards())))
        self.assertNotEqual(self.payload(original), self.payload(reordered))

    def test_outbox_accepts_sealed_projection_and_refuses_detached_image(self):
        prepared = self.prepare()
        rows = _normalize_specs(prepared.effects, "customer-7")
        self.assertEqual(len(rows), 5)
        corrupted = deepcopy(prepared.effects)
        corrupted[0]["payload"]["message"]["attachment"]["payload"]["elements"][0]["image_url"] = self.cards()[1]["image_url"]
        with self.assertRaisesRegex(ValueError, "projection"):
            _normalize_specs(corrupted, "customer-7")

    def test_content_free_references_fit_existing_authority_limit(self):
        reference = choices.catalog_choice_reference(self.prepare().manifest)
        encoded = _safe_bindings([{"selector": {"catalog_choice_reference": reference}}])
        self.assertEqual(len(encoded), 1)
        self.assertNotIn("https://", str(reference))

    def test_confirmed_native_choice_resolves_stable_product_id(self):
        prepared = self.prepare()
        result = self.deliver(self.source(self.payload(prepared, 1)), self.receipts(prepared))
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["action"]["product_id"], 102)
        self.assertEqual(result["provider_message_id"], "native-mid")

    def test_unknown_native_cannot_be_overridden_by_forged_sent_fallback(self):
        prepared = self.prepare()
        rows = self.receipts(prepared, state="unknown")
        for index, row in enumerate(rows[1:]):
            row.state, row.provider_message_id = "sent", f"fallback-mid-{index}"
        self.assertFalse(self.deliver(self.source(self.payload(prepared)), rows)["ready"])

    def test_confirmed_four_xx_activates_exact_fallback_receipt(self):
        prepared = self.prepare()
        rows = self.receipts(prepared, state="definite_failed")
        rows[0].provider_http_status, rows[0].failure_code = 400, "provider_rejected"
        for index, row in enumerate(rows[1:]):
            row.state, row.provider_message_id = "sent", f"fallback-mid-{index}"
        result = self.deliver(self.source(self.payload(prepared)), rows)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["effect_id"], rows[-1].pk)

    def test_foreign_namespace_and_changed_signature_are_not_choices(self):
        prepared = self.prepare()
        source = self.source(self.payload(prepared))
        source.provider_namespace = "instagram_login:foreign"
        self.assertFalse(self.deliver(source, self.receipts(prepared))["ready"])
        source = self.source(self.payload(prepared)[:-1] + ("0" if self.payload(prepared)[-1] != "0" else "1"))
        self.assertFalse(self.deliver(source, self.receipts(prepared))["ready"])

    def test_fallback_transcript_has_actual_text_no_fake_photo_attachment(self):
        from management.services.ig_revision_reply_projection import _project_part
        prepared = self.prepare()
        row = self.receipts(prepared)[-1]
        with patch("management.models.InstagramBotMessage.objects.get_or_create", return_value=(SimpleNamespace(pk=40), True)) as created:
            _message, shown = _project_part(SimpleNamespace(pk=7, igsid="customer-7"), row)
        self.assertEqual(created.call_args.kwargs["defaults"]["attachments"], "")
        self.assertIn("Оберіть товар", created.call_args.kwargs["defaults"]["text"])
        self.assertIsNone(shown)

    def test_configured_colors_are_stable_variant_actions_without_inventory_filter(self):
        actions = [{"kind": "color", "product_id": 101, "variant_id": 210 + index,
            "value": 210 + index, "label": label} for index, label in enumerate(("Black", "White", "Blue", "Red"))]
        prepared = choices.build_catalog_choice_effects(12, self.scope(), self.cards(1), presentation_kind="color", actions=actions)
        message = prepared.effects[0]["payload"]["message"]
        self.assertEqual(len(message["quick_replies"]), 4)
        self.assertEqual(prepared.manifest["actions"], actions)
        self.assertNotIn("attachment", message)
        self.assertTrue(choices.validate_catalog_choice_metadata(prepared.effects[0]["projection_metadata"],
            payload=prepared.effects[0]["payload"], group="template", kind="text"))

    def test_disabled_sizes_can_only_be_absent_from_captured_action_set(self):
        actions = [{"kind": "size", "product_id": 101, "size": value, "value": value, "label": value}
            for value in ("S", "L")]
        prepared = choices.build_catalog_choice_effects(12, self.scope(), self.cards(1), presentation_kind="size", actions=actions)
        buttons = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["buttons"]
        self.assertEqual([button["title"] for button in buttons], ["S", "L"])
        self.assertNotIn("M", [action["size"] for action in prepared.manifest["actions"]])

    def test_checkout_control_keeps_existing_catalog_image_lane(self):
        response = SimpleNamespace(control={"show_products": "101", "paylink": "full"})
        result = choices.prepare_catalog_choice_presentations(None, response, None)
        self.assertFalse(result.handled)

    def test_four_candidate_order_is_retained_by_signed_more_action(self):
        scope = self.scope()
        scope["ordered_product_ids"].append(104)
        prepared = choices.build_catalog_choice_effects(12, scope, self.cards())
        action = prepared.manifest["actions"][-1]
        self.assertEqual(action["kind"], "page")
        self.assertEqual(action["page"], 1)
        self.assertEqual(prepared.manifest["scope"]["ordered_product_ids"], [101, 102, 103, 104])
        self.assertEqual(prepared.effects[0]["payload"]["message"]["quick_replies"][0]["title"], "Показати ще")

    def test_browsing_without_session_captures_scope_without_bootstrap(self):
        client = SimpleNamespace(pk=7, igsid="customer-7", current_commercial_episode_id=None,
            privacy_erasure_started_at=None, reply_permission_epoch=5)
        revision = SimpleNamespace(client=client, snapshot_digest="b" * 64,
            bundle_snapshot={"sources": [{"message_id": 10, "source_namespace": "instagram_login:owner-1"}]})
        settings = SimpleNamespace(pk=4, reply_permission_epoch=2,
            active_instruction_publication=SimpleNamespace(pk=6, version=1, snapshot_hash="c" * 64))
        with patch("management.models.IgCommerceSelectionSession.objects.filter") as sessions, patch(
            "management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=1):
            sessions.return_value.first.return_value = None
            scope = choices._scope(revision, settings, "uk")
        self.assertIsNone(scope["session_id"])
        self.assertEqual(scope["selection_revision"], 0)
        self.assertEqual(scope["recipient_id"], "self")

    def test_sessionless_card_refuses_unrelated_session_created_after_presentation(self):
        scope = self.scope()
        scope.update(session_id=None, generation=0, selection_revision=0, episode_id=None)
        manifest = self.prepare().manifest
        manifest["scope"] = scope
        client = SimpleNamespace(pk=7, igsid="customer-7", current_commercial_episode_id=9,
            privacy_erasure_started_at=None, reply_permission_epoch=5)
        settings = SimpleNamespace(pk=4, reply_permission_epoch=2,
            active_instruction_publication=SimpleNamespace(pk=6, version=1, snapshot_hash="c" * 64))
        with patch("management.models.IgCommerceSelectionSession.objects.filter") as sessions, patch(
            "management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=1), patch.object(
            choices, "_own_product_continuation", return_value=False):
            sessions.return_value.first.return_value = SimpleNamespace(commercial_episode_id=9)
            reason = choices.current_catalog_choice_scope(manifest, client, settings, allow_product_continuation=True)
        self.assertEqual(reason, "catalog_choice_session_changed")

    def test_partial_fallback_photo_never_admits_later_selection(self):
        prepared = self.prepare()
        rows = self.receipts(prepared, state="definite_failed")
        rows[0].provider_http_status, rows[0].failure_code = 400, "provider_rejected"
        for index, row in enumerate(rows[1:]):
            row.state, row.provider_message_id = "sent", f"fallback-mid-{index}"
        rows[2].state, rows[2].provider_message_id = "unknown", ""
        self.assertFalse(self.deliver(self.source(self.payload(prepared)), rows)["ready"])

    def test_v1_text_only_receipt_remains_verifiable_without_inventing_photos(self):
        old = choices.build_catalog_choice_effects(12, self.scope(), self.cards(), _legacy=True)
        self.assertEqual(len(old.effects), 2)
        self.assertNotIn("attachment", old.effects[-1]["payload"]["message"])
        for effect in old.effects:
            self.assertTrue(choices.validate_catalog_choice_metadata(effect["projection_metadata"],
                payload=effect["payload"], group=effect["group"], kind=effect["kind"]))

    def test_real_fallback_photo_projects_only_its_own_product_and_attachment(self):
        from management.services.ig_revision_reply_projection import _project_part
        prepared = self.prepare()
        row = self.receipts(prepared)[2]
        with patch("management.models.InstagramBotMessage.objects.get_or_create", return_value=(SimpleNamespace(pk=40), True)) as created:
            _message, shown = _project_part(SimpleNamespace(pk=7, igsid="customer-7"), row)
        self.assertEqual(shown["product_id"], 102)
        self.assertEqual(shown["position"], 2)
        self.assertEqual(created.call_args.kwargs["defaults"]["attachments"],
            '["https://twocomms.test/media/products/choice_1.jpg"]')
