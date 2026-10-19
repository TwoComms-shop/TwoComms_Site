"""Pure compatibility checks for captured seed rows, never production I/O."""
from copy import deepcopy
from hashlib import sha256
import re

from django.test import SimpleTestCase, TestCase

from management.knowledge.publication_compat import (
    LEGACY_BODY_SHA256, LEGACY_COMPATIBILITY_TITLES, REVIEWED_BODIES,
    legacy_instruction_compatibility_plan, reviewed_legacy_instruction_values,
)


CAPTURED_LEGACY_ROWS = [{'active': True,
  'allowed_actions': [],
  'body': 'На питання про розмір пояснюй різницю classic/oversize. Якщо фасон не визначено, постав одне '
          'запитання про нього; якщо визначено — надішли в Direct зображення сітки саме цього товару й '
          'фасону та за потреби допоможи по замірах. Не гарантуй посадку без даних сітки та не обирай фасон '
          'чи розмір замість клієнта.',
  'id': 'instruction:3',
  'locale': 'all',
  'priority': 30,
  'programme_metadata': {},
  'source_id': 3,
  'tags': ['fit', 'size'],
  'title': 'Size And Fit',
  'triggers': [],
  'trust_scope': 'public_policy'},
 {'active': True,
  'allowed_actions': [],
  'body': 'Передоплата — індивідуально погоджена сума, яку менеджер або клієнт явно погодили в поточній '
          'переписці для цього замовлення. Не називай 200 грн і не перенось суму з іншого клієнта чи '
          'попереднього замовлення. Пояснюй спокійно: погоджена частина вноситься зараз, залишок — за '
          'узгодженим сценарієм.',
  'id': 'instruction:4',
  'locale': 'all',
  'priority': 40,
  'programme_metadata': {},
  'source_id': 4,
  'tags': ['payment', 'prepayment'],
  'title': 'Prepayment Objection',
  'triggers': [],
  'trust_scope': 'public_policy'},
 {'active': True,
  'allowed_actions': [],
  'body': 'Спочатку погодься з відчуттям клієнта й уточни, чи ціна — єдине, що зупиняє. Пояснюй лише ціну та '
          'параметри вибраного товару з каталогу; не виводь із цього загальну якість або перевагу бренду. '
          'Після цього додай JSON control {"kind":"objhandle","value":"price:value_breakdown"} (ніколи не '
          'додавай його до reply_text).',
  'id': 'instruction:8',
  'locale': 'all',
  'priority': 80,
  'programme_metadata': {},
  'source_id': 8,
  'tags': ['objection_price'],
  'title': 'IG Objection · Дорого',
  'triggers': [],
  'trust_scope': 'public_policy'},
 {'active': True,
  'allowed_actions': [],
  'body': 'Не сперечайся з ціною конкурента. Порівняй лише підтверджені параметри вибраного товару з '
          'каталогу; не узагальнюй склад, щільність, друк чи виробництво. Додай JSON control '
          '{"kind":"objhandle","value":"cheaper_elsewhere:value_breakdown"} (ніколи не додавай його до '
          'reply_text).',
  'id': 'instruction:14',
  'locale': 'all',
  'priority': 86,
  'programme_metadata': {},
  'source_id': 14,
  'tags': ['objection_cheaper_elsewhere'],
  'title': 'IG Objection · Є дешевше',
  'triggers': [],
  'trust_scope': 'public_policy'},
 {'active': True,
  'allowed_actions': [],
  'body': 'Для друку й догляду використовуй лише ярлик або підтверджену картку вибраного товару; не обіцяй '
          'строк служби чи невразливість. Додай JSON control '
          '{"kind":"objhandle","value":"print_quality:print_quality"} (ніколи не додавай його до '
          'reply_text).',
  'id': 'instruction:15',
  'locale': 'all',
  'priority': 87,
  'programme_metadata': {},
  'source_id': 15,
  'tags': ['objection_print_quality'],
  'title': 'IG Objection · Якість принта',
  'triggers': [],
  'trust_scope': 'public_policy'},
 {'active': True,
  'allowed_actions': [],
  'body': 'Не знецінюй інший бренд. Дай лише перевірювані відомості про вибраний товар або каталог, без '
          'загальних заяв про виробництво, тканину чи друк. Для доказів додай JSON control '
          '{"kind":"objhandle","value":"compare_brand:social_proof"} (ніколи не додавай його до reply_text), '
          'для розкладу цінності — JSON control {"kind":"objhandle","value":"compare_brand:value_breakdown"} '
          '(ніколи не додавай його до reply_text).',
  'id': 'instruction:18',
  'locale': 'all',
  'priority': 90,
  'programme_metadata': {},
  'source_id': 18,
  'tags': ['objection_compare_brand'],
  'title': 'IG Objection · Порівняння брендів',
  'triggers': [],
  'trust_scope': 'public_policy'}]


