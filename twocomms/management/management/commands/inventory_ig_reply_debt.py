"""Emit a read-only, deterministic inventory for named IG reply-debt records."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Read-only evidence inventory for explicitly named IG reply-debt source/task IDs"

    def add_arguments(self, parser):
        parser.add_argument("--source-message-id", action="append", type=int, default=[])
        parser.add_argument("--task-id", action="append", type=int, default=[])
        parser.add_argument("--json", action="store_true", help="Output is JSON in every mode")
        for flag in ("apply", "write", "recover", "finalize", "send"):
            parser.add_argument(f"--{flag}", action="store_true", dest=f"forbidden_{flag}")

    def handle(self, *args, **options):
        forbidden = [
            flag for flag in ("apply", "write", "recover", "finalize", "send")
            if options[f"forbidden_{flag}"]
        ]
        if forbidden:
            raise CommandError("mutating flags are forbidden: " + ", ".join(forbidden))
        from management.services.ig_reply_debt_inventory import (
            InventoryRequestError,
            inventory_reply_debt,
        )

        try:
            report = inventory_reply_debt(
                source_message_ids=options["source_message_id"],
                task_ids=options["task_id"],
            )
        except InventoryRequestError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
