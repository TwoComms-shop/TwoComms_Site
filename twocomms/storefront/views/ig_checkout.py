from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.crypto import constant_time_compare
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from management.models import IgCheckoutAccessToken, IgCheckoutProposal
from storefront.services.approved_policy_copy import approved_policy_copy


GRANT_SALT = "twocomms.instagram-checkout.grant.v1"
GRANT_SESSION_PREFIX = "ig_checkout_grant:"
GRANT_MAX_AGE = 60 * 60 * 12
INSTAGRAM_DIRECT_URL = "https://www.instagram.com/twocomms/"

CHECKOUT_LANGUAGES = (
    {"code": "uk", "label": "Українська", "short": "UA", "flag": "🇺🇦", "flag_image": False},
    {"code": "ru", "label": "Русский", "short": "RU", "flag": "", "flag_image": True},
    {"code": "en", "label": "English", "short": "EN", "flag": "🇬🇧", "flag_image": False},
)


CHECKOUT_COPY = {
    "uk": {
        "page_title": "Перевірте замовлення",
        "language_label": "Мова",
        "eyebrow": "Замовлення готове до оформлення",
        "lead": "Перевірте товар. Доставка й оплата — нижче.",
        "greeting": "Вітаємо",
        "order_for": "Це замовлення сформовано для вас на суму",
        "order_label": "Ваше замовлення",
        "item_singular": "товар",
        "item_plural": "товарів",
        "proposal_label": "Пропозиція",
        "expires_label": "Посилання активне ще",
        "expires_explanation": "Посилання діє 25 хвилин від створення. Його можна передати іншій людині для оплати.",
        "expires_explanation_v2": "Пропозиція та захищене посилання діють 12 годин. Кожен рахунок Monobank діє 25 хвилин.",
        "payment_method": "Спосіб оплати",
        "payment_full": "Повна оплата онлайн",
        "payment_200_cod": "200 грн зараз, решта — післяплатою",
        "expired_short": "Час завершився",
        "color_label": "Колір",
        "fit_label": "Фасон",
        "size_label": "Розмір",
        "quantity_label": "Кількість",
        "catalog_total": "Вартість товарів",
        "discount": "Узгоджена знижка",
        "total": "До сплати",
        "charge_now": "Сума до сплати зараз",
        "fixed_price_title": "Товари й ціна зафіксовані",
        "fixed_price_title_v2": "Вибір збережено на 12 годин",
        "fixed_price_hint": "Що це означає?",
        "price_dialog_title": "Ціна не зміниться",
        "price_dialog_body": "До завершення строку пропозиція зберігає обрані товари, розміри та погоджену суму.",
        "price_dialog_body_v2": "Посилання та вибрані параметри збережені на 12 годин. Перед створенням кожного 25-хвилинного рахунку ми повторно перевіряємо актуальну ціну, знижку й наявність.",
        "price_dialog_close": "Зрозуміло",
        "share_card_title": "Оплатити може інша людина",
        "share_card_body": "Передайте їй це захищене посилання",
        "share_dialog_title": "Передайте посилання для оплати",
        "share_dialog_body": "Одержувач побачить ці самі товари й суму, зможе ввести дані доставки та оплатити замість вас.",
        "share_dialog_note": "Після оплати посилання повторно використати не можна.",
        "share_cancel": "Скасувати",
        "delivery_step": "Доставка",
        "delivery_title": "Куди відправити?",
        "delivery_lead": "Заповнення займає близько двох хвилин.",
        "delivery_time": "до 2 хв",
        "full_name": "Ім'я та прізвище",
        "full_name_placeholder": "Іван Петренко",
        "phone": "Номер телефону",
        "phone_placeholder": "+380 00 000 00 00",
        "email": "Email для чека",
        "optional": "Необов'язково",
        "email_placeholder": "name@example.com",
        "email_hint": "Email не є обов'язковим. Якщо вкажете його, ми надішлемо чек і підтвердження замовлення. Без розсилок.",
        "city": "Місто Нової пошти",
        "city_placeholder": "Почніть вводити місто",
        "city_hint": "Виберіть підтверджений варіант зі списку Нової пошти.",
        "warehouse": "Відділення або поштомат",
        "warehouse_placeholder": "Номер або адреса пункту",
        "warehouse_hint": "Спочатку виберіть місто, потім пункт отримання.",
        "all_points": "Усі",
        "branches": "Відділення",
        "postomats": "Поштомати",
        "promo": "Промокод",
        "have_promo": "Маєте промокод?",
        "promo_placeholder": "Якщо маєте промокод",
        "promo_hint": "Промокод буде перевірено до створення рахунку.",
        "direct_help": "Щось не так із товаром, розміром, сумою чи доставкою? Напишіть у той самий Direct — ми оновимо пропозицію або сформуємо нове посилання.",
        "pay": "Перейти до оплати",
        "pay_loading": "Перевіряємо дані...",
        "secure_payment": "Дані картки вводяться на захищеній сторінці Monobank",
        "share": "Скопіювати посилання",
        "share_done": "Посилання скопійовано",
        "share_error": "Не вдалося скопіювати",
        "share_hint": "Посилання можна передати іншій людині для оплати.",
        "change_order": "Змінити замовлення в Direct",
        "continue_payment": "Продовжити оплату",
        "order_number": "Номер замовлення",
        "delivery_destination": "Доставка",
        "privacy": "Політика приватності",
        "returns": "Повернення та обмін",
        "delivery": "Доставка",
        "terms": "Умови користування",
        "exit_title": "Залишити оформлення?",
        "exit_body": "Введені дані залишаться на цій сторінці, але час посилання продовжить спливати.",
        "exit_stay": "Залишитися",
        "exit_leave": "Перейти на сайт",
        "support": "Підтримка в Direct",
        "analytics_consent_label": "Налаштування аналітики",
        "analytics_consent_text": "Дозволити анонімну аналітику покупки, щоб ми покращували рекламу й сторінку? Оплата працює незалежно від вибору.",
        "analytics_accept": "Дозволити",
        "analytics_reject": "Лише необхідне",
        "delivery_locked": "Дані доставки зафіксовані",
        "recipient": "Отримувач",
        "contact": "Контакт",
        "receipt": "Чек",
        "state_ready_title": "Все готово до оформлення",
        "state_ready_body": "Перевірте товари та заповніть дані для доставки.",
        "state_locked_title": "Дані прийнято",
        "state_locked_body": "Ми зафіксували отримувача. Зміни можна узгодити в Instagram Direct.",
        "state_pending_title": "Очікуємо оплату",
        "state_pending_body": "Рахунок уже створено. Не створюйте новий платіж, доки ми перевіряємо поточний.",
        "state_paid_title": "Замовлення оплачено",
        "state_paid_body": "Дякуємо. Після формування ТТН ми продублюємо її в Instagram Direct.",
        "state_failed_title": "Оплата не завершилась",
        "state_failed_body": "Платіж не підтверджено. Напишіть нам у Direct, щоб безпечно повторити оплату.",
        "state_expired_title": "Пропозиція завершилась",
        "state_expired_body": "Напишіть нам у Direct, і бот сформує актуальну пропозицію.",
        "state_unavailable_title": "Пропозиція недоступна",
        "state_unavailable_body": "Це посилання відкликано. Актуальну версію можна отримати в Direct.",
        "state_superseded_title": "Є нова версія замовлення",
        "state_superseded_body": "Цю пропозицію замінено. Відкрийте останнє посилання з Instagram Direct.",
        "state_cancelled_title": "Рахунок скасовано",
        "state_cancelled_body": "Оплата за цим рахунком більше не приймається. Запросіть нову пропозицію в Direct.",
        "state_cancellation_ambiguous_title": "Перевіряємо стан рахунку",
        "state_cancellation_ambiguous_body": "Не повторюйте оплату. Ми спочатку звіримо статус із банком.",
        "state_generation_expired_reissuable_title": "Рахунок завершився — можна створити новий",
        "state_generation_expired_reissuable_body": "Пропозиція ще активна. Дані отримувача й доставки залишаються зафіксованими; можна створити один новий 25-хвилинний рахунок.",
        "state_generation_retryable_title": "Рахунок не створено — можна повторити",
        "state_generation_retryable_body": "Дані отримувача й доставки залишаються зафіксованими. Для змін потрібна нова пропозиція в Direct.",
        "reissue_invoice": "Створити новий рахунок",
    },
    "ru": {
        "page_title": "Проверьте заказ",
        "language_label": "Язык",
        "eyebrow": "Заказ готов к оформлению",
        "lead": "Проверьте товар. Доставка и оплата — ниже.",
        "greeting": "Здравствуйте",
        "order_for": "Этот заказ сформирован для вас на сумму",
        "order_label": "Ваш заказ",
        "item_singular": "товар",
        "item_plural": "товаров",
        "proposal_label": "Предложение",
        "expires_label": "Ссылка активна еще",
        "expires_explanation": "Ссылка действует 25 минут с момента создания. Ее можно передать другому человеку для оплаты.",
        "expires_explanation_v2": "Предложение и защищённая ссылка действуют 12 часов. Каждый счёт Monobank действует 25 минут.",
        "payment_method": "Способ оплаты",
        "payment_full": "Полная оплата онлайн",
        "payment_200_cod": "200 грн сейчас, остальное — наложенным платежом",
        "expired_short": "Время истекло",
        "color_label": "Цвет",
        "fit_label": "Фасон",
        "size_label": "Размер",
        "quantity_label": "Количество",
        "catalog_total": "Стоимость товаров",
        "discount": "Согласованная скидка",
        "total": "К оплате",
        "charge_now": "Сумма к оплате сейчас",
        "fixed_price_title": "Товары и цена зафиксированы",
        "fixed_price_title_v2": "Выбор сохранён на 12 часов",
        "fixed_price_hint": "Что это значит?",
        "price_dialog_title": "Цена не изменится",
        "price_dialog_body": "До завершения срока предложение сохраняет выбранные товары, размеры и согласованную сумму.",
        "price_dialog_body_v2": "Ссылка и выбранные параметры сохранены на 12 часов. Перед созданием каждого 25-минутного счёта мы повторно проверяем актуальную цену, скидку и наличие.",
        "price_dialog_close": "Понятно",
        "share_card_title": "Оплатить может другой человек",
        "share_card_body": "Передайте ему эту защищенную ссылку",
        "share_dialog_title": "Передайте ссылку для оплаты",
        "share_dialog_body": "Получатель увидит те же товары и сумму, сможет ввести данные доставки и оплатить вместо вас.",
        "share_dialog_note": "После оплаты ссылку нельзя использовать повторно.",
        "share_cancel": "Отмена",
        "delivery_step": "Доставка",
        "delivery_title": "Куда отправить?",
        "delivery_lead": "Заполнение занимает около двух минут.",
        "delivery_time": "до 2 мин",
        "full_name": "Имя и фамилия",
        "full_name_placeholder": "Иван Петренко",
        "phone": "Номер телефона",
        "phone_placeholder": "+380 00 000 00 00",
        "email": "Email для чека",
        "optional": "Необязательно",
        "email_placeholder": "name@example.com",
        "email_hint": "Email не обязателен. Если укажете его, мы отправим чек и подтверждение заказа. Без рассылок.",
        "city": "Город Новой почты",
        "city_placeholder": "Начните вводить город",
        "city_hint": "Выберите подтвержденный вариант из списка Новой почты.",
        "warehouse": "Отделение или почтомат",
        "warehouse_placeholder": "Номер или адрес пункта",
        "warehouse_hint": "Сначала выберите город, затем пункт получения.",
        "all_points": "Все",
        "branches": "Отделения",
        "postomats": "Почтоматы",
        "promo": "Промокод",
        "have_promo": "Есть промокод?",
        "promo_placeholder": "Если у вас есть промокод",
        "promo_hint": "Промокод будет проверен до создания счета.",
        "direct_help": "Что-то не так с товаром, размером, суммой или доставкой? Напишите в тот же Direct — мы обновим предложение или сформируем новую ссылку.",
        "pay": "Перейти к оплате",
        "pay_loading": "Проверяем данные...",
        "secure_payment": "Данные карты вводятся на защищенной странице Monobank",
        "share": "Скопировать ссылку",
        "share_done": "Ссылка скопирована",
        "share_error": "Не удалось скопировать",
        "share_hint": "Ссылку можно передать другому человеку для оплаты.",
        "change_order": "Изменить заказ в Direct",
        "continue_payment": "Продолжить оплату",
        "order_number": "Номер заказа",
        "delivery_destination": "Доставка",
        "privacy": "Политика конфиденциальности",
        "returns": "Возврат и обмен",
        "delivery": "Доставка",
        "terms": "Условия использования",
        "exit_title": "Оставить оформление?",
        "exit_body": "Введенные данные останутся на этой странице, но время действия ссылки продолжит истекать.",
        "exit_stay": "Остаться",
        "exit_leave": "Перейти на сайт",
        "support": "Поддержка в Direct",
        "analytics_consent_label": "Настройки аналитики",
        "analytics_consent_text": "Разрешить анонимную аналитику покупки, чтобы мы улучшали рекламу и страницу? Оплата работает независимо от выбора.",
        "analytics_accept": "Разрешить",
        "analytics_reject": "Только необходимое",
        "delivery_locked": "Данные доставки зафиксированы",
        "recipient": "Получатель",
        "contact": "Контакт",
        "receipt": "Чек",
        "state_ready_title": "Все готово к оформлению",
        "state_ready_body": "Проверьте товары и заполните данные для доставки.",
        "state_locked_title": "Данные приняты",
        "state_locked_body": "Мы зафиксировали получателя. Изменения можно согласовать в Instagram Direct.",
        "state_pending_title": "Ожидаем оплату",
        "state_pending_body": "Счет уже создан. Не создавайте новый платеж, пока мы проверяем текущий.",
        "state_paid_title": "Заказ оплачен",
        "state_paid_body": "Спасибо. После формирования ТТН мы продублируем ее в Instagram Direct.",
        "state_failed_title": "Оплата не завершилась",
        "state_failed_body": "Платеж не подтвержден. Напишите нам в Direct, чтобы безопасно повторить оплату.",
        "state_expired_title": "Предложение завершилось",
        "state_expired_body": "Напишите нам в Direct, и бот сформирует актуальное предложение.",
        "state_unavailable_title": "Предложение недоступно",
        "state_unavailable_body": "Эта ссылка отозвана. Актуальную версию можно получить в Direct.",
        "state_superseded_title": "Есть новая версия заказа",
        "state_superseded_body": "Это предложение заменено. Откройте последнюю ссылку из Instagram Direct.",
        "state_cancelled_title": "Счет отменен",
        "state_cancelled_body": "Оплата по этому счету больше не принимается. Запросите новое предложение в Direct.",
        "state_cancellation_ambiguous_title": "Проверяем состояние счета",
        "state_cancellation_ambiguous_body": "Не повторяйте оплату. Сначала мы сверим статус с банком.",
        "state_generation_expired_reissuable_title": "Счёт завершился — можно создать новый",
        "state_generation_expired_reissuable_body": "Предложение ещё активно. Данные получателя и доставки остаются зафиксированными; можно создать один новый 25-минутный счёт.",
        "state_generation_retryable_title": "Счёт не создан — можно повторить",
        "state_generation_retryable_body": "Данные получателя и доставки остаются зафиксированными. Для изменений нужна новая ссылка из Direct.",
        "reissue_invoice": "Создать новый счёт",
    },
    "en": {
        "page_title": "Review your order",
        "language_label": "Language",
        "eyebrow": "Your order is ready",
        "lead": "Review the items. Delivery and payment are below.",
        "greeting": "Hello",
        "order_for": "This order was prepared for you with a total of",
        "order_label": "Your order",
        "item_singular": "item",
        "item_plural": "items",
        "proposal_label": "Offer",
        "expires_label": "Link available for",
        "expires_explanation": "This link is valid for 25 minutes from creation. You can forward it to someone else to pay.",
        "expires_explanation_v2": "The offer and secure link are valid for 12 hours. Each Monobank invoice is valid for 25 minutes.",
        "payment_method": "Payment method",
        "payment_full": "Pay in full online",
        "payment_200_cod": "Pay UAH 200 now, balance cash on delivery",
        "expired_short": "Time expired",
        "color_label": "Color",
        "fit_label": "Fit",
        "size_label": "Size",
        "quantity_label": "Quantity",
        "catalog_total": "Items total",
        "discount": "Agreed discount",
        "total": "Total to pay",
        "charge_now": "Amount due now",
        "fixed_price_title": "Items and price are fixed",
        "fixed_price_title_v2": "Your selection is saved for 12 hours",
        "fixed_price_hint": "What does this mean?",
        "price_dialog_title": "The price will not change",
        "price_dialog_body": "Until this offer expires, it keeps the selected items, sizes, and agreed total.",
        "price_dialog_body_v2": "The secure link and selected options are saved for 12 hours. Before each 25-minute invoice is created, current price, discount, and availability are checked again.",
        "price_dialog_close": "Got it",
        "share_card_title": "Someone else can pay",
        "share_card_body": "Send them this protected link",
        "share_dialog_title": "Share the payment link",
        "share_dialog_body": "The recipient will see the same items and total, enter delivery details, and pay for you.",
        "share_dialog_note": "The link cannot be used again after payment.",
        "share_cancel": "Cancel",
        "delivery_step": "Delivery",
        "delivery_title": "Where should we ship?",
        "delivery_lead": "This usually takes less than two minutes.",
        "delivery_time": "under 2 min",
        "full_name": "Full name",
        "full_name_placeholder": "Ivan Petrenko",
        "phone": "Phone number",
        "phone_placeholder": "+380 00 000 00 00",
        "email": "Email for receipt",
        "optional": "Not required",
        "email_placeholder": "name@example.com",
        "email_hint": "Email is optional. If you enter it, we will send the receipt and order confirmation there. No marketing emails.",
        "city": "Nova Poshta city",
        "city_placeholder": "Start typing a city",
        "city_hint": "Choose a verified option from the Nova Poshta list.",
        "warehouse": "Branch or parcel locker",
        "warehouse_placeholder": "Branch number or address",
        "warehouse_hint": "Choose the city first, then your pickup point.",
        "all_points": "All",
        "branches": "Branches",
        "postomats": "Lockers",
        "promo": "Promo code",
        "have_promo": "Have a promo code?",
        "promo_placeholder": "If you have a promo code",
        "promo_hint": "The promo code will be verified before an invoice is created.",
        "direct_help": "Something wrong with an item, size, amount, or delivery? Message the same Direct chat and we will update the offer or create a new link.",
        "pay": "Continue to payment",
        "pay_loading": "Checking details...",
        "secure_payment": "Card details are entered on Monobank's secure page",
        "share": "Copy link",
        "share_done": "Link copied",
        "share_error": "Could not copy link",
        "share_hint": "You can forward this link to someone else who will pay.",
        "change_order": "Change order in Direct",
        "continue_payment": "Continue payment",
        "order_number": "Order number",
        "delivery_destination": "Delivery",
        "privacy": "Privacy policy",
        "returns": "Returns and exchanges",
        "delivery": "Delivery",
        "terms": "Terms of service",
        "exit_title": "Leave checkout?",
        "exit_body": "Your entered details will stay on this page, but the link will keep expiring.",
        "exit_stay": "Stay",
        "exit_leave": "Go to site",
        "support": "Support in Direct",
        "analytics_consent_label": "Analytics preferences",
        "analytics_consent_text": "Allow anonymous purchase analytics so we can improve ads and this page? Payment works either way.",
        "analytics_accept": "Allow",
        "analytics_reject": "Necessary only",
        "delivery_locked": "Delivery details are locked",
        "recipient": "Recipient",
        "contact": "Contact",
        "receipt": "Receipt",
        "state_ready_title": "Ready to check out",
        "state_ready_body": "Review the items and add your delivery details.",
        "state_locked_title": "Details received",
        "state_locked_body": "The recipient is locked. Ask for changes in Instagram Direct.",
        "state_pending_title": "Waiting for payment",
        "state_pending_body": "An invoice already exists. Do not start another payment while we verify it.",
        "state_paid_title": "Order paid",
        "state_paid_body": "Thank you. We will send the tracking number in Instagram Direct.",
        "state_failed_title": "Payment was not completed",
        "state_failed_body": "The payment is not confirmed. Contact us in Direct before trying again.",
        "state_expired_title": "This offer has expired",
        "state_expired_body": "Message us in Direct and the bot will create an up-to-date offer.",
        "state_unavailable_title": "Offer unavailable",
        "state_unavailable_body": "This link was revoked. Request the current version in Direct.",
        "state_superseded_title": "A newer order version exists",
        "state_superseded_body": "This offer was replaced. Open the latest link from Instagram Direct.",
        "state_cancelled_title": "Invoice cancelled",
        "state_cancelled_body": "This invoice can no longer be paid. Request a new offer in Direct.",
        "state_cancellation_ambiguous_title": "Checking invoice status",
        "state_cancellation_ambiguous_body": "Do not pay again. We will first verify the status with the bank.",
        "state_generation_expired_reissuable_title": "The invoice expired — you can create a new one",
        "state_generation_expired_reissuable_body": "The offer is still active. Recipient and delivery details remain locked; you can create one new 25-minute invoice.",
        "state_generation_retryable_title": "The invoice was not created — retry safely",
        "state_generation_retryable_body": "Recipient and delivery details remain locked. Request a new offer in Direct to change them.",
        "reissue_invoice": "Create a new invoice",
    },
}

