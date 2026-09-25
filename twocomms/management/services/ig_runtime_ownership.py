"""Single-owner contract for Instagram runtime work.

This module is deliberately free of provider code.  It is the small, importable
source of truth used by the daemon, the periodic coordinator and contract
tests.  A lane may have exactly one runtime owner; business-level idempotency
remains mandatory inside every lane.
"""

from __future__ import annotations

from dataclasses import dataclass


DAEMON_OWNER = "instagram_daemon"
PERIODIC_OWNER = "instagram_periodic_coordinator"
SUPERVISOR_OWNER = "instagram_stdlib_supervisor"
MANUAL_OWNER = "manual_diagnostic"
DURABLE_TASK_OWNER = "django61_durable_tasks_cron"
GLOBAL_BACKGROUND_LOCK = "tmp/twocomms_heavy_background.lock"


@dataclass(frozen=True)
class RuntimeLaneOwner:
    lane: str
    owner: str
    detail: str


@dataclass(frozen=True)
class PeriodicLane:
    task_key: str
    command: str
    interval_seconds: int
    deadline_seconds: int
    options: tuple[tuple[str, object], ...]
    optional_gate: str = ""

    def command_options(self) -> dict[str, object]:
        return dict(self.options)


RUNTIME_LANE_OWNERS = (
    RuntimeLaneOwner("daemon_supervision", SUPERVISOR_OWNER, "spawn, wait, exit attribution"),
    RuntimeLaneOwner("live_reply", DAEMON_OWNER, "inbound queue and customer reply"),
    RuntimeLaneOwner("manager_notification_outbox", DAEMON_OWNER, "durable manager notification drain"),
    RuntimeLaneOwner("customer_followups", DAEMON_OWNER, "latency-sensitive due follow-ups"),
    RuntimeLaneOwner("profile_refresh", DAEMON_OWNER, "bounded provider profile refresh"),
    RuntimeLaneOwner("conversation_discovery", DAEMON_OWNER, "resumable inbox discovery"),
    RuntimeLaneOwner("conversation_analysis", DAEMON_OWNER, "durable CRM analysis queue"),
    RuntimeLaneOwner("ai_reply_recovery", DAEMON_OWNER, "failed live-reply recovery"),
    RuntimeLaneOwner("permission_transitions", DAEMON_OWNER, "reply permission changes"),
    RuntimeLaneOwner("inbox_refresh", DAEMON_OWNER, "administrator-requested inbox repair"),
    RuntimeLaneOwner("checkout_lifecycle", DAEMON_OWNER, "payment and delivery lifecycle events"),
    RuntimeLaneOwner("follow_intelligence", DAEMON_OWNER, "follow and UGC intelligence queue"),
    # Друга смуга для того ж outbox, і це НЕ дубль власника: смуга демона
    # («manager_notification_outbox») лишається швидким шляхом, а ця —
    # backstop у cron. Причина конкретна і записана в ЭА.16: єдиний drain
    # жив усередині демона, тому алерт watchdog-а про МЕРТВИЙ демон не мав
    # кому доставитись — процес, про смерть якого йдеться, і був єдиним
    # доставником. Claim рядка — compare-and-swap
    # (`filter(pk).filter(eligible).update(status=SENDING)`), тому два
    # drain-и паралельно не відправлять одне повідомлення двічі.
    RuntimeLaneOwner(
        "manager_notification_backstop",
        PERIODIC_OWNER,
        "cron drain so a dead-daemon alert still reaches the manager",
    ),
    RuntimeLaneOwner("order_telegram_reconcile", PERIODIC_OWNER, "post-payment side-effect repair"),
    RuntimeLaneOwner("ig_checkout_reconcile", PERIODIC_OWNER, "assisted-checkout repair"),
    RuntimeLaneOwner("ig_order_fulfillment", PERIODIC_OWNER, "order customer-event delivery"),
    RuntimeLaneOwner("ig_deal_payments", PERIODIC_OWNER, "payment polling backstop"),
    RuntimeLaneOwner("binotel_call_ai_analyses", PERIODIC_OWNER, "runtime-gated call analysis"),
    RuntimeLaneOwner("django61_durable_tasks", DURABLE_TASK_OWNER, "shared global admission lock"),
    RuntimeLaneOwner("nova_poshta_tracking", PERIODIC_OWNER, "tracking poll under the periodic admission lock"),
    RuntimeLaneOwner("gemini_metadata_diagnostic", MANUAL_OWNER, "explicit token-free diagnostic only"),
)


PERIODIC_LANES = (
    # Перша в порядку виконання свідомо: сенс смуги — щоб менеджер дізнався про
    # інцидент вчасно. Якби вона стояла останньою, вікно координатора могли б
    # з'їсти попередні смуги, і backstop спрацював би саме тоді, коли вже пізно.
    # Ліміт 10 і дедлайн 30 с: це підстраховка, а не основний шлях доставки, і
    # сумарний бюджет смуг мусить лишитись у межах вікна cron.
    PeriodicLane(
        "manager_notification_backstop",
        "drain_ig_notifications",
        120,
        30,
        (("limit", 10),),
    ),
    # A standalone nonblocking cron was repeatedly starved by the shared
    # heavy-process lock. Run tracking within its priority owner instead.
    PeriodicLane(
        "nova_poshta_tracking",
        "update_tracking_statuses",
        300,
        120,
        (),
    ),
    PeriodicLane(
        "order_telegram_reconcile",
        "reconcile_order_telegram_notifications",
        120,
        60,
        (("max_age_hours", 168), ("min_age_seconds", 60), ("limit", 50)),
    ),
    PeriodicLane(
        "ig_checkout_reconcile",
        "reconcile_ig_checkout",
        120,
        75,
        (("limit", 100),),
    ),
    PeriodicLane(
        "ig_order_fulfillment",
        "reconcile_ig_order_fulfillment",
        120,
        75,
        (("limit", 100),),
    ),
    PeriodicLane(
        "ig_deal_payments",
        "poll_ig_deal_payments",
        240,
        120,
        (("limit", 50),),
    ),
    PeriodicLane(
        "binotel_call_ai_analyses",
        "run_call_ai_analyses",
        300,
        180,
        (("limit", 1),),
        optional_gate="call_auto_analysis",
    ),
)


def validate_runtime_lane_owners() -> None:
    lanes = [entry.lane for entry in RUNTIME_LANE_OWNERS]
    if len(lanes) != len(set(lanes)):
        raise RuntimeError("Instagram runtime lane has more than one owner")
    periodic_owner_keys = {
        entry.lane
        for entry in RUNTIME_LANE_OWNERS
        if entry.owner == PERIODIC_OWNER
    }
    periodic_lane_keys = {entry.task_key for entry in PERIODIC_LANES}
    if periodic_owner_keys != periodic_lane_keys:
        raise RuntimeError("Periodic lane definitions and owner manifest disagree")
    if any(
        lane.deadline_seconds <= 0
        or lane.deadline_seconds >= 600
        for lane in PERIODIC_LANES
    ):
        raise RuntimeError("Periodic lane deadlines must be positive and bounded")


def lane_owner(lane: str) -> str:
    for entry in RUNTIME_LANE_OWNERS:
        if entry.lane == lane:
            return entry.owner
    raise KeyError(lane)


validate_runtime_lane_owners()
