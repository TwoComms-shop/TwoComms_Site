"""Тести Phase 3 / Task 13 — вкладка «Клиенти» (CRM IG-клієнтів).

JSON-API списку карток і детальної (переписка, кружечки воронки, summary,
угоди, замовлення). Доступ лише адмінам.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisSnapshot,
    InstagramBotLog,
    InstagramBotMessage,
)

User = get_user_model()

MGMT = override_settings(ROOT_URLCONF="twocomms.urls_management")


class FunnelProgressTests(TestCase):
    def test_progress_marks_done_up_to_current(self):
        c = IgClient.get_or_create_for_sender("p1")
        c.set_stage(IgClient.Stage.CHECKOUT)
        by = {p["stage"]: p for p in c.funnel_progress()}
        self.assertTrue(by["new"]["done"])
        self.assertTrue(by["checkout"]["done"])
        self.assertTrue(by["checkout"]["current"])
        self.assertFalse(by["paid"]["done"])


@MGMT
class ClientsApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("adm", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.c = IgClient.get_or_create_for_sender("igX")
        self.c.display_name = "Іван"
        self.c.save()
        InstagramBotMessage.objects.create(
            sender_id="igX", client=self.c, role="user", text="привіт"
        )

    def test_clients_list(self):
        r = self.client.get(reverse("management_bot_clients_api"))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["success"])
        self.assertTrue(any(cl["name"] == "Іван" for cl in data["clients"]))

    def analysis(self, client, interaction_type, *, key):
        return IgConversationAnalysisSnapshot.objects.create(
            client=client,
            dedupe_key=key,
            score_band=IgConversationAnalysisSnapshot.Band.EXPLORING,
            interaction_type=interaction_type,
            analysis_model="rules",
            rules_version="ui-test",
        )

    def test_clients_show_localized_latest_interaction_and_category_filter(self):
        self.analysis(
            self.c,
            IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT,
            key="ui-support",
        )
        other = IgClient.get_or_create_for_sender("ig-info")
        self.analysis(
            other,
            IgConversationAnalysisSnapshot.InteractionType.INFORMATION_ONLY,
            key="ui-info",
        )

        data = self.client.get(reverse("management_bot_clients_api") + "?view=complaints").json()

        self.assertEqual(data["total"], 1)
        row = data["clients"][0]
        self.assertEqual(row["id"], self.c.id)
        self.assertEqual(row["interaction_type"], "support_complaint")
        self.assertEqual(row["interaction_type_label"], "Підтримка / скарга")
        self.assertEqual(row["interaction_tone"], "support")
        self.assertEqual(row["analysis_band_label"], "Вивчає")

    def test_category_filter_uses_latest_snapshot_and_excludes_hidden(self):
        self.analysis(
            self.c,
            IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT,
            key="ui-old-support",
        )
        self.analysis(
            self.c,
            IgConversationAnalysisSnapshot.InteractionType.INFORMATION_ONLY,
            key="ui-latest-info",
        )
        hidden = IgClient.get_or_create_for_sender("ig-hidden-support")
        hidden.hidden_at = timezone.now()
        hidden.save(update_fields=["hidden_at", "updated_at"])
        self.analysis(
            hidden,
            IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT,
            key="ui-hidden-support",
        )

        data = self.client.get(reverse("management_bot_clients_api") + "?view=complaints").json()

        self.assertEqual(data["total"], 0)

    def test_stats_category_breakdown_excludes_hidden_clients(self):
        self.analysis(
            self.c,
            IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT,
            key="ui-visible-stats-support",
        )
        hidden = IgClient.get_or_create_for_sender("ig-hidden-stats-support")
        hidden.hidden_at = timezone.now()
        hidden.save(update_fields=["hidden_at", "updated_at"])
        self.analysis(
            hidden,
            IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT,
            key="ui-hidden-stats-support",
        )

        data = self.client.get(reverse("management_bot_stats_api") + "?days=0").json()
        support = next(
            row for row in data["interactions"] if row["type"] == "support_complaint"
        )

        self.assertEqual(support["label"], "Підтримка / скарга")
        self.assertEqual(support["count"], 1)

    def test_clients_list_exposes_ukrainian_delivery_block_status(self):
        setattr(self.c, "delivery_status", "message_request_check")
        setattr(self.c, "delivery_error", "Перевірте Запити на повідомлення в Instagram.")
        self.c.save()

        r = self.client.get(reverse("management_bot_clients_api") + "?view=delivery-blocked")

        self.assertEqual(r.status_code, 200)
        data = r.json()
        row = next(client for client in data["clients"] if client["id"] == self.c.id)
        self.assertEqual(row.get("delivery_status"), "message_request_check")
        self.assertIn("Запити", row.get("delivery_status_label", ""))

    def test_client_detail(self):
        r = self.client.get(reverse("management_bot_client_detail_api", args=[self.c.id]))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["client"]["id"], self.c.id)
        self.assertTrue(any(m["text"] == "привіт" for m in data["messages"]))
        self.assertGreaterEqual(len(data["funnel"]), 5)

    def test_requires_admin(self):
        self.client.logout()
        nonadmin = User.objects.create_user("u", password="x")
        self.client.force_login(nonadmin)
        r = self.client.get(reverse("management_bot_clients_api"))
        self.assertEqual(r.status_code, 403)


@MGMT
class ClientsPageRenderTests(TestCase):
    def test_bot_page_has_tabbed_structure(self):
        admin = User.objects.create_user("adm2", password="x", is_staff=True)
        self.client.force_login(admin)
        r = self.client.get(reverse("management_bot"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        # 4 вкладки
        self.assertIn("Клієнти", html)
        self.assertIn("Налаштування", html)
        self.assertIn("Інструкції", html)
        self.assertIn("Огляд", html)
        # таб-структура (панелі)
        self.assertIn('data-tab="clients"', html)
        self.assertIn('data-panel="clients"', html)
        self.assertIn('data-panel="settings"', html)
        self.assertIn('data-panel="kb"', html)
        self.assertIn("bot-tab-ind", html)  # анімований індикатор
        self.assertIn("Дані недоступні", html)
        self.assertIn("Сповіщення, які потребують перевірки", html)
        self.assertIn("/bot/api/notifications/review/", html)
        self.assertIn("Скарги / підтримка", html)
        self.assertIn('data-client-view="wholesale"', html)
        self.assertIn('data-client-view="collaboration"', html)
        self.assertIn('data-client-view="reactions"', html)
        self.assertIn("Категорія діалогу", html)
        self.assertIn("Категорії діалогів", html)


@MGMT
class ClientPauseResumeApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("adm3", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.c = IgClient.get_or_create_for_sender("igPause")

    def test_pause(self):
        r = self.client.post(reverse("management_bot_client_pause_api", args=[self.c.id]))
        self.assertEqual(r.status_code, 200)
        self.c.refresh_from_db()
        self.assertTrue(self.c.bot_paused)

    def test_resume_clears_takeover(self):
        self.c.bot_paused = True
        self.c.manager_takeover = True
        self.c.save()
        r = self.client.post(reverse("management_bot_client_resume_api", args=[self.c.id]))
        self.assertEqual(r.status_code, 200)
        self.c.refresh_from_db()
        self.assertFalse(self.c.bot_paused)
        self.assertFalse(self.c.manager_takeover)

    def test_opt_out_requires_explicit_manual_consent_and_audits_opt_in(self):
        self.c.bot_paused = True
        self.c.paused_reason = "opt_out"
        self.c.opted_out_at = timezone.now()
        self.c.opt_out_message_id = 123
        self.c.save(update_fields=[
            "bot_paused", "paused_reason", "opted_out_at", "opt_out_message_id", "updated_at",
        ])
        url = reverse("management_bot_client_resume_api", args=[self.c.id])

        refused = self.client.post(url)

        self.assertEqual(refused.status_code, 409)
        self.assertTrue(refused.json()["requires_opt_in_confirmation"])
        self.c.refresh_from_db()
        self.assertTrue(self.c.bot_paused)
        self.assertIsNone(self.c.opted_in_at)

        accepted = self.client.post(url, {"confirm_opt_in": "1"})

        self.assertEqual(accepted.status_code, 200)
        self.c.refresh_from_db()
        self.assertFalse(self.c.bot_paused)
        self.assertEqual(self.c.opted_in_by_id, self.admin.id)
        self.assertGreaterEqual(self.c.opted_in_at, self.c.opted_out_at)
        self.assertTrue(
            InstagramBotLog.objects.filter(event="manual_opt_in", detail__contains=f"user={self.admin.id}").exists()
        )


@MGMT
class ClientDetailCursorTests(TestCase):
    """Фаза 3: live chat — інкрементальна дозагрузка переписки через after_id."""

    def setUp(self):
        self.admin = User.objects.create_user("adm_cur", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.c = IgClient.get_or_create_for_sender("igCur")
        self.m1 = InstagramBotMessage.objects.create(
            sender_id="igCur", client=self.c, role="user", text="перше", mid="cur1"
        )
        self.m2 = InstagramBotMessage.objects.create(
            sender_id="igCur", client=self.c, role="model", text="відповідь", mid="cur2"
        )

    def test_detail_messages_have_ids_and_last_id(self):
        r = self.client.get(reverse("management_bot_client_detail_api", args=[self.c.id]))
        data = r.json()
        self.assertTrue(all("id" in m for m in data["messages"]))
        self.assertEqual(data["last_message_id"], self.m2.id)

    def test_detail_after_id_returns_only_new_messages(self):
        url = reverse("management_bot_client_detail_api", args=[self.c.id]) + f"?after_id={self.m1.id}"
        data = self.client.get(url).json()
        self.assertEqual([m["text"] for m in data["messages"]], ["відповідь"])
        self.assertEqual(data["last_message_id"], self.m2.id)
        self.assertNotIn("funnel", data)  # легкий інкрементальний payload

    def test_detail_after_latest_returns_empty(self):
        url = reverse("management_bot_client_detail_api", args=[self.c.id]) + f"?after_id={self.m2.id}"
        data = self.client.get(url).json()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["last_message_id"], self.m2.id)
