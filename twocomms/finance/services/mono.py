"""Інтеграція Monobank Personal API → наші моделі рахунків/операцій.

Відповідає за:
- підключення за токеном (валідація + шифрування + збереження);
- виявлення рахунків клієнта (картки, ФОП-рахунки, банки) і створення/
  оновлення локальних ``Account`` з прив'язкою через ``external_account_id``;
- синхронізацію виписки (backfill історії + інкрементальне доповнення) з
  антидублями за ``external_id`` (= 'mono:' + statement item id);
- класифікацію бізнес/особисте (ФОП → бізнес; картка → особисте);
- розпізнавання внутрішніх переказів між власними рахунками;
- обробку вебхука (push нової транзакції).

Безпека: токен зберігається лише зашифрованим (див. ``services.crypto``);
у логах/відповідях фігурує лише маска/відбиток. Усі грошові зміни проходять
через ``services.transactions`` (єдина точка перерахунку балансів + аудит).
"""
from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from ..models import Account, IntegrationConnection, Transaction, get_default_company
from . import audit as audit_service
from . import mono_api
from . import transactions as txn_service

EXTERNAL_PREFIX = 'mono'
# Картки monobank, які за замовчуванням вважаємо особистими.
PERSONAL_CARD_TYPES = {'black', 'white', 'platinum', 'iron', 'yellow', 'eaid'}
CONSUMED_EXTERNAL_IDS_KEY = 'consumed_external_ids'


def _kind_for(mono_account: mono_api.MonoAccount) -> str:
    return 'fop' if mono_account.is_fop else 'card'


def _is_business_account(mono_account: mono_api.MonoAccount) -> bool:
    """ФОП-рахунок → бізнес; інші картки → особисте (за замовчуванням)."""
    return mono_account.is_fop


def _money(minor_units) -> Decimal:
    """Копійки (int) → гривні (Decimal з 2 знаками)."""
    return (Decimal(int(minor_units or 0)) / Decimal('100')).quantize(Decimal('0.01'))


def _account_label(mono_account: mono_api.MonoAccount, client_name='') -> str:
    if mono_account.is_fop:
        base = 'monobank ФОП'
    else:
        base = f'monobank {mono_account.type or "картка"}'.strip()
    pan = mono_account.pan
    suffix = f' {pan[-4:]}' if pan and len(pan) >= 4 else ''
    return f'{base}{suffix} ({mono_account.currency})'


# ----------------------------- Підключення -----------------------------

@db_transaction.atomic
def connect_with_token(raw_token: str, *, user, existing_conn=None) -> IntegrationConnection:
    """Валідує токен через API, шифрує і зберігає підключення monobank.

    Повторне підключення тим самим токеном оновлює наявний conn (за відбитком).
    Кидає ``mono_api.MonoApiError`` за невалідного токена.
    """
    from . import crypto

    client = mono_api.MonoClient(raw_token)
    info = client.client_info()  # кине MonoAuthError якщо токен битий
    company = get_default_company()
    fp = crypto.fingerprint(raw_token)

    conn = existing_conn or IntegrationConnection.objects.filter(
        company=company, provider='monobank', token_fingerprint=fp,
    ).first()
    if conn is None:
        conn = IntegrationConnection(company=company, provider='monobank')

    conn.connection_method = 'token'
    conn.set_token(raw_token)
    conn.status = 'success'
    conn.error_message = ''
    conn.client_name = (info.get('name') or '')[:255]
    conn.external_client_id = (info.get('clientId') or '')[:128]
    if not conn.webhook_secret:
        conn.webhook_secret = secrets.token_urlsafe(24)
    # Кешуємо client-info, щоб discover/link не витрачали ліміт 1 запит/60с.
    conn.meta = {**(conn.meta or {}), 'client_info': info,
                 'client_info_at': timezone.now().isoformat()}
    conn.last_sync_at = timezone.now()
    conn.save()

    audit_service.log_action(
        user, 'connect', 'integration', conn.id,
        summary=f'Підключено monobank (токен {conn.token_mask})',
        source='integration', company=company,
    )
    return conn


def _client_for(conn: IntegrationConnection, *, use_cache=True) -> mono_api.MonoClient:
    """Будує MonoClient і (за наявності) підставляє кешований client-info,
    щоб не витрачати ліміт 1 запит/60с на повторні виклики у тому ж флоу."""
    client = mono_api.MonoClient(conn.get_token())
    if use_cache:
        cached = (conn.meta or {}).get('client_info')
        if cached:
            client.seed_client_info(cached)
    return client


