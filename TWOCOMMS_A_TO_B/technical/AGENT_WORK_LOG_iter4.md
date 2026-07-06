# AGENT WORK LOG — Итерация 4 (2026-07-06)

> Продолжение аудита по `twocomms_global_audit.md`. Предыдущий лог: `AGENT_WORK_LOG_iter3.md`.
> Ветка: `audit-and-report-8`. Все проверки read-only, код прода не менялся.

---

## 1. Статус SSH-доступа к проду (важно для следующего агента)

- Сервер `195.191.24.169` агрессивно рвёт SSH-соединения: `kex_exchange_identification: read: Connection reset by peer` после нескольких подключений подряд (похоже на fail2ban/`cphulkd` rate-limit на shared-хостинге).
- **Паттерн, который работает:** ОДНО подключение → выполнить весь батч → выйти. После обрыва ждать 5–10+ минут перед новой попыткой.
- Подготовленный батч-скрипт удалённых проверок лежал в `/tmp/remote_batch.sh` сандбокса (сандбокс эфемерный — скрипт продублирован ниже в разделе 6).
- Итог этой итерации: серверный батч (CRO-051 baseline, AN-004, AN-039, AN-032, TD-011/013/014) **НЕ выполнен** — все попытки упёрлись в connection reset. Это первоочередная задача следующей сессии.

## 2. CRO-050 — Живой прогон воронки до чекаута (ВЫПОЛНЕНО, без сабмита заказа)

Прогон через curl с UA десктопного Chrome, cookie-jar, UTM-метками `utm_source=audit&utm_medium=test&utm_campaign=funnel_check`:

| Шаг | Запрос | Результат |
|---|---|---|
| 1 | `GET /?utm_source=audit...` | 200, выданы `sessionid`, `csrftoken`, `twc_vid` |
| 2 | `GET /catalog/` → `/catalog/hoodie/` | 200, ссылки на PDP присутствуют |
| 3 | `GET /product/20-twocomms-legend/` | 200 |
| 4 | `GET /cart/` (never_cache) → свежий CSRF | 200, csrf cookie 32 chars |
| 5 | `POST /cart/add/` (product_id=20, qty=1, size=M) | 200, `{"ok": true, "count": 1, "total": 1872.0, "offer_id": "TC-0020-ЧЕРНЫЙ-M", ...}` |
| 6 | `GET /cart/count/` | 200, `{"cart_count": 1}` |
| 7 | `GET /cart/` повторно | товар «Худі "Харківська Область"» отображается, формы checkout на месте |

**Выводы CRO-050:**
- Воронка гость → корзина технически работает: CSRF, сессии, AJAX add-to-cart — всё ок.
- `offer_id` содержит цвет на русском («ЧЕРНЫЙ») — несогласованность локали в SKU-кодах (укр. сайт, рус. коды). Минорно, но влияет на фиды/аналитику.
- На странице корзины две формы (auth + guest) с полями `full_name, phone, city, np_office, pay_type, form_type`. Способы оплаты: `online_full` и `prepay_200` (наложка `cod` в UI корзины отсутствует; в `checkout.py:119` дефолт `cod` — legacy-ветка).
- Monobank-виджет: множество элементов `monobank-*` на странице корзины (pay-trigger, status, spinner).
- Сессионные ключи прогона (для сверки в БД на сервере): `sessionid=jrg8b3...`, `twc_vid=f2cd68...`, UTM `audit / test / funnel_check`. Сверка «действия из этой сессии записались в UserAction/UTMSession» — в батче, раздел 6.

## 3. Кодовый аудит аналитики (AN-002/032/033/034/040) — по репозиторию

