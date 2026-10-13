"""Read-only customer-facing facts from an order item's explicit labels."""
import re


_TECHNICAL_LABEL = re.compile(
    r"(?:^_|(?:^|_)(?:id|ids|ref|refs|digest|schema|token)(?:$|_))",
    re.IGNORECASE,
)


def customer_item_option_labels(item):
    """Return human option labels without exposing internal option values."""
    labels = item.option_labels if isinstance(item.option_labels, dict) else {}
    fit = str(item.fit_option_label or item.fit_option_code or '').strip().casefold()
    result = []
    for label, value in labels.items():
        if not isinstance(label, str) or not isinstance(value, str):
            continue
        label, value = label.strip(), value.strip()
        if not label or not value or _TECHNICAL_LABEL.search(label):
            continue
        if value.casefold() == fit or (
            fit and label.casefold() in {'fit', 'посадка', 'крій', 'крой'}
        ):
            continue
        text = f'{label}: {value}'
        if text not in result:
            result.append(text)
    return result