def discover_accounts(conn: IntegrationConnection) -> list:
    """Повертає рахунки клієнта для UI-вибору (з кешу client-info, без зайвих запитів)."""
    client = _client_for(conn)
    out = []
    for a in client.accounts():
        out.append({
            'external_id': a.id,
            'kind': _kind_for(a),
            'type': a.type,
            'currency': a.currency,
            'balance': str(_money(a.balance)),
            'iban': a.iban,
            'pan': a.pan,
            'is_business': _is_business_account(a),
            'label': _account_label(a, conn.client_name),
            'linked': Account.objects.filter(
                company=conn.company, external_account_id=a.id).exists(),
        })
    return out


def register_webhook(conn: IntegrationConnection) -> bool:
    """Best-effort реєстрація вебхука у Monobank для push нових транзакцій.

    URL = https://fin.<domain>/hooks/mono/<conn>/<secret>/. Monobank вимагає
    публічний HTTPS і робить тестовий GET, тож у DEBUG/локально пропускаємо.
    Повертає True, якщо вебхук встановлено.
    """
    from django.conf import settings

    if getattr(settings, 'DEBUG', False):
        return False
    base = getattr(settings, 'FINANCE_PUBLIC_BASE', '') or getattr(settings, 'FIN_PUBLIC_BASE', '')
    if not base:
        host = (getattr(settings, 'FIN_HOST', '') or 'fin.twocomms.shop')
        base = f'https://{host}'
    if not conn.webhook_secret:
        conn.webhook_secret = secrets.token_urlsafe(24)
        conn.save(update_fields=['webhook_secret'])
    url = f'{base.rstrip("/")}/hooks/mono/{conn.id}/{conn.webhook_secret}/'
    try:
        mono_api.MonoClient(conn.get_token()).set_webhook(url)
    except mono_api.MonoApiError:
        return False
    conn.webhook_url = url
    conn.save(update_fields=['webhook_url'])
    return True


@db_transaction.atomic
def link_mono_account(conn: IntegrationConnection, mono_account: mono_api.MonoAccount,
                      *, user, account=None, sync_from=None) -> Account:
    """Створює/прив'язує локальний Account до зовнішнього рахунку monobank.

    Баланс виставляємо одразу з client-info через initial_balance (НЕ як дохід),
    щоб картка показувала актуальну суму без жодного statement-запиту. Подальші
    імпорти виписки лише уточнюють initial через back-calc (_reconcile_balance).
    """
    company = conn.company
    is_business = _is_business_account(mono_account)
    is_new = account is None
    if account is None:
        account = Account.objects.filter(
            company=company, external_account_id=mono_account.id).first()
        is_new = account is None
    if account is None:
        last = company.accounts.order_by('-sort_order').first()
        account = Account(
            company=company, name=_account_label(mono_account, conn.client_name),
            type='card' if not mono_account.is_fop else 'bank',
            currency=mono_account.currency, sort_order=(last.sort_order + 1) if last else 0,
        )
    account.integration = conn
    account.external_account_id = mono_account.id
    account.external_kind = _kind_for(mono_account)
    account.iban = mono_account.iban or ''
    account.masked_pan = mono_account.pan or ''
    account.is_business = is_business
    account.auto_sync = True
    # Початковий баланс новоствореного рахунку = фактичний баланс у банку.
    # Для вже наявного рахунку баланс не чіпаємо (його дотягне _reconcile_balance).
    if is_new:
        account.initial_balance = _money(mono_account.balance)
    account.save()
    account.recalc_balance(save=True)

    if sync_from:
        conn.sync_from = sync_from
        conn.save(update_fields=['sync_from'])

    audit_service.log_action(
        user, 'link', 'integration', conn.id,
        summary=f'Привʼязано рахунок {account.name}', source='integration', company=company,
    )
    return account


def disconnect(conn: IntegrationConnection, *, user, wipe_token=True) -> None:
    """Відключає інтеграцію: знищує токен, відвʼязує рахунки (операції лишаються)."""
    conn.status = 'disconnected'
    if wipe_token:
        conn.encrypted_token = ''
        conn.token_fingerprint = ''
        conn.token_mask = ''
        conn.auto_sync = False
    conn.save(update_fields=['status', 'encrypted_token', 'token_fingerprint',
                             'token_mask', 'auto_sync'])
    for acc in conn.accounts.all():
        acc.auto_sync = False
        acc.save(update_fields=['auto_sync'])
    audit_service.log_action(user, 'disconnect', 'integration', conn.id,
                             summary=conn.get_provider_display(), company=conn.company)