class OwnerLegacyPublicationCompatibilityTests(SimpleTestCase):
    def test_exact_captured_rows_get_only_body_changes_and_preserve_control_bytes(self):
        before = deepcopy(CAPTURED_LEGACY_ROWS)
        proposals = legacy_instruction_compatibility_plan(before)
        self.assertEqual(len(proposals), 6)
        controls = re.compile(r'JSON control \{[^{}\r\n]+\} \(ніколи не додавай його до reply_text\)')
        by_id = {item["source_id"]: item for item in before}
        for proposal in proposals:
            old = by_id[proposal["instruction_id"]]
            values = proposal["values"]
            self.assertNotEqual(values["body"], old["body"])
            self.assertEqual(proposal["before_body_digest"], LEGACY_BODY_SHA256[old["title"]])
            self.assertEqual(proposal["after_body_digest"], sha256(values["body"].encode()).hexdigest())
            for field in values:
                if field != "body":
                    self.assertEqual(values[field], old[field])
            self.assertEqual(controls.findall(values["body"]), controls.findall(old["body"]))
        self.assertEqual(before, CAPTURED_LEGACY_ROWS)

    def test_arbitrary_owner_edits_titles_and_provenance_never_get_replaced(self):
        for row in CAPTURED_LEGACY_ROWS:
            for field, value in (("body", row["body"] + " "), ("title", row["title"] + " edited"),
                                 ("reviewed_source", {"body_hash": "owned"})):
                candidate = {**deepcopy(row), field: value}
                self.assertIsNone(reviewed_legacy_instruction_values(candidate))
                self.assertEqual(legacy_instruction_compatibility_plan([candidate]), [])
        self.assertIsNone(reviewed_legacy_instruction_values({"title": "Size And Fit"}))

    def test_replay_is_idempotent_and_values_do_not_alias_input_metadata(self):
        rows = deepcopy(CAPTURED_LEGACY_ROWS)
        rows[0]["programme_metadata"] = {"required_for_scenarios": ["size"]}
        rows[0]["triggers"] = ["size_question"]
        rows[0]["active"] = False
        values = reviewed_legacy_instruction_values(rows[0])
        self.assertFalse(values["active"])
        values["programme_metadata"]["required_for_scenarios"].append("objection")
        self.assertEqual(rows[0]["programme_metadata"], {"required_for_scenarios": ["size"]})
        for row in rows:
            row.update(reviewed_legacy_instruction_values(row))
        self.assertEqual(legacy_instruction_compatibility_plan(rows), [])
        self.assertTrue(all(reviewed_legacy_instruction_values(row) for row in rows))

    def test_owner_rules_remove_conflicts_without_issuing_server_capabilities(self):
        self.assertEqual(set(REVIEWED_BODIES), LEGACY_COMPATIBILITY_TITLES)
        payment = REVIEWED_BODIES["Prepayment Objection"]
        for marker in ("200 грн", "утримуються всі 200 грн", "поточний checkout", "окремо", "Custom"):
            self.assertIn(marker, payment)
        self.assertNotIn("Не називай 200", payment)
        self.assertIn("не проси обрати classic/oversize", REVIEWED_BODIES["Size And Fit"])
        quality = REVIEWED_BODIES["IG Objection · Якість принта"]
        for marker in ("current_print_quality", "current_care", "оцінка засновника", "30°C", "не лабораторний"):
            self.assertIn(marker, quality)
        self.assertIn("не приписуй йому гіршу якість", REVIEWED_BODIES["IG Objection · Є дешевше"])
        self.assertTrue(all(not row["values"]["allowed_actions"] for row in legacy_instruction_compatibility_plan(CAPTURED_LEGACY_ROWS)))


