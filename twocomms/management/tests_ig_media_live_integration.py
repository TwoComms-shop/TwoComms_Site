"""Canonical media producer/proposal/projection/delivery with fake network only."""
import base64
import hashlib
import json
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from management.models import (
    GeminiRequest, GeminiRequestAttempt, IgCustomerTurnRevision,
    InstagramBotMessage, IgFollowUpTask, IgBotNotification,
)
from management.services.gemini_accounting_contract import sanitize_request_policy_manifest
from management import tests_ig_revision_live as live_fixture

@override_settings(IG_REVISION_EXECUTION_ENABLED=False,GOOGLE_INDEXING_ENABLED=False)
class CanonicalMediaIntegrationTests(TransactionTestCase):
    setUp=live_fixture.RevisionLiveTests.setUp
    _message=live_fixture.RevisionLiveTests._message
    def _prepare(self):
        from management.services.ig_turn_revisions import create_collecting_revision
        self.revision=create_collecting_revision(self.turn,[self.source],bypass_quiet=True).revision
        live_fixture.RevisionLiveTests._prepare(self)
    _execute=live_fixture.RevisionLiveTests._execute
    def _generate(self, payload, **kwargs):
        from management.services.ig_turn_lineage import current_context

        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(kwargs["max_actual_dispatches"], 8)
        self.assertFalse(kwargs["legacy_provider_root"])
        self.assertLessEqual(kwargs["deadline_seconds"], 40)
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn("responseSchema", payload["generationConfig"])
        self.generation_calls.append(payload)
        context = current_context()
        revision_id = int(context["logical_turn_id"].rsplit(":", 1)[1])
        # This stub replaces begin_request too; preserve its durable frozen
        # route contract so outage fixtures exercise real recovery admission.
        from management.services.ig_revision_provider_execution import revision_provider_continuation
        from management.tests_gemini_accounting_shadow import _raw_plan

        owner = IgCustomerTurnRevision.objects.get(pk=revision_id)
        frozen = revision_provider_continuation(owner.pk, owner.claim_token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            candidate_plan=_raw_plan())
        self.assertTrue(frozen.ready, frozen.reason)
        if self.provider_failure:
            from management.services.call_ai_analysis import CallAIAnalysisError

            graph = GeminiRequest.objects.create(
                request_id=f"live-request-{revision_id}", lane="live", task_class="simple_live",
                logical_turn_id=context["logical_turn_id"], source_execution_key=context["logical_turn_id"],
                client_id=self.customer.pk, source_message_id=context["source_message_id"],
                policy_manifest=sanitize_request_policy_manifest(kwargs["request_policy_manifest"]),
                accounting_mode="shadow", terminal_resolution="failed", terminal_reason="provider_outage",
            )
            GeminiRequestAttempt.objects.create(request_id=graph.request_id, request_graph=graph, role="chat", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="transient", fsm_state="failed", http_code=503, logical_turn_id=graph.logical_turn_id, client_id=graph.client_id, source_message_id=graph.source_message_id, lane="live", attempt_index=1, candidate_index=1, accounting_mode="shadow")
            raise CallAIAnalysisError("http_5xx")
        if self.before_validate:
            self.before_validate()
        actual_hashes = [
            hashlib.sha256(base64.b64decode(part["inline_data"]["data"])).hexdigest()
            for content in payload["contents"] for part in content["parts"] if "inline_data" in part
        ][:self.actual_inline_count]
        usage = {"_request_inline_count": self.actual_inline_count}
        if self.actual_hash_override == "observed":
            usage["_request_inline_content_hashes"] = actual_hashes
        elif self.actual_hash_override is not None:
            usage["_request_inline_content_hashes"] = self.actual_hash_override
        context = current_context()
        request_id = f"live-request-{revision_id}"
        model = "gemini-3.7-flash"
        graph = GeminiRequest.objects.create(
            request_id=request_id, lane="live", task_class="simple_live",
            logical_turn_id=context["logical_turn_id"], client_id=self.customer.pk,
            source_execution_key=context["logical_turn_id"],
            source_message_id=context["source_message_id"],
            policy_manifest=sanitize_request_policy_manifest(kwargs["request_policy_manifest"]),
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
        )
        from management.services.ig_turn_lineage import bind_request_id
        bind_request_id(request_id)
        decision = kwargs["result_validator"](self.parsed, usage=usage)
        if not decision.valid:
            from management.services.call_ai_analysis import CallAIAnalysisError

            self.assertIsNone(kwargs["repair_payload_factory"](payload, self.parsed, decision.reason_codes))
            raise CallAIAnalysisError("failed deterministic result validation")
        winner = GeminiRequestAttempt.objects.create(
            request_id=request_id, request_graph=graph, role="chat", key_name="GEMINI_API",
            model=model, outcome="succeeded", fsm_state="succeeded", winner_claimed=True,
            logical_turn_id=graph.logical_turn_id, client_id=graph.client_id,
            source_message_id=graph.source_message_id, lane="live",
            attempt_index=1, candidate_index=1, accounting_mode=graph.accounting_mode,
        )
        graph.winner_attempt = winner
        graph.terminal_resolution = "succeeded"
        graph.save(update_fields=["winner_attempt", "terminal_resolution", "updated_at"])
        if self.after_validate:
            self.after_validate()
        return {
            "parsed": self.parsed, "model": model,
            "meta": {"request_id": request_id}, "usage": usage,
        }


    def media(self, mimes=("image/jpeg",), *, kinds=None, sentiments=None, complaints=None, unavailable=False):
        parts=[]; self.bodies={}; observations=[]; images=[]
        for index,mime in enumerate(mimes):
            body=("media-body-"+str(index)).encode(); pk="mp1_"+format(index+1,"032x")
            parts.append({"source_part_id":pk,"original_index":index,"identity_origin":"ingress",
                "type":mime.split("/")[0],"status":"owned","mime":mime,"bytes":len(body),
                "content_hash":hashlib.sha256(body).hexdigest(),"private_storage":True,"storage_name":"ig/private/"+pk})
            self.bodies[pk]=(mime,body)
            observations.append({"source_inline_index":index,"outcome":"understood",
                "content_kind":(kinds or ["product_photo"]*len(mimes))[index],
                "sentiment":(sentiments or ["positive"]*len(mimes))[index],"confidence":.9,
                "evidence_code":"audio_speech" if mime.startswith("audio/") else "video_content" if mime.startswith("video/") else "visual_content",
                "evidence":"Customer reports torn seam" if (complaints or [False]*len(mimes))[index] else "Observed media content",
                "complaint_code":"current_customer_complaint" if (complaints or [False]*len(mimes))[index] else "none",
                "audio_status":"transcribed" if mime.startswith("audio/") else "not_applicable",
                "transcript":"Голосове повідомлення клієнта" if mime.startswith("audio/") else ""})
            if mime.startswith("image/"):
                images.append({"source_image_index":index,"outcome":"understood","evidence_code":"visual_content","type_code":"product"})
        if unavailable:
            parts.append({"source_part_id":"mp1_"+"f"*32,"original_index":len(parts),"type":"audio","status":"unavailable","mime":"audio/ogg"})
        self.source.text="";self.source.attachment_media=parts;self.source.media_capture_eligible=True
        self.source.private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE
        self.source.save(update_fields=["text","attachment_media","media_capture_eligible","private_media_state"])
        self.actual_inline_count=len(mimes)
        self.parsed={"reply_text":"Дякуємо, що поділилися!","controls":[],"turn_intelligence":{
            "catalog_candidates":[],"transcript":"Голосове повідомлення клієнта" if any(m.startswith("audio/") for m in mimes) else "",
            "audio_status":"transcribed" if any(m.startswith("audio/") for m in mimes) else "not_applicable",
            "intent":"media_review","confidence":.9,"image_observations":images,"media_observations":observations}}

    def run_media(self, *, expected_state="completed"):
        original_media = deepcopy(self.source.attachment_media)
        self._prepare()
        with patch("management.services.instagram_bot._owned_media_bytes", side_effect=lambda item,**kw:self.bodies[item["source_part_id"]]):
            result,generate,http=self._execute()
        self.assertEqual(result.state,expected_state,result.reasons)
        self.assertEqual(generate.call_count,1)
        self.source.refresh_from_db();self.revision.refresh_from_db()
        self.assertEqual(
            [{key: value for key, value in part.items() if key != "inspection"}
             for part in self.source.attachment_media],
            original_media,
        )
        return result,http

    def test_audio_only_is_projected_and_delivered_without_second_generation(self):
        self.media(("audio/ogg",),sentiments=["neutral"])
        self.parsed["reply_text"]="Дякую, ваше питання зрозуміле. Що саме потрібно уточнити?"
        self.run_media()
        row=self.source.attachment_media[0]["inspection"]
        self.assertEqual(row["analysis_state"],"understood")
        self.assertEqual(row["transcript"],"Голосове повідомлення клієнта")
        self.assertEqual(row["sentiment"],"neutral")

    def test_applicable_video_only_is_projected(self):
        self.media(("video/webm",),kinds=["review_video"])
        self.run_media()
        self.assertEqual(self.source.attachment_media[0]["inspection"]["content_kind"],"review_video")

    def native_ugc(self):
        from management.services.ig_ugc_assessment import LIVE_PROVENANCE, BRAND_TARGET_USERNAME
        self.source.attachment_media[0].update(provenance=LIVE_PROVENANCE,
            provider_native_mention=True,target_username=BRAND_TARGET_USERNAME,media_type="story_mention")
        self.source.save(update_fields=["attachment_media"])

    def test_native_unboxing_uses_observed_kind_without_catalog_action(self):
        self.media(kinds=["unboxing"])
        self.native_ugc()
        self.parsed["reply_text"]="Ви чудово виглядаєте в нашому одязі!"
        self.parsed["controls"]=[{"kind":"product","value":"999"}]
        self.run_media()
        reply=self.revision.generation_proposal["response"]
        self.assertIn("розпакування",reply["reply_text"])
        self.assertEqual(reply["controls"],[])
        self.assertNotIn("client_configuration_update",self.revision.generation_proposal["authority"]["allowed_actions"])

    def test_native_design_uses_observed_kind_and_does_not_invent_custom_order(self):
        self.media(kinds=["custom_design"])
        self.native_ugc()
        self.parsed["reply_text"]="Ви чудово виглядаєте в нашому одязі!"
        self.run_media()
        reply=self.revision.generation_proposal["response"]["reply_text"]
        self.assertIn("дизайн",reply)
        self.assertNotIn("нашому одязі",reply)
        self.assertNotIn("checkout_proposal_create",self.revision.generation_proposal["authority"]["allowed_actions"])

    def test_native_mention_does_not_erase_current_explicit_commerce_question(self):
        self.media(kinds=["wearing"])
        self.native_ugc()
        self.source.text="Підберіть мені футболку. Які є кольори?"
        self.source.save(update_fields=["text"])
        self.parsed["reply_text"]="Який колір футболки вам подобається?"
        result, http = self.run_media(expected_state="delivery_pending")
        self.assertEqual(result.reasons, ("semantic_reply_incomplete",))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.generation_proposal["response"]["reply_text"],self.parsed["reply_text"])

    def test_native_photo_allows_source_bound_current_selection_reply(self):
        self.media(kinds=["wearing"])
        self.native_ugc()
        self.source.text = "Підберіть мені футболку."
        self.source.save(update_fields=["text"])
        self.parsed["reply_text"] = "Ви обрали футболку. Яка модель футболки вам подобається?"
        self.run_media()
        self.assertEqual(self.revision.generation_proposal["response"]["reply_text"], self.parsed["reply_text"])

    def test_negative_complaint_service_precedes_praise_and_replays_one_case(self):
        self.media(sentiments=["negative"],complaints=[True])
        self.parsed["reply_text"]="Ви круто виглядаєте в нашому одязі!"
        self.parsed["controls"]=[{"kind":"manager","value":True}]
        self.run_media()
        reply=self.revision.generation_proposal["response"]["reply_text"]
        self.assertNotIn("круто",reply)
        self.assertIn("проблем",reply)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer,reason="revision_case:media_complaint").count(),1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(),1)
        # Finalized replay must not regenerate or create another case.
        result,generation,http=self._execute()
        self.assertEqual(generation.call_count,0)
        self.assertEqual(http.call_count,0)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer,reason="revision_case:media_complaint").count(),1)

    def test_native_positive_photo_preserves_independent_care_question(self):
        self.media(kinds=["wearing"])
        self.native_ugc()
        self.source.text = "Чи можна прати цю футболку при 30 градусах?"
        self.source.save(update_fields=["text"])
        self.parsed["reply_text"] = "Періть навиворіт при 30 градусах. Чи потрібно уточнити догляд?"
        self.parsed["controls"] = [{"kind": "product", "value": "999"}]
        self.run_media()
        reply = self.revision.generation_proposal["response"]
        self.assertIn("30 градусах", reply["reply_text"])
        self.assertIn("уточнити догляд?", reply["reply_text"])
        self.assertEqual(reply["controls"], [])

    def test_native_positive_photo_does_not_override_text_complaint(self):
        self.media(kinds=["wearing"])
        self.native_ugc()
        self.source.text = "На футболці порвався шов. Допоможіть вирішити проблему."
        self.source.save(update_fields=["text"])
        self.parsed["reply_text"] = "Ви чудово виглядаєте! Підкажіть, будь ласка, де саме порвався шов?"
        self.run_media()
        reply = self.revision.generation_proposal["response"]["reply_text"]
        self.assertNotIn("чудово", reply)
        self.assertIn("шов", reply)

    def test_positive_negative_and_unavailable_parts_keep_service_and_outcomes(self):
        self.media(("image/jpeg","image/png"),sentiments=["positive","negative"],complaints=[False,True],unavailable=True)
        self.parsed["reply_text"]="Ви круто виглядаєте!"
        self.run_media()
        self.assertNotIn("круто",self.revision.generation_proposal["response"]["reply_text"])
        self.assertEqual([p["inspection"]["analysis_state"] for p in self.source.attachment_media],["understood","understood","unavailable"])
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer,reason="revision_case:media_complaint").count(),1)

    def test_retained_complaint_in_trimmed_request_keeps_authority_and_omission(self):
        self.media(("image/jpeg","image/png"),sentiments=["negative","positive"],complaints=[True,False])
        self.actual_inline_count=1
        self.parsed["controls"]=[{"kind":"manager","value":True}]
        self.parsed["reply_text"]="Допоможемо з проблемою. Підкажіть, що сталося?"
        self.parsed["turn_intelligence"]["media_observations"]=self.parsed["turn_intelligence"]["media_observations"][:1]
        self.parsed["turn_intelligence"]["image_observations"]=self.parsed["turn_intelligence"]["image_observations"][:1]
        def trim(payload):
            modified=deepcopy(payload)
            for content in reversed(modified["contents"]):
                for index in reversed(range(len(content["parts"]))):
                    if "inline_data" in content["parts"][index]:
                        content["parts"].pop(index)
                        return modified,1,len(json.dumps(modified).encode())
            return modified,0,len(json.dumps(modified).encode())
        with patch("management.services.instagram_bot._fit_inline_request_budget",side_effect=trim):
            self.run_media()
        self.assertIn("manager_escalation_intent",self.revision.generation_proposal["authority"]["allowed_actions"])
        self.assertEqual(self.source.attachment_media[1]["inspection"]["analysis_state"],"omitted")

    def test_meaningful_caption_answer_is_preserved_with_partial_media(self):
        self.media(sentiments=["neutral"],unavailable=True)
        self.source.text="Чи можна прати цю футболку при 30 градусах?"
        self.source.save(update_fields=["text"])
        self.parsed["reply_text"]="Періть навиворіт у делікатному режимі при 30 градусах."
        self.run_media()
        self.assertIn("30 градусах",self.revision.generation_proposal["response"]["reply_text"])

    def test_unavailable_audio_asks_for_text_without_model(self):
        self.media(("audio/ogg",))
        self.source.attachment_media[0]["status"]="unavailable"
        self.source.attachment_media[0].pop("storage_name")
        self.source.save(update_fields=["attachment_media"])
        self._prepare()
        result,generate,http=self._execute()
        self.assertEqual(result.state,"completed",result.reasons)
        self.assertEqual(generate.call_count,0)
        self.revision.refresh_from_db()
        reply=self.revision.action_receipts["media_unavailable_reply"]["reply_text"]
        self.assertIn("текст",reply)
        self.assertNotIn("картин",reply)