# ----------------------------- Імпорт виписки -----------------------------

def _external_id(item_id: str) -> str:
    return f'{EXTERNAL_PREFIX}:{item_id}'


def _amount_bounds(amount: Decimal, tolerance_percent=2.0) -> tuple[Decimal, Decimal]:
    tolerance = amount * Decimal(str(tolerance_percent)) / Decimal('100')
    return amount - tolerance, amount + tolerance


def _consumed_external_ids(txn: Transaction) -> list[str]:
    raw = (txn.external_data or {}).get(CONSUMED_EXTERNAL_IDS_KEY) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(v) for v in raw if v]


def _mark_transfer_consumed_income(
    transfer: Transaction,
    *,
    income_external_id: str,
    income_amount: Decimal,
    income_account_id: int | None,
    income_date=None,
) -> None:
    """Запам'ятовує incoming StatementItem, який уже зведено в transfer.

    Monobank може повторно віддати той самий вхідний item через webhook/backfill.
    Оскільки income-рядок ми видаляємо, dedupe за ``Transaction.external_id`` вже
    не спрацьовує. Маркер на transfer робить повторний імпорт ідемпотентним.
    """
    if not income_external_id:
        return
    transfer.refresh_from_db(fields=['external_data'])
    data = dict(transfer.external_data or {})
    consumed = _consumed_external_ids(transfer)
    if income_external_id not in consumed:
        consumed.append(income_external_id)
    data[CONSUMED_EXTERNAL_IDS_KEY] = consumed
    data['matched_income_external_id'] = income_external_id
    data['matched_income_amount'] = str(income_amount)
    if income_account_id:
        data['matched_income_account_id'] = income_account_id
    if income_date:
        data['matched_income_date'] = income_date.isoformat()
    Transaction.objects.filter(pk=transfer.pk).update(external_data=data)
    transfer.external_data = data


def _matches_counterparty_hint(transfer: Transaction, hint: dict) -> bool:
    counter_iban = (hint or {}).get('counter_iban', '')
    if counter_iban and transfer.account and transfer.account.iban:
        return counter_iban == transfer.account.iban

    counter_name = ((hint or {}).get('counter_name') or '').lower()
    if counter_name and transfer.account and transfer.account.name.lower() in counter_name:
        return True
    return False


def _transfer_already_absorbed_income(transfer: Transaction) -> bool:
    """True, якщо переказ уже «поглинув» зустрічний дохід (вхідну ногу).

    Один переказ має поглинати РІВНО один income. Якщо вже є matched-маркер —
    другий дохід тієї ж суми НЕ матчимо до цього ж переказу (захист від
    помилкового видалення легітимного другого доходу).
    """
    data = transfer.external_data or {}
    return bool(data.get('matched_income_external_id'))


def _find_matching_existing_transfer(
    account: Account,
    *,
    amount: Decimal,
    when,
    external_id: str = '',
    hint: dict | None = None,
    window_hours=168,
    tolerance_percent=2.0,
) -> Transaction | None:
    """Шукає вже створений transfer, який відповідає incoming income item.

    ВАЖЛИВО: integration-перекази створює ЛИШЕ ``reconcile_internal_transfers``
    і ЛИШЕ після підтвердження, що counter_iban вихідної ноги = IBAN власного
    рахунку. Тобто будь-який integration-transfer на цей ``account`` — це вже
    доведений ВНУТРІШНІЙ переказ. Тому зведення його вхідної ноги (income) за
    сумою+часом БЕЗПЕЧНЕ (на відміну від загального випадку довільних доходів).

    Порядок матчингу (від найнадійнішого):
      1) external_id уже у consumed-списку переказу (ідемпотентність повтору);
      2) збіг counterparty hint (counter_iban/counter_name);
      3) integration-переказ, що ще НЕ поглинув income, із сумою в толерантності
         та в межах вікна — обираємо найближчий за часом.
    Друга нога перевіряється проти ``to_amount`` переказу (сума зарахування).
    """
    if amount <= 0 or when is None:
        return None

    min_amount, max_amount = _amount_bounds(amount, tolerance_percent)
    window = dt.timedelta(hours=window_hours)
    qs = (Transaction.objects.filter(
            company=account.company, source='integration',
            status=Transaction.STATUS_ACTUAL, type=Transaction.TYPE_TRANSFER,
            to_account=account, currency=account.currency,
            date_actual__gte=when - window, date_actual__lte=when + window)
          .exclude(account_id=account.id)
          .select_related('account', 'to_account')
          .order_by('date_actual'))

    fallback = None
    fallback_gap = None
    for transfer in qs:
        target = transfer.to_amount if transfer.to_amount is not None else transfer.amount
        if target is None or not (min_amount <= target <= max_amount):
            continue
        # 1) Точна ідемпотентність: цей же item уже зведено в цей переказ.
        if external_id and external_id in _consumed_external_ids(transfer):
            return transfer
        # 2) Підтверджений збіг за реквізитами контрагента.
        if _matches_counterparty_hint(transfer, hint or {}):
            return transfer
        # 3) Кандидат на безпечне зведення за сумою+часом: лише переказ, що ще
        #    НЕ поглинув income. Обираємо найближчий за часом.
        if not _transfer_already_absorbed_income(transfer):
            gap = abs((transfer.date_actual - when).total_seconds())
            if fallback is None or gap < fallback_gap:
                fallback = transfer
                fallback_gap = gap
    return fallback


