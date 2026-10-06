"""Pure manager-facing labels for the private lifecycle projection."""
from management.services.ig_private_media_lifecycle import project_private_media_lifecycle

_LABELS = {"pending": "Очікується", "active": "Приватний файл", "expired": "Строк зберігання минув",
    "deleting": "Видаляється", "delete_failed": "Видалення не завершене", "deleted": "Видалено",
    "missing": "Файл недоступний", "unverified": "Доступність невідома"}
_DETAILS = {"capture_pending": "Очікується отримання вкладення.",
    "deletion_requested": "Видалення очікується; завершення ще не підтверджене.",
    "privacy_erasure": "Видалення приватних даних очікується.",
    "owned_capture": "Перегляд перевіряє права, строк і цілісність файла.",
    "retention_elapsed": "Видалення ще не підтверджене.",
    "deletion_in_progress": "Завершення видалення ще не підтверджене.",
    "deletion_retry": "Очікується повторна спроба видалення.",
    "deletion_confirmed": "Видалення підтверджене.",
    "capture_missing": "Вкладення недоступне.",
    "expiry_unknown": "Строк зберігання невідомий; приватний перегляд недоступний.",
    "preview_unsupported": "Цей тип вкладення недоступний для приватного перегляду."}


def _duration(seconds):
    for unit, label in ((86400, "дн."), (3600, "год."), (60, "хв.")):
        if seconds % unit == 0:
            return f"{seconds // unit} {label}"
    return f"{seconds} с"


def project_media_lifecycle_presentation(part, *, message_state="", message_delete_after=None,
                                       message_parts=(), owner_verified=False,
                                       erasure_started=False, now=None):
    """Return finite labels/DTO only; no database, file, URL or access action."""
    lifecycle = project_private_media_lifecycle(part, message_state=message_state,
        message_delete_after=message_delete_after, message_parts=message_parts,
        owner_verified=owner_verified, erasure_started=erasure_started, now=now)
    policy = lifecycle["policy"]
    policy_label = ("Зберігання: до " + _duration(policy["retention_seconds"])
                    if policy["state"] == "verified" else "Політика зберігання: невідома")
    due = lifecycle["deletion_due"]
    retry = lifecycle["retry_due"]
    return {"lifecycle": lifecycle, "display": {
        "label": _LABELS[lifecycle["state"]],
        "detail": _DETAILS.get(lifecycle["reason"], "Підтверджених даних недостатньо; перегляд недоступний."),
        "due_label": "Видалення після " + due if due else "" if lifecycle["state"] == "deleted" else "Строк зберігання: невідомий",
        "retry_label": (("Повторна спроба після " + retry) if retry else "Час повторної спроби невідомий") if lifecycle["state"] == "delete_failed" else "",
        "policy_label": policy_label,
        "preview_hint": "Файл буде перевірено під час приватного перегляду." if lifecycle["readable"] else "Приватний перегляд недоступний.",
    }}