CHECKOUT_ERROR_COPY = {
    "uk": {
        "expired": "Термін дії пропозиції завершився.",
        "unavailable": "Цю пропозицію більше не можна оплатити.",
        "in_progress": "Платіж уже створюється. Зачекайте кілька секунд.",
        "provider_ambiguous": "Банк ще перевіряє платіж. Не повторюйте оплату — ми звіримо статус і повідомимо вас у Direct.",
        "full_name": "Вкажіть ім'я та прізвище.",
        "phone": "Вкажіть коректний український номер телефону.",
        "email": "Перевірте email для чека.",
        "city": "Оберіть місто зі списку Нової пошти.",
        "np_office": "Оберіть відділення або поштомат зі списку Нової пошти.",
        "promo_unavailable": "Промокод для цієї пропозиції недоступний.",
        "promo_invalid": "Промокод недійсний або вже використаний.",
        "promo_requires_account": "Цей промокод доступний лише в особистому кабінеті.",
        "provider_error": "Не вдалося створити платіж. Спробуйте ще раз.",
        "catalog_changed": "Товар або його умови змінилися. Попросіть бота оновити пропозицію.",
        "invalid_amount": "Сума замовлення має бути більшою за нуль.",
        "item_unavailable": "Один із товарів більше недоступний.",
        "empty_items": "У пропозиції немає товарів.",
    },
    "ru": {
        "expired": "Срок действия предложения истек.",
        "unavailable": "Это предложение больше нельзя оплатить.",
        "in_progress": "Платеж уже создается. Подождите несколько секунд.",
        "provider_ambiguous": "Банк еще проверяет платеж. Не повторяйте оплату — мы сверим статус и сообщим вам в Direct.",
        "full_name": "Укажите имя и фамилию.",
        "phone": "Укажите корректный украинский номер телефона.",
        "email": "Проверьте email для чека.",
        "city": "Выберите город из списка Новой почты.",
        "np_office": "Выберите отделение или почтомат из списка Новой почты.",
        "promo_unavailable": "Промокод для этого предложения недоступен.",
        "promo_invalid": "Промокод недействителен или уже использован.",
        "promo_requires_account": "Этот промокод доступен только в личном кабинете.",
        "provider_error": "Не удалось создать платеж. Попробуйте еще раз.",
        "catalog_changed": "Товар или его условия изменились. Попросите бота обновить предложение.",
        "invalid_amount": "Сумма заказа должна быть больше нуля.",
        "item_unavailable": "Один из товаров больше недоступен.",
        "empty_items": "В предложении нет товаров.",
    },
    "en": {
        "expired": "This offer has expired.",
        "unavailable": "This offer can no longer be paid.",
        "in_progress": "A payment is already being created. Please wait a few seconds.",
        "provider_ambiguous": "The bank is still checking this payment. Do not pay again; we will verify it and message you in Direct.",
        "full_name": "Enter your first and last name.",
        "phone": "Enter a valid Ukrainian phone number.",
        "email": "Check the receipt email.",
        "city": "Choose a city from the Nova Poshta list.",
        "np_office": "Choose a branch or locker from the Nova Poshta list.",
        "promo_unavailable": "A promo code is not available for this offer.",
        "promo_invalid": "The promo code is invalid or already used.",
        "promo_requires_account": "This promo code is available only in an account.",
        "provider_error": "We could not create the payment. Please try again.",
        "catalog_changed": "An item or its terms changed. Ask the bot for an updated offer.",
        "invalid_amount": "The order total must be greater than zero.",
        "item_unavailable": "One of the items is no longer available.",
        "empty_items": "This offer has no items.",
    },
}