def _statement_window(frm: dt.datetime, to: dt.datetime):
    """Розбиває [frm, to] на вікна ≤ 31 доба (вимога API), у хронологічному
    порядку (старіші — першими), щоб імпорт ішов від давніх до свіжих."""
    windows = []
    step = dt.timedelta(days=30)  # запас від ліміту 31 доба
    cursor = frm
    while cursor < to:
        end = min(cursor + step, to)
        windows.append((cursor, end))
        cursor = end
    return windows


@db_transaction.atomic
def _import_item(account: Account, item: dict, *, user, apply_rules=True):
    """Створює операцію з одного StatementItem (idempotent за external_id)."""
    company = account.company
    ext = _external_id(item.get('id', ''))
    if not item.get('id'):
        return None

    amount_minor = int(item.get('amount', 0) or 0)
    if amount_minor == 0:
        return None
    when = timezone.datetime.fromtimestamp(int(item.get('time', 0)), tz=dt.timezone.utc)
    txn_type = Transaction.TYPE_INCOME if amount_minor > 0 else Transaction.TYPE_EXPENSE
    comment = (item.get('description') or item.get('comment') or '').strip()[:1000]
    mcc = item.get('mcc')
    try:
        mcc = int(mcc) if mcc is not None else None
    except (TypeError, ValueError):
        mcc = None

    # Зберігаємо багаті дані провайдера для подальшого аналізу/фільтрів.
    external_data = {
        'provider': 'monobank',
        'mcc': mcc,
        'original_mcc': item.get('originalMcc'),
        'operation_amount': item.get('operationAmount'),
        'operation_currency': item.get('currencyCode'),
        'commission_rate': item.get('commissionRate'),
        'cashback_amount': item.get('cashbackAmount'),
        'balance_after': item.get('balance'),
        'hold': True if item.get('hold') else False,
        'counter_iban': item.get('counterIban', ''),
        'counter_name': item.get('counterName', ''),
        'counter_edrpou': item.get('counterEdrpou', ''),
        'receipt_id': item.get('receiptId', ''),
    }
    # Прибираємо порожні значення, щоб JSON був компактним.
    external_data = {k: v for k, v in external_data.items() if v not in (None, '', 0)}

    existing = Transaction.objects.filter(company=company, external_id=ext).first()
    if existing is not None:
        # hold знято (операція фіналізована банком) — оновлюємо суму/дату/прапор.
        # Сама операція вже була ACTUAL (hold-кошти зняті одразу), тож статус
        # лишаємо фактичним — змінюється лише уточнена сума/дата проведення.
        if existing.external_data.get('hold') and not item.get('hold'):
            external_data.pop('hold', None)
            txn = txn_service.update_transaction(
                existing, user=user, type=txn_type, amount=abs(_money(amount_minor)),
                account=account, currency=account.currency, date_actual=when,
                comment=comment, status=Transaction.STATUS_ACTUAL,
                external_data=external_data, mcc=mcc,
            )
            if apply_rules:
                try:
                    from . import rules_engine
                    rules_engine.apply_rules_to_transaction(txn, user=user, source='integration')
                except Exception:  # noqa: BLE001 — правила не мають ламати імпорт
                    pass
            return txn
        return None

    amount = abs(_money(amount_minor))
    if txn_type == Transaction.TYPE_INCOME:
        transfer = _find_matching_existing_transfer(
            account, amount=amount, when=when, external_id=ext, hint=external_data,
        )
        if transfer is not None:
            _mark_transfer_consumed_income(
                transfer, income_external_id=ext, income_amount=amount,
                income_account_id=account.id, income_date=when,
            )
            return None

    # ВАЖЛИВО: hold-операції (заблоковані кошти) створюємо як ФАКТИЧНІ — банк уже
    # зняв/заблокував гроші й вони входять у баланс рахунку, тож мають одразу
    # враховуватись в аналітиці (cash flow / P&L / особисті витрати). Прапор
    # hold лишається в external_data для інформації; коли hold знімається —
    # просто оновлюємо суму (гілка existing вище).
    txn = txn_service.create_transaction(
        user=user, type=txn_type, amount=amount,
        account=account, currency=account.currency, date_actual=when,
        comment=comment, source='integration', external_id=ext,
        status=Transaction.STATUS_ACTUAL,
        external_data=external_data, mcc=mcc,
    )
    # is_business успадковується від account у create_transaction (ФОП → бізнес).
    if apply_rules and txn.status == Transaction.STATUS_ACTUAL:
        try:
            from . import rules_engine
            rules_engine.apply_rules_to_transaction(txn, user=user, source='integration')
        except Exception:  # noqa: BLE001 — правила не мають ламати імпорт
            pass
    # Авто-категоризація за MCC (лише витрати без категорії — правила мають
    # пріоритет). Призначає finance.Category, що відповідає групі MCC monobank.
    _auto_categorize_by_mcc(txn)
    return txn


