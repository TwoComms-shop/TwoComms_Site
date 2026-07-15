"""Cron entry point: regenerate marketplace feeds when product data changed.

Usage (cron, every 10 minutes):

    */10 * * * * cd /home/.../twocomms && /.../python manage.py regenerate_feeds_if_dirty >> /.../logs/feeds_cron.log 2>&1

Options:
    --force         rebuild feeds even if the dirty flag is not set.
    --min-age-sec=N wait at least N seconds after the flag was set before
                    rebuilding (default: 120). Acts as debounce — rapid bulk
                    edits do not spam rebuilds.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from storefront.services.feeds_queue import (
    are_feeds_dirty,
    dirty_since_seconds,
    mark_feeds_clean,
)

logger = logging.getLogger(__name__)

FEED_COMMANDS = {
    "generate_google_merchant_feed": "google-merchant-v3.xml",
    "generate_rozetka_feed": "rozetka-feed.xml",
    "generate_kasta_feed": "kasta-feed.xml",
    "generate_buyme_feed": "buyme-feed.xml",
    "generate_prom_feed": "prom-feed.xml",
    "generate_instagram_feed": "instagram-feed.xml",
}


class Command(BaseCommand):
    help = "Regenerate marketplace feeds iff the dirty flag is set and debounce expired."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Rebuild regardless of flag.")
        parser.add_argument(
            "--min-age-sec",
            type=int,
            default=120,
            help="Min seconds since dirty flag was set before a rebuild triggers (debounce).",
        )
        parser.add_argument(
            "--only",
            default="",
            help="Comma-separated list of feed command names to run (default: all).",
        )

    def handle(self, *args, **options):
        force: bool = options["force"]
        min_age: int = max(0, int(options["min_age_sec"]))
        only: str = (options.get("only") or "").strip()

        if not force:
            if not are_feeds_dirty():
                self.stdout.write("feeds clean; nothing to do")
                return
            age = dirty_since_seconds() or 0
            if age < min_age:
                self.stdout.write(f"feeds dirty but only {age:.0f}s old (< {min_age}s debounce); skipping")
                return

        media_root = Path(getattr(settings, "MEDIA_ROOT", Path(settings.BASE_DIR) / "media"))
        media_root.mkdir(parents=True, exist_ok=True)

        selected = None
        if only:
            selected = {name.strip() for name in only.split(",") if name.strip()}

        started = time.time()
        failures = 0
        for command_name, filename in FEED_COMMANDS.items():
            if selected is not None and command_name not in selected:
                continue
            out_path = media_root / filename
            try:
                call_command(command_name, output=str(out_path), verbosity=0)
                self.stdout.write(f"ok {command_name} -> {out_path}")
                if command_name == "generate_google_merchant_feed":
                    legacy_path = media_root / "google-merchant-v2.xml"
                    shutil.copyfile(out_path, legacy_path)
                    self.stdout.write(f"ok legacy alias -> {legacy_path}")
            except Exception as exc:
                failures += 1
                logger.error("Feed %s failed: %s", command_name, exc, exc_info=True)
                self.stderr.write(f"FAIL {command_name}: {exc}")

        elapsed = time.time() - started
        if failures == 0:
            mark_feeds_clean()
            self.stdout.write(f"done; all feeds rebuilt in {elapsed:.1f}s")
        else:
            # Leave dirty flag so next run tries again.
            self.stderr.write(
                f"{failures} feed(s) failed after {elapsed:.1f}s; dirty flag kept for retry"
            )