def _private_headers(response):
    if response.get("Content-Type", "").startswith("text/html"):
        response["Content-Type"] = "text/html; charset=utf-8"
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    response["Pragma"] = "no-cache"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _rate_limited(request, action, *, identity="", limit=30, window=60):
    """Bound bearer/payment probes without persisting the bearer or PII."""
    from twocomms.middleware import _client_rate_limit_ip

    remote = str(_client_rate_limit_ip(request) or "unknown")[:64]
    digest = IgCheckoutAccessToken.digest(f"{remote}:{identity}")
    key = f"ig-checkout-rate:{action}:{digest}"
    try:
        if cache.add(key, 1, timeout=window):
            return False
        count = cache.incr(key)
        return count > limit
    except Exception:
        # Cache outages must not make an otherwise valid payment inaccessible.
        return False


def _grant_session_key(proposal):
    return f"{GRANT_SESSION_PREFIX}{proposal.public_id}"


def _save_grant(request, proposal, token):
    expires_at = min(proposal.expires_at, token.expires_at)
    payload = {
        "proposal_id": str(proposal.public_id),
        "token_id": token.pk,
        "revision": proposal.revision,
        "grant_id": secrets.token_urlsafe(18),
        "expires_at": int(expires_at.timestamp()),
    }
    request.session[_grant_session_key(proposal)] = signing.dumps(
        payload,
        salt=GRANT_SALT,
        compress=True,
    )
    request.session.modified = True
    return payload