def _auto_categorize_by_mcc(txn) -> None:
    """Призначає категорію витраті за MCC, якщо її ще не визначено правилами."""
    if (txn.type != Transaction.TYPE_EXPENSE or txn.category_id
            or txn.status != Transaction.STATUS_ACTUAL or txn.mcc is None):
        return
    try:
        from . import mcc as mcc_mod
        cat = mcc_mod.get_or_create_category_for_mcc(txn.company, txn.mcc)
        if cat is not None:
            Transaction.objects.filter(pk=txn.pk).update(category=cat)
            txn.category = cat
    except Exception:  # noqa: BLE001 — категоризація не має ламати імпорт
        pass


def import_statement(account: Account, items: list, *, user, apply_rules=True) -> dict:
    """Імпортує список StatementItem у рахунок. Повертає статистику."""
    created = skipped = 0
    for item in items:
        txn = _import_item(account, item, user=user, apply_rules=apply_rules)
        if txn is None:
            skipped += 1
        else:
            created += 1
    return {'created': created, 'skipped': skipped}


def _reconcile_balance(account: Account, mono_balance_minor: int) -> None:
    """Доводить current_balance рахунку до фактичного балансу Monobank.

    Виписка може не покривати всю історію, тож після імпорту виставляємо
    ``initial_balance`` так, щоб current_balance == баланс у банку (back-calc):
        initial = mono_balance − (рух за фактичними операціями рахунку).
    """
    account.recalc_balance(save=True)
    account.refresh_from_db()
    target = _money(mono_balance_minor)
    diff = target - account.current_balance
    if diff != 0:
        account.initial_balance = (account.initial_balance + diff).quantize(Decimal('0.01'))
        account.save(update_fields=['initial_balance'])
        account.recalc_balance(save=True)


def reconcile_balances_from_client(conn: IntegrationConnection, client=None) -> int:
    """Фінальний back-calc балансів усіх рахунків підключення проти client-info.

    ВАЖЛИВО: викликається ПІСЛЯ reconcile_internal_transfers, бо той мутує
    транзакції (перетворює витрати на перекази, видаляє дублі-доходи). Тільки
    після усіх мутацій ми можемо гарантовано прив'язати current_balance до
    реального балансу банку — інакше баланс у сайдбарі «дрейфує».
    Повертає к-сть звірених рахунків. Не кидає винятки (best-effort).
    """
    try:
        client = client or _client_for(conn, use_cache=True)
        by_ext = {a.id: a for a in client.accounts()}
    except mono_api.MonoApiError:
        return 0
    n = 0
    for account in conn.accounts.filter(external_account_id__in=by_ext.keys()):
        try:
            _reconcile_balance(account, by_ext[account.external_account_id].balance)
            n += 1
        except Exception:  # noqa: BLE001 — звірка балансу не має ламати флоу
            continue
    return n


