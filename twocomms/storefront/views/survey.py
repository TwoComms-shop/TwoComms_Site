"""Survey views - data-driven survey API endpoints."""
import json
import logging
import uuid
from typing import Any, Dict

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..models import PromoCode, SurveySession
from ..services.survey_engine import (
    SurveyEngine,
    award_anonymous_survey_promocode,
    award_survey_promocode,
    get_soft_hint,
    load_survey_definition,
)
from ..tasks import queue_survey_report
from ..utm_tracking import record_survey_event
from ..utm_utils import parse_user_agent

logger = logging.getLogger(__name__)


ANONYMOUS_SURVEY_SESSION_KEY = "anonymous_survey_sessions"


def _json_body(request) -> Dict[str, Any]:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _parse_client_version(value: Any) -> tuple[int | None, JsonResponse | None]:
    if value is None:
        return None, None
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, JsonResponse({"success": False, "error": "invalid_version"}, status=400)


def _definition_or_error():
    definition = load_survey_definition()
    if not definition:
        return None, JsonResponse({"success": False, "error": "survey_unavailable"}, status=500)
    return definition, None


def _is_authenticated(request) -> bool:
    return bool(getattr(getattr(request, "user", None), "is_authenticated", False))


def _anonymous_survey_bucket(request) -> Dict[str, Any]:
    bucket = request.session.get(ANONYMOUS_SURVEY_SESSION_KEY)
    return bucket if isinstance(bucket, dict) else {}


def _save_anonymous_survey_state(request, survey_key: str, state: Dict[str, Any]) -> None:
    bucket = _anonymous_survey_bucket(request)
    bucket[survey_key] = state
    request.session[ANONYMOUS_SURVEY_SESSION_KEY] = bucket
    request.session.modified = True


