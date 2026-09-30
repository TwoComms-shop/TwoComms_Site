"""
Synchronization layer between session-based cart and the persistent ``UserCart``.

Корзина живёт в ``request.session['cart']`` и ``request.session['custom_print_cart']``,
поэтому каждое устройство имеет свою сессию. Чтобы у одного пользователя на всех
устройствах была одна и та же корзина, на каждом запросе:

1. ``hydrate_session_from_db`` сравнивает версию (``updated_at``) DB-кошика с тем,
   что сессия видела последний раз. Если в DB новее (значит, на другом устройстве
   что-то поменяли) — заменяем содержимое сессии данными из БД. Это даёт мгновенное
   обновление: добавили на десктопе → тут же видно на телефоне.
2. ``persist_session_to_db`` после view сравнивает текущее содержимое сессии со
   снапшотом, который мы сделали в начале запроса. Если юзер реально что-то менял
   (через add_to_cart/remove/update/promo) — записываем в БД и обновляем версию.
3. ``merge_session_into_db`` срабатывает на ``user_logged_in``: гостевая корзина
   мерджится с DB-кошиком (сумма количеств, объединение кастомных позиций), чтобы
   гость, который что-то добавил и потом залогинился, ничего не потерял.

Все функции ловят и логируют исключения, чтобы синхронизация никогда не валила запрос.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from django.contrib.auth import get_user_model
from django.db import transaction

from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY

from .cart_models import UserCart

logger = logging.getLogger('storefront.cart.sync')

SESSION_CART_KEY = 'cart'
SESSION_PROMO_KEY = 'promo_code_id'
# Ревизия (ISO-строка `updated_at`) синхронизированной DB-версии корзины.
# Если в DB ревизия отличается от того, что мы видим в сессии — значит, было
# изменение с другого устройства, надо подтянуть данные.
SESSION_SYNCED_FLAG = '_cart_db_revision'
# Снапшот корзины на момент входа в запрос; используется в process_response,
# чтобы определить, действительно ли юзер что-то менял в течение запроса.
REQUEST_SNAPSHOT_ATTR = '_cart_sync_snapshot'

User = get_user_model()


def _ensure_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _normalize_qty(value: Any, default: int = 1) -> int:
    try:
        qty = int(value)
    except (TypeError, ValueError):
        return default
    return qty if qty > 0 else default


def _merge_standard_carts(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Объединяет два словаря обычной корзины. Если один и тот же ключ
    (product_id:size:color:fit) встречается в обоих, количества складываются.
    Используется ТОЛЬКО при логине (мерж гостевой корзины с DB-кошиком).
    """

    merged: Dict[str, Any] = {}
    for source in (primary, secondary):
        if not isinstance(source, dict):
            continue
        for key, item in source.items():
            if not isinstance(item, dict):
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(item)
                merged[key]['qty'] = _normalize_qty(item.get('qty'))
                continue
            existing_qty = _normalize_qty(existing.get('qty'))
            incoming_qty = _normalize_qty(item.get('qty'))
            existing['qty'] = existing_qty + incoming_qty
            for meta_key, meta_val in item.items():
                if meta_key == 'qty':
                    continue
                if meta_val and not existing.get(meta_key):
                    existing[meta_key] = meta_val
    return merged