def reconcile_balance_from_statement(account: Account) -> bool:
    """Перепинає current_balance до останнього достовірного ``balance_after``
    зі ВЛАСНОЇ виписки рахунку — БЕЗ запиту до API (для cron, що поважає ліміти).

    Перекази, отримані з чужого рахунку (account != self), мають чужий
    balance_after, тож беремо лише статемент-айтеми, де ``account == self``.
    Повертає True, якщо вдалося звірити.
    """
    last = (Transaction.objects.filter(
                status=Transaction.STATUS_ACTUAL, account=account)
            .exclude(external_data__balance_after__isnull=True)
            .order_by('-date_actual', '-id').first())
    if not last:
        return False
    ba = (last.external_data or {}).get('balance_after')
    if ba is None:
        return False
    try:
        _reconcile_balance(account, int(ba))
    except Exception:  # noqa: BLE001
        return False
    return True


@db_transaction.atomic
def sync_account(account: Account, *, user, apply_rules=True, full=False,
                 client=None, max_windows=None, reconcile_balance=True) -> dict:
    """Синхронізує одну прив'язку: тягне виписку (свіжі — першими) та звіряє баланс.

    Дві фази, що поважають ліміт Monobank (1 запит/60с):
      1) **інкремент** — нові операції від (last_sync_at − 3 доби) до now;
      2) **backfill** — добір історії вглиб через курсор ``backfill_until`` у
         conn.meta, вікнами по ≤30 діб, поки не дійдемо до sync_from / −365 діб.
    ``max_windows`` обмежує к-сть statement-запитів за прогін. На 429 — м'яко
    зупиняємось (``rate_limited=True``), курсор зберігається для наступного разу.
    (``full`` лишено для сумісності виклику; глибину добору задає ``max_windows``.)
    ``reconcile_balance=False`` використовується cron-командою, щоб не робити
    додатковий client-info запит у тому ж короткому polling-прогоні.
    """
    conn = account.integration
    if conn is None or not conn.has_token or not account.external_account_id:
        return {'created': 0, 'skipped': 0, 'error': 'Рахунок не підключено до API'}

    client = client or _client_for(conn, use_cache=False)
    now = timezone.now()
    meta = dict(conn.meta or {})
    cur_key = f'backfill_until_{account.external_account_id}'
    floor = (timezone.make_aware(dt.datetime.combine(conn.sync_from, dt.time.min))
             if conn.sync_from else now - dt.timedelta(days=365))

    totals = {'created': 0, 'skipped': 0, 'rate_limited': False}
    budget = max_windows or 999

    # Фаза 1: інкремент (свіжі операції) — виконується завжди.
    inc_from = (conn.last_sync_at - dt.timedelta(days=3)) if conn.last_sync_at else floor
    for w_from, w_to in reversed(_statement_window(inc_from, now)):  # свіжі першими
        if budget <= 0:
            break
        try:
            items = client.statement(account.external_account_id,
                                     int(w_from.timestamp()), int(w_to.timestamp()))
        except mono_api.MonoRateLimitError:
            totals['rate_limited'] = True
            break
        r = import_statement(account, items, user=user, apply_rules=apply_rules)
        totals['created'] += r['created']; totals['skipped'] += r['skipped']
        budget -= 1

    # Фаза 2: backfill вглиб (від курсора донизу до floor), теж свіжі-першими.
    until = meta.get(cur_key)
    until_dt = dt.datetime.fromisoformat(until) if until else now
    if timezone.is_naive(until_dt):
        until_dt = timezone.make_aware(until_dt)
    while budget > 0 and not totals['rate_limited'] and until_dt > floor:
        w_from = max(until_dt - dt.timedelta(days=30), floor)
        try:
            items = client.statement(account.external_account_id,
                                     int(w_from.timestamp()), int(until_dt.timestamp()))
        except mono_api.MonoRateLimitError:
            totals['rate_limited'] = True
            break
        r = import_statement(account, items, user=user, apply_rules=apply_rules)
        totals['created'] += r['created']; totals['skipped'] += r['skipped']
        until_dt = w_from
        budget -= 1
    meta[cur_key] = until_dt.isoformat()

    if reconcile_balance:
        # Баланс беремо з кешованого/свіжого client-info (без зайвого запиту).
        try:
            for a in client.accounts():
                if a.id == account.external_account_id:
                    _reconcile_balance(account, a.balance)
                    break
        except mono_api.MonoApiError:
            pass

    conn.meta = meta
    update_fields = ['meta']
    if not totals['rate_limited']:
        conn.last_sync_at = now
        conn.error_message = ''
        update_fields += ['last_sync_at', 'error_message']
    conn.save(update_fields=update_fields)
    return totals