def _get_anonymous_survey_state(
    request,
    survey_key: str,
    *,
    create: bool = False,
    device_data: Dict[str, Any] | None = None,
) -> tuple[Dict[str, Any] | None, bool]:
    bucket = _anonymous_survey_bucket(request)
    state = bucket.get(survey_key)
    if isinstance(state, dict):
        return state, False
    if not create:
        return None, False

    state = {
        "survey_key": survey_key,
        "status": "in_progress",
        "answers": {},
        "history": [],
        "current_question_id": None,
        "back_used": False,
        "version": 1,
        "module_order": [],
        "device_type": (device_data or {}).get("device_type"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
        "awarded_promocode_id": None,
        "report_id": uuid.uuid4().hex[:16],
        "started_at": timezone.now().isoformat(),
        "last_activity_at": timezone.now().isoformat(),
    }
    _save_anonymous_survey_state(request, survey_key, state)
    return state, True


def _promo_payload(promo_code: PromoCode | None) -> Dict[str, Any] | None:
    if not promo_code:
        return None
    return {
        "code": promo_code.code,
        "expires_at": promo_code.valid_until.isoformat() if promo_code.valid_until else None,
    }


def _serialize_payload(
    *,
    engine: SurveyEngine,
    survey_key: str,
    status: str,
    answers: Dict[str, Any],
    history: list[str],
    current_question_id: str | None,
    version: int,
    back_used: bool,
    promo_code: PromoCode | None = None,
    session_id: int | None = None,
    authenticated: bool = False,
) -> Dict[str, Any]:
    question = engine.get_question(current_question_id) if current_question_id else None
    payload = {
        "success": True,
        "status": status,
        "session_id": session_id,
        "version": version,
        "back_used": back_used,
        "answered_count": len(history or []),
        "soft_hint": get_soft_hint(len(history or [])),
        # ``authenticated`` lets the client decide whether to render the
        # "save your promo code — sign in" CTA on the thank-you screen.
        # It comes from the server (not a template ``data-`` attribute) so
        # the homepage stays cache-safe and the template-source guard tests
        # keep passing.
        "authenticated": bool(authenticated),
        "question": engine.serialize_question(
            question,
            answers.get(question.get("id")),
            answers=answers,
        ) if question else None,
    }
    promo = _promo_payload(promo_code)
    if promo:
        payload["promo"] = promo
    return payload


def _serialize_state(session: SurveySession, engine: SurveyEngine) -> Dict[str, Any]:
    return _serialize_payload(
        engine=engine,
        survey_key=session.survey_key,
        status=session.status,
        answers=session.answers or {},
        history=session.history or [],
        current_question_id=session.current_question_id,
        version=session.version,
        back_used=session.back_used,
        promo_code=session.awarded_promocode,
        session_id=session.id,
        authenticated=True,
    )


def _anonymous_promocode(state: Dict[str, Any]) -> PromoCode | None:
    promo_id = state.get("awarded_promocode_id")
    if not promo_id:
        return None
    return PromoCode.objects.filter(pk=promo_id).first()


def _anonymous_promo_is_expired(state: Dict[str, Any]) -> bool:
    """True when the anonymous reward exists but its validity window passed.

    Anonymous visitors cannot actually redeem a promo code (redemption
    requires signing in at checkout), so once the awarded code expires we
    treat the previous run as spent and allow a fresh attempt. Returns
    False when there is no awarded code yet (the run is simply unfinished).
    """
    promo = _anonymous_promocode(state)
    if not promo:
        return False
    if not promo.valid_until:
        return False
    return timezone.now() >= promo.valid_until


def _reset_anonymous_state(request, survey_key: str, device_data: Dict[str, Any] | None) -> Dict[str, Any]:
    """Drop a finished anonymous run and start a clean session in-place."""
    state = {
        "survey_key": survey_key,
        "status": "in_progress",
        "answers": {},
        "history": [],
        "current_question_id": None,
        "back_used": False,
        "version": 1,
        "module_order": [],
        "device_type": (device_data or {}).get("device_type"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
        "awarded_promocode_id": None,
        "report_id": uuid.uuid4().hex[:16],
        "started_at": timezone.now().isoformat(),
        "last_activity_at": timezone.now().isoformat(),
    }
    _save_anonymous_survey_state(request, survey_key, state)
    return state


def _serialize_anonymous_state(state: Dict[str, Any], engine: SurveyEngine) -> Dict[str, Any]:
    return _serialize_payload(
        engine=engine,
        survey_key=state.get("survey_key", ""),
        status=state.get("status", "in_progress"),
        answers=state.get("answers") or {},
        history=state.get("history") or [],
        current_question_id=state.get("current_question_id"),
        version=int(state.get("version") or 1),
        back_used=bool(state.get("back_used")),
        promo_code=_anonymous_promocode(state),
        session_id=None,
        authenticated=False,
    )


def _ensure_current_question(session: SurveySession, engine: SurveyEngine) -> None:
    if session.current_question_id:
        return
    question = engine.get_next_question(
        session.answers or {},
        session.history or [],
        meta=None,
        module_order=session.module_order or None,
    )
    if question:
        session.current_question_id = question.get("id")
        session.save(update_fields=["current_question_id", "last_activity_at"])


def _ensure_anonymous_current_question(state: Dict[str, Any], engine: SurveyEngine) -> None:
    if state.get("current_question_id"):
        return
    question = engine.get_next_question(
        state.get("answers") or {},
        state.get("history") or [],
        meta=None,
        module_order=state.get("module_order") or None,
    )
    if question:
        state["current_question_id"] = question.get("id")


def _required_satisfied(engine: SurveyEngine, answers: Dict[str, Any]) -> bool:
    for qid in engine.required_questions:
        value = answers.get(qid)
        if value in (None, "", [], {}):
            return False
    return True


def _queue_survey_report_after_commit(session_id: int, status: str) -> None:
    transaction.on_commit(lambda: queue_survey_report(session_id, status, background=True))


def _complete_session(session: SurveySession, definition: Dict[str, Any]) -> SurveySession:
    if session.status == 'completed':
        return session

    reward_def = definition.get("reward", {})
    promo_code, _ = award_survey_promocode(session.user, session.survey_key, reward_def)
    session.awarded_promocode = promo_code
    session.status = 'completed'
    session.completed_at = timezone.now()
    session.current_question_id = None
    session.save(update_fields=["awarded_promocode", "status", "completed_at", "current_question_id", "last_activity_at"])
    _queue_survey_report_after_commit(session.id, "FINAL")
    return session


def _complete_anonymous_session(
    request,
    state: Dict[str, Any],
    definition: Dict[str, Any],
) -> Dict[str, Any]:
    if state.get("status") == "completed":
        return state

    promo_code = _anonymous_promocode(state)
    if not promo_code:
        reward_def = definition.get("reward", {})
        promo_code, _ = award_anonymous_survey_promocode(state.get("survey_key", ""), reward_def)
        state["awarded_promocode_id"] = promo_code.id

    state["status"] = "completed"
    state["completed_at"] = timezone.now().isoformat()
    state["current_question_id"] = None
    _save_anonymous_survey_state(request, state.get("survey_key", ""), state)

    # 2026-05-30: Generate and send survey report for anonymous users synchronously
    try:
        from ..services.survey_reports import send_anonymous_survey_report
        sent = send_anonymous_survey_report(state, definition, "FINAL")
        if not sent:
            logger.error(
                "Anonymous survey report delivery failed survey_key=%s promo_id=%s",
                state.get("survey_key"),
                state.get("awarded_promocode_id"),
            )
    except Exception as e:
        logger.error("Failed to send anonymous survey report: %s", e, exc_info=True)

    return state


@require_http_methods(["POST"])
def survey_start_or_resume(request):
    definition, error_response = _definition_or_error()
    if error_response:
        return error_response

    engine = SurveyEngine(definition)
    survey_key = definition.get("survey_key", "print_feedback_v1")

    device_data = parse_user_agent(request.META.get("HTTP_USER_AGENT", ""))

    if not _is_authenticated(request):
        payload = _json_body(request)
        wants_restart = bool(payload.get("restart"))

        state, created = _get_anonymous_survey_state(
            request,
            survey_key,
            create=True,
            device_data=device_data,
        )
        if not state:
            return JsonResponse({"success": False, "error": "session_unavailable"}, status=500)

        # Re-entry after the previous reward expired: allow a brand-new run
        # so the homepage block can resurface a fresh promo code. The server
        # validates that the old code is actually expired to avoid farming.
        if (
            wants_restart
            and state.get("status") == "completed"
            and _anonymous_promo_is_expired(state)
        ):
            state = _reset_anonymous_state(request, survey_key, device_data)
            created = True

        if state.get("status") != "completed":
            _ensure_anonymous_current_question(state, engine)
            _save_anonymous_survey_state(request, survey_key, state)

        if created:
            record_survey_event(
                request,
                "survey_start",
                question_id=state.get("current_question_id"),
                metadata={"created": True, "survey_key": survey_key, "anonymous": True},
            )
        return JsonResponse(_serialize_anonymous_state(state, engine))

    created = False
    try:
        with transaction.atomic():
            session, created = SurveySession.objects.select_for_update().get_or_create(
                user=request.user,
                survey_key=survey_key,
                defaults={
                    "device_type": device_data.get("device_type"),
                    "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
                },
            )
            if not created and not session.device_type:
                session.device_type = device_data.get("device_type")
                session.user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
                session.save(update_fields=["device_type", "user_agent", "last_activity_at"])
    except IntegrityError:
        session = SurveySession.objects.filter(user=request.user, survey_key=survey_key).first()

    if not session:
        return JsonResponse({"success": False, "error": "session_unavailable"}, status=500)

    if session.status == 'completed':
        return JsonResponse(_serialize_state(session, engine))

    _ensure_current_question(session, engine)
    session.save(update_fields=["last_activity_at"])
    if created:
        record_survey_event(
            request,
            "survey_start",
            session=session,
            question_id=session.current_question_id,
            metadata={"created": True},
        )
    return JsonResponse(_serialize_state(session, engine))


@require_http_methods(["GET"])
def survey_current_question(request):
    definition, error_response = _definition_or_error()
    if error_response:
        return error_response

    engine = SurveyEngine(definition)
    survey_key = definition.get("survey_key", "print_feedback_v1")

    if not _is_authenticated(request):
        state, _ = _get_anonymous_survey_state(request, survey_key)
        if not state:
            return JsonResponse({"success": False, "error": "session_not_found"}, status=404)
        if state.get("status") != "completed":
            _ensure_anonymous_current_question(state, engine)
            _save_anonymous_survey_state(request, survey_key, state)
        return JsonResponse(_serialize_anonymous_state(state, engine))

    session = SurveySession.objects.filter(user=request.user, survey_key=survey_key).first()
    if not session:
        return JsonResponse({"success": False, "error": "session_not_found"}, status=404)

    if session.status == 'completed':
        return JsonResponse(_serialize_state(session, engine))

    _ensure_current_question(session, engine)
    return JsonResponse(_serialize_state(session, engine))


@require_http_methods(["POST"])
def survey_submit_answer(request):
    definition, error_response = _definition_or_error()
    if error_response:
        return error_response

    engine = SurveyEngine(definition)
    survey_key = definition.get("survey_key", "print_feedback_v1")
    payload = _json_body(request)
    question_id = payload.get("question_id")
    client_version, version_error = _parse_client_version(payload.get("version"))
    answer = payload.get("answer")

    if not question_id:
        return JsonResponse({"success": False, "error": "missing_question"}, status=400)
    if version_error:
        return version_error

    if not _is_authenticated(request):
        state, _ = _get_anonymous_survey_state(request, survey_key)
        if not state:
            return JsonResponse({"success": False, "error": "session_not_found"}, status=404)
        if state.get("status") == "completed":
            return JsonResponse(_serialize_anonymous_state(state, engine))

        if client_version is not None and client_version != int(state.get("version") or 1):
            return JsonResponse(
                {
                    "success": False,
                    "error": "version_conflict",
                    "current": _serialize_anonymous_state(state, engine),
                },
                status=409,
            )

        if state.get("current_question_id") and state.get("current_question_id") != question_id:
            return JsonResponse(
                {
                    "success": False,
                    "error": "question_mismatch",
                    "current": _serialize_anonymous_state(state, engine),
                },
                status=409,
            )

        question = engine.get_question(question_id)
        if not question:
            return JsonResponse({"success": False, "error": "question_not_found"}, status=404)

        valid, cleaned, reason = engine.validate_answer(question, answer)
        if not valid:
            return JsonResponse({"success": False, "error": reason}, status=400)

        answers = state.get("answers") or {}
        answers[question_id] = cleaned
        history = state.get("history") or []
        if question_id in history:
            history = [qid for qid in history if qid != question_id]
        history.append(question_id)

        state["answers"] = answers
        state["history"] = history
        state["version"] = int(state.get("version") or 1) + 1
        state["last_activity_at"] = timezone.now().isoformat()

        next_question = engine.get_next_question(
            state["answers"],
            state["history"],
            meta=None,
            module_order=state.get("module_order") or None,
        )

        if next_question:
            state["current_question_id"] = next_question.get("id")
            _save_anonymous_survey_state(request, survey_key, state)
            record_survey_event(
                request,
                "survey_skip" if answer is None else "survey_answer",
                question_id=question_id,
                metadata={
                    "question_type": question.get("type"),
                    "next_question_id": state.get("current_question_id"),
                    "survey_key": survey_key,
                    "anonymous": True,
                },
            )
            return JsonResponse(_serialize_anonymous_state(state, engine))

        min_required = int(definition.get("end_conditions", {}).get("min_answered_questions", 0))
        if len(state.get("history") or []) < min_required and not _required_satisfied(engine, state.get("answers") or {}):
            logger.warning("Anonymous survey ended early for survey %s", survey_key)

        _save_anonymous_survey_state(request, survey_key, state)
        record_survey_event(
            request,
            "survey_skip" if answer is None else "survey_answer",
            question_id=question_id,
            metadata={"question_type": question.get("type"), "survey_key": survey_key, "anonymous": True},
        )
        state = _complete_anonymous_session(request, state, definition)
        record_survey_event(
            request,
            "survey_complete",
            question_id=question_id,
            metadata={"question_type": question.get("type"), "survey_key": survey_key, "anonymous": True},
        )
        return JsonResponse(_serialize_anonymous_state(state, engine))

    try:
        with transaction.atomic():
            session = SurveySession.objects.select_for_update().get(
                user=request.user,
                survey_key=survey_key,
            )
            if session.status == 'completed':
                return JsonResponse(_serialize_state(session, engine))

            if client_version is not None and client_version != session.version:
                return JsonResponse({"success": False, "error": "version_conflict", "current": _serialize_state(session, engine)}, status=409)

            if session.current_question_id and session.current_question_id != question_id:
                return JsonResponse({"success": False, "error": "question_mismatch", "current": _serialize_state(session, engine)}, status=409)

            question = engine.get_question(question_id)
            if not question:
                return JsonResponse({"success": False, "error": "question_not_found"}, status=404)

            valid, cleaned, reason = engine.validate_answer(question, answer)
            if not valid:
                return JsonResponse({"success": False, "error": reason}, status=400)

            answers = session.answers or {}
            answers[question_id] = cleaned
            history = session.history or []
            if question_id in history:
                history = [qid for qid in history if qid != question_id]
            history.append(question_id)

            session.answers = answers
            session.history = history
            session.version = session.version + 1

            next_question = engine.get_next_question(
                session.answers,
                session.history,
                meta=None,
                module_order=session.module_order or None,
            )

            if next_question:
                session.current_question_id = next_question.get("id")
                session.save(update_fields=[
                    "answers",
                    "history",
                    "current_question_id",
                    "version",
                    "last_activity_at",
                ])
                record_survey_event(
                    request,
                    "survey_skip" if answer is None else "survey_answer",
                    session=session,
                    question_id=question_id,
                    metadata={
                        "question_type": question.get("type"),
                        "next_question_id": session.current_question_id,
                    },
                )
                return JsonResponse(_serialize_state(session, engine))

            min_required = int(definition.get("end_conditions", {}).get("min_answered_questions", 0))
            if len(session.history or []) < min_required and not _required_satisfied(engine, session.answers):
                logger.warning("Survey ended early for session %s", session.id)

            session.save(update_fields=["answers", "history", "version", "last_activity_at"])
            record_survey_event(
                request,
                "survey_skip" if answer is None else "survey_answer",
                session=session,
                question_id=question_id,
                metadata={"question_type": question.get("type")},
            )
            session = _complete_session(session, definition)
            record_survey_event(
                request,
                "survey_complete",
                session=session,
                question_id=question_id,
                metadata={"question_type": question.get("type")},
            )
            return JsonResponse(_serialize_state(session, engine))

    except SurveySession.DoesNotExist:
        return JsonResponse({"success": False, "error": "session_not_found"}, status=404)


@require_http_methods(["POST"])
def survey_back_one_step(request):
    definition, error_response = _definition_or_error()
    if error_response:
        return error_response

    engine = SurveyEngine(definition)
    survey_key = definition.get("survey_key", "print_feedback_v1")
    payload = _json_body(request)
    client_version, version_error = _parse_client_version(payload.get("version"))
    if version_error:
        return version_error

    if not _is_authenticated(request):
        state, _ = _get_anonymous_survey_state(request, survey_key)
        if not state:
            return JsonResponse({"success": False, "error": "session_not_found"}, status=404)
        if state.get("status") == "completed":
            return JsonResponse(_serialize_anonymous_state(state, engine))
        if state.get("back_used"):
            return JsonResponse({"success": False, "error": "back_used"}, status=400)
        if client_version is not None and client_version != int(state.get("version") or 1):
            return JsonResponse(
                {
                    "success": False,
                    "error": "version_conflict",
                    "current": _serialize_anonymous_state(state, engine),
                },
                status=409,
            )

        history = state.get("history") or []
        if not history:
            return JsonResponse({"success": False, "error": "no_history"}, status=400)

        last_question_id = history.pop()
        state["history"] = history
        state["current_question_id"] = last_question_id
        state["back_used"] = True
        state["version"] = int(state.get("version") or 1) + 1
        state["last_activity_at"] = timezone.now().isoformat()
        _save_anonymous_survey_state(request, survey_key, state)
        record_survey_event(
            request,
            "survey_back",
            question_id=last_question_id,
            metadata={"client_version": client_version, "survey_key": survey_key, "anonymous": True},
        )
        return JsonResponse(_serialize_anonymous_state(state, engine))

    try:
        with transaction.atomic():
            session = SurveySession.objects.select_for_update().get(
                user=request.user,
                survey_key=survey_key,
            )
            if session.status == 'completed':
                return JsonResponse(_serialize_state(session, engine))
            if session.back_used:
                return JsonResponse({"success": False, "error": "back_used"}, status=400)
            if client_version is not None and client_version != session.version:
                return JsonResponse({"success": False, "error": "version_conflict", "current": _serialize_state(session, engine)}, status=409)

            history = session.history or []
            if not history:
                return JsonResponse({"success": False, "error": "no_history"}, status=400)

            last_question_id = history.pop()
            session.history = history
            session.current_question_id = last_question_id
            session.back_used = True
            session.version = session.version + 1
            session.save(update_fields=[
                "history",
                "current_question_id",
                "back_used",
                "version",
                "last_activity_at",
            ])
            record_survey_event(
                request,
                "survey_back",
                session=session,
                question_id=last_question_id,
                metadata={"client_version": client_version},
            )
            return JsonResponse(_serialize_state(session, engine))

    except SurveySession.DoesNotExist:
        return JsonResponse({"success": False, "error": "session_not_found"}, status=404)


@require_http_methods(["POST"])
def survey_close(request):
    definition, error_response = _definition_or_error()
    if error_response:
        return error_response

    survey_key = definition.get("survey_key", "print_feedback_v1")

    if not _is_authenticated(request):
        state, _ = _get_anonymous_survey_state(request, survey_key)
        if state:
            state["last_activity_at"] = timezone.now().isoformat()
            _save_anonymous_survey_state(request, survey_key, state)
            record_survey_event(
                request,
                "survey_close",
                question_id=state.get("current_question_id"),
                metadata={"survey_key": survey_key, "anonymous": True},
            )
        return JsonResponse({"success": True})

    session = SurveySession.objects.filter(user=request.user, survey_key=survey_key).first()
    if session:
        SurveySession.objects.filter(pk=session.pk).update(last_activity_at=timezone.now())
        record_survey_event(
            request,
            "survey_close",
            session=session,
            question_id=session.current_question_id,
        )
    return JsonResponse({"success": True})


@require_http_methods(["POST"])
def survey_complete(request):
    definition, error_response = _definition_or_error()
    if error_response:
        return error_response

    engine = SurveyEngine(definition)
    survey_key = definition.get("survey_key", "print_feedback_v1")

    if not _is_authenticated(request):
        state, _ = _get_anonymous_survey_state(request, survey_key)
        if not state:
            return JsonResponse({"success": False, "error": "session_not_found"}, status=404)

        if state.get("status") == "completed":
            return JsonResponse(_serialize_anonymous_state(state, engine))

        min_required = int(definition.get("end_conditions", {}).get("min_answered_questions", 0))
        if len(state.get("history") or []) < min_required:
            return JsonResponse({"success": False, "error": "not_enough_answers"}, status=400)

        if not _required_satisfied(engine, state.get("answers") or {}):
            return JsonResponse({"success": False, "error": "required_missing"}, status=400)

        state = _complete_anonymous_session(request, state, definition)
        record_survey_event(
            request,
            "survey_complete",
            metadata={"completed_via": "explicit_endpoint", "survey_key": survey_key, "anonymous": True},
        )
        return JsonResponse(_serialize_anonymous_state(state, engine))

    session = SurveySession.objects.filter(user=request.user, survey_key=survey_key).first()
    if not session:
        return JsonResponse({"success": False, "error": "session_not_found"}, status=404)

    if session.status == 'completed':
        return JsonResponse(_serialize_state(session, engine))

    min_required = int(definition.get("end_conditions", {}).get("min_answered_questions", 0))
    if len(session.history or []) < min_required:
        return JsonResponse({"success": False, "error": "not_enough_answers"}, status=400)

    if not _required_satisfied(engine, session.answers or {}):
        return JsonResponse({"success": False, "error": "required_missing"}, status=400)

    session = _complete_session(session, definition)
    record_survey_event(
        request,
        "survey_complete",
        session=session,
        metadata={"completed_via": "explicit_endpoint"},
    )
    return JsonResponse(_serialize_state(session, engine))


@require_http_methods(["POST"])
def survey_dismiss(request):
    """Hide the homepage survey block for someone who is not interested.

    Anonymous visitors persist the choice client-side (localStorage), because
    the homepage is cached for everyone. Authenticated users get a server-side
    marker (a ``survey_dismiss`` UserAction) so the block stays hidden across
    their devices — the homepage is never cached for signed-in users.
    """
    definition = load_survey_definition()
    survey_key = definition.get("survey_key", "print_feedback_v1") if definition else "print_feedback_v1"

    if _is_authenticated(request):
        record_survey_event(
            request,
            "survey_dismiss",
            metadata={"survey_key": survey_key, "source": "homepage_block"},
        )
    return JsonResponse({"success": True})