class OwnerLegacyPublicationIntegrationTests(TestCase):
    def setUp(self):
        from management.models import BotInstruction, InstagramBotSettings
        from management.services.ig_policy_publication import draft_state, publish_instruction_policy

        InstagramBotSettings.load()
        source = next(row for row in CAPTURED_LEGACY_ROWS if row["source_id"] == 8)
        self.legacy = BotInstruction.objects.create(title=source["title"], body=source["body"],
            is_active=True, priority=source["priority"], locale="all", trust_scope="public_policy",
            intent_tags="objection_price", trigger_codes=["price_objection"],
            programme_metadata={}, allowed_actions=[])
        self.unrelated = BotInstruction.objects.create(title="Keep unrelated owner policy", body="Preserve this policy.", is_active=True)
        state = draft_state()
        publish_instruction_policy(expected_draft_revision=state.revision, expected_draft_hash=state.snapshot_hash,
            expected_head_id=None, expected_head_hash="", note="disposable known legacy baseline")

    def test_publish_upgrades_exact_legacy_body_preserves_routing_controls_and_is_idempotent(self):
        from management.models import BotInstruction
        from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
        from management.services.ig_policy_publication import load_active_policy_snapshot

        before = next(row for row in load_active_policy_snapshot().snapshot["instructions"] if row["source_id"] == self.legacy.pk)
        plan = publish_reviewed_owner_scenarios()
        self.assertEqual([row["instruction_id"] for row in plan["compatibility"]], [self.legacy.pk])
        self.assertEqual(BotInstruction.objects.count(), 2)
        result = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=plan["draft_hash"], expected_head_hash=plan["head_hash"])
        after = next(row for row in load_active_policy_snapshot().snapshot["instructions"] if row["source_id"] == self.legacy.pk)
        self.assertNotEqual(before["body"], after["body"])
        self.assertEqual({key: value for key, value in before.items() if key != "body"},
                         {key: value for key, value in after.items() if key != "body"})
        self.assertEqual(result["compatibility_updated_ids"], [self.legacy.pk])
        self.assertEqual(set(result["coverage"]), {"custom_print", "collaboration", "ugc", "size", "objection"})
        self.assertTrue(all(row["status"] == "declared_covered" for row in result["coverage"].values()))
        self.unrelated.refresh_from_db()
        self.assertEqual(self.unrelated.body, "Preserve this policy.")
        repeat = publish_reviewed_owner_scenarios()
        self.assertEqual(repeat["compatibility"], [])
        replay = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=repeat["draft_hash"], expected_head_hash=repeat["head_hash"])
        self.assertEqual(replay["head_id"], result["head_id"])
        self.assertFalse(replay["changed"])

    def test_owner_edited_active_body_requires_review_without_creating_scenarios(self):
        from management.models import BotInstruction
        from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
        from management.services.ig_policy_publication import (
            PolicyPublicationError, draft_state, load_active_policy_snapshot, publish_instruction_policy,
        )

        self.legacy.body += " Owner edited this policy."
        self.legacy.save(update_fields=["body"])
        state, head = draft_state(), load_active_policy_snapshot()
        publish_instruction_policy(expected_draft_revision=state.revision, expected_draft_hash=state.snapshot_hash,
            expected_head_id=head.publication_id, expected_head_hash=head.snapshot_hash, note="disposable owner-edited active policy")
        with self.assertRaises(PolicyPublicationError) as raised:
            publish_reviewed_owner_scenarios()
        self.assertEqual(raised.exception.code, "legacy_instruction_requires_review")
        self.assertEqual(BotInstruction.objects.count(), 2)
        self.legacy.refresh_from_db()
        self.assertTrue(self.legacy.body.endswith("Owner edited this policy."))

    def test_other_window_unrelated_draft_prevents_any_compatibility_publication(self):
        from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
        from management.services.ig_policy_publication import PolicyPublicationError, load_active_policy_snapshot

        original_head = load_active_policy_snapshot().snapshot_hash
        self.unrelated.body = "Other window pending review."
        self.unrelated.save(update_fields=["body"])
        with self.assertRaises(PolicyPublicationError) as raised:
            publish_reviewed_owner_scenarios()
        self.assertEqual(raised.exception.code, "unrelated_instruction_draft_changes")
        self.assertEqual(load_active_policy_snapshot().snapshot_hash, original_head)
        self.legacy.refresh_from_db()
        self.assertIn("Пояснюй лише ціну та параметри", self.legacy.body)