def _merge_custom_carts(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Кастомные позиции уникальны по lead_id, объединение без удвоения.
    Используется ТОЛЬКО при логине.
    """

    merged: Dict[str, Any] = {}
    for source in (primary, secondary):
        if not isinstance(source, dict):
            continue
        for key, item in source.items():
            if not isinstance(item, dict):
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(item)
                continue
            try:
                if int(item.get('quantity') or 0) > int(existing.get('quantity') or 0):
                    merged[key] = dict(item)
            except (TypeError, ValueError):
                pass
    return merged


def _read_session_cart(session) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[int]]:
    cart = _ensure_dict(session.get(SESSION_CART_KEY))
    custom_cart = _ensure_dict(session.get(SESSION_CUSTOM_CART_KEY))
    promo_raw = session.get(SESSION_PROMO_KEY)
    try:
        promo_id = int(promo_raw) if promo_raw is not None else None
    except (TypeError, ValueError):
        promo_id = None
    return cart, custom_cart, promo_id


def _write_session_cart(session, cart: Dict[str, Any], custom_cart: Dict[str, Any], promo_id: Optional[int]) -> None:
    session[SESSION_CART_KEY] = cart or {}
    if custom_cart:
        session[SESSION_CUSTOM_CART_KEY] = custom_cart
    else:
        session.pop(SESSION_CUSTOM_CART_KEY, None)
    if promo_id is not None:
        session[SESSION_PROMO_KEY] = promo_id
    else:
        session.pop(SESSION_PROMO_KEY, None)
    session.modified = True


def _carts_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (a or {}) == (b or {})


def _db_revision(db_cart: UserCart) -> str:
    """
    Строка-ревизия DB-кошика. Базируется на содержимом, а не на ``updated_at``,
    потому что у некоторых СУБД (особенно SQLite в тестах) разрешение датавремени
    может быть слишком грубым, а нам нужно ловить изменения, происходящие в
    пределах одной секунды.
    """

    import hashlib
    import json

    payload = {
        'cart': db_cart.cart_data or {},
        'custom': db_cart.custom_cart_data or {},
        'promo': db_cart.promo_code_id,
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    return f'pk:{db_cart.pk}:{digest}'


def _make_snapshot(cart: Dict[str, Any], custom_cart: Dict[str, Any], promo_id: Optional[int]) -> tuple:
    """
    Глубокий снапшот корзины для сравнения «было/стало» внутри одного запроса.
    deepcopy обязателен, потому что views часто меняют корзину in-place
    (``cart[key]['qty'] = N`` — без deepcopy snapshot мутирует вместе с session).
    """

    import copy

    return (
        copy.deepcopy(cart or {}),
        copy.deepcopy(custom_cart or {}),
        promo_id,
    )


def get_user_cart(user) -> UserCart:
    """Возвращает (или создаёт) запись UserCart для пользователя."""

    cart, _ = UserCart.objects.get_or_create(user=user)
    return cart


def hydrate_session_from_db(request, *, strict=False) -> None:
    """
    На входе запроса:
    - читаем DB-кошик и сравниваем его ревизию с той, что сессия видела последний раз
    - если ревизии разные → REPLACE сессионную корзину содержимым из БД
      (это значит, что на другом устройстве были изменения; локальная сессия,
      которая ещё не отправляла обновления, остаётся синхронной)
    - сохраняем «снапшот» текущей сессии на ``request``, чтобы потом понять,
      менял ли юзер корзину в течение запроса
    """

    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return

    try:
        session = request.session
    except AttributeError:
        return

    try:
        db_cart = get_user_cart(user)
    except Exception:
        logger.warning('Failed to load UserCart for user=%s', getattr(user, 'pk', None), exc_info=True)
        if strict:
            raise
        return

    db_revision = _db_revision(db_cart)
    session_revision = session.get(SESSION_SYNCED_FLAG)

    if session_revision != db_revision:
        # На другом устройстве (или на этом же раньше) кошик уже изменён —
        # тащим свежую версию из БД, чтобы интерфейс показал актуальное состояние.
        db_cart_data = _ensure_dict(db_cart.cart_data)
        db_custom_data = _ensure_dict(db_cart.custom_cart_data)
        db_promo = db_cart.promo_code_id
        _write_session_cart(session, db_cart_data, db_custom_data, db_promo)
        session[SESSION_SYNCED_FLAG] = db_revision

    # Запоминаем «то, что юзер видит сейчас» — после view сравним с этим.
    cart, custom_cart, promo = _read_session_cart(session)
    setattr(request, REQUEST_SNAPSHOT_ATTR, _make_snapshot(cart, custom_cart, promo))


def persist_session_to_db(request, *, strict=False) -> None:
    """
    На выходе запроса:
    - если view ничего не менял (текущая сессия равна снапшоту в начале запроса),
      пропускаем — экономим записи в БД
    - иначе пишем в БД и обновляем ревизию в сессии, чтобы следующий
      ``process_request`` не считал, что данные «устарели»
    """

    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return

    try:
        session = request.session
    except AttributeError:
        return

    snapshot = getattr(request, REQUEST_SNAPSHOT_ATTR, None)
    cart, custom_cart, promo = _read_session_cart(session)

    if snapshot is not None:
        prev_cart, prev_custom, prev_promo = snapshot
        if (
            _carts_equal(cart, prev_cart)
            and _carts_equal(custom_cart, prev_custom)
            and promo == prev_promo
        ):
            # Юзер ничего не трогал в этом запросе. БД и сессия уже синхронны
            # (см. hydrate_session_from_db).
            return

    try:
        with transaction.atomic():
            db_cart, _ = UserCart.objects.select_for_update().get_or_create(user=user)

            db_cart.cart_data = cart or {}
            db_cart.custom_cart_data = custom_cart or {}
            db_cart.promo_code_id = promo
            db_cart.save(update_fields=['cart_data', 'custom_cart_data', 'promo_code_id', 'updated_at'])
    except Exception:
        logger.warning('Failed to persist cart for user=%s', getattr(user, 'pk', None), exc_info=True)
        if strict:
            raise
        return

    # Обновляем ревизию в сессии, чтобы следующий запрос с этого же устройства
    # не считал DB «новее» и не подтягивал лишний раз.
    session[SESSION_SYNCED_FLAG] = _db_revision(db_cart)
    session.modified = True


def merge_session_into_db(request, user) -> None:
    """
    Логин/регистрация: гостевая сессионная корзина мержится с DB-кошиком.
    Количества по совпадающим ключам суммируются, кастомные позиции — union.
    """

    if not user or not getattr(user, 'pk', None):
        return

    try:
        session = request.session
    except AttributeError:
        return

    session_cart, session_custom_cart, session_promo = _read_session_cart(session)

    try:
        with transaction.atomic():
            db_cart, _ = UserCart.objects.select_for_update().get_or_create(user=user)
            db_cart_data = _ensure_dict(db_cart.cart_data)
            db_custom_data = _ensure_dict(db_cart.custom_cart_data)
            db_promo = db_cart.promo_code_id

            merged_cart = _merge_standard_carts(session_cart, db_cart_data)
            merged_custom = _merge_custom_carts(session_custom_cart, db_custom_data)
            # Если юзер только что применил промокод в гостевой сессии,
            # отдаём приоритет ему; иначе берём из БД.
            merged_promo = session_promo if session_promo else db_promo

            db_cart.cart_data = merged_cart or {}
            db_cart.custom_cart_data = merged_custom or {}
            db_cart.promo_code_id = merged_promo
            db_cart.save(update_fields=['cart_data', 'custom_cart_data', 'promo_code_id', 'updated_at'])
    except Exception:
        logger.warning('Failed to merge anonymous cart into DB for user=%s', getattr(user, 'pk', None), exc_info=True)
        return

    _write_session_cart(session, merged_cart, merged_custom, merged_promo)
    session[SESSION_SYNCED_FLAG] = _db_revision(db_cart)
    # Снапшот тоже обновим — мы только что синхронизировали и сессию, и БД.
    setattr(request, REQUEST_SNAPSHOT_ATTR, _make_snapshot(merged_cart, merged_custom, merged_promo))
    session.modified = True