- **AN-002 (клиентский fast-path UTM):** в `base.html` и `static/js/analytics-loader.js` grep по `utm_|fbclid|gclid|ttclid` — **пусто**. Клиентского перехвата UTM нет, вся атрибуция строится server-side в `storefront/utm_middleware.py`.
- **AN-034 (consent):** grep `cookie consent|consent mode|gdpr` по всем шаблонам и JS — **пусто**. Cookie-баннера и Consent Mode нет вообще. Для UA-рынка не критично юридически, но GA4/Ads в ЕС-трафике будет работать без consent-сигналов.
- **AN-032 (нормализация источников):** словаря нормализации в `utm_middleware.py`/`utm_tracking.py` **нет** (grep `normaliz` — пусто). `utm_source` пишется как есть → фрагментация (`fb`/`facebook`/`Facebook`). Реальное распределение значений — в серверном батче.
- **AN-033 (AI-источники):** упоминания Perplexity/ChatGPT есть только в `seo_utils.py` (SEO-аннотации), детекции AI-рефереров в трекинге нет.
- **AN-040 (GA4/Clarity connectors):** `storefront/services/external_analytics.py` — качественная защитная обвязка: GA4 cache TTL 30 мин, Clarity TTL 4 ч + дневной бюджет 10 req (лимит API), stale-while-revalidate 30 дней, backoff 1 ч. Используется только в `admin_analytics.py`. Creds: `GA4_SERVICE_ACCOUNT_FILE`/`GOOGLE_APPLICATION_CREDENTIALS` + **авто-дискавери `*service*account*.json` рядом с проектом**. В git-репозитории таких JSON нет (`git ls-files` чисто) — но надо проверить на сервере (см. батч): авто-подхват creds-файла из каталога проекта — риск, если файл лежит в webroot.
- **Гео/IP (AN-050 privacy):** `utm_middleware.py:153-192` пишет в UTMSession `ip_address, country, city, region, timezone` через `get_geolocation(ip)`. Шаблона политики приватности с упоминанием обработки IP/гео **не найдено** (в `templates/pages/` нет polityka/privacy файла — страница либо в БД, либо отсутствует). Несоответствие: собираем IP+гео без задокументированной политики.

## 4. TD-блок — что удалось проверить по коду

- **Порядок middleware (base `settings.py:199-226`):** `UTMTrackingMiddleware` стоит ПОСЛЕ Session/Auth и ПЕРЕД `SimpleAnalyticsMiddleware` — корректно. Полного page-cache middleware (Fetch/UpdateCacheMiddleware) нет → UTM-трекинг не ломается кэшем страниц. `ImageOptimizationMiddleware` включён (строка 209, «Enabled with caching»).
- **Kill-switch аналитики:** `production_settings.py` — env `DISABLE_ANALYTICS=true` вырезает все 3 аналитические middleware. При проверках прод-инцидентов сначала смотреть этот env.
- **Retention (TD-030-смежное):** `trim_analytics.py` — retention 90 дней для PageView/SiteSession чанками по 5000 + `clearsessions`. **UTMSession и UserAction командой НЕ чистятся** → таблицы растут бесконечно. Комментарий в докстринге: «analytics was disabled in May 2026» из-за роста таблиц на shared hosting — подтверждает актуальность риска. Отдельной cleanup-команды для UTM-таблиц нет (`ls management/commands` — только `prune_orphan_media`, `send_utm_report`).
- **env-файлы:** `production_settings.py` ищет `.env.production`/`.env` в BASE_DIR и родителе, поддерживает `DJANGO_ENV_FILE`. Проверка секретов на сервере — в батче.

## 5. Обновление чеклиста аудита

Отмечено в `twocomms_global_audit.md` этой итерацией:
- CRO-050 — done (воронка до чекаута прогнана, сабмит заказа не делался намеренно).
- AN-002, AN-032 (кодовая часть), AN-033, AN-034, AN-040 (кодовая часть) — done с выводами выше.
- CRO-051, AN-004, AN-039, TD-011/013/014 — **blocked: требуют серверного батча** (раздел 6).

## 6. Батч для следующей SSH-сессии (скопировать целиком, выполнить ОДНИМ подключением)