def _load_grant(request, proposal):
    value = request.session.get(_grant_session_key(proposal))
    if not value:
        raise Http404("proposal grant not found")
    try:
        payload = signing.loads(value, salt=GRANT_SALT, max_age=GRANT_MAX_AGE)
    except signing.BadSignature:
        raise Http404("proposal grant invalid")
    if payload.get("proposal_id") != str(proposal.public_id):
        raise Http404("proposal grant invalid")
    if int(payload.get("expires_at") or 0) <= int(timezone.now().timestamp()):
        raise Http404("proposal grant expired")
    token = IgCheckoutAccessToken.objects.filter(
        pk=payload.get("token_id"),
        proposal=proposal,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if token is None:
        raise Http404("proposal token unavailable")
    if int(payload.get("revision") or 0) != proposal.revision:
        if proposal.payment_attempt_id:
            raise Http404("proposal revision changed after payment started")
        payload["revision"] = proposal.revision
        request.session[_grant_session_key(proposal)] = signing.dumps(
            payload,
            salt=GRANT_SALT,
            compress=True,
        )
        request.session.modified = True
    return payload, token


def _locale(value):
    code = str(value or "uk").lower().split("-", 1)[0].split("_", 1)[0]
    return code if code in CHECKOUT_COPY else "uk"


def _checkout_language(request, proposal):
    """Use an explicit checkout switch without changing proposal ownership."""
    requested = str(request.GET.get("lang") or "").lower().split("-", 1)[0].split("_", 1)[0]
    if requested in CHECKOUT_COPY:
        return requested
    return _locale(proposal.locale)


def _localized_proposal_url(request, proposal, language):
    base = request.build_absolute_uri(
        reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id})
    )
    return f"{base}?{urlencode({'lang': language})}"


