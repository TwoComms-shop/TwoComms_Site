"""Offline integration of persisted ingress, stats contracts and actual UI renderers."""
from datetime import timedelta
from pathlib import Path
import json
import shutil
import subprocess
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.bot_access import VIEW_IG_CONVERSATION_PII_PERMISSION
from management.models import (
    IgClient, IgCommercialEpisodeEvent, IgDeal, IgPaymentConfirmationReview,
    IgPaymentReviewDecision, InstagramBotMessage, InstagramBotSettings,
)
from management.services import instagram_bot
from management.services.ig_conversion_cohorts import ATTRIBUTION_KEY, OBSERVATION_KEY, build_conversion_cohort_report
from orders.models import Order


@override_settings(ROOT_URLCONF="twocomms.urls_management", ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False)
class ConversionMetricsIntegrationTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user("conversion-integration", is_staff=True)
        app_label, codename = VIEW_IG_CONVERSATION_PII_PERMISSION.split(".", 1)
        self.staff.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
        self.client.force_login(self.staff)
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.allowed_senders = ""
        self.settings.save()
        self.at = timezone.now() - timedelta(days=20)

    def enqueue(self, text, *, sender="metrics-integration", mid="metrics-integration-mid", referral=None):
        with patch.object(instagram_bot, "_schedule_inbound_analysis"), patch.object(instagram_bot, "_enqueue_memory_source_event"), patch.object(instagram_bot, "_enqueue_payment_observation_event"):
            self.assertTrue(instagram_bot.enqueue_inbound(self.settings, sender_id=sender, text=text, mid=mid, received_at=self.at, referral_payload=referral, persistence_only=True))
        return InstagramBotMessage.objects.get(mid=mid)

    def test_persisted_commercial_inbound_survives_processing_failure_and_enters_unknown_history_baseline(self):
        row = self.enqueue("Скільки коштує футболка?", referral={"ad_id": "AD-A", "source": "ads"})
        observation = IgCommercialEpisodeEvent.objects.get(event_type="commercial_inquiry_observed")
        self.assertEqual(observation.evidence[OBSERVATION_KEY]["classification"], "retail")
        self.assertEqual(observation.evidence[OBSERVATION_KEY]["history_status"], "unknown")
        self.assertEqual(observation.evidence[ATTRIBUTION_KEY]["ad_id"], "AD-A")
        self.assertEqual(observation.evidence[ATTRIBUTION_KEY]["source_message_ids"], [row.pk])

        claimed = instagram_bot._claim_exact_row(row)
        self.assertIsNotNone(claimed)

        def fail_without_transport(_settings, claimed):
            claimed.send_state = "sending"
            raise RuntimeError("offline mocked reply boundary failure")

        with patch.object(instagram_bot, "_claim_next", return_value=claimed), patch("management.services.ig_revision_live.process_revision_finalizations", return_value=0), patch("management.services.ig_revision_live.process_pending_revisions", return_value=0), patch("management.services.ig_revision_live.revision_execution_enabled", return_value=False), patch.object(instagram_bot, "_send_rate_limit_backoff_active", return_value=False), patch.object(instagram_bot, "_gemini_backoff_active", return_value=False), patch.object(instagram_bot, "_process_one", side_effect=fail_without_transport), patch.object(instagram_bot, "_finalize_turn_lifecycle"), patch.object(instagram_bot, "maintenance_status", return_value={"active": False}):
            instagram_bot.process_pending(self.settings, max_items=1)
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        report = build_conversion_cohort_report()
        self.assertEqual(report["primary"]["entered"], 0)
        self.assertEqual(report["segments"]["unknown"]["windows"]["14"]["matured"], 1)
        self.assertEqual(report["segments"]["unknown"]["windows"]["14"]["paid"], 0)

    def test_current_own_classification_excludes_receipt_service_creator_and_unknown_language(self):
        cases = [
            ("Ось квитанція, я оплатила 200 грн", "service"),
            ("Де моє замовлення?", "service"),
            ("Пропоную співпрацю, я UGC creator", "collaboration"),
            ("こんにちは", "unknown"),
            ("Де моє замовлення? Також хочу купити ще одну футболку.", "retail"),
            ("Хочу свій принт на футболці", "custom"),
            ("Де моє замовлення? Яка ціна на футболку?", "retail"),
            ("Я фотограф и відеограф, можу створити для вашого бренду фото та відеоконтент.", "ugc"),
        ]
        for index, (text, expected) in enumerate(cases):
            with self.subTest(text=text):
                row = self.enqueue(text, sender=f"metrics-case-{index}", mid=f"metrics-case-mid-{index}")
                event = IgCommercialEpisodeEvent.objects.get(episode__client=row.client, event_type="commercial_inquiry_observed")
                self.assertEqual(event.evidence[OBSERVATION_KEY]["classification"], expected)
        report = build_conversion_cohort_report()
        self.assertEqual(report["segments"]["unknown"]["entered"], 3)
        self.assertEqual(report["coverage"]["unclassified"], 1)

    def test_hidden_own_commercial_message_remains_observed_without_enabling_reply(self):
        lead = IgClient.get_or_create_for_sender("metrics-hidden-ingress")
        lead.hidden_at = timezone.now()
        lead.save(update_fields=["hidden_at", "updated_at"])
        row = self.enqueue("Ціна?", sender=lead.igsid)
        self.assertEqual(row.status, "done")
        self.assertEqual(build_conversion_cohort_report()["segments"]["unknown"]["entered"], 1)

    def test_stats_api_adds_cohorts_and_preserves_current_ad_snapshot_contract(self):
        self.enqueue("Ціна?", referral={"ad_id": "AD-IMMUTABLE", "source": "ads"})
        lead = IgClient.objects.get(igsid="metrics-integration")
        lead.ad_id = "AD-CURRENT"
        lead.save(update_fields=["ad_id", "updated_at"])
        response = self.client.get(reverse("management_bot_stats_api") + "?days=0")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        report = payload["conversion_cohorts"]
        self.assertEqual(report["version"], "ig-commercial-conversion-v1")
        self.assertIsNone(report["primary"]["rate_pct"])
        self.assertEqual(report["segments"]["unknown"]["entered"], 1)
        self.assertEqual(report["segments"]["unknown"]["acquisition_sources"][0]["ad_id"], "AD-IMMUTABLE")
        self.assertTrue(report["scope"]["includes_hidden_clients"])
        self.assertFalse(report["scope"]["requires_successful_bot_reply"])
        self.assertEqual(payload["ad_analytics"]["attribution"]["basis"], "current_client_snapshot")

    def test_stats_permission_still_requires_actual_pii_capability(self):
        other = get_user_model().objects.create_user("conversion-no-pii", is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("management_bot_stats_api")).status_code, 403)

    def test_actual_journey_api_labels_unpaid_consultation_then_one_confirmed_purchase(self):
        row = self.enqueue("Хочу купити футболку")
        lead = row.client
        episode = lead.commercial_episodes.get(open_slot=1)
        order = Order.objects.create(full_name="Metrics", phone="380501112233", total_sum="1000", payment_status="unpaid")
        episode.intended_order = order
        episode.save(update_fields=["intended_order", "updated_at"])
        url = reverse("management_bot_client_detail_api", args=[lead.pk]) + "?journey_only=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        selector = response.json()["journey"]["viewed_episode"]
        self.assertEqual(selector["label"], "Консультація 1")
        self.assertIsNone(selector["purchase_ordinal"])
        review = IgPaymentConfirmationReview.objects.create(client=lead, order=order, status="confirmed", dedupe_key="integration-manager-paid")
        IgPaymentReviewDecision.objects.create(client=lead, review=review, decision="manager_verified", verification_source="manager", verification_scope="prepayment", confirmed_amount="200", actor_source="management_user", actor_external_id=str(self.staff.pk))
        response = self.client.get(url)
        selector = response.json()["journey"]["viewed_episode"]
        self.assertEqual(selector["label"], "Покупка 1")
        self.assertEqual(selector["purchase_ordinal"], 1)
        self.assertTrue(selector["confirmed_purchase"])
        self.assertEqual(response.json()["journey"]["episodes"]["confirmed_purchase_count"], 1)


