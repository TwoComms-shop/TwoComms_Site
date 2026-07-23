import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from management.models import GeminiKeyState
from management.services import gemini_keys
from management.services import gemini_probe


class Command(BaseCommand):
    help = "Проверить доступность Gemini-пула без отправки клиентских сообщений."

    def add_arguments(self, parser):
        parser.add_argument("--role", choices=("chat", "management", "checker"), default="chat")
        parser.add_argument("--model", default=None)
        parser.add_argument("--parallel", type=int, default=2)
        parser.add_argument("--timeout", type=int, default=20)

    def handle(self, *args, **options):
        role = options["role"]
        model = options["model"] or gemini_keys.model_chain(role)[0]
        if role == "chat":
            model = (model or "").strip()
            if not gemini_keys.is_allowed_chat_model(model):
                raise CommandError("Модель не входит в разрешенный chat allowlist.")
        elif model not in gemini_keys.model_chain(role):
            raise CommandError("Модель не входит в цепочку выбранной роли.")
        parallel = options["parallel"]
        if parallel < 1 or parallel > 4:
            raise CommandError("--parallel должен быть от 1 до 4.")
        timeout = max(1, min(options["timeout"], 60))

        names = []
        pool = gemini_keys.role_key_pools().get(role, {})
        for name in list(pool.get("own", [])) + list(pool.get("borrow", [])):
            if name not in names:
                names.append(name)
        present = [(name, (os.environ.get(name) or "").strip()) for name in names]
        probeable = []
        busy = set()
        for name, key in present:
            if not key:
                continue
            if cache.add(f"ig_gemini_probe_lock:{name}", "1", timeout=max(30, timeout + 10)):
                probeable.append((name, key))
            else:
                busy.add(name)
        results = {}
        with ThreadPoolExecutor(max_workers=min(parallel, max(1, len(probeable)))) as executor:
            futures = {
                executor.submit(gemini_probe.probe_key, model, key, (5, timeout)): name
                for name, key in probeable
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception:
                    results[name] = {
                        "status": "probe_error", "http_code": 0, "finish_reason": "", "latency_ms": 0,
                        "thoughts_tokens": 0, "candidates_tokens": 0, "model": model,
                    }
                finally:
                    cache.delete(f"ig_gemini_probe_lock:{name}")

        for name, key in present:
            result = results.get(name) or ({
                "status": "busy", "http_code": 0, "finish_reason": "", "latency_ms": 0,
                "thoughts_tokens": 0, "candidates_tokens": 0, "model": model,
            } if name in busy else {
                "status": "absent", "http_code": 0, "finish_reason": "", "latency_ms": 0,
                "thoughts_tokens": 0, "candidates_tokens": 0, "model": model,
            })
            state = GeminiKeyState.get(name)
            state.last_probe_at = timezone.now()
            state.last_probe_status = str(result.get("status") or "")[:32]
            state.last_probe_model = model[:80]
            state.last_probe_latency_ms = int(result.get("latency_ms") or 0)
            state.last_probe_finish_reason = str(result.get("finish_reason") or "")[:32]
            state.last_probe_http_code = int(result.get("http_code") or 0) or None
            state.last_probe_error = "" if state.last_probe_status in {"ok", "reachable_degraded", "blocked", "reachable_empty"} else state.last_probe_status
            state.save(update_fields=[
                "last_probe_at", "last_probe_status", "last_probe_model", "last_probe_latency_ms",
                "last_probe_finish_reason", "last_probe_http_code", "last_probe_error", "updated_at",
            ])
            safe = {"key_name": name, "model": model, **{k: result.get(k) for k in (
                "status", "http_code", "finish_reason", "latency_ms", "thoughts_tokens", "candidates_tokens"
            )}}
            self.stdout.write(json.dumps(safe, ensure_ascii=False, sort_keys=True))
