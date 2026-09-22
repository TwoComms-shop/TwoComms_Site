"""
Social-auth pipeline для Google OAuth.

Поведение, важливе для нашого UX:

* Користувач, який зайшов через Telegram, не має ні email, ні Google-привʼязки.
  Коли він на /profile/setup/ клікає «Підключити Google», ми запускаємо
  OAuth /oauth/login/google-oauth2/?next=/profile/setup/. У такому випадку
  pipeline отримує `strategy.request.user = <Telegram-юзер>`, але
  `social_user` може повернути іншого юзера (User_Google), якщо цей
  Google-акаунт вже був повʼязаний з іншим записом. За замовчуванням
  social-django падає з AuthAlreadyAssociated. Ми ж хочемо ЗЛИТИ ці два
  записи в один.

* Те саме у зворотному напрямку: користувач залогінений через Google,
  привʼязує Telegram через нашу модалку — це йде через `purpose=profile_link`,
  без OAuth, тому конфлікту не виникає (див. `accounts.telegram_bot._apply_profile_link_purpose`).

Стратегія merge (у функції `merge_with_authenticated_user`):

  - Беремо `current_user = strategy.request.user` (якщо authenticated).
  - Якщо `social_user` знайшов іншого user (`other_user != current_user`):
      1. Переносимо все, що належить other_user, на current_user
         (orders, points, favorites, social_auth, profile data).
      2. Видаляємо other_user.
      3. Замінюємо `kwargs['user'] = current_user` і `kwargs['social']`
         оновлюється відповідно.
  - Це безпечно, бо current_user — це юзер, на якому юзер реально
    активний у браузері; other_user — старий, ймовірно «orphaned»
    запис, створений автоматично при першому Google-логіні.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from social_core.exceptions import AuthAlreadyAssociated, AuthException

from accounts.models import UserProfile

logger = logging.getLogger(__name__)
User = get_user_model()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _get_current_user(strategy):
    """Поточний user з Django session (тобто той, кого браузер реально бачить)."""
    request = getattr(strategy, "request", None) or getattr(strategy, "_request", None)
    if not request:
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


def _merge_user_data(*, source: "User", target: "User") -> None:
    """Переносимо все важливе з `source` на `target`.

    Захист: ніколи не видаляємо superuser або staff. Якщо source —
    superuser/staff, операція skip-иться.

    Нічого не перезаписуємо у target, якщо там уже є дані. Беремо лише ті
    поля, що порожні.
    """
    from accounts.models import UserPoints, FavoriteProduct
    from orders.models import Order

    # SAFETY GUARD: ніколи не видаляємо superuser/staff як source
    # (це призведе до видалення адміна).
    if source.is_superuser or source.is_staff:
        logger.warning(
            "merge_user_data: refused to merge source user '%s' (is_staff=%s, is_superuser=%s)",
            source.username, source.is_staff, source.is_superuser,
        )
        raise ValueError("Cannot merge admin/staff source user")

    # Залити В адміна — дозволено: source — звичайний юзер,
    # target — той самий реальний користувач, що адмінить сайт.
    # Права не змінюються (беремо їх з target).

    with transaction.atomic():
        # 1) Базові поля User
        if not target.email and source.email:
            target.email = source.email
        if not target.first_name and source.first_name:
            target.first_name = source.first_name
        if not target.last_name and source.last_name:
            target.last_name = source.last_name
        target.save(update_fields=["email", "first_name", "last_name"])

        # 2) UserProfile — підтягуємо порожні поля з source
        try:
            src_profile = UserProfile.objects.get(user=source)
        except UserProfile.DoesNotExist:
            src_profile = None
        target_profile, _ = UserProfile.objects.get_or_create(user=target)
        if src_profile:
            mergeable_fields = [
                "phone", "city", "np_office",
                "np_settlement_ref", "np_city_ref", "np_warehouse_ref",
                "full_name", "email", "telegram", "instagram",
                "whatsapp", "viber", "birth_date",
                "telegram_id",
                "company_name", "company_number", "delivery_address", "website",
                "payment_method", "payment_details",
            ]
            updates = []
            for field in mergeable_fields:
                src_val = getattr(src_profile, field, None)
                cur_val = getattr(target_profile, field, None)
                if src_val and not cur_val:
                    setattr(target_profile, field, src_val)
                    updates.append(field)
            # Аватарку беремо з source ТІЛЬКИ якщо у target її нема
            if src_profile.avatar and not target_profile.avatar:
                target_profile.avatar = src_profile.avatar
                updates.append("avatar")
                # detach, щоб не видалився файл при cascade-delete src_profile
                src_profile.avatar = None
                src_profile.save(update_fields=["avatar"])
            if src_profile.ubd_doc and not target_profile.ubd_doc:
                target_profile.ubd_doc = src_profile.ubd_doc
                updates.append("ubd_doc")
                src_profile.ubd_doc = None
                src_profile.save(update_fields=["ubd_doc"])
            if updates:
                target_profile.save(update_fields=list(set(updates)))

        # 3) UserPoints — складаємо
        try:
            src_points = UserPoints.objects.get(user=source)
        except UserPoints.DoesNotExist:
            src_points = None
        target_points, _ = UserPoints.objects.get_or_create(user=target)
        if src_points:
            target_points.points += src_points.points
            target_points.total_earned += src_points.total_earned
            target_points.total_spent += src_points.total_spent
            target_points.save()
            # Історія балів — переносимо
            try:
                from accounts.models import PointsHistory

                PointsHistory.objects.filter(user=source).update(user=target)
            except Exception:
                pass

        # 4) Замовлення
        try:
            Order.objects.filter(user=source).update(user=target)
        except Exception:
            pass

        # 5) Обране — переносимо без дублів
        try:
            for fav in FavoriteProduct.objects.filter(user=source):
                FavoriteProduct.objects.get_or_create(
                    user=target, product=fav.product
                )
            FavoriteProduct.objects.filter(user=source).delete()
        except Exception:
            pass

        # 6) Соц.привʼязки (інші провайдери) — переносимо на target
        try:
            from social_django.models import UserSocialAuth

            for sa in UserSocialAuth.objects.filter(user=source):
                # Якщо у target уже є привʼязка цього provider — пропускаємо
                if UserSocialAuth.objects.filter(
                    user=target, provider=sa.provider
                ).exists():
                    continue
                sa.user = target
                sa.save(update_fields=["user"])
        except Exception as exc:
            logger.warning("merge: failed to move social auths: %s", exc)

        # 7) Видаляємо source
        try:
            source.delete()
        except Exception as exc:
            logger.warning("merge: failed to delete source user %s: %s", source.pk, exc)


# ────────────────────────────────────────────────────────────────────
# Pipeline steps
# ────────────────────────────────────────────────────────────────────


def merge_with_authenticated_user(strategy, details, backend, user=None, social=None, *args, **kwargs):
    """Якщо в браузері юзер уже залогінений (наприклад через Telegram), а
    `social_user` повернув іншого user (наприклад старий Google-акаунт
    з тим самим email), — зливаємо other_user → current_user.

    Викликається ПІСЛЯ `social_user`. Якщо `user` (kwargs) дорівнює
    current_user — нічого не робимо. Якщо різні — мерджимо й замінюємо
    `kwargs['user']` на current_user.
    """
    current_user = _get_current_user(strategy)
    if not current_user:
        return None

    if user is None:
        # social_user не знайшов привʼязку — нехай далі pipeline створить
        # її та привʼяже до current_user (associate_user робить це завдяки
        # `user=current_user` нижче). Просто проставляємо current_user.
        return {"user": current_user}

    if user.pk == current_user.pk:
        return None

    # Конфлікт: інший user. Мерджимо.
    try:
        _merge_user_data(source=user, target=current_user)
    except Exception as exc:
        logger.exception("merge_with_authenticated_user failed: %s", exc)
        # Не падаємо в pipeline — лишаємо social_user-а, але керування
        # запитом перекладаємо на нього (поведінка за замовчуванням).
        return None

    # Якщо social-привʼязка існувала — після merge вона тепер на current_user.
    refreshed_social = None
    if social is not None:
        try:
            social.refresh_from_db()
            refreshed_social = social
        except Exception:
            refreshed_social = None

    return {
        "user": current_user,
        "social": refreshed_social,
        "is_new": False,
    }


def social_user_with_authenticated_merge(
    strategy, details, backend, uid, user=None, *args, **kwargs
):
    """Resolve a Google identity without losing an already logged-in user.

    ``social_core.pipeline.social_auth.social_user`` raises
    ``AuthAlreadyAssociated`` before a later pipeline step can merge the old
    social account. This wrapper performs the same lookup and handles that
    conflict while the current session user is still available.
    """
    from social_core.pipeline.social_auth import social_user

    current_user = _get_current_user(strategy)
    try:
        return social_user(
            backend,
            uid,
            *args,
            user=user,
            **kwargs,
        )
    except AuthAlreadyAssociated:
        if current_user is None:
            raise
        social = strategy.storage.user.get_social_auth(backend.name, uid)
        if social is None or social.user_id == current_user.pk:
            raise
        try:
            _merge_user_data(source=social.user, target=current_user)
        except ValueError:
            # Keep the provider's normal safe redirect for protected source
            # accounts (for example, a staff account) instead of surfacing a
            # merge guard as an unhandled callback error.
            raise AuthAlreadyAssociated(backend) from None
        social.refresh_from_db()
        return {
            "social": social,
            "user": current_user,
            "is_new": False,
            "new_association": False,
        }


def get_avatar_url(strategy, details, backend, user=None, social=None, *args, **kwargs):
    """Извлекает URL аватарки из Google для подальшого збереження."""
    if backend.name == "google-oauth2":
        response = kwargs.get("response", {})
        avatar_url = response.get("picture")
        if avatar_url:
            kwargs["avatar_url"] = avatar_url
    return kwargs


def create_or_update_profile(strategy, details, backend, user=None, social=None, *args, **kwargs):
    """Створює або оновлює профіль користувача даними з Google.

    Принципи:
      * Email/повне імʼя — заповнюємо ЛИШЕ якщо у профілі порожньо.
      * Аватарку — ставимо ТІЛЬКИ якщо її ще нема. Не перезаписуємо
        існуючу (інакше при кожному логіні зʼявляється «чужий» аватар).
    """
    if not user:
        return

    try:
        profile, _ = UserProfile.objects.get_or_create(user=user)

        if details.get("email") and not profile.email:
            profile.email = details["email"]

        if not profile.full_name:
            if details.get("fullname"):
                profile.full_name = details["fullname"]
            elif details.get("first_name") and details.get("last_name"):
                profile.full_name = f"{details['first_name']} {details['last_name']}"

        # Аватарка — ТІЛЬКИ якщо порожньо.
        avatar_url = kwargs.get("avatar_url")
        if avatar_url and not profile.avatar:
            try:
                import requests
                from django.core.files.base import ContentFile

                response = requests.get(avatar_url, timeout=(2, 5))
                if response.status_code == 200:
                    filename = f"avatar_{user.id}_{user.username}.jpg"
                    profile.avatar.save(
                        filename, ContentFile(response.content), save=False
                    )
            except Exception as exc:
                logger.debug("Failed to download avatar for %s: %s", user.username, exc)

        profile.save()
    except Exception as exc:
        logger.warning("create_or_update_profile failed: %s", exc)


def require_email(strategy, details, backend, user=None, social=None, *args, **kwargs):
    """Гарантуємо наявність email при реєстрації через Google."""
    if backend.name == "google-oauth2":
        if not details.get("email"):
            raise AuthException(backend, "Email is required for registration")
    return kwargs
