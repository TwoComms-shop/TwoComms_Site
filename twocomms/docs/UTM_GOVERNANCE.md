# UTM Governance — конвенция для TwoComms

**W2-8 (AN-032/AN-033).** Единые правила разметки ссылок, чтобы источники не дробились на «ig / Instagram / IGShopping / Inst_Vid».

## Правила

1. **Всё lowercase.** `utm_source=instagram`, не `Instagram`.
2. **Без пробелов** — разделитель `_` (нижнее подчёркивание).
3. **utm_source — канонические значения** (см. таблицу ниже). Новые источники согласовывать перед запуском кампании.
4. **utm_medium** — тип канала: `cpc` (платный клик), `paid_social`, `social` (органика соцсетей), `email`, `ai`, `referral`, `qr`, `sms`.
5. **utm_campaign** — `{цель}_{сезон/дата}`: `newdrop_fall2026`, `remarketing_black_friday`.
6. **utm_content** — идентификатор креатива/плейсмента: `stories_video1`, `feed_carousel`.

## Канонические utm_source

| Канон | Схлопываемые алиасы (нормализуются автоматически) |
|---|---|
| `instagram` | ig, inst, insta, igshopping, ig_shopping, inst_vid, instvid, ig_stories, instagram_stories, instagram_reels, ig_reels |
| `facebook` | fb, meta, fb_ads, facebook_ads, facebookads, m.facebook.com, l.facebook.com |
| `tiktok` | tt, tik_tok, tiktok_ads, tiktokads |
| `google` | adwords, google_ads, googleads |
| `telegram` | tg, t.me, org.telegram.messenger |
| `youtube` | yt, youtube.com |
| `chatgpt` | chatgpt.com, chat.openai.com, openai |
| `perplexity` | perplexity.ai |
| `gemini` | gemini.google.com, bard |
| `claude` | claude.ai |
| `copilot` | copilot.microsoft.com, bing_chat |
| `you` | you.com |
| `poe` | poe.com |

Нормализация выполняется в `storefront/utm_utils.py::normalize_utm_source()` (вызывается из `UTMTrackingMiddleware`). Неизвестные значения приводятся к lowercase, но не переименовываются.

## Канал «AI»

Трафик из AI-ассистентов (chatgpt.com, perplexity.ai, gemini.google.com, claude.ai, copilot.microsoft.com, you.com, poe.com):

- Если пришёл **без UTM** — детектится по referrer (`detect_ai_source()`), создаётся синтетическая атрибуция `utm_source={ai-источник}`, `utm_medium=ai`.
- Если `utm_source` — AI-источник, а `utm_medium` не задан — проставляется `utm_medium=ai`.
- В отчётах канал «AI» = `utm_medium=ai`.

## Историческая нормализация

Нормализация действует **с момента деплоя** (новые сессии). Исторические
ChatGPT-алиасы нормализуются dry-run-first командой `normalize_ai_attribution`,
а остальные детерминированные aliases/case-варианты — командой
`normalize_utm_sources`. Обе команды запускаются только после приватного
бэкапа и с точными `--expect-*` guard-параметрами. Неизвестные источники не
переназначаются другому каналу (RISK-07).

Production normalization 2026-07-16 обновила 364 UTM-сессии, 35 first-touch
payloads и 1 Order с нулём конфликтов. Повторный dry-run/apply вернул 0/0/0;
финальная проверка не нашла source, отличающийся от результата governance
normalizer. Rollback snapshot хранится вне Git в mode 0600.