def _localized_error(language, code, fallback):
    if code == "brigade_full_payment_required":
        from storefront.services.brigade_presentation import brigade_text
        return brigade_text("full_payment", language)
    return CHECKOUT_ERROR_COPY.get(language, CHECKOUT_ERROR_COPY["uk"]).get(code, fallback)


def _money(value):
    return f"{value:.2f}"


def _mask_phone(value):
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return f"+380 •• ••• •• {digits[-4:]}" if digits else "—"


def _mask_email(value):
    local, separator, domain = str(value or "").partition("@")
    if not separator or not domain:
        return "—"
    visible = local[:1] if local else ""
    return f"{visible}•••@{domain}"


def _mask_name(value):
    parts = [part for part in str(value or "").split() if part]
    return " ".join(f"{part[0]}." for part in parts[:3]) or "—"


def _customer_name(value):
    parts = str(value or "").strip().split(maxsplit=1)
    return parts[0][:50] if parts else ""


def _analytics_event_id(event_name, proposal, grant_id=""):
    """Return a stable, opaque event id without exposing proposal/session data."""
    secret = str(getattr(settings, "SECRET_KEY", ""))
    message = f"ig-checkout:v1:{event_name}:{proposal.pk}:{proposal.revision}:{grant_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:40]


_GENERATION_UNSET = object()


def _checkout_generation(proposal):
    if not proposal.assisted_checkout_v2:
        return None
    if proposal.current_invoice_generation_id:
        return proposal.current_invoice_generation
    return (
        proposal.invoice_generations.select_related("payment_attempt")
        .order_by("-generation", "-pk")
        .first()
    )


