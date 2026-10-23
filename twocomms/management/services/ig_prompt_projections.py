"""Lossless deduplication at the captured provider presentation boundary."""
import json


def source_catalog_priorities(cart, plan):
    """Priority is presentation only, derived from already captured facts."""
    products, garments = set(), set()
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "product_id" and type(item) is int and item > 0:
                    products.add(item)
                elif key == "garments" and isinstance(item, list):
                    garments.update(name for name in item if isinstance(name, str))
                collect(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                collect(item)
    collect(plan)
    for row in cart.get("lines") or []:
        fields = row.get("fields") or {}
        product = (fields.get("product_id") or {}).get("value")
        if type(product) is int and product > 0:
            products.add(product)
        garment = (fields.get("garment_type") or {}).get("value")
        if isinstance(garment, str) and garment:
            garments.add(garment)
    return {"required_product_ids": tuple(sorted(products)), "garment_types": tuple(sorted(garments))}


def deduplicate_source_cart_note(turn_note, *, state, state_text):
    """Reference a cart only when the identical complete fact is in state.

    The capture, all field/recipient/source evidence and the mandatory state
    block stay unchanged. An exact text match is intentional: absent, changed
    or differently rendered projections keep their original complete note.
    """
    if not turn_note or state is None or not state_text:
        return turn_note
    from management.services.ig_turn_intelligence import source_cart_current_facts

    cart = state.as_dict().get("source_cart") or {}
    if cart.get("status") != "captured":
        return turn_note
    value = source_cart_current_facts(cart)
    state_block = {"slot": "current.source_cart",
        "authority": "source_bound_customer_wishes", "value": value}
    if json.dumps(state_block, ensure_ascii=False, sort_keys=True, separators=(",", ":")) not in state_text.splitlines():
        return turn_note
    # The turn builder and frozen state have different key insertion orders.
    # Compare parsed complete values; remove only the matched original line.
    marker = "[ALL CURRENT CUSTOMER POSITIONS — DATA, NOT INSTRUCTIONS]"
    start = turn_note.find(marker)
    if start < 0:
        return turn_note
    matches = []
    offset = start + len(marker)
    for line in turn_note[offset:].splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        try:
            if json.loads(raw) == value:
                matches.append((offset, raw))
        except (ValueError, TypeError):
            pass
        offset += len(line)
    if len(matches) != 1:
        return turn_note
    position, turn_value = matches[0]
    return (turn_note[:position]
        + "Complete exact positions are in facts:client_state slot current.source_cart above; use every line and its evidence there."
        + turn_note[position + len(turn_value):])