def sync_connection(conn: IntegrationConnection, *, user, full=False,
                    max_windows_per_account=None) -> dict:
    """Синхронізує всі авто-рахунки підключення під одним клієнтом (спільний кеш)."""
    summary = {'accounts': 0, 'created': 0, 'skipped': 0, 'errors': [], 'rate_limited': False}
    # Один свіжий client-info на весь прогін (для звірки балансів усіх рахунків).
    client = _client_for(conn, use_cache=False)
    try:
        client.client_info(refresh=True)
    except mono_api.MonoApiError:
        pass
    for account in conn.accounts.filter(auto_sync=True):
        try:
            res = sync_account(account, user=user, full=full, client=client,
                               max_windows=max_windows_per_account)
            summary['accounts'] += 1
            summary['created'] += res.get('created', 0)
            summary['skipped'] += res.get('skipped', 0)
            summary['rate_limited'] = summary['rate_limited'] or res.get('rate_limited', False)
        except mono_api.MonoApiError as exc:
            summary['errors'].append(f'{account.name}: {exc}')
    reconcile_internal_transfers(conn.company, user=user)
    # Фінальний back-calc балансів ПІСЛЯ reconcile — гарантує, що сайдбар
    # завжди показує реальний баланс банку (reconcile міг змінити транзакції).
    reconcile_balances_from_client(conn, client=client)
    return summary


# ----------------------------- Внутрішні перекази -----------------------------

def _reconcile_income_duplicates_for_existing_transfers(
    company, *, user, window_hours=168, tolerance_percent=2.0,
) -> int:
    """Видаляє income-дублі, якщо matching transfer уже існує.

    Це покриває legacy-стан: outgoing уже був перетворений на синій transfer,
    але incoming statement item пізніше знову імпортувався як зелений дохід.
    """
    incomes = (Transaction.objects.filter(
            company=company, source='integration', status=Transaction.STATUS_ACTUAL,
            type=Transaction.TYPE_INCOME)
        .exclude(account__isnull=True)
        .select_related('account'))
    matched = 0
    for income in list(incomes):
        transfer = _find_matching_existing_transfer(
            income.account, amount=income.amount, when=income.date_actual,
            external_id=income.external_id, hint=income.external_data or {},
            window_hours=window_hours, tolerance_percent=tolerance_percent,
        )
        if transfer is None:
            continue

        income_id = income.id
        income_external_id = income.external_id
        _mark_transfer_consumed_income(
            transfer, income_external_id=income_external_id,
            income_amount=income.amount, income_account_id=income.account_id,
            income_date=income.date_actual,
        )
        txn_service.delete_transaction(income, user=user)
        audit_service.log_action(
            user, 'reconcile', 'transaction', transfer.id,
            summary=f'Прибрано дубль income #{income_id} для переказу #{transfer.id}',
            source='integration', company=company,
        )
        matched += 1
    return matched