def _checkout_state(proposal, generation=_GENERATION_UNSET):
    if proposal.status == proposal.Status.PAID:
        return "paid"
    if proposal.status == proposal.Status.REVOKED:
        return "unavailable"
    if proposal.status == proposal.Status.SUPERSEDED:
        return "superseded"
    if proposal.status == proposal.Status.MANAGER_REVIEW:
        return "cancellation_ambiguous"
    if proposal.status == proposal.Status.EXPIRED or proposal.is_expired:
        return "expired"
    if generation is _GENERATION_UNSET:
        generation = _checkout_generation(proposal)
    attempt = (
        generation.payment_attempt
        if generation is not None and generation.payment_attempt_id
        else proposal.payment_attempt
    )
    if generation is not None:
        from management.services.ig_checkout_generation import (
            _retry_source_is_safe,
        )

        retry_source_safe = _retry_source_is_safe(generation, attempt)
        if generation.state == generation.State.PAID_WINNER:
            return "paid"
        if generation.state in {
            generation.State.PLANNED,
            generation.State.PROVIDER_INFLIGHT,
            generation.State.PROVIDER_AMBIGUOUS,
            generation.State.RESOURCE_REVIEW,
            generation.State.AMBIGUITY_REVIEW,
            generation.State.LATE_PROVIDER_REVIEW,
        }:
            return "cancellation_ambiguous"
        if (
            generation.state == generation.State.INVOICE_CREATED
            and not retry_source_safe
        ):
            return "cancellation_ambiguous"
        if (
            generation.state == generation.State.EXPIRED
            and not retry_source_safe
        ):
            return "cancellation_ambiguous"
        if (
            proposal.expires_at > timezone.now()
            and (
                generation.state == generation.State.EXPIRED
                or (
                    generation.expires_at <= timezone.now()
                    and generation.state == generation.State.INVOICE_CREATED
                    and retry_source_safe
                )
            )
        ):
            return "generation_expired_reissuable"
        if generation.state in {
            generation.State.FAILED,
            generation.State.CANCELLED,
        }:
            if not retry_source_safe:
                return "cancellation_ambiguous"
            return (
                "generation_retryable"
                if proposal.expires_at > timezone.now()
                else "expired"
            )
        if generation.state == generation.State.EXPIRED:
            return "expired"
        if generation.state == generation.State.LATE_PAID_REVIEW:
            return "cancellation_ambiguous"
    if attempt is not None and (attempt.event_state or {}).get("invoice_creation_ambiguous"):
        return "cancellation_ambiguous"
    if proposal.status == proposal.Status.CANCELLED:
        return (
            "cancelled"
            if proposal.has_provider_confirmed_cancellation()
            else "cancellation_ambiguous"
        )
    if attempt is not None and attempt.status == attempt.Status.FAILED:
        return "failed"
    if attempt is not None and attempt.status == attempt.Status.EXPIRED:
        return "expired"
    if proposal.status == proposal.Status.INVOICE_CREATED:
        return "pending"
    if proposal.status == proposal.Status.DETAILS_LOCKED:
        return "locked"
    return "ready"


def _item_context(item, frozen=None):
    option_values = item.option_values or {}
    option_labels = item.option_labels or {}
    return {
        "title": item.product_title,
        "image_url": item.image_url,
        "color_code": item.color_code,
        "color_label": item.color_label,
        "fit_label": item.fit_label,
        "option_values": option_values,
        "option_labels": option_labels,
        "option_facts": [
            {
                "code": str(code),
                "value": str(value),
                "label": str(option_labels.get(code) or value),
            }
            for code, value in option_values.items()
            if code != "fit"
        ],
        "size": item.size,
        "quantity": item.quantity,
        "unit_price": _money(item.quoted_unit_price),
        "line_total": _money(item.quoted_line_total),
        "price_parts": (frozen or {}).get("price_parts") or [],
        "bundle_pricing": (frozen or {}).get("bundle_pricing") or {},
    }