class ConversionMetricsUiTests(SimpleTestCase):
    def run_node(self, program):
        executable = shutil.which("node")
        self.assertIsNotNone(executable, "Local Node runtime is required to execute UI regressions")
        result = subprocess.run([executable, "-e", program], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_journey_label_renderer_ignores_unpaid_old_purchase_labels(self):
        path = Path(__file__).parent / "static/management/ig_journey.js"
        self.run_node("const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');const source=fs.readFileSync(" + json.dumps(str(path)) + ",'utf8').replace('window.TwcJourney={create:options=>new Journey(options)};','window.TwcJourney={episodeLabel};');const sandbox={window:{}};vm.runInNewContext(source,sandbox);const label=sandbox.window.TwcJourney.episodeLabel;assert.equal(label({sequence:3,label:'Покупка 3',purchase_number:3}),'Консультація 3');assert.equal(label({sequence:9,confirmed_purchase:true,purchase_ordinal:1}),'Покупка 1');assert.equal(label({sequence:9,confirmed_purchase:true,purchase_ordinal:null}),'Консультація 9');")

    def test_real_stats_renderers_show_unknown_baseline_and_unmeasured_dash(self):
        path = Path(__file__).parent / "templates/management/bot.html"
        self.run_node("const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');const source=fs.readFileSync(" + json.dumps(str(path)) + ",'utf8');const table=source.slice(source.indexOf('    function tableSection('),source.indexOf('    function compactCohortFacts('));const render=source.slice(source.indexOf('    function conversionCount('),source.indexOf('    function renderStageBars('));const sandbox={num:v=>Number(v)||0,esc:v=>String(v??''),numberFormat:new Intl.NumberFormat('uk-UA'),moneyFormat:new Intl.NumberFormat('uk-UA'),fmtDateTime:v=>v};vm.createContext(sandbox);vm.runInContext(table+render+';globalThis.render={renderConversionCohorts,renderFollowupEffectiveness};',sandbox);const window={entered:1,matured:1,paid:0,right_censored:0,late_paid:0,rate_pct:0};const html=sandbox.render.renderConversionCohorts({conversion_cohorts:{version:'v1',primary:{rate_pct:null},segments:{unknown:{windows:{14:window}}}}});assert.match(html,/Історія покупок невідома/);assert.match(html,/Усі спостережувані/);assert.match(html,/Недостатньо підтвердженої історії/);const follow=sandbox.render.renderFollowupEffectiveness([{kind:'payment',sent:2,recovered:null,bought:null,measurement_status:'not_measured'}]);assert.match(follow,/Не виміряно/);assert.equal((follow.match(/<td>—<\\/td>/g)||[]).length,2);")