@db_transaction.atomic
def reconcile_internal_transfers(company, *, user, window_hours=168, tolerance_percent=2.0) -> int:
    """Розпізнає перекази між власними рахунками (напр. ФОП → особиста картка).

    Зустрічна пара = витрата на одному рахунку + дохід на іншому на близьку
    суму в межах вікна. Перетворюємо їх на один Transfer (не дохід/витрата),
    щоб не подвоювати P&L. Працює з імпортованими (integration) парами.

    Покращення v2:
    - Толерантність по сумі (±2% за замовчуванням) для врахування комісії
    - Розширене вікно пошуку (168 годин = 7 днів)
    - Пошук за counter_iban/counter_name з external_data
    - Не вимагає щоб обидва рахунки були з однієї інтеграції
    """
    qs = (Transaction.objects.filter(
            company=company, source='integration', status=Transaction.STATUS_ACTUAL,
            type=Transaction.TYPE_EXPENSE)
          .exclude(account__isnull=True))
    window = dt.timedelta(hours=window_hours)
    matched = _reconcile_income_duplicates_for_existing_transfers(
        company, user=user, window_hours=window_hours,
        tolerance_percent=tolerance_percent,
    )

    # Матеріалізуємо список наперед: у циклі ми міняємо type/видаляємо рядки,
    # тож стрімити з курсора (iterator) небезпечно на частині бекендів.
    # Карта IBAN → власний рахунок: надійне розпізнавання ВЛАСНИХ переказів.
    own_by_iban = {a.iban: a for a in company.accounts.exclude(iban='') if a.iban}

    for expense in list(qs):
        # ВНУТРІШНІЙ переказ підтверджуємо ЛИШЕ якщо counter_iban витрати збігається
        # з IBAN одного з ВЛАСНИХ рахунків. Перекази на чужі картки/ФОП (іншій людині)
        # лишаються ВИТРАТОЮ. Це усуває хибне злиття за часом+сумою (попередній баг,
        # коли переказ на чужу картку рахувався як переказ на власний ФОП).
        expense_counter_iban = (expense.external_data or {}).get('counter_iban', '')
        if not expense_counter_iban:
            continue  # без IBAN призначення не можемо підтвердити власний переказ
        dest = own_by_iban.get(expense_counter_iban)
        if dest is None or dest.id == expense.account_id:
            continue  # призначення — НЕ наш рахунок → це реальна витрата, не чіпаємо

        # Підтверджено внутрішній переказ на власний рахунок dest. Шукаємо
        # зустрічний дохід саме на dest (для точного to_amount з урахуванням комісії).
        min_amount, max_amount = _amount_bounds(expense.amount, tolerance_percent)
        income = (Transaction.objects.filter(
                    company=company, source='integration',
                    status=Transaction.STATUS_ACTUAL, type=Transaction.TYPE_INCOME,
                    account=dest, currency=expense.currency,
                    amount__gte=min_amount, amount__lte=max_amount,
                    date_actual__gte=expense.date_actual - window,
                    date_actual__lte=expense.date_actual + window)
                  .order_by('date_actual').first())

        # Витрату перетворюємо на переказ → власний рахунок dest; дубль-дохід (за
        # наявності) видаляємо. Якщо доходу ще немає — переказ усе одно валідний
        # (підтверджено IBAN), to_amount беремо = сумі витрати.
        income_id = income.id if income else None
        income_external_id = income.external_id if income else ''
        income_amount = income.amount if income is not None else expense.amount
        income_account_id = income.account_id if income else dest.id
        income_date = income.date_actual if income else None

        if income is not None:
            txn_service.delete_transaction(income, user=user)
        transfer = txn_service.convert_to_transfer(
            expense, user=user, to_account=dest, to_amount=income_amount)
        if income_external_id:
            _mark_transfer_consumed_income(
                transfer, income_external_id=income_external_id,
                income_amount=income_amount, income_account_id=income_account_id,
                income_date=income_date,
            )

        # Переказ між власними рахунками не є ні доходом, ні витратою бізнесу.
        # Призначаємо системну категорію "Вивід на особисте" (Owner's Drawings)
        owner_drawings_cat = company.categories.filter(
            is_system=True, name='Вивід на особисте'
        ).first()

        update_fields = {'is_business': False}
        if owner_drawings_cat:
            update_fields['category_id'] = owner_drawings_cat.id

        Transaction.objects.filter(pk=expense.pk).update(**update_fields)

        diff_note = (f', різниця {abs(expense.amount - income_amount):.2f}'
                     if income is not None else '')
        audit_service.log_action(
            user, 'reconcile', 'transaction', expense.id,
            summary=f'Зведено у переказ на власний {dest.name}'
                    + (f' (дубль #{income_id} видалено{diff_note})' if income_id else ''),
            source='integration', company=company,
        )
        matched += 1
    return matched


# ----------------------------- Вебхук -----------------------------

@db_transaction.atomic
def process_webhook(conn: IntegrationConnection, payload: dict, *, user=None) -> dict:
    """Обробляє push Monobank: {type:'StatementItem', data:{account, statementItem}}."""
    data = (payload or {}).get('data') or {}
    ext_account_id = data.get('account', '')
    item = data.get('statementItem') or {}
    account = Account.objects.filter(
        company=conn.company, external_account_id=ext_account_id).first()
    if account is None:
        return {'ok': False, 'error': 'unknown account'}
    txn = _import_item(account, item, user=user, apply_rules=True)
    if txn is not None:
        from .payment_intents import match_pending_intents
        match_pending_intents(txn, user=user)
        # Спершу зводимо внутрішні перекази (може змінити транзакції рахунку),
        # і ЛИШЕ ПОТІМ робимо back-calc балансу проти банку — інакше баланс
        # дрейфує, бо reconcile мутує дані вже після фіксації initial_balance.
        reconcile_internal_transfers(conn.company, user=user)
        if 'balance' in item:
            _reconcile_balance(account, item.get('balance'))
        conn.last_sync_at = timezone.now()
        conn.save(update_fields=['last_sync_at'])
    return {'ok': True, 'created': 1 if txn else 0}