def _proposal_context(proposal, *, request, grant_id="", form_error="", form_error_field="", form_values=None):
    from management.models import IgCheckoutRevision
    revision = IgCheckoutRevision.objects.filter(proposal=proposal, revision=proposal.revision).order_by('-id').first()
    frozen_items = (revision.snapshot or {}).get('items') or [] if revision else []
    language = _checkout_language(request, proposal)
    copy = dict(CHECKOUT_COPY[language])
    approved_policy = approved_policy_copy(language)
    if proposal.assisted_checkout_v2:
        copy["fixed_price_title"] = copy["fixed_price_title_v2"]
        copy["price_dialog_body"] = copy["price_dialog_body_v2"]
    generation = _checkout_generation(proposal)
    state = _checkout_state(proposal, generation)
    attempt = (
        generation.payment_attempt
        if generation is not None and generation.payment_attempt_id
        else proposal.payment_attempt
    )
    from management.services.ig_checkout_payment import _proposal_brigade_policy
    brigade_full_only, _promo_eligible = _proposal_brigade_policy(proposal)
    brigade_partial_blocked = bool(
        brigade_full_only
        and state != "paid"
        and (
            proposal.pay_type != proposal.PayType.ONLINE_FULL
            or (attempt is not None and attempt.pay_type != attempt.PayType.ONLINE_FULL)
        )
    )
    locked_retry_states = {
        "generation_expired_reissuable",
        "generation_retryable",
    }
    delivery_locked = bool(proposal.details_locked_at) or state in {
        "locked",
        "pending",
        "paid",
        *locked_retry_states,
    }
    payable = state == "ready" and not delivery_locked and not brigade_partial_blocked
    reissue_allowed = bool(
        state in locked_retry_states
        and generation is not None
        and attempt is not None
        and not brigade_partial_blocked
    )
    share_allowed = state in {
        "ready", "locked", "pending", *locked_retry_states,
    } and not brigade_partial_blocked
    payment_url = ""
    if (
        attempt is not None
        and state in {"locked", "pending"}
        and attempt.invoice_url
        and not (attempt.event_state or {}).get("invoice_creation_ambiguous")
        and not brigade_partial_blocked
    ):
        payment_url = attempt.invoice_url
    masked_delivery = None
    if delivery_locked and attempt is not None:
        masked_delivery = {
            "name": _mask_name(attempt.full_name),
            "phone": _mask_phone(attempt.phone),
            "email": _mask_email(attempt.email),
        }
    paid_summary = None
    purchase_event_id = ""
    if (
        state == "paid"
        and attempt is not None
        and attempt.order_id
        and request.session.get("ig_checkout_paid_attempt_id") == attempt.pk
    ):
        order = attempt.order
        paid_summary = {
            "order_number": order.order_number,
            "recipient": order.full_name,
            "phone": order.phone,
            "email": order.email or "—",
            "destination": " · ".join(
                value for value in (order.city, order.np_office) if value
            ),
        }
        purchase_event_id = order.get_purchase_event_id()
    language_options = [
        {
            **item,
            "is_current": item["code"] == language,
            "url": _localized_proposal_url(request, proposal, item["code"]),
        }
        for item in CHECKOUT_LANGUAGES
    ]
    form_values = form_values or {}
    selected_payment_choice = str(
        form_values.get("payment_choice") or "online_full"
    )
    if brigade_full_only:
        selected_payment_choice = "online_full"
    payment_options = [
        {
            "value": "online_full",
            "label": copy["payment_full"],
            "selected": selected_payment_choice != "prepay_200_cod",
            "amount": _money(proposal.quoted_total),
        }
    ]
    if (
        proposal.assisted_checkout_v2
        and proposal.payment_policy
        == proposal.PaymentPolicy.FULL_OR_200_COD
        and not proposal.custom_print_full_only
        and not brigade_full_only
    ):
        payment_options.append({
            "value": "prepay_200_cod",
            "label": copy["payment_200_cod"],
            "selected": selected_payment_choice == "prepay_200_cod",
            "amount": "200.00",
        })
    state_body = copy[f"state_{state}_body"]
    if brigade_partial_blocked:
        from storefront.services.brigade_presentation import brigade_text
        state_body = brigade_text("full_payment", language)
    return {
        "copy": copy,
        "approved_policy": approved_policy["text"],
        # Kept for server-side review/parity checks; never rendered to buyers.
        "approved_policy_manifest": approved_policy["metadata"],
        "html_lang": language,
        "language_options": language_options,
        "checkout_state": state,
        "state_title": copy[f"state_{state}_title"],
        "state_body": state_body,
        "brigade_full_payment_only": brigade_full_only,
        "customer_name": (
            ""
            if state in locked_retry_states
            else _customer_name(proposal.client.display_name)
        ),
        "proposal": {
            "public_id": str(proposal.public_id),
            "reference": str(proposal.public_id).split("-", 1)[0].upper(),
            "revision": proposal.revision,
            "currency": proposal.currency,
            "catalog_total": _money(proposal.catalog_total),
            "discount": _money(proposal.negotiated_discount),
            "total": _money(proposal.quoted_total),
            "charge_now": _money(proposal.requested_payment_amount),
            "is_prepayment": proposal.pay_type == IgCheckoutProposal.PayType.PREPAYMENT,
            "is_v2": proposal.assisted_checkout_v2,
            "has_discount": proposal.negotiated_discount > 0,
            "allow_promo": proposal.allow_promo,
            "expires_at": proposal.expires_at,
            "expires_at_iso": proposal.expires_at.isoformat(),
            "created_at_iso": proposal.created_at.isoformat(),
        },
        "items": [_item_context(item, frozen_items[index] if index < len(frozen_items) else {})
                  for index, item in enumerate(proposal.items.order_by('position', 'id'))],
        "expires_explanation": (
            copy["expires_explanation_v2"]
            if proposal.assisted_checkout_v2
            else copy["expires_explanation"]
        ),
        "payment_options": payment_options,
        "generation_expires_at_iso": (
            generation.expires_at.isoformat() if generation is not None else ""
        ),
        "delivery_locked": delivery_locked,
        "masked_delivery": masked_delivery,
        "paid_summary": paid_summary,
        "payment_url": payment_url,
        "payable": payable,
        "reissue_allowed": reissue_allowed,
        "reissue_generation": (
            generation.generation if reissue_allowed else ""
        ),
        "share_allowed": share_allowed,
        "share_url": reverse(
            "ig_checkout_share_token",
            kwargs={"proposal_id": proposal.public_id},
        ),
        "proposal_url": _localized_proposal_url(request, proposal, language),
        "status_url": reverse(
            "ig_checkout_status",
            kwargs={"proposal_id": proposal.public_id},
        ),
        "np_city_search_url": reverse("cart_np_city_search"),
        "np_warehouse_search_url": reverse("cart_np_warehouse_search"),
        "direct_url": INSTAGRAM_DIRECT_URL,
        "returns_url": reverse("returns"),
        "delivery_url": reverse("delivery"),
        "privacy_url": reverse("privacy_policy"),
        "terms_url": reverse("terms_of_service"),
        "form_error": form_error,
        "form_error_field": form_error_field,
        "form_values": form_values,
        "analytics": {
            "view_content_event_id": _analytics_event_id("ViewContent", proposal, grant_id)
            if state in {"ready", "locked", "pending", "paid"}
            else "",
            "initiate_checkout_event_id": _analytics_event_id("InitiateCheckout", proposal, grant_id),
            "value": _money(proposal.quoted_total),
            "charge_value": _money(proposal.requested_payment_amount),
            "currency": proposal.currency,
            "purchase_event_id": purchase_event_id,
        },
    }


@require_GET
@never_cache
def ig_checkout_token_entry(request, token):
    """Consume a bearer URL once, then redirect before page assets load."""
    digest = IgCheckoutAccessToken.digest(token)
    if _rate_limited(request, "token", identity="entry", limit=30):
        return _private_headers(HttpResponse("Спробуйте пізніше.", status=429))
    now = timezone.now()
    with __import__("django.db", fromlist=["transaction"]).transaction.atomic():
        access_token = (
            IgCheckoutAccessToken.objects.select_for_update()
            .select_related("proposal")
            .filter(token_digest=digest, revoked_at__isnull=True)
            .first()
        )
        if access_token is None:
            return _private_headers(HttpResponse("Посилання недійсне.", status=410))
        if not constant_time_compare(access_token.token_digest, digest):
            return _private_headers(HttpResponse("Посилання недійсне.", status=410))
        proposal = access_token.proposal
        if access_token.expires_at <= now or proposal.expires_at <= now:
            return _private_headers(HttpResponse("Термін посилання завершився.", status=410))
        _save_grant(request, proposal, access_token)
        access_token.use_count += 1
        access_token.last_used_at = now
        access_token.save(update_fields=["use_count", "last_used_at"])
        if proposal.status == IgCheckoutProposal.Status.READY:
            proposal.status = IgCheckoutProposal.Status.VIEWED
            proposal.viewed_at = proposal.viewed_at or now
            proposal.save(update_fields=["status", "viewed_at", "updated_at"])
            from management.models import IgClient, IgFunnelStepEvent
            from management.services.ig_funnel_analytics import (
                record_episode_step_event_in_transaction,
            )

            record_episode_step_event_in_transaction(
                proposal.commercial_episode,
                event_type=IgFunnelStepEvent.Type.PAYLINK_VIEWED,
                event_key=f"ig-paylink-viewed:{proposal.pk}",
                occurred_at=proposal.viewed_at,
                stage=IgClient.Stage.CHECKOUT,
                actor="customer",
                evidence={
                    "proposal_id": proposal.pk,
                    "proposal_public_id": str(proposal.public_id),
                    "access_token_id": access_token.pk,
                },
            )
    response = redirect("ig_checkout_proposal", proposal_id=proposal.public_id)
    return _private_headers(response)


