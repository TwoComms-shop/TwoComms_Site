#!/usr/bin/env python3
"""Import every project management command and build its parser, never handle()."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "twocomms"
# The reviewed inventory contained 176 non-DTF commands. This release adds
# `prepare_ig_operator_reply_draft` for explicit private Gemini generation.
# Keep the exact-count guard current so additions remain intentional and
# reviewable.
EXPECTED_COMMAND_COUNT = 177


def validate_command_count(command_count: int) -> list[dict[str, object]]:
    if command_count == EXPECTED_COMMAND_COUNT:
        return []
    return [
        {
            "module": "<inventory>",
            "error": "CommandCountMismatch",
            "expected": EXPECTED_COMMAND_COUNT,
            "actual": command_count,
        }
    ]


def discover_command_modules() -> list[str]:
    modules = []
    for path in APP_ROOT.glob("*/management/commands/*.py"):
        relative = path.relative_to(APP_ROOT)
        module_name = ".".join(relative.with_suffix("").parts)
        if "dtf" in module_name.casefold():
            continue
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        modules.append(module_name)
    return sorted(modules)


def check_commands() -> dict[str, object]:
    sys.path.insert(0, str(APP_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_settings_no_network_non_dtf")
    os.environ.setdefault("SECRET_KEY", "management-command-smoke")

    import django

    django.setup()
    from django.db.backends.base.base import BaseDatabaseWrapper

    original_ensure_connection = BaseDatabaseWrapper.ensure_connection

    def _deny_database_connection(*args, **kwargs):
        raise RuntimeError("database connections denied by management command smoke")

    BaseDatabaseWrapper.ensure_connection = _deny_database_connection
    failures = []
    modules = discover_command_modules()
    try:
        for module_name in modules:
            try:
                module = importlib.import_module(module_name)
                command_class = getattr(module, "Command")
                command = command_class()
                command.create_parser("manage.py", module_name.rsplit(".", 1)[-1])
            except BaseException as exc:
                failures.append(
                    {"module": module_name, "error": type(exc).__name__}
                )
    finally:
        BaseDatabaseWrapper.ensure_connection = original_ensure_connection
    failures.extend(validate_command_count(len(modules)))
    return {
        "status": "ok" if not failures else "failed",
        "command_count": len(modules),
        "modules": modules,
        "failed": failures,
        "dtf_scope": "excluded",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = check_commands()
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