```bash
sshpass -p '<PASS>' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 "bash -s" <<'REMOTE'
set +H
source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate
cd /home/qlknpodo/TWC/TwoComms_Site/twocomms
echo "===GIT==="; git log --oneline -3
echo "===LOGS==="; ls -lah *.log 2>/dev/null | awk '{print $5, $9}'
echo "===OPT_CACHE==="; du -sh media/optimized_cache 2>/dev/null || echo none
echo "===SA_JSON_ON_SERVER==="; find . -maxdepth 2 -iname "*service*account*.json" -o -iname "*credentials*.json" 2>/dev/null | head
python manage.py shell <<'PYEOF'
from django.apps import apps
from django.conf import settings
from django.db.models import Count
UserAction = apps.get_model('storefront','UserAction')
PageView = apps.get_model('storefront','PageView')
SiteSession = apps.get_model('storefront','SiteSession')
UTMSession = apps.get_model('storefront','UTMSession')
AnalyticsExclusion = apps.get_model('storefront','AnalyticsExclusion')
Order = apps.get_model('orders','Order')
print("== CRO-051 baseline ==")
print("UserAction:", UserAction.objects.count())
for r in UserAction.objects.values('action_type').annotate(c=Count('id')).order_by('-c'): print(" ", r['action_type'], r['c'])
print("PageView:", PageView.objects.count())
print("SiteSession:", SiteSession.objects.count(), "bots:", SiteSession.objects.filter(is_bot=True).count())
print("UTMSession:", UTMSession.objects.count(), "converted:", UTMSession.objects.filter(is_converted=True).count())
print("Orders:", list(Order.objects.values('status').annotate(c=Count('id'))))
print("== CRO-050 verify (audit funnel from 2026-07-06) ==")
for s in UTMSession.objects.filter(utm_source='audit').order_by('-created_at')[:3]:
    print(" utm_session:", s.id, s.utm_campaign, s.created_at, "conv:", s.is_converted)
    print("  actions:", list(UserAction.objects.filter(session_key=s.session_key).values('action_type').annotate(c=Count('id'))))
print("== AN-004 exclusions ==")
for e in AnalyticsExclusion.objects.all()[:20]: print(" ", e.__dict__)
print("== AN-039 search PII ==")
for a in UserAction.objects.filter(action_type='search').order_by('-created_at')[:50]:
    md = a.metadata if isinstance(a.metadata, dict) else {}
    print(" ", repr(md.get('query') or md)[:100])
print("== AN-032 utm_source fragmentation ==")
for r in UTMSession.objects.values('utm_source').annotate(c=Count('id')).order_by('-c')[:25]: print(" ", r['utm_source'], r['c'])
print("== TD-011/013/014 ==")
print("DEBUG:", settings.DEBUG)
print("CACHE:", settings.CACHES['default']['BACKEND'], settings.CACHES['default'].get('LOCATION',''))
import os
croot = getattr(settings,'COMPRESS_ROOT', None) or settings.STATIC_ROOT
print("compress manifest:", os.path.exists(os.path.join(str(croot),'CACHE','manifest.json')))
PYEOF
REMOTE
```

## 7. Новые находки для отчёта (сводно)

| ID | Находка | Серьёзность |
|---|---|---|
| NEW-401 | Нет cookie-consent/Consent Mode вообще (AN-034) | Medium (для EU-трафика) |
| NEW-402 | Нет нормализации `utm_source` → фрагментация источников | Medium |
| NEW-403 | UTMSession хранит IP+гео, публичной политики приватности с этим не найдено | Medium |
| NEW-404 | `trim_analytics` не чистит UTMSession/UserAction → бесконечный рост (та же причина, по которой аналитику уже отключали в мае 2026) | High |
| NEW-405 | Авто-дискавери `*service*account*.json` в каталоге проекта (external_analytics.py) — риск подхвата creds из webroot; проверить наличие файла на сервере | Medium |
| NEW-406 | `offer_id` содержит русскоязычный цвет («ЧЕРНЫЙ») при укр. локали сайта | Low |
| NEW-407 | Legacy-дефолт `pay_type='cod'` в `checkout.py:119` при отсутствии cod в UI | Low |
