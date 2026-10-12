"""Manager admission is independent of physical inventory and Meta permission."""


def admit_restock(subscription, availability, *, now):
    from management.services.ig_fulfillment_reviews import restock_manager_admission
    return restock_manager_admission(subscription, availability, now=now)
