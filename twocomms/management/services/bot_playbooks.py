"""Instruction routing for the IG sales bot."""
from __future__ import annotations

from management.models import BotInstruction, IgClient


def _split_tags(raw: str) -> set[str]:
    return {p.strip().lower() for p in (raw or "").replace(";", ",").split(",") if p.strip()}


def tags_for_client(client: IgClient | None) -> set[str]:
    tags = {"global", "core", "sales"}
    if not client:
        return tags
    for value in (
        client.intent,
        client.stage,
        client.primary_objection,
        client.language,
    ):
        if value:
            tags.add(str(value).lower())
    if client.current_product_id:
        tags.add("product")
        tags.add("catalog")
    if client.stage == IgClient.Stage.PAYMENT_PENDING:
        tags.add("payment")
        tags.add("payment_pending")
    if client.intent == IgClient.Intent.CUSTOM_PRINT:
        tags.add("custom_print")
    if client.primary_objection == IgClient.Objection.PREPAYMENT:
        tags.add("prepayment")
    if client.primary_objection == IgClient.Objection.PRICE:
        tags.add("price")
        tags.add("discount")
    if client.primary_objection == IgClient.Objection.SIZE:
        tags.add("size")
        tags.add("fit")
    return tags


def active_instruction_block(client: IgClient | None = None) -> str:
    """Return relevant active instructions.

    No client means admin/tests/manual generation get all active instructions for
    backwards compatibility. With a client, route by intent tags and include
    untagged/global instructions as compact always-on guidance.
    """
    parts: list[str] = []
    client_tags = tags_for_client(client)
    qs = BotInstruction.objects.filter(is_active=True).order_by("priority", "id")
    for inst in qs:
        body = (inst.body or "").strip()
        if not body:
            continue
        inst_tags = _split_tags(inst.intent_tags)
        if client is not None and inst_tags and not (inst_tags & client_tags):
            continue
        title = (inst.title or "").strip()
        parts.append(f"• {title}: {body}" if title else f"• {body}")
    return "\n".join(parts)
