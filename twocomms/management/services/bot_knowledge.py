"""Reviewed public knowledge supplied to the bot policy compiler.

The repository Markdown directory is retained as an archive for editorial
review.  It is deliberately not read into provider payloads: only the
versioned runtime facts in :mod:`approved_public_facts` are provider-safe.
Live directives and the product catalogue remain separate compiler sources.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from django.core.cache import cache

from management.services.approved_public_facts import (
    approved_provider_fact_definitions, approved_public_facts,
)

CACHE_KEY_PREFIX = "ig_bot_knowledge_manifest_v4"


class KnowledgeReadinessError(RuntimeError):
    """The versioned knowledge source cannot safely participate in a prompt.

    Details deliberately contain source identifiers only, never file contents.
    """

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class KnowledgeModule:
    id: str
    body: str
    priority: int
    required: bool = False
    source_ref: str = "reviewed:canonical-public-facts"
    fact_version: str = ""
    review_status: str = "reviewed"

    def policy_input(self) -> dict:
        return {"id": self.id, "body": self.body, "priority": self.priority, "tags": (), "active": True,
                "required": self.required}


@dataclass(frozen=True)
class KnowledgeManifest:
    modules: tuple[KnowledgeModule, ...]
    content_hash: str
    omissions: tuple[tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(module.body for module in self.modules)

    def policy_inputs(self) -> list[dict]:
        return [module.policy_input() for module in self.modules]

    def metadata(self, *, consumed_ids=()) -> dict:
        """Editorial/review lineage; consumption is supplied by the compiler."""
        consumed = set(consumed_ids)
        return {"runtime_owner": "approved_public_facts", "content_hash": self.content_hash,
            "modules": [{"id": module.id, "source_ref": module.source_ref,
                         "version": module.fact_version, "review_status": module.review_status,
                         "consumed": module.id in consumed, "required": module.required}
                        for module in self.modules],
            "omissions": [{"id": identity, "reason": reason} for identity, reason in self.omissions]}


def read_knowledge_manifest(language: str = "uk", *, topics=None, base_keys=None) -> KnowledgeManifest:
    """Build provider modules from the reviewed runtime fact publication.

    No repository Markdown is opened here.  A missing runtime fact language
    is an explicit readiness failure rather than an accidental fall back to an
    unreviewed archive.
    """
    try:
        definitions = approved_provider_fact_definitions(language)
        facts = {fact.key: fact for fact in approved_public_facts(language)}
        modules = []
        omissions = []
        for module_id, body, priority in definitions:
            key = module_id.rsplit(":", 1)[-1]
            fact = facts[key]
            required = key == "unconfirmed_bases"
            if topics is not None and not required:
                wanted = set(topics)
                wanted.update({"material"} if wanted & {"composition", "density"} else ())
                if fact.topic not in wanted and key not in wanted:
                    omissions.append((module_id, "not_relevant"))
                    continue
                if fact.base_keys and base_keys is not None and not set(fact.base_keys) & set(base_keys):
                    omissions.append((module_id, "scope_unproven"))
                    continue
            modules.append(KnowledgeModule(module_id, body, priority, required,
                                           fact.source_ref, fact.version, fact.review_status))
    except ValueError as exc:
        raise KnowledgeReadinessError(
            "approved_public_facts_unavailable",
            "approved public facts are unavailable for the requested language",
            details={"language": language},
        ) from exc
    payload = [{"id": module.id, "body": module.body, "priority": module.priority,
                "required": module.required} for module in modules]
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return KnowledgeManifest(tuple(modules), digest, tuple(omissions))


def _read_all() -> str:
    """Compatibility wrapper for callers awaiting compiler integration."""
    return read_knowledge_manifest().text


def get_brand_knowledge(language: str = "uk") -> str:
    """Reviewed public knowledge cached by its actual language and content hash."""
    manifest = read_knowledge_manifest(language)
    cache_key = f"{CACHE_KEY_PREFIX}:{language}:{manifest.content_hash}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    cache.set(cache_key, manifest.text, 3600)
    return manifest.text
