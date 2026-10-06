"""Read-only identities for distinct core, instruction and knowledge owners."""
from difflib import unified_diff

from management.services.ig_core_policy import (
    CANONICAL_IG_CORE_POLICY, CORE_POLICY_SHA256, CORE_POLICY_VERSION, core_policy_hash,
)


def read_policy_parity(settings_obj=None):
    from management.models import InstagramBotSettings
    from management.services.approved_public_facts import (
        SUPPORTED_PUBLIC_FACT_LANGUAGES, approved_public_fact_manifest,
    )
    from management.services.bot_knowledge import read_knowledge_manifest
    from management.services.ig_policy_publication import load_active_policy_snapshot, PolicyPublicationError
    from management.services.ig_policy_compiler import PolicyReadinessError
    from management.services.instagram_bot import validate_core_policy_for_publication

    row = settings_obj if settings_obj is not None else InstagramBotSettings.objects.filter(pk=1).first()
    body = str(row.system_prompt or "") if row else ""
    directives = str(row.knowledge_base or "") if row else ""
    reasons = []
    if row is None:
        reasons.append("policy_settings_missing")
    elif not body.strip():
        reasons.append("effective_core_missing")
    else:
        try:
            validate_core_policy_for_publication(body, directives)
        except PolicyReadinessError as exc:
            reasons.append(exc.code)
    publication = {"ready": False, "id": getattr(row, "active_instruction_publication_id", None),
        "version": None, "snapshot_hash": "", "compiler_version": "", "reason": "active_publication_missing"}
    if row is not None:
        try:
            bound = load_active_policy_snapshot(settings_obj=row)
            publication.update(ready=True, id=bound.publication_id, version=bound.version,
                snapshot_hash=bound.snapshot_hash, compiler_version=bound.compiler_version, reason="")
            publication["reviewed_sources"] = [
                {"instruction_id": item["source_id"], **item["reviewed_source"]}
                for item in bound.snapshot["instructions"] if item.get("reviewed_source")]
        except PolicyPublicationError as exc:
            publication["reason"] = exc.code
    if not publication["ready"]:
        reasons.append(publication["reason"])
    facts, knowledge = {}, {}
    for language in SUPPORTED_PUBLIC_FACT_LANGUAGES:
        manifest = approved_public_fact_manifest(language)
        facts[language] = {"version": manifest["version"], "content_hash": manifest["content_hash"]}
        knowledge[language] = {"content_hash": read_knowledge_manifest(language).content_hash}
    return {"ready": not reasons, "readiness": reasons,
        "core": {"canonical_version": CORE_POLICY_VERSION, "canonical_raw_hash": CORE_POLICY_SHA256,
            "raw_hash": core_policy_hash(body), "effective_hash": core_policy_hash(body.strip()),
            "canonical_effective_hash": core_policy_hash(CANONICAL_IG_CORE_POLICY.strip()),
            "matches_raw": body == CANONICAL_IG_CORE_POLICY,
            "matches_effective": body.strip() == CANONICAL_IG_CORE_POLICY.strip(),
            "effective_version": CORE_POLICY_VERSION if body.strip() == CANONICAL_IG_CORE_POLICY.strip() else "custom",
            "directives_raw_hash": core_policy_hash(directives),
            "directives_effective_hash": core_policy_hash(directives.strip()),
            "diff": "\n".join(unified_diff(CANONICAL_IG_CORE_POLICY.splitlines(), body.splitlines(),
                fromfile="canonical core", tofile="effective core", lineterm=""))},
        "instruction_publication": publication, "facts": facts, "knowledge": knowledge}


def reviewed_source_status(rows, current_source_hash):
    """Editorial freshness is separate from immutable publication integrity."""
    return [{"instruction_id": row.pk, "section_key": row.reviewed_source["section_key"],
        "source_hash": row.reviewed_source["source_hash"],
        "stale": row.reviewed_source["source_hash"] != current_source_hash}
        for row in rows if getattr(row, "reviewed_source", {})]