@require_http_methods(["GET", "POST"])
@never_cache
def ig_checkout_proposal(request, proposal_id):
    proposal = get_object_or_404(
        IgCheckoutProposal.objects.select_related(
            "client",
            "payment_attempt",
            "payment_attempt__order",
            "provider_cancellation_event",
            "deal",
            "current_invoice_generation",
            "current_invoice_generation__payment_attempt",
        ).prefetch_related("items"),
        public_id=proposal_id,
    )
    grant, _token = _load_grant(request, proposal)
    if request.method == "POST":
        if _rate_limited(request, "submit", identity=str(proposal.public_id), limit=12, window=300):
            return _private_headers(HttpResponse("Спробуйте оформити платіж трохи пізніше.", status=429))
        from management.services.ig_checkout_payment import CheckoutPaymentError

        if proposal.assisted_checkout_v2:
            from management.services.ig_checkout_generation import (
                create_or_reuse_generation_invoice as create_invoice,
            )
        else:
            from management.services.ig_checkout_payment import (
                create_or_reuse_invoice as create_invoice,
            )

        try:
            _attempt, invoice_url, _reused = create_invoice(
                proposal,
                request=request,
                payload=request.POST,
                grant_id=grant.get("grant_id", ""),
            )
        except CheckoutPaymentError as exc:
            proposal.refresh_from_db()
            language = _checkout_language(request, proposal)
            if "application/json" in request.headers.get("Accept", ""):
                return _private_headers(JsonResponse({
                    "error": exc.code,
                    "message": _localized_error(language, exc.code, exc.message),
                    "field": exc.field,
                }, status=409 if exc.code == "in_progress" else 400))
            context = _proposal_context(
                proposal,
                request=request,
                grant_id=grant.get("grant_id", ""),
                form_error=_localized_error(language, exc.code, exc.message),
                form_error_field=exc.field,
                form_values=request.POST,
            )
            status = 409 if exc.code == "in_progress" else 400
            return _private_headers(render(request, "pages/ig_checkout.html", context, status=status))
        if "application/json" in request.headers.get("Accept", ""):
            return _private_headers(JsonResponse({
                "invoice_url": invoice_url,
                "reused": bool(_reused),
                "add_payment_event_id": _attempt.add_payment_event_id,
                "initiate_event_id": _analytics_event_id(
                    "InitiateCheckout", proposal, grant.get("grant_id", "")
                ),
                "value": str(_attempt.payment_amount),
                "currency": proposal.currency,
            }))
        return _private_headers(redirect(invoice_url))
    context = _proposal_context(proposal, request=request, grant_id=grant.get("grant_id", ""))
    return _private_headers(render(request, "pages/ig_checkout.html", context))


@require_GET
@never_cache
def ig_checkout_status(request, proposal_id):
    """Expose only state/revision for truthful pending-payment polling."""
    proposal = get_object_or_404(
        IgCheckoutProposal.objects.select_related("current_invoice_generation"),
        public_id=proposal_id,
    )
    _load_grant(request, proposal)
    generation = _checkout_generation(proposal)
    ui_state = _checkout_state(proposal, generation)
    public_state = (
        "verified" if ui_state == "paid" else
        "reissue" if ui_state in {
            "generation_expired_reissuable", "generation_retryable",
        } else
        "expired" if ui_state == "expired" else
        "cancellation_ambiguous" if ui_state == "cancellation_ambiguous" else
        "failed" if ui_state in {"failed", "unavailable", "cancelled", "superseded"} else
        "pending"
    )
    response = JsonResponse({
        "state": public_state,
        "ui_state": ui_state,
        "revision": proposal.revision,
        "generation": (
            generation.generation
            if proposal.assisted_checkout_v2 and generation is not None
            else None
        ),
        "expires_at": proposal.expires_at.isoformat(),
        "redirect": reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id})
        if public_state == "verified" else "",
    })
    return _private_headers(response)


@require_POST
@never_cache
def ig_checkout_share_token(request, proposal_id):
    proposal = get_object_or_404(IgCheckoutProposal, public_id=proposal_id)
    _grant, _token = _load_grant(request, proposal)
    if _rate_limited(request, "share", identity=str(proposal.public_id), limit=6, window=300):
        return _private_headers(JsonResponse({"error": "rate_limited"}, status=429))
    now = timezone.now()
    if proposal.expires_at <= now or proposal.status not in {
        IgCheckoutProposal.Status.READY,
        IgCheckoutProposal.Status.VIEWED,
        IgCheckoutProposal.Status.DETAILS_LOCKED,
        IgCheckoutProposal.Status.INVOICE_CREATED,
    }:
        return _private_headers(JsonResponse({"error": "unavailable"}, status=410))
    raw_token, share_token = IgCheckoutAccessToken.issue(
        proposal=proposal,
        kind=IgCheckoutAccessToken.Kind.SHARE,
        expires_at=proposal.expires_at,
    )
    url = request.build_absolute_uri(
        reverse("ig_checkout_token_entry", kwargs={"token": raw_token})
    )
    response = JsonResponse({"url": url, "expires_at": share_token.expires_at.isoformat()})
    return _private_headers(response)
