"""Approved q23.1 can be read and upgraded, never used as q23.2 authority."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from management.knowledge.scenarios import reviewed_scenario_values
from management.models import BotInstruction, BotPolicyPublication, InstagramBotSettings
from management.services.ig_cooperation_policy import (
    LANGUAGES, capture_cooperation_policy, reviewed_values, validate_reviewed_source,
)
from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
from management.services.ig_policy_publication import (
    ActivePolicySnapshot, PolicyPublicationError, draft_state, load_active_policy_snapshot,
    select_policy_snapshot, snapshot_from_rows, snapshot_hash,
)
from management.services.ig_response_plan import _business_approval_reason
from management import tests_ig_q23_cooperation_policy as policy_fixture


def historical_rows():
    fixture = Path(__file__).resolve().parent / 'test_fixtures' / 'qa23_cooperation_q23_1.json'
    data = json.loads(fixture.read_text())
    if (data['fixture_schema'] != 1 or data['approved_publication'] != {
            'id': 8, 'version': 8,
            'snapshot_hash': 'c45762e2bdbb42e26defc6ac838c7b97f21726ef4e82975d52a00cb29a89b632'}
        or [(row['instruction_id'], row['locale']) for row in data['instructions']] !=
            [(26, 'uk'), (27, 'ru'), (28, 'en')]):
        raise AssertionError('historical public fixture identity changed')
    return data['instructions']


def row_values(key):
    values = reviewed_scenario_values(key)
    return {'title': values['title'], 'body': values['body'], 'is_active': values['active'],
        'priority': values['priority'], 'locale': values['locale'], 'trust_scope': values['trust_scope'],
        'intent_tags': ','.join(values['tags']), 'trigger_codes': values.get('triggers', []),
        'allowed_actions': values['allowed_actions'], 'programme_metadata': values['programme_metadata'],
        'reviewed_source': values.get('reviewed_source', {})}


class ApprovedHistoricalProvenanceTests(SimpleTestCase):
    def test_each_exact_approved_language_can_be_read_without_mutating_provenance(self):
        for row in historical_rows():
            with self.subTest(locale=row['locale']):
                actual = validate_reviewed_source(row['reviewed_source'], row['body'])
                self.assertEqual(actual, row['reviewed_source'])
                self.assertIsNot(actual, row['reviewed_source'])
                self.assertIn('Version: owner-2026-10-23.q23.1', row['body'])

    def test_every_historical_provenance_field_is_required_and_exactly_typed(self):
        for row in historical_rows():
            provenance = row['reviewed_source']
            for key in provenance:
                missing = {name: value for name, value in provenance.items() if name != key}
                wrong = {**provenance, key: 2 if key == 'version' else 'unapproved'}
                for changed in (missing, wrong):
                    with self.subTest(locale=row['locale'], field=key, changed=changed):
                        with self.assertRaisesRegex(ValueError, 'cooperation_source_body_mismatch'):
                            validate_reviewed_source(changed, row['body'])
            for changed in ({**provenance, 'unreviewed': True}, {**provenance, 'version': True},
                    {**provenance, 'version': 1.0}):
                with self.assertRaisesRegex(ValueError, 'cooperation_source_body_mismatch'):
                    validate_reviewed_source(changed, row['body'])

    def test_body_language_cross_source_and_self_signed_hashes_cannot_enter_history(self):
        from hashlib import sha256
        rows = historical_rows()
        for index, row in enumerate(rows):
            other = rows[(index + 1) % len(rows)]
            tampered = row['body'].replace('10–15%', '20%')
            self_signed = {**row['reviewed_source'],
                'source_hash': sha256((tampered + '\n').encode()).hexdigest(),
                'body_hash': sha256(tampered.encode()).hexdigest(),
                'section_hash': sha256(tampered.encode()).hexdigest()}
            for provenance, body in ((row['reviewed_source'], tampered),
                    (row['reviewed_source'], other['body']), (other['reviewed_source'], row['body']),
                    (self_signed, tampered), ({**self_signed, 'parser_version': 'cooperation-source.v2'}, tampered)):
                with self.subTest(locale=row['locale'], body=body[:80]):
                    with self.assertRaisesRegex(ValueError, 'cooperation_source_body_mismatch'):
                        validate_reviewed_source(provenance, body)

    def test_current_normal_source_validation_is_unchanged(self):
        for language in LANGUAGES:
            values = reviewed_values(language)
            self.assertEqual(validate_reviewed_source(values['reviewed_source'], values['body']),
                values['reviewed_source'])

    def test_readable_old_publication_cannot_grant_current_facts(self):
        rows = []
        for old in historical_rows():
            values = row_values('public_cooperation_' + old['locale'])
            rows.append(SimpleNamespace(pk=old['instruction_id'], **{
                **values, 'body': old['body'], 'reviewed_source': old['reviewed_source']}))
        snapshot = snapshot_from_rows(rows)
        old = ActivePolicySnapshot(8, 8, snapshot_hash(snapshot), 'instruction-set-v1', snapshot)
        for language in LANGUAGES:
            sources = [policy_fixture.source(policy_fixture.SOURCES[language])]
            self.assertIsNone(capture_cooperation_policy(old, sources))
            self.assertEqual(_business_approval_reason(
                SimpleNamespace(reply_text=policy_fixture.sentence(language, 'wholesale')),
                {'case_kind': 'collaboration_review'}), 'response_plan_business_terms_unconfirmed')
            selected = select_policy_snapshot(snapshot, locale=language,
                client_tags={'collaboration', language}, active_triggers={'business_cooperation'},
                budget_chars=48000)
            self.assertIn('instruction:' + str(next(row['instruction_id'] for row in historical_rows()
                if row['locale'] == language)), selected['selected_ids'])


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeHistoricalPublicationTransitionTests(TestCase):
    def setUp(self):
        # Only the three historical rows are archived. The other 25 test rows
        # are an explicit current skeleton, not copied production policies.
        InstagramBotSettings.objects.update_or_create(pk=1)
        InstagramBotSettings.objects.filter(pk=1).update(active_instruction_publication=None)
        BotInstruction.objects.all().delete()
        for identity in range(1, 21):
            BotInstruction.objects.create(pk=identity, title=f'Unrelated fixture {identity}',
                body=f'Preserve unrelated fixture {identity}.', is_active=False)
        for identity, key in ((21, 'custom_print'), (22, 'collaboration'), (23, 'ugc'),
                (24, 'size'), (25, 'objection')):
            values = row_values(key)
            if key == 'collaboration':
                values['body'] = 'Previous fixture collaboration guidance; the current reviewed body must replace this.'
            BotInstruction.objects.create(pk=identity, **values)
        for old in historical_rows():
            values = row_values('public_cooperation_' + old['locale'])
            values.update(body=old['body'], reviewed_source=old['reviewed_source'])
            BotInstruction.objects.create(pk=old['instruction_id'], **values)
        snapshot = snapshot_from_rows(BotInstruction.objects.all())
        self.old = BotPolicyPublication.objects.create(pk=8, version=8, kind='publish', schema_version=1,
            snapshot=snapshot, snapshot_hash=snapshot_hash(snapshot),
            compiler_version='instruction-set-v1', instruction_count=28)
        InstagramBotSettings.objects.filter(pk=1).update(active_instruction_publication=self.old,
            reply_permission_epoch=12)
        for target in ('management.services.instagram_bot._provider_http',
                'management.services.call_ai_analysis.gemini_generate_text'):
            guard = patch(target, side_effect=AssertionError('publication must perform zero provider I/O'))
            observed = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(observed.assert_not_called)

    def test_native_read_dry_run_and_cas_upgrade_preserve_other_24_rows(self):
        before = load_active_policy_snapshot()
        self.assertEqual(draft_state().snapshot, before.snapshot)
        plan = publish_reviewed_owner_scenarios()
        self.assertEqual(sorted(row['instruction_id'] for row in plan['changes'] if row['action'] == 'update'),
            [22, 26, 27, 28])
        self.assertFalse(any(row['action'] == 'create' for row in plan['changes']))
        self.assertFalse(plan['compatibility'])
        self.assertEqual(InstagramBotSettings.load().reply_permission_epoch, 12)
        self.assertEqual(InstagramBotSettings.load().active_instruction_publication_id, self.old.pk)
        sources = [policy_fixture.source(policy_fixture.SOURCES['en'])]
        self.assertIsNone(capture_cooperation_policy(before, sources))
        result = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=plan['draft_hash'], expected_head_hash=plan['head_hash'])
        after = load_active_policy_snapshot()
        self.assertEqual(after.version, 9)
        self.assertEqual(InstagramBotSettings.load().reply_permission_epoch, 13)
        self.assertNotEqual(after.publication_id, self.old.pk)
        changed = {22, 26, 27, 28}
        untouched_before = [row for row in before.snapshot['instructions'] if row['source_id'] not in changed]
        untouched_after = [row for row in after.snapshot['instructions'] if row['source_id'] not in changed]
        self.assertEqual(untouched_before, untouched_after)
        self.assertEqual(len(untouched_after), 24)
        self.assertEqual(len(after.snapshot['instructions']), 28)
        self.assertIsNotNone(capture_cooperation_policy(after, sources))
        self.old.refresh_from_db()
        self.assertEqual(self.old.snapshot, before.snapshot)
        self.assertEqual(self.old.snapshot_hash, before.snapshot_hash)
        self.assertTrue(result['changed'])
        self.assertFalse(result['provider_io'])
        again = publish_reviewed_owner_scenarios()
        replay = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=again['draft_hash'], expected_head_hash=again['head_hash'])
        self.assertEqual(replay['head_id'], result['head_id'])
        self.assertFalse(replay['changed'])
        self.assertEqual(InstagramBotSettings.load().reply_permission_epoch, 13)

    def test_stale_native_draft_cas_cannot_update_historical_head(self):
        plan = publish_reviewed_owner_scenarios()
        with self.assertRaises(PolicyPublicationError) as rejected:
            publish_reviewed_owner_scenarios(apply=True,
                expected_draft_hash='0' * 64, expected_head_hash=plan['head_hash'])
        self.assertEqual(rejected.exception.code, 'owner_publication_cas_changed')
        self.assertEqual(InstagramBotSettings.load().active_instruction_publication_id, self.old.pk)
        self.assertEqual(InstagramBotSettings.load().reply_permission_epoch, 12)
        self.assertEqual(BotInstruction.objects.count(), 28)
