#!/usr/bin/env python3
"""Phase 17b — fill RU/EN translations for chrome strings in locale/.

Usage:  python scripts/fill_translations.py
Idempotent: if a msgstr is already non-empty for a known msgid, it is left as-is.

Adds translations for header/footer/common UI labels we marked with
``{% trans %}`` in Phase 17b. Page-level templates will be added in
subsequent phases (17d).
"""
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = ROOT / "locale"

# msgid -> {ru: ..., en: ...}
TRANSLATIONS: dict[str, dict[str, str]] = {
    # ===== Footer / language switcher =====
    "Мова сайту":             {"ru": "Язык сайта",         "en": "Site language"},
    "Мова":                   {"ru": "Язык",               "en": "Language"},
    "Соцмережі TwoComms":     {"ru": "Соцсети TwoComms",   "en": "TwoComms socials"},
    "Контакти TwoComms":      {"ru": "Контакты TwoComms",  "en": "TwoComms contacts"},
    # ===== Footer columns =====
    "Покупка":                {"ru": "Покупка",            "en": "Shopping"},
    "Підтримка":              {"ru": "Поддержка",          "en": "Support"},
    "Бренд":                  {"ru": "Бренд",              "en": "Brand"},
    "Швидкий доступ":         {"ru": "Быстрый доступ",     "en": "Quick links"},
    "Каталог":                {"ru": "Каталог",            "en": "Catalog"},
    "Доставка і оплата":      {"ru": "Доставка и оплата",  "en": "Delivery & payment"},
    "Кастомний принт":        {"ru": "Кастомный принт",    "en": "Custom print"},
    "Співпраця":              {"ru": "Сотрудничество",     "en": "Cooperation"},
    "Допомога":               {"ru": "Помощь",             "en": "Help"},
    "FAQ":                    {"ru": "FAQ",                "en": "FAQ"},
    "Повернення та обмін":    {"ru": "Возврат и обмен",    "en": "Returns & exchanges"},
    "Догляд за одягом":       {"ru": "Уход за одеждой",    "en": "Garment care"},
    "Про бренд":              {"ru": "О бренде",           "en": "About the brand"},
    "Новини":                 {"ru": "Новости",            "en": "News"},
    "Контакти":               {"ru": "Контакты",           "en": "Contacts"},
    "Карта сайту":            {"ru": "Карта сайта",        "en": "Site map"},
    "Розмірна сітка":         {"ru": "Размерная сетка",    "en": "Size guide"},
    "Відстеження замовлення": {"ru": "Отслеживание заказа", "en": "Order tracking"},
    "Політика конфіденційності": {"ru": "Политика конфиденциальности", "en": "Privacy policy"},
    "Умови використання":     {"ru": "Условия использования", "en": "Terms of use"},

    # ===== Header / navigation =====
    "TwoComms логотип — стріт & мілітарі одяг": {
        "ru": "TwoComms логотип — стрит & милитари одежда",
        "en": "TwoComms logo — street & military apparel",
    },
    "Перемикач навігації":    {"ru": "Переключатель навигации", "en": "Navigation toggler"},
    "Пошук…":                 {"ru": "Поиск…",             "en": "Search…"},
    "Пошук":                  {"ru": "Поиск",              "en": "Search"},
    "Створити свій принт":    {"ru": "Создать свой принт", "en": "Create your print"},

    # ===== Header — favorites/cart/user =====
    "Перейти до обраних товарів": {"ru": "Перейти к избранным товарам", "en": "Go to favorites"},
    "Відкрити міні-корзину":  {"ru": "Открыть мини-корзину", "en": "Open mini cart"},
    "Кошик":                  {"ru": "Корзина",            "en": "Cart"},
    "Закрити":                {"ru": "Закрыть",            "en": "Close"},
    "Завантаження…":          {"ru": "Загрузка…",          "en": "Loading…"},
    "Умови доставки і оплати": {"ru": "Условия доставки и оплаты", "en": "Delivery & payment terms"},
    "Залишайте заявку на сайті або оплачуйте миттєво через mono checkout": {
        "ru": "Оставляйте заявку на сайте или оплачивайте моментально через mono checkout",
        "en": "Submit an order on the site or pay instantly via mono checkout",
    },
    "Відкрити панель користувача": {"ru": "Открыть панель пользователя", "en": "Open user panel"},
    "Аватар користувача — TwoComms": {"ru": "Аватар пользователя — TwoComms", "en": "User avatar — TwoComms"},
    "Телефон не вказано":     {"ru": "Телефон не указан",  "en": "Phone not specified"},
    "Ваші бали":              {"ru": "Ваши баллы",         "en": "Your points"},
    "Знижки • Промокоди • Підтримка ЗСУ": {
        "ru": "Скидки • Промокоды • Поддержка ВСУ",
        "en": "Discounts • Promo codes • UA Army support",
    },
    "Налаштування профілю":   {"ru": "Настройки профиля",  "en": "Profile settings"},
    "Мої замовлення":         {"ru": "Мои заказы",         "en": "My orders"},
    "Мої відгуки":            {"ru": "Мои отзывы",         "en": "My reviews"},
    "Обрані товари":          {"ru": "Избранные товары",   "en": "Favorite items"},
    "Кількість товарів у обраних": {"ru": "Количество товаров в избранном", "en": "Number of favorites"},
    "Мої промокоди":          {"ru": "Мои промокоды",      "en": "My promo codes"},
    "Панель адміністратора":  {"ru": "Панель администратора", "en": "Admin panel"},
    "Вийти":                  {"ru": "Выйти",              "en": "Sign out"},
    "Вхід в акаунт":          {"ru": "Вход в аккаунт",     "en": "Sign in"},
    "Увійти":                 {"ru": "Войти",              "en": "Sign in"},
    "Зареєструватись":        {"ru": "Зарегистрироваться", "en": "Sign up"},
    "Вхід через Google — TwoComms": {"ru": "Вход через Google — TwoComms", "en": "Sign in with Google — TwoComms"},
    "Увійти через Google":    {"ru": "Войти через Google", "en": "Sign in with Google"},
    "Авторизуйтесь, щоб не вводити дані кожного разу та накопичувати бали": {
        "ru": "Авторизуйтесь, чтобы не вводить данные каждый раз и копить баллы",
        "en": "Sign in to skip re-entering details and earn points",
    },
    "Головна":                {"ru": "Главная",            "en": "Home"},
    "Обране":                 {"ru": "Избранное",          "en": "Favorites"},
    "Профіль":                {"ru": "Профиль",            "en": "Profile"},

    # ===== Product card =====
    "Купити":                 {"ru": "Купить",             "en": "Buy"},
    "Додати до обраних":      {"ru": "Добавить в избранное", "en": "Add to favorites"},
    "Дізнатись про бали":     {"ru": "Узнать о баллах",    "en": "About points"},
    "Доступні кольори":       {"ru": "Доступные цвета",    "en": "Available colors"},
    "Кольори товару":         {"ru": "Цвета товара",       "en": "Product colors"},
    "Колір":                  {"ru": "Цвет",               "en": "Color"},
    "плюс":                   {"ru": "плюс",               "en": "plus"},
    "грн":                    {"ru": "грн",                "en": "UAH"},
    "0 балів":                {"ru": "0 баллов",           "en": "0 points"},
    # blocktrans plural strings — Django stores them with msgid/msgid_plural
    "Переглянути %(title)s":  {"ru": "Просмотреть %(title)s", "en": "View %(title)s"},
    "Купити %(title)s":       {"ru": "Купить %(title)s",   "en": "Buy %(title)s"},
    "Показати кольори %(title)s": {"ru": "Показать цвета %(title)s", "en": "Show colors of %(title)s"},

    # ===== Home page (index.html) =====
    "TwoComms — Стріт & Мілітарі Одяг | Головна": {
        "ru": "TwoComms — стрит & милитари одежда | Главная",
        "en": "TwoComms — Street & Military Apparel | Home",
    },
    "TwoComms — стріт та мілітарі одяг з Харкова: футболки, худі": {
        "ru": "TwoComms — стрит и милитари одежда из Харькова: футболки, худи",
        "en": "TwoComms — street and military apparel from Kharkiv: t-shirts, hoodies",
    },
    "TwoComms — український магазин стріт і мілітарі одягу: футболки, худі, лонгсліви, кастомний друк і швидка доставка по Україні.": {
        "ru": "TwoComms — украинский магазин стрит и милитари одежды: футболки, худи, лонгсливы, кастомный принт и быстрая доставка по Украине.",
        "en": "TwoComms — Ukrainian streetwear & military apparel store: t-shirts, hoodies, longsleeves, custom print and fast Ukraine-wide shipping.",
    },
    "TwoComms — харківський бренд стрітвір та мілітарі одягу: футболки, худі, лонгсліви, кастомний DTF-друк, доставка Новою Поштою по всій Україні.": {
        "ru": "TwoComms — харьковский бренд стритвир и милитари одежды: футболки, худи, лонгсливы, кастомная DTF-печать, доставка Новой Почтой по всей Украине.",
        "en": "TwoComms is a Kharkiv streetwear and military apparel brand: t-shirts, hoodies, longsleeves, custom DTF print and Nova Poshta delivery across Ukraine.",
    },
    "TwoComms — стріт & мілітарі одяг з ексклюзивним дизайном": {
        "ru": "TwoComms — стрит & милитари одежда с эксклюзивным дизайном",
        "en": "TwoComms — street & military apparel with exclusive design",
    },
    "Футболки, худі та лонгсліви з авторськими принтами. Бренд TwoComms — українське виробництво, швидка доставка.": {
        "ru": "Футболки, худи и лонгсливы с авторскими принтами. Бренд TwoComms — украинское производство, быстрая доставка.",
        "en": "T-shirts, hoodies and longsleeves with original prints. TwoComms — Ukrainian production with fast delivery.",
    },
    "TwoComms — стріт & мілітарі одяг": {
        "ru": "TwoComms — стрит & милитари одежда",
        "en": "TwoComms — street & military apparel",
    },
    "Ексклюзивні футболки, худі та лонгсліви з характером. Швидка доставка по Україні.": {
        "ru": "Эксклюзивные футболки, худи и лонгсливы с характером. Быстрая доставка по Украине.",
        "en": "Exclusive t-shirts, hoodies and longsleeves with character. Fast Ukraine-wide shipping.",
    },
    "TwoComms — бренд:":      {"ru": "TwoComms — бренд:",  "en": "TwoComms — the brand:"},
    "Стріт & мілітарі":       {"ru": "Стрит & милитари",   "en": "Street & military"},
    "одяг":                   {"ru": "одежда",             "en": "apparel"},
    "Ексклюзивні футболки, худі та лонгсліви. Бренд TwoComms — дизайн з характером.": {
        "ru": "Эксклюзивные футболки, худи и лонгсливы. Бренд TwoComms — дизайн с характером.",
        "en": "Exclusive t-shirts, hoodies and longsleeves. TwoComms — design with character.",
    },
    "Перейти в каталог":      {"ru": "Перейти в каталог",  "en": "Go to catalog"},
    "Ексклюзивний дизайн":    {"ru": "Эксклюзивный дизайн", "en": "Exclusive design"},

    # ===== Catalog page =====
    "Streetwear / Military-adjacent / Made in Ukraine": {
        "ru": "Streetwear / Military-adjacent / Made in Ukraine",
        "en": "Streetwear / Military-adjacent / Made in Ukraine",
    },
    "Пошук у каталозі":       {"ru": "Поиск в каталоге",   "en": "Search the catalog"},
    "одягу":                  {"ru": "одежды",             "en": "of apparel"},
    "Результати за запитом «%(q)s» у каталозі TwoComms.": {
        "ru": "Результаты по запросу «%(q)s» в каталоге TwoComms.",
        "en": "Results for «%(q)s» in the TwoComms catalog.",
    },
    "Обирайте футболки, худі та лонгсліви з авторськими принтами, українським виробництвом та швидкою доставкою по всій країні.": {
        "ru": "Выбирайте футболки, худи и лонгсливы с авторскими принтами, украинским производством и быстрой доставкой по всей стране.",
        "en": "Pick t-shirts, hoodies and longsleeves with original prints — Ukrainian production with fast country-wide delivery.",
    },
    "Переваги TwoComms":      {"ru": "Преимущества TwoComms", "en": "TwoComms advantages"},
    "Дизайни":                {"ru": "Дизайны",            "en": "Designs"},
    "власного виробництва":   {"ru": "собственного производства", "en": "in-house"},
    "Якість":                 {"ru": "Качество",           "en": "Quality"},
    "преміум матеріалів":     {"ru": "премиум материалов", "en": "premium materials"},
    "Доставка":               {"ru": "Доставка",           "en": "Delivery"},
    "по всій Україні":        {"ru": "по всей Украине",    "en": "Ukraine-wide"},
    "Створити свій дизайн":   {"ru": "Создать свой дизайн", "en": "Design your own"},
    "Створи свій":            {"ru": "Создай свой",        "en": "Make your"},
    "дизайн":                 {"ru": "дизайн",             "en": "design"},
    "Свій принт. Своя ідея.": {"ru": "Свой принт. Своя идея.", "en": "Your print. Your idea."},
    "Своє розташування.":     {"ru": "Своё расположение.", "en": "Your placement."},
    "Етапи створення принта": {"ru": "Этапы создания принта", "en": "Print design steps"},
    "Свій принт":             {"ru": "Свой принт",         "en": "Your print"},
    "Своя ідея":              {"ru": "Своя идея",          "en": "Your idea"},
    "Своє розташування":      {"ru": "Своё расположение",  "en": "Your placement"},
    "Приклад чорного худі TwoComms з власним принтом": {
        "ru": "Пример чёрного худи TwoComms с собственным принтом",
        "en": "Example of a black TwoComms hoodie with custom print",
    },
    "Зона друку":             {"ru": "Зона печати",        "en": "Print area"},
    "Створити свій принт":    {"ru": "Создать свой принт", "en": "Create your print"},
    "Від 1 одиниці • Швидке виготовлення": {
        "ru": "От 1 единицы • Быстрое изготовление",
        "en": "From 1 unit • Fast turnaround",
    },
    "Фільтр категорій":       {"ru": "Фильтр категорий",   "en": "Category filter"},
    "Усі товари":             {"ru": "Все товары",         "en": "All items"},
    "Усі категорії":          {"ru": "Все категории",      "en": "All categories"},
    "Категорії одягу TwoComms": {"ru": "Категории одежды TwoComms", "en": "TwoComms apparel categories"},
    "Опис категорії":         {"ru": "Описание категории", "en": "Category description"},
    "Результати пошуку":      {"ru": "Результаты поиска",  "en": "Search results"},
    "Товари категорії":       {"ru": "Товары категории",   "en": "Category products"},
    "Кольори:":               {"ru": "Цвета:",             "en": "Colors:"},
    "Немає товарів.":         {"ru": "Нет товаров.",       "en": "No products."},
    "Попередня":              {"ru": "Предыдущая",         "en": "Previous"},
    "Наступна":               {"ru": "Следующая",          "en": "Next"},
    "Додаткові розділи каталогу": {"ru": "Дополнительные разделы каталога", "en": "More catalog sections"},
    # blocktrans plurals
    "%(counter)s бал":        {"ru": "%(counter)s балл",   "en": "%(counter)s point"},
    "%(counter)s балів":      {"ru": "%(counter)s баллов", "en": "%(counter)s points"},
    "%(counter)s товар":      {"ru": "%(counter)s товар",  "en": "%(counter)s item"},
    "%(counter)s товарів":    {"ru": "%(counter)s товаров", "en": "%(counter)s items"},
    "%(counter)s товар знайдено": {"ru": "%(counter)s товар найден", "en": "%(counter)s item found"},
    "%(counter)s товарів знайдено": {"ru": "%(counter)s товаров найдено", "en": "%(counter)s items found"},

    # ===== Product detail page (PDP) =====
    "Галерея товару":         {"ru": "Галерея товара",     "en": "Product gallery"},
    "Дії з фото":             {"ru": "Действия с фото",    "en": "Photo actions"},
    "Збільшити фото":         {"ru": "Увеличить фото",     "en": "Zoom photo"},
    "Попереднє фото":         {"ru": "Предыдущее фото",    "en": "Previous photo"},
    "Наступне фото":          {"ru": "Следующее фото",     "en": "Next photo"},
    "Мініатюри товару":       {"ru": "Миниатюры товара",   "en": "Product thumbnails"},
    "Переваги покупки":       {"ru": "Преимущества покупки", "en": "Purchase benefits"},
    "Швидка відправка":       {"ru": "Быстрая отправка",   "en": "Fast shipping"},
    "1-2 дні по Україні":     {"ru": "1-2 дня по Украине", "en": "1-2 days in Ukraine"},
    "Індивідуальний підхід":  {"ru": "Индивидуальный подход", "en": "Personal approach"},
    "Допоможемо з вибором":   {"ru": "Поможем с выбором",  "en": "We help with the choice"},
    "Обмін розміру":          {"ru": "Обмен размера",      "en": "Size exchange"},
    "14 днів на обмін":       {"ru": "14 дней на обмен",   "en": "14 days for exchange"},
    "Індивідуальний позивний або бренд?": {
        "ru": "Индивидуальный позывной или бренд?",
        "en": "Custom call sign or brand?",
    },
    "Створимо принт з нуля під ваш позивний, логотип або ідею. Унікальна річ разом з TwoComms.": {
        "ru": "Создадим принт с нуля под ваш позывной, логотип или идею. Уникальная вещь вместе с TwoComms.",
        "en": "We'll design a print from scratch for your call sign, logo or idea. A unique piece with TwoComms.",
    },
    "Дії з товаром":          {"ru": "Действия с товаром", "en": "Product actions"},
    "Поділитися товаром":     {"ru": "Поделиться товаром", "en": "Share product"},
    "Особливості товару":     {"ru": "Особенности товара", "en": "Product features"},
    "Преміум тканина":        {"ru": "Премиум ткань",      "en": "Premium fabric"},
    "Лімітований дроп":       {"ru": "Лимитированный дроп", "en": "Limited drop"},
    "Розмір":                 {"ru": "Размер",             "en": "Size"},
    "Як обрати розмір?":      {"ru": "Как выбрать размер?", "en": "How to choose a size?"},
    "Розміри уточнюються менеджером після замовлення.": {
        "ru": "Размеры уточняются менеджером после заказа.",
        "en": "Sizes are confirmed by the manager after the order is placed.",
    },
    "Для цього товару доступний один колір.": {
        "ru": "Для этого товара доступен один цвет.",
        "en": "Only one color is available for this product.",
    },
    "Посадка":                {"ru": "Посадка",            "en": "Fit"},
    "Інформація про товар":   {"ru": "Информация о товаре", "en": "Product information"},
    "Опис":                   {"ru": "Описание",           "en": "Description"},
    "Розмірна сітка":         {"ru": "Размерная сетка",    "en": "Size guide"},
    "Догляд":                 {"ru": "Уход",               "en": "Care"},
    "FAQ товару":             {"ru": "FAQ товара",         "en": "Product FAQ"},
    # PDP plurals + dynamic strings
    "Рейтинг товару, %(counter)s відгук": {
        "ru": "Рейтинг товара, %(counter)s отзыв",
        "en": "Product rating, %(counter)s review",
    },
    "Рейтинг товару, %(counter)s відгуків": {
        "ru": "Рейтинг товара, %(counter)s отзывов",
        "en": "Product rating, %(counter)s reviews",
    },
    "%(counter)s відгук":     {"ru": "%(counter)s отзыв",  "en": "%(counter)s review"},
    "%(counter)s відгуків":   {"ru": "%(counter)s отзывов", "en": "%(counter)s reviews"},
    "Бонусні бали за покупку: %(n)s": {
        "ru": "Бонусные баллы за покупку: %(n)s",
        "en": "Bonus points for the purchase: %(n)s",
    },

    # ===== Mini cart / cart =====
    "Кастомний друк":         {"ru": "Кастомный принт",    "en": "Custom print"},
    "Видалити кастомну позицію": {"ru": "Удалить кастомную позицию", "en": "Remove custom item"},
    "Видалити":               {"ru": "Удалить",            "en": "Remove"},
    "Разом":                  {"ru": "Итого",              "en": "Total"},
    "Кастомні позиції очікують підтвердження менеджером. Фінальну ціну узгоджуєте в кошику.": {
        "ru": "Кастомные позиции ожидают подтверждения менеджером. Финальную цену согласуете в корзине.",
        "en": "Custom items await manager confirmation. The final price is agreed in the cart.",
    },
    "Оформити на сайті":      {"ru": "Оформить на сайте",  "en": "Checkout on site"},
    "Оформити через":         {"ru": "Оформить через",     "en": "Checkout via"},
    "Кошик порожній.":        {"ru": "Корзина пуста.",     "en": "Cart is empty."},

    # ===== Cart page =====
    "Кошик":                  {"ru": "Корзина",            "en": "Cart"},
    "Ваші вибрані товари":    {"ru": "Ваши выбранные товары", "en": "Your selected items"},
    "Очистити":               {"ru": "Очистить",           "en": "Clear"},
    "Кошик порожній":         {"ru": "Корзина пуста",      "en": "Cart is empty"},
    "Додайте товари до кошика, щоб зробити замовлення": {
        "ru": "Добавьте товары в корзину, чтобы оформить заказ",
        "en": "Add products to the cart to place an order",
    },
    "Перейти до покупок":     {"ru": "Перейти к покупкам", "en": "Continue shopping"},
    "Оформлення замовлення":  {"ru": "Оформление заказа",  "en": "Checkout"},
    "Підсумок вашого замовлення": {"ru": "Итог вашего заказа", "en": "Your order summary"},
    "Промокод":               {"ru": "Промокод",           "en": "Promo code"},
    "Застосовано":            {"ru": "Применён",           "en": "Applied"},
    "Знижка:":                {"ru": "Скидка:",            "en": "Discount:"},
    "Введіть код промокоду":  {"ru": "Введите код промокода", "en": "Enter promo code"},
    "Товари":                 {"ru": "Товары",             "en": "Items"},
    "До сплати:":             {"ru": "К оплате:",          "en": "Total to pay:"},
    "Залишок при отриманні:": {"ru": "Остаток при получении:", "en": "Remainder on delivery:"},
    "Знижка магазину":        {"ru": "Скидка магазина",    "en": "Store discount"},
    "Знижка промокоду":       {"ru": "Скидка по промокоду", "en": "Promo discount"},
    "Знижка від TwoComms":    {"ru": "Скидка от TwoComms", "en": "TwoComms discount"},
    "Разом ви економите":     {"ru": "Всего вы экономите", "en": "You save in total"},
    "Бали за замовлення":     {"ru": "Баллы за заказ",     "en": "Order points"},
    "буде нараховано після отримання товару": {
        "ru": "будут начислены после получения товара",
        "en": "will be credited after receipt",
    },
    "Бали не нараховуються":  {"ru": "Баллы не начисляются", "en": "No points awarded"},
    "Для отримання балів потрібно авторизуватися": {
        "ru": "Чтобы получать баллы, необходимо авторизоваться",
        "en": "Sign in to earn points",
    },
    "Авторизуватися":         {"ru": "Авторизоваться",     "en": "Sign in"},
    "Авторизація":            {"ru": "Авторизация",        "en": "Sign in"},
    "Доставка оплачується згідно тарифів перевізника.": {
        "ru": "Доставка оплачивается согласно тарифам перевозчика.",
        "en": "Delivery is paid according to the carrier's tariffs.",
    },
    "Зберегти зміни":         {"ru": "Сохранить изменения", "en": "Save changes"},
    "Потрібна консультація?": {"ru": "Нужна консультация?", "en": "Need a consultation?"},
    "Безкоштовна консультація": {"ru": "Бесплатная консультация", "en": "Free consultation"},
    "Закрити":                {"ru": "Закрыть",            "en": "Close"},
    "Залиште свої контактні дані, і наш менеджер звʼяжеться з вами найближчим часом для консультації": {
        "ru": "Оставьте свои контактные данные, и наш менеджер свяжется с вами в ближайшее время для консультации",
        "en": "Leave your contact details and our manager will reach out shortly for a consultation",
    },
    "Щоб не вводити дані кожного разу — авторизуйтесь і накопичуйте бали.": {
        "ru": "Чтобы не вводить данные каждый раз — авторизуйтесь и копите баллы.",
        "en": "Skip re-entering details next time — sign in and earn points.",
    },
    "ПІБ":                    {"ru": "ФИО",                "en": "Full name"},
    "Прізвище Імʼя По батькові": {"ru": "Фамилия Имя Отчество", "en": "Last First Middle name"},
    "Телефон":                {"ru": "Телефон",            "en": "Phone"},
    "0931234567":             {"ru": "0931234567",         "en": "+380931234567"},
    "Можна вводити 093..., 809..., 380... або +380... — номер приведемо до потрібного формату.": {
        "ru": "Можно вводить 093..., 809..., 380... или +380... — номер приведём к нужному формату.",
        "en": "You can enter 093..., 809..., 380... or +380... — we'll normalize the format.",
    },
    "Email":                  {"ru": "Email",              "en": "Email"},
    "your@email.com":         {"ru": "your@email.com",     "en": "your@email.com"},
    "Місто":                  {"ru": "Город",              "en": "City"},
    "Почніть вводити місто Нової пошти": {
        "ru": "Начните вводить город Новой почты",
        "en": "Start typing a Nova Poshta city",
    },
    "Почніть вводити назву міста і виберіть підтверджений варіант зі списку Нової пошти.": {
        "ru": "Начните вводить название города и выберите подтверждённый вариант из списка Новой почты.",
        "en": "Start typing the city name and pick a confirmed Nova Poshta option from the list.",
    },
    "Відділення / Поштомат НП": {"ru": "Отделение / Почтомат НП", "en": "NP branch / parcel locker"},
    "Оберіть відділення або поштомат": {
        "ru": "Выберите отделение или почтомат",
        "en": "Choose a branch or parcel locker",
    },
    "Усі пункти":             {"ru": "Все пункты",         "en": "All points"},
    "Відділення":             {"ru": "Отделение",          "en": "Branch"},
    "Поштомат":               {"ru": "Почтомат",           "en": "Parcel locker"},
    "Після вибору міста почніть вводити номер або адресу і виберіть відділення чи поштомат зі списку Нової пошти.": {
        "ru": "После выбора города начните вводить номер или адрес и выберите отделение или почтомат из списка Новой почты.",
        "en": "After choosing a city, start typing a number or address and pick a branch or parcel locker from the Nova Poshta list.",
    },
    "Тип оплати":             {"ru": "Тип оплаты",         "en": "Payment type"},
    "Оберіть тип оплати":     {"ru": "Выберите тип оплаты", "en": "Choose payment type"},
    "Онлайн оплата (повна сума)": {"ru": "Онлайн оплата (полная сумма)", "en": "Online payment (full amount)"},
    "Передплата 200 грн (решта при отриманні)": {
        "ru": "Предоплата 200 грн (остаток при получении)",
        "en": "Prepayment UAH 200 (remainder on delivery)",
    },
    "недоступно з кастомним принтом": {
        "ru": "недоступно с кастомным принтом",
        "en": "unavailable with custom print",
    },
    "У кошику є кастомний принт, тому передплата 200 грн тимчасово недоступна — менеджер узгодить фінальну ціну.": {
        "ru": "В корзине есть кастомный принт, поэтому предоплата 200 грн временно недоступна — менеджер согласует финальную цену.",
        "en": "There's a custom print in the cart, so the UAH 200 prepayment is temporarily unavailable — the manager will confirm the final price.",
    },
    "Це додаткова передоплата для вас — решту сплатите при отриманні.": {
        "ru": "Это дополнительная предоплата — остаток оплатите при получении.",
        "en": "This is an extra prepayment — pay the remainder on delivery.",
    },
    "Орієнтовно за кастомний друк": {"ru": "Ориентировочно за кастомный принт", "en": "Estimated for custom print"},
    "Менеджер погодив усі кастомні позиції. Тепер можна оплатити все замовлення разом.": {
        "ru": "Менеджер согласовал все кастомные позиции. Теперь можно оплатить весь заказ сразу.",
        "en": "The manager has approved all custom items. You can now pay for the entire order.",
    },
    "Кастомний одяг уже переданий менеджеру на перевірку. Поки триває модерація, позиція відображається в кошику окремо і <strong>не входить до суми оплати зараз</strong>. Щойно менеджер погодить деталі та фінальну ціну — кастом автоматично приєднається до рахунку.": {
        "ru": "Кастомная одежда уже передана менеджеру на проверку. Пока идёт модерация, позиция отображается в корзине отдельно и <strong>не входит в сумму оплаты сейчас</strong>. Как только менеджер согласует детали и финальную цену — кастом автоматически добавится к счёту.",
        "en": "The custom item has been sent to the manager for review. While moderation is in progress, the item is shown separately and <strong>not included in the current payment total</strong>. Once the manager approves the details and final price, the custom item will be added to the invoice automatically.",
    },
    "Написати менеджеру в Telegram": {"ru": "Написать менеджеру в Telegram", "en": "Message the manager on Telegram"},
    "Менеджер відхилив попередню заявку. Перегляньте деталі вище та надішліть її повторно, або ж оформіть замовлення <strong>без кастомних позицій</strong>.": {
        "ru": "Менеджер отклонил предыдущую заявку. Просмотрите детали выше и отправьте её повторно, либо оформите заказ <strong>без кастомных позиций</strong>.",
        "en": "The manager rejected the previous request. Review the details above and resubmit, or place the order <strong>without custom items</strong>.",
    },
    "Менеджер уже працює з вашим кастомним запитом. Коли модерацію буде завершено, позиція автоматично стане доступною для оплати в цьому ж кошику.": {
        "ru": "Менеджер уже работает с вашим кастомным запросом. Когда модерация завершится, позиция автоматически станет доступной для оплаты в этой же корзине.",
        "en": "The manager is already handling your custom request. Once moderation finishes, the item will automatically become available for payment in this cart.",
    },
    "Онлайн оплата карткою":  {"ru": "Онлайн оплата картой", "en": "Online card payment"},
    "(без урахування кастомного одягу)": {
        "ru": "(без учёта кастомной одежды)",
        "en": "(custom items not included)",
    },
    "Pay":                    {"ru": "Pay",                "en": "Pay"},
    "Оформити замовлення":    {"ru": "Оформить заказ",     "en": "Place order"},
    "Замовити як гість":      {"ru": "Заказать как гость", "en": "Order as guest"},
    "Оплата стане доступною після погодження менеджером. Зараз у кошику немає позицій, які можна оплатити окремо.": {
        "ru": "Оплата станет доступной после согласования менеджером. Сейчас в корзине нет позиций, которые можно оплатить отдельно.",
        "en": "Payment becomes available after manager approval. Currently there are no items in the cart that can be paid separately.",
    },
    "Оплата покриє лише звичайні товари. Кастомна позиція приєднається до замовлення автоматично після погодження менеджером — на ваш номер надійде додаткове повідомлення з фінальною ціною.": {
        "ru": "Оплата покроет только обычные товары. Кастомная позиция добавится к заказу автоматически после согласования менеджером — на ваш номер придёт дополнительное сообщение с финальной ценой.",
        "en": "Payment will cover only standard items. The custom item will be added to the order automatically after manager approval — you'll receive an additional message with the final price.",
    },
    "Після оформлення замовлення менеджер звʼяжеться з вами за вказаним номером телефону, щоб підтвердити деталі та узгодити доставку.": {
        "ru": "После оформления заказа менеджер свяжется с вами по указанному номеру телефона, чтобы подтвердить детали и согласовать доставку.",
        "en": "After placing the order, the manager will contact you at the phone number provided to confirm details and arrange delivery.",
    },
    "Заробите %(n)s балів":   {"ru": "Заработаете %(n)s баллов", "en": "Earn %(n)s points"},

    # ===== Accounts =====
    "Вхід":                   {"ru": "Вход",               "en": "Sign in"},
    "Вхід до акаунту":        {"ru": "Вход в аккаунт",     "en": "Sign in to account"},
    "Швидкий доступ до замовлень, балів та обраного": {
        "ru": "Быстрый доступ к заказам, баллам и избранному",
        "en": "Quick access to orders, points and favorites",
    },
    "Вхід через Google - TwoComms": {"ru": "Вход через Google - TwoComms", "en": "Sign in via Google - TwoComms"},
    "Увійти через Google":    {"ru": "Войти через Google", "en": "Continue with Google"},
    "Продовжуючи, ви погоджуєтесь з умовами використання": {
        "ru": "Продолжая, вы соглашаетесь с условиями использования",
        "en": "By continuing, you agree to the terms of use",
    },
    "Логін":                  {"ru": "Логин",              "en": "Login"},
    "Використовуйте свій нікнейм. Реєстрація — за хвилину.": {
        "ru": "Используйте свой никнейм. Регистрация — за минуту.",
        "en": "Use your nickname. Registration takes a minute.",
    },
    "Пароль":                 {"ru": "Пароль",             "en": "Password"},
    "Показати пароль":        {"ru": "Показать пароль",    "en": "Show password"},
    "Увійти":                 {"ru": "Войти",              "en": "Sign in"},
    "Немає облікового запису?": {"ru": "Нет аккаунта?",    "en": "No account yet?"},
    "Зареєструватись":        {"ru": "Зарегистрироваться", "en": "Sign up"},
    "Реєстрація":             {"ru": "Регистрация",        "en": "Sign up"},
    "Створення акаунту":      {"ru": "Создание аккаунта",  "en": "Create account"},
    "Отримуйте бонуси, спостерігайте за замовленнями та обраним": {
        "ru": "Получайте бонусы, следите за заказами и избранным",
        "en": "Earn bonuses, track your orders and favorites",
    },
    "Продовжити через Google": {"ru": "Продолжить через Google", "en": "Continue with Google"},
    "Ми створимо акаунт на основі вашого Google профілю": {
        "ru": "Мы создадим аккаунт на основе вашего Google-профиля",
        "en": "We'll create an account based on your Google profile",
    },
    "Мін. 3 символи, латиниця/цифри/._-": {
        "ru": "Мин. 3 символа, латиница/цифры/._-",
        "en": "Min. 3 characters, Latin letters/digits/._-",
    },
    "8+ символів, бажано букви велики/малі та цифри": {
        "ru": "8+ символов, желательно большие/маленькие буквы и цифры",
        "en": "8+ characters, ideally mixed case and digits",
    },
    "Складність паролю":      {"ru": "Сложность пароля",   "en": "Password strength"},
    "Повтор паролю":          {"ru": "Повтор пароля",      "en": "Repeat password"},
    "Створити акаунт":        {"ru": "Создать аккаунт",    "en": "Create account"},
    "Вже маєте акаунт?":      {"ru": "Уже есть аккаунт?",  "en": "Already have an account?"},

    # favorites
    "Обрані товари":          {"ru": "Избранные товары",   "en": "Favorite items"},
    "Перейти в каталог":      {"ru": "Перейти в каталог",  "en": "Go to catalog"},
    "Кольори:":               {"ru": "Цвета:",             "en": "Colors:"},
    "Увага!":                 {"ru": "Внимание!",          "en": "Notice!"},
    "Ви переглядаєте обрані товари як гість.": {
        "ru": "Вы просматриваете избранные товары как гость.",
        "en": "You're viewing favorites as a guest.",
    },
    "Увійдіть в акаунт":      {"ru": "Войдите в аккаунт",  "en": "Sign in"},
    "або":                    {"ru": "или",                "en": "or"},
    "зареєструйтесь":         {"ru": "зарегистрируйтесь",  "en": "sign up"},
    "щоб зберегти обрані товари назавжди.": {
        "ru": "чтобы сохранить избранные товары навсегда.",
        "en": "to save your favorites permanently.",
    },
    "У вас поки немає обраних товарів": {
        "ru": "У вас пока нет избранных товаров",
        "en": "You don't have any favorites yet",
    },
    "Додавайте товари до обраного, натискаючи на сердечко на карточці товару": {
        "ru": "Добавляйте товары в избранное, нажимая на сердечко на карточке товара",
        "en": "Add items to favorites by clicking the heart on a product card",
    },

    # my_orders
    "Мої замовлення":         {"ru": "Мои заказы",         "en": "My orders"},
    "Відстежуйте статус ваших замовлень": {
        "ru": "Отслеживайте статус своих заказов",
        "en": "Track the status of your orders",
    },

    # my_reviews
    "Мої відгуки":            {"ru": "Мои отзывы",         "en": "My reviews"},
    "Усі ваші відгуки про куплені товари TwoComms.": {
        "ru": "Все ваши отзывы о купленных товарах TwoComms.",
        "en": "All your reviews of TwoComms products you've purchased.",
    },
    "Статистика відгуків":    {"ru": "Статистика отзывов", "en": "Reviews statistics"},
    "Опубліковано":           {"ru": "Опубликовано",       "en": "Published"},
    "На модерації":           {"ru": "На модерации",       "en": "Pending"},
    "Відхилено":              {"ru": "Отклонено",          "en": "Rejected"},
    "Відгук про %(t)s":       {"ru": "Отзыв о %(t)s",      "en": "Review of %(t)s"},
    "Оцінка %(n)s з 5":       {"ru": "Оценка %(n)s из 5",  "en": "Rating %(n)s out of 5"},
    "Фото відгуку %(n)s про %(t)s": {
        "ru": "Фото отзыва %(n)s о %(t)s",
        "en": "Review photo %(n)s of %(t)s",
    },
    "Причина відхилення:":    {"ru": "Причина отклонения:", "en": "Rejection reason:"},
    "Ви ще не залишили жодного відгуку.": {
        "ru": "Вы ещё не оставили ни одного отзыва.",
        "en": "You haven't left any reviews yet.",
    },
    "Перейти до моїх замовлень": {"ru": "Перейти к моим заказам", "en": "Go to my orders"},
    "та поділіться враженнями про придбані товари.": {
        "ru": "и поделитесь впечатлениями о купленных товарах.",
        "en": "and share your impressions of the purchased items.",
    },

    # support_content (footer)
    "Streetwear / Military-adjacent / Made in Ukraine": {
        "ru": "Стритвир / Military-adjacent / сделано в Украине",
        "en": "Streetwear / Military-adjacent / Made in Ukraine",
    },
    "All Rights Reserved © TWOCOMMS, 2026": {
        "ru": "Все права защищены © TWOCOMMS, 2026",
        "en": "All Rights Reserved © TWOCOMMS, 2026",
    },
    "Швидкий доступ":         {"ru": "Быстрый доступ",     "en": "Quick access"},
    "Доставка і оплата":      {"ru": "Доставка и оплата",  "en": "Delivery & payment"},
    "Кастомний принт":        {"ru": "Кастомный принт",    "en": "Custom print"},
    "Догляд за одягом":       {"ru": "Уход за одеждой",    "en": "Garment care"},
    "Розмірна сітка":         {"ru": "Размерная сетка",    "en": "Size guide"},
    "Відстеження замовлення": {"ru": "Отслеживание заказа","en": "Order tracking"},
    "Політика конфіденційності": {"ru": "Политика конфиденциальности", "en": "Privacy Policy"},
    "Умови використання":     {"ru": "Условия использования", "en": "Terms of Service"},

    # profile_setup
    "Профіль":                {"ru": "Профиль",            "en": "Profile"},
    "Налаштування профілю":   {"ru": "Настройки профиля",  "en": "Profile settings"},
    "Заповніть інформацію для зручного замовлення": {
        "ru": "Заполните информацию для удобного оформления заказа",
        "en": "Fill in your details for a smooth checkout",
    },

    # ===== order_failed / order_success (Phase 17d.8) =====
    "Оплату не завершено": {"ru": "Оплата не завершена", "en": "Payment not completed"},
    "Платіж не пройшов. Спробуйте ще раз або оберіть інший спосіб оплати.": {
        "ru": "Платёж не прошёл. Попробуйте ещё раз или выберите другой способ оплаты.",
        "en": "The payment did not go through. Try again or choose another payment method.",
    },
    "Повернутися в кошик": {"ru": "Вернуться в корзину", "en": "Back to cart"},
    "На головну": {"ru": "На главную", "en": "Back to home"},

    "Замовлення оформлено": {"ru": "Заказ оформлен", "en": "Order placed"},
    "Замовлення успішно оформлено!": {"ru": "Заказ успешно оформлен!", "en": "Order placed successfully!"},
    "Номер замовлення:": {"ru": "Номер заказа:", "en": "Order number:"},
    "Переглянути деталі замовлення": {"ru": "Посмотреть детали заказа", "en": "View order details"},
    "Оплата успішно пройшла!": {"ru": "Оплата прошла успешно!", "en": "Payment completed successfully!"},
    "Дякуємо, брат! Твоє замовлення оплачено та відправлено в обробку. Ми розпочнемо підготовку найближчим часом.": {
        "ru": "Спасибо, брат! Твой заказ оплачен и отправлен в обработку. Мы начнём подготовку в ближайшее время.",
        "en": "Thanks, brother! Your order is paid and is being processed. We'll start preparing it shortly.",
    },
    "Дякуємо за покупку! Твоє замовлення оплачено через Monobank та відправлено в обробку. Ми розпочнемо підготовку найближчим часом.": {
        "ru": "Спасибо за покупку! Ваш заказ оплачен через Monobank и отправлен в обработку. Мы начнём подготовку в ближайшее время.",
        "en": "Thanks for your purchase! Your order is paid via Monobank and is being processed. We'll start preparing it shortly.",
    },
    "Передплата внесена!": {"ru": "Предоплата внесена!", "en": "Prepayment received!"},
    "Дякуємо, брат! Передплата внесена. Залишок оплатиш при отриманні на Новій Пошті. Ми розпочнемо підготовку та відправимо на вказане відділення.": {
        "ru": "Спасибо, брат! Предоплата внесена. Остаток оплатишь при получении в Новой Почте. Мы начнём подготовку и отправим на указанное отделение.",
        "en": "Thanks, brother! Prepayment received. You'll pay the remainder on pickup at Nova Poshta. We'll start preparing and ship to the chosen branch.",
    },
    "Дякуємо за покупку! Передплата внесена. Залишок необхідно оплатити при отриманні посилки на відділенні Нової Пошти. Ми розпочнемо підготовку та відправимо на вказане відділення.": {
        "ru": "Спасибо за покупку! Предоплата внесена. Остаток необходимо оплатить при получении посылки в отделении Новой Почты. Мы начнём подготовку и отправим на указанное отделение.",
        "en": "Thanks for your purchase! Prepayment received. The remainder is payable on pickup at the Nova Poshta branch. We'll start preparing and ship to the chosen branch.",
    },
    "Ваша заявка відправлена в обробку": {"ru": "Ваша заявка отправлена в обработку", "en": "Your request has been submitted"},
    "Дякуємо, брат! Замовлення відправлено в обробку. Статус надійде в Telegram.": {
        "ru": "Спасибо, брат! Заказ отправлен в обработку. Статус придёт в Telegram.",
        "en": "Thanks, brother! The order is in processing. You'll get status updates in Telegram.",
    },
    "Дякуємо за замовлення! Заявка відправлена в обробку.": {
        "ru": "Спасибо за заказ! Заявка отправлена в обработку.",
        "en": "Thanks for your order! The request has been submitted.",
    },
    "Внесена передплата:": {"ru": "Внесённая предоплата:", "en": "Prepayment made:"},
    "грн": {"ru": "грн", "en": "UAH"},
    "Залишок (при отриманні):": {"ru": "Остаток (при получении):", "en": "Remainder (on pickup):"},
    "Дякуємо за вибір стилю TwoComms!": {"ru": "Спасибо за выбор стиля TwoComms!", "en": "Thanks for choosing TwoComms style!"},
    "Твій стиль — твоя сильна сторона. Ми цінуємо, що ти обрав саме нас для свого образу.": {
        "ru": "Твой стиль — твоя сильная сторона. Мы ценим, что ты выбрал именно нас для своего образа.",
        "en": "Your style is your strength. We appreciate you choosing us for your look.",
    },
    "Є питання чи потрібна допомога?": {"ru": "Есть вопросы или нужна помощь?", "en": "Have questions or need help?"},
    "Написати в Telegram": {"ru": "Написать в Telegram", "en": "Message us on Telegram"},
    "Демо режим": {"ru": "Демо режим", "en": "Demo mode"},
    "Це тестовий перегляд сторінки. Для реального замовлення дані будуть заповнені автоматично.": {
        "ru": "Это тестовый просмотр страницы. Для реального заказа данные заполнятся автоматически.",
        "en": "This is a preview page. Real order data will be filled in automatically.",
    },
    "Деталі замовлення": {"ru": "Детали заказа", "en": "Order details"},
    "Деталі замовлення (демо)": {"ru": "Детали заказа (демо)", "en": "Order details (demo)"},
    "ПІБ": {"ru": "ФИО", "en": "Full name"},
    "Телефон": {"ru": "Телефон", "en": "Phone"},
    "Місто": {"ru": "Город", "en": "City"},
    "Відділення НП": {"ru": "Отделение НП", "en": "Nova Poshta branch"},
    "Не вказано": {"ru": "Не указано", "en": "Not provided"},
    "Тип оплати": {"ru": "Тип оплаты", "en": "Payment type"},
    "Передплата 200 грн": {"ru": "Предоплата 200 грн", "en": "Prepayment 200 UAH"},
    "Онлайн оплата (повна сума)": {"ru": "Онлайн оплата (полная сумма)", "en": "Online payment (full amount)"},
    "Оплата при отриманні": {"ru": "Оплата при получении", "en": "Cash on delivery"},
    "Сума замовлення": {"ru": "Сумма заказа", "en": "Order total"},
    "Товари в замовленні:": {"ru": "Товары в заказе:", "en": "Items in order:"},
    "Розмір:": {"ru": "Размер:", "en": "Size:"},
    "Посадка:": {"ru": "Посадка:", "en": "Fit:"},
    "Колір:": {"ru": "Цвет:", "en": "Colour:"},
    "Кількість:": {"ru": "Количество:", "en": "Quantity:"},
    "Тип:": {"ru": "Тип:", "en": "Type:"},
    "Статус: погоджено": {"ru": "Статус: согласовано", "en": "Status: approved"},
    "Система балів": {"ru": "Система баллов", "en": "Points programme"},
    "Дякуємо, брат! Твоє замовлення оформлено. Бали будуть нараховані після отримання товару.": {
        "ru": "Спасибо, брат! Заказ оформлен. Баллы будут начислены после получения товара.",
        "en": "Thanks, brother! The order is placed. Points will be credited after you receive the item.",
    },
    "Статус замовлення приходить в Telegram": {
        "ru": "Статус заказа приходит в Telegram",
        "en": "Order status is sent to Telegram",
    },
    "Оновлення про твоє замовлення надсилаються автоматично": {
        "ru": "Обновления по твоему заказу отправляются автоматически",
        "en": "Updates about your order are sent automatically",
    },
    "Мої замовлення": {"ru": "Мои заказы", "en": "My orders"},
    "Створіть акаунт або увійдіть в систему, щоб отримувати бали за покупки!": {
        "ru": "Создайте аккаунт или войдите в систему, чтобы получать баллы за покупки!",
        "en": "Create an account or log in to earn points on your purchases!",
    },
    "Накопичувати бали за покупки": {"ru": "Копить баллы за покупки", "en": "Earn points on purchases"},
    "Відстежувати статус замовлень": {"ru": "Отслеживать статус заказов", "en": "Track order statuses"},
    "Зберігати історію покупок": {"ru": "Хранить историю покупок", "en": "Keep purchase history"},
    "Отримувати персональні пропозиції": {"ru": "Получать персональные предложения", "en": "Receive personal offers"},
    "Увійти": {"ru": "Войти", "en": "Sign in"},
    "Зареєструватись": {"ru": "Зарегистрироваться", "en": "Sign up"},
    "Якщо ви зареєструєтесь, це замовлення автоматично привʼяжеться до вашого акаунту": {
        "ru": "Если вы зарегистрируетесь, этот заказ автоматически привяжется к вашему аккаунту",
        "en": "If you register, this order will automatically be linked to your account",
    },
    "Отримати чек на email": {"ru": "Получить чек на email", "en": "Get the receipt by email"},
    "Введіть вашу email адресу, і ми надішлемо чек про оплату": {
        "ru": "Введите ваш email, и мы пришлём чек об оплате",
        "en": "Enter your email and we'll send you the payment receipt",
    },
    "Відправити чек": {"ru": "Отправить чек", "en": "Send receipt"},
    "Залиште відгук в Instagram": {"ru": "Оставьте отзыв в Instagram", "en": "Leave a review on Instagram"},
    "Поділіться своїми враженнями про покупку! Залиште відгук в Instagram з відміткою нашої сторінки, і ми даруємо вам промокод на знижку 10% на наступне замовлення!": {
        "ru": "Поделитесь впечатлениями о покупке! Оставьте отзыв в Instagram с отметкой нашей страницы — и мы подарим вам промокод на 10% скидку на следующий заказ!",
        "en": "Share your purchase experience! Leave a review on Instagram tagging our page and we'll gift you a 10% discount code for your next order!",
    },
    "Ваш промокод на знижку": {"ru": "Ваш промокод на скидку", "en": "Your discount code"},
    "10% знижка на наступне замовлення": {"ru": "10% скидка на следующий заказ", "en": "10% off your next order"},
    "Відкрити Instagram": {"ru": "Открыть Instagram", "en": "Open Instagram"},
    "Повернутись на головну": {"ru": "Вернуться на главную", "en": "Back to home"},
    "Переглянути каталог": {"ru": "Посмотреть каталог", "en": "Browse catalog"},
    "Будь ласка, введіть коректну email адресу": {"ru": "Пожалуйста, введите корректный email адрес", "en": "Please enter a valid email address"},
    "Відправляється...": {"ru": "Отправляется...", "en": "Sending..."},
    "Чек буде відправлено на": {"ru": "Чек будет отправлен на", "en": "Receipt will be sent to"},
    "Функціонал буде додано найближчим часом.": {"ru": "Функционал будет добавлен в ближайшее время.", "en": "This feature will be added shortly."},

    # ===== Phase 17g — home page survey card =====
    "Бонус за фідбек":            {"ru": "Бонус за фидбэк",                 "en": "Feedback bonus"},
    "грн":                          {"ru": "грн",                              "en": "UAH"},
    "Промокод активується автоматично одразу після проходження та діє на весь асортимент.": {
        "ru": "Промокод активируется автоматически сразу после прохождения и действует на весь ассортимент.",
        "en": "The promo code is activated automatically right after completion and applies to the entire range.",
    },
    "≈ 5 хвилин часу":             {"ru": "≈ 5 минут времени",                "en": "≈ 5 minutes"},
    "Один раз на рік":             {"ru": "Один раз в год",                   "en": "Once a year"},
    "Можна повернутись потім":     {"ru": "Можно вернуться позже",            "en": "Can return later"},
    "Допоможи покращити":          {"ru": "Помоги улучшить",                  "en": "Help us improve"},
    "Коротке опитування без довгої анкети: відповіді допоможуть нам стати кращими.": {
        "ru": "Короткий опрос без длинной анкеты: ответы помогут нам стать лучше.",
        "en": "A short survey — no lengthy form. Your answers help us improve.",
    },
    "БІЛЬШЕ НЕ ПОКАЗУВАТИ":         {"ru": "БОЛЬШЕ НЕ ПОКАЗЫВАТЬ",             "en": "DO NOT SHOW AGAIN"},
    "ВИГРАЙ %(amount)s ГРН":        {"ru": "ВЫИГРАЙ %(amount)s ГРН",           "en": "WIN %(amount)s UAH"},
    "ЗА ОПИТУВАННЯ!":              {"ru": "ЗА ОПРОС!",                        "en": "FOR THE SURVEY!"},
    "Дай відповідь на кілька запитань та забери свій промокод!": {
        "ru": "Ответь на несколько вопросов и забери свой промокод!",
        "en": "Answer a few questions and grab your promo code!",
    },
    "Без форми на одну сторінку":  {"ru": "Без формы на одну страницу",       "en": "No single-page form"},
    "Лише для зареєстрованих користувачів": {
        "ru": "Только для зарегистрированных пользователей",
        "en": "Registered users only",
    },
    "Пройти опитування":           {"ru": "Пройти опрос",                     "en": "Take the survey"},
    "Продовжити опитування":       {"ru": "Продолжить опрос",                 "en": "Continue the survey"},
    "Опитування":                  {"ru": "Опрос",                            "en": "Survey"},
    "Коротке опитування про TWOCOMMS": {
        "ru": "Короткий опрос о TWOCOMMS",
        "en": "Short survey about TWOCOMMS",
    },
    "Твої відповіді допоможуть покращити сайт і колекції. Орієнтовно 5 хвилин.": {
        "ru": "Твои ответы помогут улучшить сайт и коллекции. Примерно 5 минут.",
        "en": "Your answers will help improve the site and collections. Around 5 minutes.",
    },
    "Ми використовуємо відповіді лише для аналітики та покращення сервісу. Контактні дані — лише за твоєю згодою.": {
        "ru": "Мы используем ответы только для аналитики и улучшения сервиса. Контактные данные — только с твоего согласия.",
        "en": "We use answers solely for analytics and service improvement. Contact details — only with your consent.",
    },
    "Завантаження…":               {"ru": "Загрузка…",                        "en": "Loading…"},
    "Далі":                         {"ru": "Далее",                            "en": "Next"},
    "Повернутись на 1 крок":       {"ru": "Вернуться на 1 шаг",               "en": "Back one step"},
    "Закрити":                     {"ru": "Закрыть",                          "en": "Close"},
    "Пропустити":                  {"ru": "Пропустить",                       "en": "Skip"},
    "Дякуємо!":                    {"ru": "Спасибо!",                         "en": "Thank you!"},
    "Твій промокод готовий.":      {"ru": "Твой промокод готов.",             "en": "Your promo code is ready."},
    "Скопіювати код":              {"ru": "Скопировать код",                  "en": "Copy code"},

    # ===== Phase 17g — home categories + custom-design CTA =====
    "Категорії":                   {"ru": "Категории",                        "en": "Categories"},
    "Перейти в каталог":           {"ru": "Перейти в каталог",                "en": "Go to catalog"},
    "Згорнути/розгорнути категорії": {"ru": "Свернуть/развернуть категории",  "en": "Collapse/expand categories"},
    "Згорнути":                    {"ru": "Свернуть",                         "en": "Collapse"},
    "Весь каталог":                {"ru": "Весь каталог",                     "en": "Full catalog"},
    "Замовити кастомний одяг":     {"ru": "Заказать кастомную одежду",        "en": "Order custom apparel"},
    "Свій дизайн":                 {"ru": "Свой дизайн",                      "en": "Your design"},
    "Збери одяг під себе":         {"ru": "Собери одежду под себя",           "en": "Build your own apparel"},
    "Обери основу, свій принт, матеріали й деталі — вартість побачиш одразу.": {
        "ru": "Выбери основу, свой принт, материалы и детали — стоимость увидишь сразу.",
        "en": "Pick the base, your print, materials and details — see the price instantly.",
    },
    "Спробувати конструктор":      {"ru": "Попробовать конструктор",          "en": "Try the builder"},
    "Потрібна допомога? Менеджер підкаже.": {
        "ru": "Нужна помощь? Менеджер подскажет.",
        "en": "Need help? Our manager will assist you.",
    },

    # ===== Phase 17g — home color filter + new products =====
    "Перейти до каталогу за кольором": {
        "ru": "Перейти к каталогу по цвету",
        "en": "Browse catalog by colour",
    },
    "Обери за кольором":           {"ru": "Выбери по цвету",                  "en": "Choose by colour"},
    "Усі кольори":                 {"ru": "Все цвета",                        "en": "All colours"},
    "Новинки":                     {"ru": "Новинки",                          "en": "New arrivals"},
    "Немає товарів.":              {"ru": "Нет товаров.",                     "en": "No products."},
    "Показати ще":                 {"ru": "Показать ещё",                     "en": "Show more"},
    "Завантаження...":             {"ru": "Загрузка...",                      "en": "Loading..."},
    "Наступна сторінка":           {"ru": "Следующая страница",               "en": "Next page"},

    # ===== Phase 17g — product detail recommendations =====
    "Схожі товари":                {"ru": "Похожие товары",                   "en": "Related products"},
    "Переглянути всі":             {"ru": "Смотреть все",                     "en": "View all"},
    "Рекомендовані товари":        {"ru": "Рекомендуемые товары",             "en": "Recommended products"},

    # ===== Phase 17g — catalog general SEO H2 block =====
    "Купити одяг з принтом онлайн в Україні — доставка Києвом, Харковом, Львовом": {
        "ru": "Купить одежду с принтом онлайн в Украине — доставка по Киеву, Харькову, Львову",
        "en": "Buy printed apparel online in Ukraine — delivery to Kyiv, Kharkiv, Lviv",
    },
    "<strong>Купити одяг з принтом</strong> у TwoComms можна онлайн із доставкою у будь-яке місто України: <strong>Київ, Харків, Львів, Дніпро, Одеса, Запоріжжя, Вінниця, Полтава, Чернівці, Івано-Франківськ, Тернопіль, Луцьк, Хмельницький, Ужгород, Чернігів</strong>. У каталозі — <a href=\"/catalog/tshirts/\">футболки</a>, <a href=\"/catalog/hoodie/\">худі</a> та <a href=\"/catalog/long-sleeve/\">лонгсліви</a> з авторськими принтами ЗСУ та streetwear-графікою. Доставка Новою Поштою 1–2 дні, безкоштовний обмін розміру протягом 14 днів.": {
        "ru": "<strong>Купить одежду с принтом</strong> в TwoComms можно онлайн с доставкой в любой город Украины: <strong>Киев, Харьков, Львов, Днепр, Одесса, Запорожье, Винница, Полтава, Черновцы, Ивано-Франковск, Тернополь, Луцк, Хмельницкий, Ужгород, Чернигов</strong>. В каталоге — <a href=\"/catalog/tshirts/\">футболки</a>, <a href=\"/catalog/hoodie/\">худи</a> и <a href=\"/catalog/long-sleeve/\">лонгсливы</a> с авторскими принтами ВСУ и streetwear-графикой. Доставка Новой Почтой 1–2 дня, бесплатный обмен размера в течение 14 дней.",
        "en": "<strong>Buy printed apparel</strong> at TwoComms online with delivery to any city in Ukraine: <strong>Kyiv, Kharkiv, Lviv, Dnipro, Odesa, Zaporizhzhia, Vinnytsia, Poltava, Chernivtsi, Ivano-Frankivsk, Ternopil, Lutsk, Khmelnytskyi, Uzhhorod, Chernihiv</strong>. The catalog features <a href=\"/catalog/tshirts/\">t-shirts</a>, <a href=\"/catalog/hoodie/\">hoodies</a> and <a href=\"/catalog/long-sleeve/\">longsleeves</a> with original Armed Forces and streetwear artwork. Nova Poshta delivery in 1–2 days, free size exchange within 14 days.",
    },
    "Замовити чоловічий або жіночий streetwear від українського бренду TwoComms": {
        "ru": "Заказать мужской или женский streetwear от украинского бренда TwoComms",
        "en": "Order men's or women's streetwear from the Ukrainian brand TwoComms",
    },
    "<strong>Замовити streetwear-одяг</strong> онлайн — це спосіб підтримати українське виробництво. Усі футболки, худі та лонгсліви TwoComms шиємо в Україні з преміум-бавовни та трикотажу щільністю 200–320 г/м². У каталозі є чоловічі моделі прямого крою, жіночі фасони зі звуженою посадкою та унісекс-варіанти у розмірах XS–XXL. Принти виконуємо в техніці DTF — стійкі до 30+ циклів прання.": {
        "ru": "<strong>Заказать streetwear-одежду</strong> онлайн — это способ поддержать украинское производство. Все футболки, худи и лонгсливы TwoComms мы шьём в Украине из премиум-хлопка и трикотажа плотностью 200–320 г/м². В каталоге есть мужские модели прямого кроя, женские фасоны с приталенной посадкой и унисекс-варианты в размерах XS–XXL. Принты выполняем по технологии DTF — устойчивы к 30+ циклам стирки.",
        "en": "<strong>Order streetwear apparel</strong> online — your way to support Ukrainian production. All TwoComms t-shirts, hoodies and longsleeves are made in Ukraine from premium cotton and 200–320 g/m² jersey. The catalog includes straight-cut men's models, fitted women's silhouettes and unisex options in sizes XS–XXL. Prints are produced using DTF technology — they survive 30+ wash cycles.",
    },
    "Український патріотичний streetwear — авторські принти ЗСУ та колаборації": {
        "ru": "Украинский патриотический streetwear — авторские принты ВСУ и коллаборации",
        "en": "Ukrainian patriotic streetwear — original Armed Forces prints and collaborations",
    },
    "<strong>TwoComms</strong> — український streetwear-бренд із мілітарним ДНК. Створюємо одяг для тих, хто живе в Україні, підтримує ЗСУ та цінує спокійну, але сильну естетику. У каталозі — колаборації з військовими підрозділами, авторські ілюстрації на тему Збройних Сил України, тризуба й сучасної української поп-культури. Частину прибутку від кожного замовлення направляємо на потреби ЗСУ. Дізнайтеся більше про <a href=\"/pro-brand/\">філософію бренду</a> або обирайте <a href=\"/custom-print/\">кастомний друк</a> власного принта на обраній моделі.": {
        "ru": "<strong>TwoComms</strong> — украинский streetwear-бренд с милитари-ДНК. Создаём одежду для тех, кто живёт в Украине, поддерживает ВСУ и ценит спокойную, но сильную эстетику. В каталоге — коллаборации с военными подразделениями, авторские иллюстрации на тему Вооружённых Сил Украины, тризуба и современной украинской поп-культуры. Часть прибыли с каждого заказа направляем на нужды ВСУ. Узнайте больше о <a href=\"/pro-brand/\">философии бренда</a> или выбирайте <a href=\"/custom-print/\">кастомную печать</a> собственного принта на выбранной модели.",
        "en": "<strong>TwoComms</strong> is a Ukrainian streetwear brand with military DNA. We make apparel for people who live in Ukraine, support the Armed Forces and value calm yet powerful aesthetics. The catalog includes collaborations with military units, original illustrations dedicated to the Armed Forces of Ukraine, the trident and contemporary Ukrainian pop culture. We donate a share of every order to the Armed Forces. Learn more about <a href=\"/pro-brand/\">our brand philosophy</a> or choose <a href=\"/custom-print/\">custom printing</a> of your own artwork on a selected model.",
    },

    # ===== Phase 17g — general catalog SEO panel (color_seo_copy.py) =====
    "Каталог одягу TwoComms — український стрітвір з характером": {
        "ru": "Каталог одежды TwoComms — украинский стритвир с характером",
        "en": "TwoComms apparel catalog — Ukrainian streetwear with character",
    },
    "TwoComms — це український бренд одягу, який створює стрітвір у трьох ключових категоріях: <a href=\"/catalog/hoodie/\">худі</a>, <a href=\"/catalog/tshirts/\">футболки</a> й <a href=\"/catalog/long-sleeve/\">лонгсліви</a>. Усі моделі ми розробляємо в Україні, друкуємо принти на власному обладнанні за технологією DTF і підбираємо тканини так, щоб одяг витримував щоденне носіння, прання й любий клімат — від літньої спеки до сирої осені.": {
        "ru": "TwoComms — это украинский бренд одежды, который создаёт стритвир в трёх ключевых категориях: <a href=\"/catalog/hoodie/\">худи</a>, <a href=\"/catalog/tshirts/\">футболки</a> и <a href=\"/catalog/long-sleeve/\">лонгсливы</a>. Все модели мы разрабатываем в Украине, печатаем принты на собственном оборудовании по технологии DTF и подбираем ткани так, чтобы одежда выдерживала ежедневную носку, стирку и любой климат — от летней жары до сырой осени.",
        "en": "TwoComms is a Ukrainian apparel brand creating streetwear in three core categories: <a href=\"/catalog/hoodie/\">hoodies</a>, <a href=\"/catalog/tshirts/\">t-shirts</a> and <a href=\"/catalog/long-sleeve/\">longsleeves</a>. Every model is designed in Ukraine, printed on our own DTF equipment, and made from fabrics chosen to withstand daily wear, laundering and any climate — from summer heat to damp autumn.",
    },
    "Кожен товар у каталозі доступний у кількох кольорах: класичний <a href=\"/catalog/?color=black\">чорний</a> для тих, хто шукає універсальну базу під будь-який принт; <a href=\"/catalog/?color=coyote\">кайот</a> і <a href=\"/catalog/?color=olive\">олива</a> для прихильників мілітарної естетики; нейтральний <a href=\"/catalog/?color=grey\">сірий</a> і чистий <a href=\"/catalog/?color=white\">білий</a> для весняно-літніх образів. Усі кольори перевіряються на стійкість до УФ та перфектне зберігання форми навіть після 30+ циклів прання.": {
        "ru": "Каждый товар в каталоге доступен в нескольких цветах: классический <a href=\"/catalog/?color=black\">чёрный</a> для тех, кто ищет универсальную базу под любой принт; <a href=\"/catalog/?color=coyote\">койот</a> и <a href=\"/catalog/?color=olive\">олива</a> для поклонников милитари-эстетики; нейтральный <a href=\"/catalog/?color=grey\">серый</a> и чистый <a href=\"/catalog/?color=white\">белый</a> для весенне-летних образов. Все цвета проверяются на устойчивость к УФ и идеально держат форму даже после 30+ циклов стирки.",
        "en": "Every item in the catalog comes in several colours: classic <a href=\"/catalog/?color=black\">black</a> for those who want a universal base for any print; <a href=\"/catalog/?color=coyote\">coyote</a> and <a href=\"/catalog/?color=olive\">olive</a> for fans of military aesthetics; neutral <a href=\"/catalog/?color=grey\">grey</a> and clean <a href=\"/catalog/?color=white\">white</a> for spring–summer looks. All colours are tested for UV resistance and hold their shape even after 30+ wash cycles.",
    },
    "Більшість принтів TwoComms — це авторські ілюстрації на тему патріотизму, ЗСУ, української історії та сучасної поп-культури. Ми передаємо частину прибутку на підтримку Збройних Сил України, тому кожна покупка — це одночасно вибір якісного одягу й вклад у перемогу. На сторінці кожного товару ви знайдете розмірну сітку, детальні фото матеріалу, відгуки клієнтів і прозору інформацію про склад тканини.": {
        "ru": "Большинство принтов TwoComms — это авторские иллюстрации на тему патриотизма, ВСУ, украинской истории и современной поп-культуры. Мы передаём часть прибыли на поддержку Вооружённых Сил Украины, поэтому каждая покупка — это одновременно выбор качественной одежды и вклад в победу. На странице каждого товара вы найдёте размерную сетку, детальные фото материала, отзывы клиентов и прозрачную информацию о составе ткани.",
        "en": "Most TwoComms prints are original illustrations on themes of patriotism, the Armed Forces, Ukrainian history and contemporary pop culture. We donate a portion of the profit to the Armed Forces of Ukraine, so every purchase combines quality apparel with a contribution to victory. On every product page you'll find a size guide, detailed material photos, customer reviews and transparent fabric composition.",
    },
    "Якщо ви не знайшли потрібну графіку — спробуйте розділ <a href=\"/custom-print/\">«Власний принт»</a>: ми надрукуємо будь-яку ілюстрацію на обраній моделі від однієї одиниці. Доставка по Україні — Новою Поштою на відділення або в поштомат за 1–2 дні. Оплата — карткою через Monobank/LiqPay або накладеним платежем. Усі товари мають 14 днів на повернення, якщо не підійшов розмір.": {
        "ru": "Если вы не нашли нужную графику — попробуйте раздел <a href=\"/custom-print/\">«Собственный принт»</a>: мы напечатаем любую иллюстрацию на выбранной модели от одной единицы. Доставка по Украине — Новой Почтой в отделение или почтомат за 1–2 дня. Оплата — картой через Monobank/LiqPay или наложенным платежом. У всех товаров есть 14 дней на возврат, если не подошёл размер.",
        "en": "If you didn't find the right artwork — try the <a href=\"/custom-print/\">Custom Print</a> section: we'll print any illustration on the selected model from a single unit. Delivery within Ukraine — via Nova Poshta to a branch or parcel locker in 1–2 days. Payment — by card through Monobank/LiqPay or cash on delivery. Every item can be returned within 14 days if the size doesn't fit.",
    },
}

# ===========================================================================
# Phase 17i (2026-05-12) — color_seo_copy.py dynamic + GENERAL chip labels.
# Kept in a separate dict so we can grow the curated set without ballooning
# the original TRANSLATIONS dict. Merged into TRANSLATIONS below.
# ===========================================================================
TRANSLATIONS_PHASE_17I: dict[str, dict[str, str]] = {
    # H2 templates with %(adj_*)s / %(name)s / %(cat)s placeholders.
    # Note: source uses ``adj_m`` (Ukrainian: «чорний одяг» — masculine noun
    # «одяг»). Russian noun «одежда» is feminine, so we drop the leading
    # adjective and let «%(name)s» carry the colour. English has no gender,
    # so we keep the natural attributive order.
    "%(adj_m_cap)s одяг TwoComms — стрітвір у відтінку «%(name)s»": {
        "ru": "%(adj_m_cap)s одежда TwoComms — стритвир в оттенке «%(name)s»",
        "en": "%(adj_m_cap)s TwoComms apparel — streetwear in the %(name)s tone",
    },
    "%(adj_n_cap)s %(cat)s TwoComms — український стрітвір з принтом": {
        "ru": "%(adj_n_cap)s %(cat)s TwoComms — украинский стритвир с принтом",
        "en": "%(adj_n_cap)s %(cat)s TwoComms — Ukrainian streetwear with print",
    },

    # Cross-category landing paragraphs.
    # RU rewording: source uses neuter «%(adj_n_cap)s одяг» (Ukrainian
    # «одяг» = masculine singular). Russian «одежда» is feminine — keep
    # the colour reference but drop the strict adjective agreement.
    "У каталозі TwoComms ви знайдете %(adj_n)s <a href=\"/catalog/hoodie/?color=%(slug)s\">худі</a>, <a href=\"/catalog/tshirts/?color=%(slug)s\">футболки</a> та <a href=\"/catalog/long-sleeve/?color=%(slug)s\">лонгсліви</a> з авторськими принтами. Усі моделі шиємо в Україні з натуральних тканин, друкуємо DTF-технологією й перевіряємо на стійкість кольору до прання. %(adj_n_cap)s одяг легко комбінувати з джинсами, карго-штанами та мілітарними аксесуарами.": {
        "ru": "В каталоге TwoComms вы найдёте %(adj_n)s <a href=\"/catalog/hoodie/?color=%(slug)s\">худи</a>, <a href=\"/catalog/tshirts/?color=%(slug)s\">футболки</a> и <a href=\"/catalog/long-sleeve/?color=%(slug)s\">лонгсливы</a> с авторскими принтами. Все модели шьём в Украине из натуральных тканей, печатаем по технологии DTF и проверяем на устойчивость цвета к стирке. %(adj_n_cap)s одежду легко комбинировать с джинсами, карго-штанами и милитари-аксессуарами.",
        "en": "In the TwoComms catalogue you'll find %(adj_n)s <a href=\"/catalog/hoodie/?color=%(slug)s\">hoodies</a>, <a href=\"/catalog/tshirts/?color=%(slug)s\">t-shirts</a> and <a href=\"/catalog/long-sleeve/?color=%(slug)s\">long sleeves</a> with original prints. Every model is made in Ukraine from natural fabrics, printed with DTF and colour-tested for laundering. %(adj_n_cap)s pieces pair easily with jeans, cargo trousers and tactical accessories.",
    },
    "Якщо вас цікавить конкретний принт у %(name)s — скористайтесь сторінкою <a href=\"/custom-print/\">«Власний принт»</a>: ми надрукуємо будь-яку ілюстрацію на обраній моделі від однієї одиниці. Доставка Новою Поштою по всій Україні — 1–2 дні; оплата карткою або накладеним платежем; повернення впродовж 14 днів, якщо не підійшов розмір.": {
        "ru": "Если вас интересует конкретный принт в %(name)s — воспользуйтесь страницей <a href=\"/custom-print/\">«Собственный принт»</a>: мы напечатаем любую иллюстрацию на выбранной модели от одной единицы. Доставка Новой Почтой по всей Украине — 1–2 дня; оплата картой или наложенным платежом; возврат в течение 14 дней, если не подошёл размер.",
        "en": "Looking for a specific print in %(name)s? Use the <a href=\"/custom-print/\">Custom Print</a> page: we'll print any artwork on the chosen model from a single unit. Nova Poshta delivery across Ukraine in 1–2 days; card payment or cash on delivery; 14-day return window if the size doesn't fit.",
    },

    # Category × colour paragraphs.
    "У категорії «%(cat)s» %(adj_n)s TwoComms — це поєднання якісної тканини, насиченого друку й продуманої посадки. Ми використовуємо щільні полотна, так що принт не просвічується, а сам одяг тримає форму після десятків прань. Звертайте увагу на розмірну сітку — %(cat)s у TwoComms ідуть у двох посадках: класична та оверсайз.": {
        "ru": "В категории «%(cat)s» %(adj_n)s TwoComms — это сочетание качественной ткани, насыщенной печати и продуманной посадки. Мы используем плотные полотна, поэтому принт не просвечивает, а сама одежда держит форму после десятков стирок. Обратите внимание на размерную сетку — %(cat)s в TwoComms идут в двух посадках: классическая и оверсайз.",
        "en": "In the %(cat)s category, %(adj_n)s TwoComms gear combines quality fabric, saturated print and a thought-through fit. We use heavy knits so the print doesn't show through, and the garment keeps its shape after dozens of washes. Check the size chart — TwoComms %(cat)s come in two cuts: classic and oversized.",
    },
    "Подивіться також %(adj_n)s <a href=\"/catalog/?color=%(slug)s\">в інших категоріях</a> або оберіть інший відтінок цієї ж категорії — <a href=\"/catalog/%(cat_slug)s/\">%(cat)s TwoComms</a>. Якщо потрібен конкретний принт — скористайтесь сторінкою <a href=\"/custom-print/\">«Власний принт»</a>: ми надрукуємо вашу ілюстрацію на обраній моделі від однієї одиниці.": {
        "ru": "Посмотрите также %(adj_n)s <a href=\"/catalog/?color=%(slug)s\">в других категориях</a> или выберите другой оттенок этой же категории — <a href=\"/catalog/%(cat_slug)s/\">%(cat)s TwoComms</a>. Если нужен конкретный принт — воспользуйтесь страницей <a href=\"/custom-print/\">«Собственный принт»</a>: мы напечатаем вашу иллюстрацию на выбранной модели от одной единицы.",
        "en": "Also see %(adj_n)s <a href=\"/catalog/?color=%(slug)s\">across the other categories</a> or pick a different shade in this category — <a href=\"/catalog/%(cat_slug)s/\">%(cat)s TwoComms</a>. Need a specific print? Use the <a href=\"/custom-print/\">Custom Print</a> page: we'll print your artwork on the chosen model from a single unit.",
    },

    # Generic fallback (uncurated colour) tone paragraph + chips.
    "Колір «%(label)s» у каталозі TwoComms — це вибір для тих, хто хоче відійти від базової палітри, але не готовий поступатися якістю принту. Ми друкуємо на цьому відтінку DTF-технологією з підвищеним базовим шаром, тому ілюстрація не «провалюється» у тон тканини й залишається насиченою після десятків прань.": {
        "ru": "Цвет «%(label)s» в каталоге TwoComms — это выбор для тех, кто хочет отойти от базовой палитры, но не готов уступать в качестве печати. Мы печатаем на этом оттенке по технологии DTF с усиленным базовым слоем, поэтому иллюстрация не «проваливается» в тон ткани и остаётся насыщенной после десятков стирок.",
        "en": "The %(label)s shade at TwoComms is the pick for anyone who wants to leave the basic palette behind without compromising on print quality. We print on this colour with DTF and a boosted base layer, so the artwork doesn't blend into the fabric and stays saturated after dozens of washes.",
    },
    "вибраний": {"ru": "выбранный", "en": "selected"},
    "Купити %(color)s худі":            {"ru": "Купить %(color)s худи",           "en": "Buy %(color)s hoodie"},
    "%(color)s футболка з принтом":     {"ru": "%(color)s футболка с принтом",    "en": "%(color)s t-shirt with print"},
    "%(color)s лонгслів":               {"ru": "%(color)s лонгслив",              "en": "%(color)s long sleeve"},
    "%(color)s стрітвір TwoComms":      {"ru": "%(color)s стритвир TwoComms",     "en": "%(color)s streetwear TwoComms"},
    "%(color)s одяг ЗСУ донат":         {"ru": "%(color)s одежда ВСУ донат",      "en": "%(color)s clothing AFU donation"},
    "%(color)s футболка з тризубом купити Україна": {
        "ru": "%(color)s футболка с тризубом купить Украина",
        "en": "%(color)s t-shirt with trident buy Ukraine",
    },
    "%(color)s худі з патріотичним принтом": {
        "ru": "%(color)s худи с патриотическим принтом",
        "en": "%(color)s hoodie with patriotic print",
    },

    # GENERAL_CATALOG_SEO_COPY chip labels.
    "Купити худі":                           {"ru": "Купить худи",                       "en": "Buy a hoodie"},
    "Купити футболку з принтом":             {"ru": "Купить футболку с принтом",         "en": "Buy a printed t-shirt"},
    "Купити лонгслів":                       {"ru": "Купить лонгслив",                   "en": "Buy a long sleeve"},
    "Український стрітвір":                  {"ru": "Украинский стритвир",               "en": "Ukrainian streetwear"},
    "Худі ЗСУ":                              {"ru": "Худи ВСУ",                          "en": "AFU hoodie"},
    "Чорна футболка з тризубом":             {"ru": "Чёрная футболка с тризубом",        "en": "Black t-shirt with trident"},
    "Кайотовий лонгслів":                    {"ru": "Койотовый лонгслив",                "en": "Coyote long sleeve"},
    "Оливкове худі мілітарі":                {"ru": "Оливковое худи милитари",           "en": "Olive military hoodie"},
    "Подарунок захиснику український бренд": {"ru": "Подарок защитнику украинский бренд","en": "Soldier gift Ukrainian brand"},
    "Худі з патріотичним принтом купити Київ": {
        "ru": "Худи с патриотическим принтом купить Киев",
        "en": "Hoodie with patriotic print buy Kyiv",
    },
    "Футболка ЗСУ донат на ЗСУ Україна": {
        "ru": "Футболка ВСУ донат на ВСУ Украина",
        "en": "AFU t-shirt donation to Armed Forces Ukraine",
    },
    "Власний принт на одязі від 1 одиниці": {
        "ru": "Собственный принт на одежде от 1 единицы",
        "en": "Custom print on clothing from 1 unit",
    },
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17I)


# ===========================================================================
# Phase 17j (2026-05-13) — contacts.html + product_detail.html wrappings.
# ===========================================================================
TRANSLATIONS_PHASE_17J: dict[str, dict[str, str]] = {
    # ----- contacts.html -----
    "Контакти": {"ru": "Контакты", "en": "Contacts"},
    "Зв'яжіться з TwoComms: телефон, Telegram, Instagram. Офіційні магазини та форма зворотного зв'язку.": {
        "ru": "Свяжитесь с TwoComms: телефон, Telegram, Instagram. Официальные магазины и форма обратной связи.",
        "en": "Get in touch with TwoComms: phone, Telegram, Instagram. Official stores and a contact form.",
    },
    "контакти TwoComms, телефон, telegram, instagram, зворотній зв’язок, магазини TwoComms": {
        "ru": "контакты TwoComms, телефон, telegram, instagram, обратная связь, магазины TwoComms",
        "en": "TwoComms contacts, phone, telegram, instagram, feedback, TwoComms stores",
    },
    "Зв'яжіться з TwoComms: телефон, Telegram, Instagram. Швидка відповідь на будь-які питання.": {
        "ru": "Свяжитесь с TwoComms: телефон, Telegram, Instagram. Быстрый ответ на любые вопросы.",
        "en": "Reach out to TwoComms: phone, Telegram, Instagram. Quick answers to any question.",
    },
    "Зв'яжіться з нами будь-яким зручним способом": {
        "ru": "Свяжитесь с нами любым удобным способом",
        "en": "Reach us in any way that suits you",
    },
    "Телефон": {"ru": "Телефон", "en": "Phone"},
    "Показати телефон": {"ru": "Показать телефон", "en": "Show phone"},
    "Наші магазини": {"ru": "Наши магазины", "en": "Our stores"},
    "Відвідайте наші партнерські магазини": {
        "ru": "Посетите наши партнёрские магазины",
        "en": "Visit our partner stores",
    },
    "Магазин №1": {"ru": "Магазин №1", "en": "Store #1"},
    "Магазин №2": {"ru": "Магазин №2", "en": "Store #2"},
    "вул. Хрещатик, 22<br>\n                Київ, 01001": {
        "ru": "ул. Крещатик, 22<br>\n                Киев, 01001",
        "en": "22 Khreshchatyk St.<br>\n                Kyiv, 01001",
    },
    "пр. Соборний, 15<br>\n                Львів, 79000": {
        "ru": "пр. Соборный, 15<br>\n                Львов, 79000",
        "en": "15 Sobornyi Ave.<br>\n                Lviv, 79000",
    },
    "Графік роботи:": {"ru": "График работы:", "en": "Working hours:"},
    "Пн-Нд: 10:00 - 22:00": {"ru": "Пн-Вс: 10:00 - 22:00", "en": "Mon-Sun: 10:00 - 22:00"},
    "Написати нам": {"ru": "Написать нам", "en": "Write to us"},
    "Маєте питання? Залиште повідомлення і ми зв'яжемося з вами": {
        "ru": "Есть вопрос? Оставьте сообщение, и мы свяжемся с вами",
        "en": "Got a question? Leave a message and we'll get back to you",
    },
    "Ім'я": {"ru": "Имя", "en": "Name"},
    "Тема": {"ru": "Тема", "en": "Subject"},
    "Повідомлення": {"ru": "Сообщение", "en": "Message"},
    "Надіслати повідомлення": {"ru": "Отправить сообщение", "en": "Send message"},

    # ----- product_detail.html: copy block -----
    "%(title)s — це не просто одяг. Це стан.": {
        "ru": "%(title)s — это не просто одежда. Это состояние.",
        "en": "%(title)s is more than apparel. It's a mindset.",
    },
    "Створений для тих, хто йде своїм шляхом, навіть коли все навколо летить під три чорти.": {
        "ru": "Создано для тех, кто идёт своим путём, даже когда всё вокруг рушится.",
        "en": "Made for those who walk their own path even when everything around falls apart.",
    },
    "Деталі": {"ru": "Детали", "en": "Details"},
    "Матеріал: 95% бавовна, 5% еластан — преміум якість, щільність 190 г/м²": {
        "ru": "Материал: 95% хлопок, 5% эластан — премиум-качество, плотность 190 г/м²",
        "en": "Fabric: 95% cotton, 5% elastane — premium quality, 190 g/m²",
    },
    "М'який та приємний до тіла, дихає і не сковує рухів": {
        "ru": "Мягкий и приятный к телу, дышит и не сковывает движений",
        "en": "Soft and pleasant on the skin, breathable and unrestrictive",
    },
    "Підсилені шви та еластичні манжети для довговічності": {
        "ru": "Усиленные швы и эластичные манжеты для долговечности",
        "en": "Reinforced seams and elastic cuffs for durability",
    },
    "Принт витримує багато прань — не тріскається та не вигорає": {
        "ru": "Принт выдерживает много стирок — не трескается и не выгорает",
        "en": "The print survives many washes — no cracking, no fading",
    },
    "Зроблено в Україні з любов'ю та увагою до деталей": {
        "ru": "Сделано в Украине с любовью и вниманием к деталям",
        "en": "Made in Ukraine with love and attention to detail",
    },
    "Кому підійде": {"ru": "Кому подойдёт", "en": "Who it suits"},
    "Показати більше": {"ru": "Показать больше", "en": "Show more"},
    "Згорнути": {"ru": "Свернуть", "en": "Collapse"},
    "Рекомендації по посадці": {"ru": "Рекомендации по посадке", "en": "Fit recommendations"},
    "Підбір розміру": {"ru": "Подбор размера", "en": "Size selection"},
    "Відкрийте загальну розмірну сітку TwoComms або напишіть нам для уточнення посадки.": {
        "ru": "Откройте общую размерную сетку TwoComms или напишите нам, чтобы уточнить посадку.",
        "en": "Open the general TwoComms size chart or write to us to clarify the fit.",
    },
    "Розмірна сітка": {"ru": "Размерная сетка", "en": "Size chart"},
    "Допомога з вибором": {"ru": "Помощь с выбором", "en": "Help with selection"},

    "Відправка": {"ru": "Отправка", "en": "Shipping"},
    "1–2 дні по Україні після підтвердження замовлення.": {
        "ru": "1–2 дня по Украине после подтверждения заказа.",
        "en": "1–2 days across Ukraine after the order is confirmed.",
    },
    "Оплата": {"ru": "Оплата", "en": "Payment"},
    "Онлайн або після узгодження деталей з менеджером.": {
        "ru": "Онлайн или после согласования деталей с менеджером.",
        "en": "Online, or after the manager confirms the details.",
    },
    "Обмін": {"ru": "Обмен", "en": "Exchange"},
    "14 днів на обмін розміру, якщо річ не була у використанні.": {
        "ru": "14 дней на обмен размера, если вещь не была в использовании.",
        "en": "14 days to exchange the size if the item hasn't been worn.",
    },

    "Прання": {"ru": "Стирка", "en": "Washing"},
    "30°C, делікатний режим, навиворіт.": {
        "ru": "30°C, деликатный режим, наизнанку.",
        "en": "30°C, delicate cycle, inside out.",
    },
    "Принт": {"ru": "Принт", "en": "Print"},
    "Не прасувати напряму по нанесенню.": {
        "ru": "Не гладить напрямую по принту.",
        "en": "Do not iron directly over the print.",
    },
    "Сушка": {"ru": "Сушка", "en": "Drying"},
    "Без агресивної машинної сушки.": {
        "ru": "Без агрессивной машинной сушки.",
        "en": "Avoid aggressive tumble drying.",
    },

    "Кількість": {"ru": "Количество", "en": "Quantity"},
    "Зменшити кількість": {"ru": "Уменьшить количество", "en": "Decrease quantity"},
    "Збільшити кількість": {"ru": "Увеличить количество", "en": "Increase quantity"},
    "ДОДАТИ В КОШИК": {"ru": "ДОБАВИТЬ В КОРЗИНУ", "en": "ADD TO CART"},

    "Швидка доставка": {"ru": "Быстрая доставка", "en": "Fast delivery"},
    "1-3 дні Новою Поштою": {"ru": "1-3 дня Новой Почтой", "en": "1-3 days via Nova Poshta"},
    "Обмін розміру": {"ru": "Обмен размера", "en": "Size exchange"},
    "14 днів на повернення": {"ru": "14 дней на возврат", "en": "14-day returns"},
    "Гайд по посадці": {"ru": "Гайд по посадке", "en": "Fit guide"},

    "Отримайте %(points)s балів за покупку цього товару": {
        "ru": "Получите %(points)s баллов за покупку этого товара",
        "en": "Earn %(points)s points for buying this product",
    },

    "Нещодавно переглядали": {"ru": "Недавно просмотренные", "en": "Recently viewed"},
    "До каталогу": {"ru": "В каталог", "en": "Back to catalog"},
    "— схожий товар TwoComms": {
        "ru": "— похожий товар TwoComms",
        "en": "— related TwoComms product",
    },
    "Додати %(title)s до обраних": {
        "ru": "Добавить %(title)s в избранное",
        "en": "Add %(title)s to favourites",
    },

    # Phase 19c FAQ tab label.
    "FAQ товару": {"ru": "FAQ товара", "en": "Product FAQ"},

    # Recurring tiny labels.
    "Швидка відправка": {"ru": "Быстрая отправка", "en": "Fast shipping"},
    "1-2 дні по Україні": {"ru": "1-2 дня по Украине", "en": "1-2 days across Ukraine"},
    "Індивідуальний підхід": {"ru": "Индивидуальный подход", "en": "Personal approach"},
    "Допоможемо з вибором": {"ru": "Поможем с выбором", "en": "We'll help you choose"},
    "14 днів на обмін": {"ru": "14 дней на обмен", "en": "14 days to exchange"},
    "Галерея товару": {"ru": "Галерея товара", "en": "Product gallery"},
    "Дії з фото": {"ru": "Действия с фото", "en": "Photo actions"},
    "Збільшити фото": {"ru": "Увеличить фото", "en": "Zoom photo"},
    "Попереднє фото": {"ru": "Предыдущее фото", "en": "Previous photo"},
    "Наступне фото": {"ru": "Следующее фото", "en": "Next photo"},
    "Мініатюри товару": {"ru": "Миниатюры товара", "en": "Product thumbnails"},
    "Переваги покупки": {"ru": "Преимущества покупки", "en": "Purchase benefits"},
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17J)


# ===========================================================================
# Phase 17k (2026-05-13) — cart.html wrappings (custom + regular items,
# delivery form, summary block, consultation modal).
# ===========================================================================
TRANSLATIONS_PHASE_17K: dict[str, dict[str, str]] = {
    "Кошик": {"ru": "Корзина", "en": "Cart"},
    "Ваші вибрані товари": {"ru": "Ваши выбранные товары", "en": "Your selected items"},
    "Очистити": {"ru": "Очистить", "en": "Clear"},
    "Кастомний друк": {"ru": "Кастомная печать", "en": "Custom print"},
    "Передаємо менеджеру на перевірку": {
        "ru": "Передаём менеджеру на проверку",
        "en": "Sending to the manager for review",
    },
    "На перевірці менеджера": {"ru": "На проверке менеджера", "en": "Under manager review"},
    "✅ Погоджено — можна оплачувати": {
        "ru": "✅ Согласовано — можно оплачивать",
        "en": "✅ Approved — ready for payment",
    },
    "❌ Відхилено менеджером": {
        "ru": "❌ Отклонено менеджером",
        "en": "❌ Rejected by the manager",
    },
    "Коментар менеджера: %(note)s": {
        "ru": "Комментарий менеджера: %(note)s",
        "en": "Manager note: %(note)s",
    },
    "Виріб:": {"ru": "Изделие:", "en": "Item:"},
    "Розміщення:": {"ru": "Размещение:", "en": "Placement:"},
    "Кількість:": {"ru": "Количество:", "en": "Quantity:"},
    "Режим розмірів:": {"ru": "Режим размеров:", "en": "Size mode:"},
    "Розміри:": {"ru": "Размеры:", "en": "Sizes:"},
    "Крій:": {"ru": "Крой:", "en": "Cut:"},
    "Тканина:": {"ru": "Ткань:", "en": "Fabric:"},
    "Колір:": {"ru": "Цвет:", "en": "Colour:"},
    "Послуга:": {"ru": "Услуга:", "en": "Service:"},
    "Підготовка файлу:": {"ru": "Подготовка файла:", "en": "File preparation:"},
    "Додатково:": {"ru": "Дополнительно:", "en": "Add-ons:"},
    "Коментар до розміщення:": {"ru": "Комментарий к размещению:", "en": "Placement note:"},
    "🎁 Подарунок:": {"ru": "🎁 Подарок:", "en": "🎁 Gift:"},
    "Упаковка + промокод 10%": {"ru": "Упаковка + промокод 10%", "en": "Packaging + 10% promo code"},
    "B2B знижка:": {"ru": "B2B скидка:", "en": "B2B discount:"},
    "грн/шт": {"ru": "грн/шт", "en": "UAH/pc"},
    "Разом:": {"ru": "Итого:", "en": "Total:"},
    "Після погодження": {"ru": "После согласования", "en": "After approval"},
    "Орієнтовно %(amount)s грн": {
        "ru": "Ориентировочно %(amount)s грн",
        "en": "Approximately %(amount)s UAH",
    },
    "Написати менеджеру": {"ru": "Написать менеджеру", "en": "Message the manager"},
    "Видалити": {"ru": "Удалить", "en": "Remove"},
    "%(title)s - товар у кошику TwoComms": {
        "ru": "%(title)s — товар в корзине TwoComms",
        "en": "%(title)s — item in the TwoComms cart",
    },

    "Розмір:": {"ru": "Размер:", "en": "Size:"},
    "Посадка:": {"ru": "Посадка:", "en": "Fit:"},
    "Ціна:": {"ru": "Цена:", "en": "Price:"},
    "грн": {"ru": "грн", "en": "UAH"},

    # Empty cart.
    "Кошик порожній": {"ru": "Корзина пуста", "en": "Cart is empty"},
    "Додайте товари до кошика, щоб зробити замовлення": {
        "ru": "Добавьте товары в корзину, чтобы оформить заказ",
        "en": "Add items to the cart to place an order",
    },
    "Перейти до покупок": {"ru": "Перейти к покупкам", "en": "Start shopping"},

    # Sidebar / delivery form.
    "Доставка": {"ru": "Доставка", "en": "Delivery"},
    "Авторизація": {"ru": "Авторизация", "en": "Sign in"},
    "Щоб не вводити дані кожного разу — авторизуйтесь і накопичуйте бали.": {
        "ru": "Чтобы не вводить данные каждый раз — авторизуйтесь и копите баллы.",
        "en": "Sign in so you don't have to re-enter data every time — and earn points.",
    },
    "ПІБ": {"ru": "ФИО", "en": "Full name"},
    "ПІБ *": {"ru": "ФИО *", "en": "Full name *"},
    "Прізвище Ім'я По батькові": {
        "ru": "Фамилия Имя Отчество",
        "en": "Last name First name Patronymic",
    },
    "Прізвище Імʼя По батькові": {
        "ru": "Фамилия Имя Отчество",
        "en": "Last name First name Patronymic",
    },
    "Телефон *": {"ru": "Телефон *", "en": "Phone *"},
    "0931234567": {"ru": "0931234567", "en": "0931234567"},
    "Можна вводити 093..., 809..., 380... або +380... — номер приведемо до потрібного формату.": {
        "ru": "Можно вводить 093..., 809..., 380... или +380... — номер приведём к нужному формату.",
        "en": "You can type 093…, 809…, 380… or +380… — we'll normalise the format.",
    },
    "Місто *": {"ru": "Город *", "en": "City *"},
    "Місто": {"ru": "Город", "en": "City"},
    "Почніть вводити місто Нової пошти": {
        "ru": "Начните вводить город Новой почты",
        "en": "Start typing a Nova Poshta city",
    },
    "Почніть вводити назву міста і виберіть підтверджений варіант зі списку Нової пошти.": {
        "ru": "Начните вводить название города и выберите подтверждённый вариант из списка Новой почты.",
        "en": "Start typing the city name and pick a confirmed option from the Nova Poshta list.",
    },
    "Відділення / Поштомат НП *": {
        "ru": "Отделение / Почтомат НП *",
        "en": "Branch / Nova Poshta parcel locker *",
    },
    "Відділення / Поштомат НП": {
        "ru": "Отделение / Почтомат НП",
        "en": "Branch / Nova Poshta parcel locker",
    },
    "Оберіть відділення або поштомат": {
        "ru": "Выберите отделение или почтомат",
        "en": "Choose a branch or parcel locker",
    },
    "Усі пункти": {"ru": "Все пункты", "en": "All points"},
    "Відділення": {"ru": "Отделение", "en": "Branch"},
    "Поштомат": {"ru": "Почтомат", "en": "Parcel locker"},
    "Після вибору міста почніть вводити номер або адресу і виберіть відділення чи поштомат зі списку Нової пошти.": {
        "ru": "После выбора города начните вводить номер или адрес и выберите отделение или почтомат из списка Новой почты.",
        "en": "After picking a city, start typing the number or address and select a branch or parcel locker from the Nova Poshta list.",
    },
    "Тип оплати *": {"ru": "Тип оплаты *", "en": "Payment method *"},
    "Тип оплати": {"ru": "Тип оплаты", "en": "Payment method"},
    "Оберіть тип оплати": {"ru": "Выберите тип оплаты", "en": "Choose a payment method"},
    "Онлайн оплата (повна сума)": {
        "ru": "Онлайн-оплата (полная сумма)",
        "en": "Online payment (full amount)",
    },
    "Передплата 200 грн (решта при отриманні)": {
        "ru": "Предоплата 200 грн (остаток при получении)",
        "en": "200 UAH prepayment (rest on delivery)",
    },
    "— недоступно з кастомним принтом": {
        "ru": "— недоступно с кастомным принтом",
        "en": "— unavailable with a custom print",
    },
    "недоступно з кастомним принтом": {
        "ru": "недоступно с кастомным принтом",
        "en": "unavailable with a custom print",
    },
    "У кошику є кастомний принт, тому передплата 200 грн тимчасово недоступна — менеджер узгодить фінальну ціну.": {
        "ru": "В корзине есть кастомный принт, поэтому предоплата 200 грн временно недоступна — менеджер согласует финальную цену.",
        "en": "Your cart contains a custom print, so the 200 UAH prepayment is temporarily disabled — the manager will confirm the final price.",
    },
    "Потрібна консультація?": {"ru": "Нужна консультация?", "en": "Need a consultation?"},
    "Зберегти зміни": {"ru": "Сохранить изменения", "en": "Save changes"},
    "Email": {"ru": "Email", "en": "Email"},
    "your@email.com": {"ru": "your@email.com", "en": "your@email.com"},

    # Promocode + summary.
    "Промокод": {"ru": "Промокод", "en": "Promo code"},
    "Застосовано": {"ru": "Применено", "en": "Applied"},
    "Знижка:": {"ru": "Скидка:", "en": "Discount:"},
    "Введіть код промокоду": {"ru": "Введите код промокода", "en": "Enter the promo code"},
    "Оформлення замовлення": {"ru": "Оформление заказа", "en": "Checkout"},
    "Підсумок вашого замовлення": {
        "ru": "Итог вашего заказа",
        "en": "Your order summary",
    },
    "Товари": {"ru": "Товары", "en": "Items"},
    "Орієнтовно за кастомний друк": {
        "ru": "Ориентировочно за кастомную печать",
        "en": "Approximate cost for custom print",
    },
    "Знижка магазину": {"ru": "Скидка магазина", "en": "Store discount"},
    "Знижка промокоду": {"ru": "Скидка по промокоду", "en": "Promo code discount"},
    "До сплати:": {"ru": "К оплате:", "en": "To pay:"},
    "Залишок при отриманні:": {"ru": "Остаток при получении:", "en": "Balance on delivery:"},
    "Це додаткова передоплата для вас — решту сплатите при отриманні.": {
        "ru": "Это дополнительная предоплата для вас — остальное оплатите при получении.",
        "en": "This is an additional prepayment — pay the remainder upon delivery.",
    },
    "Знижка від TwoComms": {"ru": "Скидка от TwoComms", "en": "TwoComms discount"},
    "Разом ви економите": {"ru": "Всего вы экономите", "en": "You save in total"},
    "Бали за замовлення": {"ru": "Баллы за заказ", "en": "Points for the order"},
    "буде нараховано після отримання товару": {
        "ru": "будут начислены после получения товара",
        "en": "will be credited after the item is received",
    },
    "Бали не нараховуються": {"ru": "Баллы не начисляются", "en": "No points awarded"},
    "Для отримання балів потрібно авторизуватися": {
        "ru": "Чтобы получать баллы, нужно авторизоваться",
        "en": "Sign in to earn points",
    },
    "Авторизуватися": {"ru": "Авторизоваться", "en": "Sign in"},
    "Доставка оплачується згідно тарифів перевізника.": {
        "ru": "Доставка оплачивается по тарифам перевозчика.",
        "en": "Delivery is paid according to the carrier's tariff.",
    },

    # CTA buttons + checkout notes.
    "Онлайн оплата карткою": {
        "ru": "Онлайн-оплата картой",
        "en": "Online card payment",
    },
    "(без урахування кастомного одягу)": {
        "ru": "(без учёта кастомной одежды)",
        "en": "(excluding custom apparel)",
    },
    "Pay": {"ru": "Pay", "en": "Pay"},
    "Оформити замовлення": {"ru": "Оформить заказ", "en": "Place order"},
    "Замовити як гість": {"ru": "Заказать как гость", "en": "Order as a guest"},
    "Оплата стане доступною після погодження менеджером. Зараз у кошику немає позицій, які можна оплатити окремо.": {
        "ru": "Оплата станет доступной после согласования менеджером. Сейчас в корзине нет позиций, которые можно оплатить отдельно.",
        "en": "Payment will be available after the manager approves it. Right now the cart has no items that can be paid separately.",
    },
    "Оплата покриє лише звичайні товари. Кастомна позиція приєднається до замовлення автоматично після погодження менеджером — на ваш номер надійде додаткове повідомлення з фінальною ціною.": {
        "ru": "Оплата покроет только обычные товары. Кастомная позиция автоматически присоединится к заказу после согласования менеджером — на ваш номер придёт дополнительное сообщение с финальной ценой.",
        "en": "The payment will cover regular items only. The custom item will join the order automatically once the manager approves it — you'll get a follow-up message with the final price.",
    },
    "Після оформлення замовлення менеджер звʼяжеться з вами за вказаним номером телефону, щоб підтвердити деталі та узгодити доставку.": {
        "ru": "После оформления заказа менеджер свяжется с вами по указанному номеру телефона, чтобы подтвердить детали и согласовать доставку.",
        "en": "After you place the order, the manager will reach out via the phone number you provided to confirm details and arrange delivery.",
    },
    "Менеджер погодив усі кастомні позиції. Тепер можна оплатити все замовлення разом.": {
        "ru": "Менеджер согласовал все кастомные позиции. Теперь можно оплатить весь заказ сразу.",
        "en": "The manager has approved all custom items. You can now pay for the whole order at once.",
    },
    "Кастомний одяг уже переданий менеджеру на перевірку. Поки триває модерація, позиція відображається в кошику окремо і <strong>не входить до суми оплати зараз</strong>. Щойно менеджер погодить деталі та фінальну ціну — кастом автоматично приєднається до рахунку.": {
        "ru": "Кастомная одежда уже передана менеджеру на проверку. Пока идёт модерация, позиция отображается в корзине отдельно и <strong>не входит в сумму оплаты сейчас</strong>. Как только менеджер согласует детали и финальную цену — кастом автоматически присоединится к счёту.",
        "en": "The custom apparel has been forwarded to the manager for review. During moderation it stays in the cart separately and <strong>is not included in the payable amount yet</strong>. As soon as the manager confirms the details and the final price, the custom item will join the invoice automatically.",
    },
    "Написати менеджеру в Telegram": {
        "ru": "Написать менеджеру в Telegram",
        "en": "Message the manager on Telegram",
    },
    "Менеджер відхилив попередню заявку. Перегляньте деталі вище та надішліть її повторно, або ж оформіть замовлення <strong>без кастомних позицій</strong>.": {
        "ru": "Менеджер отклонил предыдущую заявку. Просмотрите детали выше и отправьте её повторно, либо оформите заказ <strong>без кастомных позиций</strong>.",
        "en": "The manager has rejected the previous request. Review the details above and resubmit it, or place the order <strong>without the custom items</strong>.",
    },
    "Менеджер уже працює з вашим кастомним запитом. Коли модерацію буде завершено, позиція автоматично стане доступною для оплати в цьому ж кошику.": {
        "ru": "Менеджер уже работает с вашим кастомным запросом. Когда модерация завершится, позиция автоматически станет доступна для оплаты в этой же корзине.",
        "en": "The manager is already handling your custom request. Once moderation is complete, the item will be ready for payment in the same cart.",
    },

    # Consultation modal.
    "Безкоштовна консультація": {
        "ru": "Бесплатная консультация",
        "en": "Free consultation",
    },
    "Закрити": {"ru": "Закрыть", "en": "Close"},
    "Залиште свої контактні дані, і наш менеджер звʼяжеться з вами найближчим часом для консультації": {
        "ru": "Оставьте свои контактные данные, и наш менеджер свяжется с вами в ближайшее время для консультации",
        "en": "Leave your contact details and our manager will reach out shortly for a consultation",
    },
    "Введіть ваше ПІБ": {"ru": "Введите ваше ФИО", "en": "Enter your full name"},
    "@username або номер телефону": {
        "ru": "@username или номер телефона",
        "en": "@username or phone number",
    },
    "Опціонально, але допоможе зв'язатися швидше": {
        "ru": "Опционально, но поможет связаться быстрее",
        "en": "Optional, but helps us reach you faster",
    },
    "Опціонально": {"ru": "Опционально", "en": "Optional"},
    "Зв'язатися зі мною": {"ru": "Связаться со мной", "en": "Get in touch with me"},
    "Заробите %(n)s бал": {"ru": "Заработаете %(n)s балл", "en": "You'll earn %(n)s point"},
    "Заробите %(n)s балів": {"ru": "Заработаете %(n)s баллов", "en": "You'll earn %(n)s points"},
    "%(counter)s бал": {"ru": "%(counter)s балл", "en": "%(counter)s point"},
    "%(counter)s балів": {"ru": "%(counter)s баллов", "en": "%(counter)s points"},
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17K)


# ===========================================================================
# Phase 17l (2026-05-13) — my_orders.html wrappings (order card, payment
# modal, status labels).
# ===========================================================================
TRANSLATIONS_PHASE_17L: dict[str, dict[str, str]] = {
    "Мої замовлення": {"ru": "Мои заказы", "en": "My orders"},
    "Відстежуйте статус ваших замовлень": {
        "ru": "Отслеживайте статус ваших заказов",
        "en": "Track the status of your orders",
    },
    "Отримано": {"ru": "Получено", "en": "Received"},
    "Очікується": {"ru": "Ожидается", "en": "Pending"},
    "Оформлено": {"ru": "Оформлено", "en": "Placed"},
    "Важливо": {"ru": "Важно", "en": "Important"},
    "Ваше замовлення в обробці": {
        "ru": "Ваш заказ в обработке",
        "en": "Your order is being processed",
    },
    "Очікуйте: з вами зв'яжеться менеджер для підтвердження деталей. Або ви можете <strong>оплатити прямо зараз</strong> — так ми відправимо швидше.": {
        "ru": "Ожидайте: с вами свяжется менеджер для подтверждения деталей. Или вы можете <strong>оплатить прямо сейчас</strong> — так мы отправим быстрее.",
        "en": "Please wait: a manager will reach out to confirm the details. Or you can <strong>pay right now</strong> so we can ship faster.",
    },
    "ТТН очікується": {"ru": "ТТН ожидается", "en": "Tracking number pending"},
    "Статус посилки: %(status)s": {
        "ru": "Статус посылки: %(status)s",
        "en": "Parcel status: %(status)s",
    },
    "Отримувати сповіщення": {"ru": "Получать уведомления", "en": "Receive notifications"},
    "В обробці": {"ru": "В обработке", "en": "Processing"},
    "Готується": {"ru": "Готовится", "en": "Preparing"},
    "Відправлено": {"ru": "Отправлено", "en": "Shipped"},
    "Товари в замовленні": {"ru": "Товары в заказе", "en": "Order items"},
    "шт": {"ru": "шт", "en": "pcs"},
    "Тип:": {"ru": "Тип:", "en": "Type:"},
    "Кастомний виріб %(number)s": {
        "ru": "Кастомное изделие %(number)s",
        "en": "Custom item %(number)s",
    },
    "Статус:": {"ru": "Статус:", "en": "Status:"},
    "Погоджено менеджером": {"ru": "Согласовано менеджером", "en": "Approved by the manager"},
    "Кастомне замовлення": {"ru": "Кастомный заказ", "en": "Custom order"},
    "Передплата 200 грн": {"ru": "Предоплата 200 грн", "en": "200 UAH prepayment"},
    "Оплата при отриманні": {"ru": "Оплата при получении", "en": "Payment on delivery"},
    "Змінити спосіб оплати": {"ru": "Изменить способ оплаты", "en": "Change payment method"},
    "Оплачено повністю": {"ru": "Оплачено полностью", "en": "Paid in full"},
    "Внесена передплата": {"ru": "Внесена предоплата", "en": "Prepayment received"},
    "На перевірці": {"ru": "На проверке", "en": "Under review"},
    "Не оплачено": {"ru": "Не оплачено", "en": "Not paid"},
    "Часткова оплата": {"ru": "Частичная оплата", "en": "Partial payment"},
    "Повна оплата": {"ru": "Полная оплата", "en": "Full payment"},
    "Зберегти": {"ru": "Сохранить", "en": "Save"},
    "Внести передплату": {"ru": "Внести предоплату", "en": "Submit prepayment"},
    "Оплатити повністю": {"ru": "Оплатить полностью", "en": "Pay in full"},

    "У вас ще немає замовлень": {
        "ru": "У вас ещё нет заказов",
        "en": "You don't have any orders yet",
    },
    "Зробіть перше замовлення, щоб побачити його тут": {
        "ru": "Сделайте первый заказ, чтобы увидеть его здесь",
        "en": "Place your first order to see it here",
    },

    "Внесення передплати": {"ru": "Внесение предоплаты", "en": "Submit prepayment"},
    "Інструкція по внесенню передплати": {
        "ru": "Инструкция по внесению предоплаты",
        "en": "Prepayment instructions",
    },
    "Передплата у розмірі <strong>200 грн</strong> необхідна для забезпечення безпеки компанії у випадку, якщо посилка буде відправлена, а користувач або не забере її, або розмір не підійде.": {
        "ru": "Предоплата в размере <strong>200 грн</strong> необходима для обеспечения безопасности компании на случай, если посылка будет отправлена, а пользователь её не заберёт или размер не подойдёт.",
        "en": "A <strong>200 UAH prepayment</strong> protects the company in case the parcel ships but the customer doesn't pick it up or the size doesn't fit.",
    },
    "Номер замовлення:": {"ru": "Номер заказа:", "en": "Order number:"},
    "Реквізити для оплати:": {"ru": "Реквизиты для оплаты:", "en": "Payment details:"},
    "Отримувач:": {"ru": "Получатель:", "en": "Recipient:"},
    "ІПН/ЄДРПОУ:": {"ru": "ИНН/ЕГРПОУ:", "en": "Tax ID:"},
    "Призначення платежу:": {"ru": "Назначение платежа:", "en": "Payment purpose:"},
    "АБО": {"ru": "ИЛИ", "en": "OR"},
    "Карта Монобанк:": {"ru": "Карта Монобанк:", "en": "Monobank card:"},
    "Завантажте скріншот оплати:": {
        "ru": "Загрузите скриншот оплаты:",
        "en": "Upload a payment screenshot:",
    },
    "Скріншот необхідний для забезпечення гарантій та підтвердження оплати. Будь ласка, зробіть знімок екрану з підтвердженням транзакції.": {
        "ru": "Скриншот необходим для обеспечения гарантий и подтверждения оплаты. Пожалуйста, сделайте снимок экрана с подтверждением транзакции.",
        "en": "A screenshot ensures guarantees and confirms the payment. Please take a screenshot of the transaction confirmation.",
    },
    "Натисніть для завантаження скріншоту": {
        "ru": "Нажмите, чтобы загрузить скриншот",
        "en": "Click to upload a screenshot",
    },
    "PNG, JPG або JPEG (макс. 5MB)": {
        "ru": "PNG, JPG или JPEG (макс. 5 МБ)",
        "en": "PNG, JPG or JPEG (max. 5 MB)",
    },
    "Оплатив": {"ru": "Оплатил", "en": "I have paid"},
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17L)


# ===========================================================================
# Phase 17m (2026-05-13) — order_success.html translations (status, points,
# Instagram review, email receipt, action buttons).
# ===========================================================================
TRANSLATIONS_PHASE_17M: dict[str, dict[str, str]] = {
    "Замовлення оформлено": {"ru": "Заказ оформлен", "en": "Order placed"},
    "Замовлення успішно оформлено!": {
        "ru": "Заказ успешно оформлен!",
        "en": "Order placed successfully!",
    },
    "Переглянути деталі замовлення": {
        "ru": "Посмотреть детали заказа",
        "en": "View order details",
    },
    "Оплата успішно пройшла!": {
        "ru": "Оплата успешно прошла!",
        "en": "Payment successful!",
    },
    "Дякуємо, брат! Твоє замовлення оплачено та відправлено в обробку. Ми розпочнемо підготовку найближчим часом.": {
        "ru": "Спасибо, бро! Твой заказ оплачен и отправлен в обработку. Мы начнём подготовку в ближайшее время.",
        "en": "Thanks, bro! Your order is paid and queued for processing. We'll start preparing it shortly.",
    },
    "Дякуємо за покупку! Твоє замовлення оплачено через Monobank та відправлено в обробку. Ми розпочнемо підготовку найближчим часом.": {
        "ru": "Спасибо за покупку! Ваш заказ оплачен через Monobank и отправлен в обработку. Мы начнём подготовку в ближайшее время.",
        "en": "Thanks for your purchase! Your order has been paid via Monobank and is queued for processing. We'll start preparing it shortly.",
    },
    "Передплата внесена!": {"ru": "Предоплата внесена!", "en": "Prepayment received!"},
    "Дякуємо, брат! Передплата внесена. Залишок оплатиш при отриманні на Новій Пошті. Ми розпочнемо підготовку та відправимо на вказане відділення.": {
        "ru": "Спасибо, бро! Предоплата внесена. Остаток оплатишь при получении на Новой Почте. Мы начнём подготовку и отправим в указанное отделение.",
        "en": "Thanks, bro! Prepayment received. You'll pay the balance when picking up the parcel at Nova Poshta. We'll start preparing and ship to the chosen branch.",
    },
    "Дякуємо за покупку! Передплата внесена. Залишок необхідно оплатити при отриманні посилки на відділенні Нової Пошти. Ми розпочнемо підготовку та відправимо на вказане відділення.": {
        "ru": "Спасибо за покупку! Предоплата внесена. Остаток нужно оплатить при получении посылки в отделении Новой Почты. Мы начнём подготовку и отправим в указанное отделение.",
        "en": "Thanks for your purchase! Prepayment received. The balance must be paid when picking up the parcel at the Nova Poshta branch. We'll start preparing and ship to the chosen branch.",
    },
    "Ваша заявка відправлена в обробку": {
        "ru": "Ваша заявка отправлена в обработку",
        "en": "Your order is being processed",
    },
    "Дякуємо, брат! Замовлення відправлено в обробку. Статус надійде в Telegram.": {
        "ru": "Спасибо, бро! Заказ отправлен в обработку. Статус придёт в Telegram.",
        "en": "Thanks, bro! Your order is being processed. The status will arrive in Telegram.",
    },
    "Дякуємо за замовлення! Заявка відправлена в обробку.": {
        "ru": "Спасибо за заказ! Заявка отправлена в обработку.",
        "en": "Thank you for your order! It has been sent for processing.",
    },
    "Внесена передплата:": {"ru": "Внесена предоплата:", "en": "Prepayment received:"},
    "Залишок (при отриманні):": {
        "ru": "Остаток (при получении):",
        "en": "Balance (on delivery):",
    },
    "Дякуємо за вибір стилю TwoComms!": {
        "ru": "Спасибо за выбор стиля TwoComms!",
        "en": "Thanks for choosing the TwoComms style!",
    },
    "Твій стиль — твоя сильна сторона. Ми цінуємо, що ти обрав саме нас для свого образу.": {
        "ru": "Твой стиль — твоя сильная сторона. Мы ценим, что ты выбрал именно нас для своего образа.",
        "en": "Your style is your strength. We appreciate that you chose us for your look.",
    },
    "Є питання чи потрібна допомога?": {
        "ru": "Есть вопросы или нужна помощь?",
        "en": "Got questions or need help?",
    },
    "Написати в Telegram": {"ru": "Написать в Telegram", "en": "Message us on Telegram"},
    "Демо режим": {"ru": "Демо-режим", "en": "Demo mode"},
    "Це тестовий перегляд сторінки. Для реального замовлення дані будуть заповнені автоматично.": {
        "ru": "Это тестовый просмотр страницы. Для реального заказа данные будут заполнены автоматически.",
        "en": "This is a test preview of the page. For a real order, the fields will be populated automatically.",
    },
    "Деталі замовлення": {"ru": "Детали заказа", "en": "Order details"},
    "Деталі замовлення (демо)": {
        "ru": "Детали заказа (демо)",
        "en": "Order details (demo)",
    },
    "Не вказано": {"ru": "Не указано", "en": "Not specified"},
    "Відділення НП": {"ru": "Отделение НП", "en": "Nova Poshta branch"},
    "Сума замовлення": {"ru": "Сумма заказа", "en": "Order total"},
    "Товари в замовленні:": {"ru": "Товары в заказе:", "en": "Order items:"},
    "Статус: погоджено": {"ru": "Статус: согласовано", "en": "Status: approved"},
    "Система балів": {"ru": "Система баллов", "en": "Points system"},
    "Дякуємо, брат! Твоє замовлення оформлено. Бали будуть нараховані після отримання товару.": {
        "ru": "Спасибо, бро! Твой заказ оформлен. Баллы будут начислены после получения товара.",
        "en": "Thanks, bro! Your order is placed. Points will be awarded once you receive the parcel.",
    },
    "Статус замовлення приходить в Telegram": {
        "ru": "Статус заказа приходит в Telegram",
        "en": "Order status arrives via Telegram",
    },
    "Оновлення про твоє замовлення надсилаються автоматично": {
        "ru": "Обновления о твоём заказе отправляются автоматически",
        "en": "Updates about your order are sent automatically",
    },
    "Створіть акаунт або увійдіть в систему, щоб отримувати бали за покупки!": {
        "ru": "Создайте аккаунт или войдите в систему, чтобы получать баллы за покупки!",
        "en": "Create an account or sign in to earn points for your purchases!",
    },
    "Накопичувати бали за покупки": {
        "ru": "Накапливать баллы за покупки",
        "en": "Accumulate points for purchases",
    },
    "Відстежувати статус замовлень": {
        "ru": "Отслеживать статус заказов",
        "en": "Track the status of your orders",
    },
    "Зберігати історію покупок": {
        "ru": "Сохранять историю покупок",
        "en": "Keep your purchase history",
    },
    "Отримувати персональні пропозиції": {
        "ru": "Получать персональные предложения",
        "en": "Get personalised offers",
    },
    "Увійти": {"ru": "Войти", "en": "Sign in"},
    "Зареєструватись": {"ru": "Зарегистрироваться", "en": "Sign up"},
    "Якщо ви зареєструєтесь, це замовлення автоматично привʼяжеться до вашого акаунту": {
        "ru": "Если вы зарегистрируетесь, этот заказ автоматически привяжется к вашему аккаунту",
        "en": "If you sign up, this order will be linked to your account automatically",
    },
    "Отримати чек на email": {"ru": "Получить чек на email", "en": "Receive receipt by email"},
    "Введіть вашу email адресу, і ми надішлемо чек про оплату": {
        "ru": "Введите ваш email, и мы пришлём чек об оплате",
        "en": "Enter your email and we'll send you the payment receipt",
    },
    "Відправити чек": {"ru": "Отправить чек", "en": "Send receipt"},
    "Будь ласка, введіть коректну email адресу": {
        "ru": "Пожалуйста, введите корректный email",
        "en": "Please enter a valid email address",
    },
    "Відправляється...": {"ru": "Отправляется...", "en": "Sending..."},
    "Чек буде відправлено на": {
        "ru": "Чек будет отправлен на",
        "en": "The receipt will be sent to",
    },
    "Функціонал буде додано найближчим часом.": {
        "ru": "Функционал будет добавлен в ближайшее время.",
        "en": "This feature is coming soon.",
    },
    "Залиште відгук в Instagram": {
        "ru": "Оставьте отзыв в Instagram",
        "en": "Leave a review on Instagram",
    },
    "Поділіться своїми враженнями про покупку! Залиште відгук в Instagram з відміткою нашої сторінки, і ми даруємо вам промокод на знижку 10% на наступне замовлення!": {
        "ru": "Поделитесь впечатлениями о покупке! Оставьте отзыв в Instagram с отметкой нашей страницы, и мы подарим вам промокод на скидку 10% на следующий заказ!",
        "en": "Share your purchase experience! Leave a review on Instagram tagging our page, and we'll gift you a 10% discount promo code for your next order!",
    },
    "Ваш промокод на знижку": {"ru": "Ваш промокод на скидку", "en": "Your discount promo code"},
    "10% знижка на наступне замовлення": {
        "ru": "10% скидка на следующий заказ",
        "en": "10% off your next order",
    },
    "Відкрити Instagram": {"ru": "Открыть Instagram", "en": "Open Instagram"},
    "Повернутись на головну": {"ru": "Вернуться на главную", "en": "Back to home"},
    "Переглянути каталог": {"ru": "Посмотреть каталог", "en": "Browse catalogue"},
    "Відділення НП": {"ru": "Отделение НП", "en": "Nova Poshta branch"},
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17M)


# Phase 17o — index.html meta keywords, structured data, decorative alt texts,
# pagination suffix, dynamic category icon alt (blocktrans).
TRANSLATIONS_PHASE_17O: dict[str, dict[str, str]] = {
    # Pagination suffix in <title>
    "сторінка %(n)s": {
        "ru": "страница %(n)s",
        "en": "page %(n)s",
    },
    # Meta keywords
    "стріт одяг, мілітарі одяг, футболки, худі, лонгсліви, TwoComms, ексклюзивний дизайн, якісний одяг, одяг з характером, стріт стиль, мілітарі стиль": {
        "ru": "стрит одежда, милитари одежда, футболки, худи, лонгсливы, TwoComms, эксклюзивный дизайн, качественная одежда, одежда с характером, стрит стиль, милитари стиль",
        "en": "streetwear, military style apparel, t-shirts, hoodies, longsleeves, TwoComms, exclusive design, quality clothing, statement apparel, street style, military style",
    },
    # Home page structured data
    "TwoComms — Головна": {
        "ru": "TwoComms — Главная",
        "en": "TwoComms — Home",
    },
    "Головна сторінка бренду TwoComms з каталогом категорій, кастомним друком і актуальним асортиментом.": {
        "ru": "Главная страница бренда TwoComms с каталогом категорий, кастомной печатью и актуальным ассортиментом.",
        "en": "TwoComms brand home page featuring category catalogue, custom print and the current line-up.",
    },
    # Decorative alt texts on the home page
    "TwoComms логотип": {
        "ru": "Логотип TwoComms",
        "en": "TwoComms logo",
    },
    "TwoComms логотип — декоративний": {
        "ru": "Логотип TwoComms — декоративный",
        "en": "TwoComms logo — decorative",
    },
    # Dynamic blocktrans for category icon alt
    "%(name)s категорія — TwoComms": {
        "ru": "Категория %(name)s — TwoComms",
        "en": "%(name)s category — TwoComms",
    },
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17O)


# Phase 17p — points info modal (base.html + product_points_modal partial),
# contacts.html structured data, expanded SEO copy in catalog/index/PDP.
TRANSLATIONS_PHASE_17P: dict[str, dict[str, str]] = {
    # ===== base.html / partials/product_points_modal points modal =====
    "Як працюють бали за покупки": {
        "ru": "Как работают баллы за покупки",
        "en": "How points work on purchases",
    },
    "Як працюють бали?": {
        "ru": "Как работают баллы?",
        "en": "How do points work?",
    },
    "балів за цей товар": {
        "ru": "баллов за этот товар",
        "en": "points for this item",
    },
    "Назва товару": {"ru": "Название товара", "en": "Product name"},
    "Нарахування балів:": {"ru": "Начисление баллов:", "en": "Earning points:"},
    "Бали нараховуються автоматично після підтвердження замовлення": {
        "ru": "Баллы начисляются автоматически после подтверждения заказа",
        "en": "Points are credited automatically once your order is confirmed",
    },
    "Використання балів:": {"ru": "Использование баллов:", "en": "Spending points:"},
    "Зареєстровані користувачі можуть витратити бали у кабінеті": {
        "ru": "Зарегистрированные пользователи могут потратить баллы в личном кабинете",
        "en": "Registered shoppers can redeem points from the personal dashboard",
    },
    "Можливості:": {"ru": "Возможности:", "en": "Possibilities:"},
    "Обмін на промокоди або пожертвування на ЗСУ": {
        "ru": "Обмен на промокоды или пожертвования на ВСУ",
        "en": "Exchange for promo codes or donate to the Armed Forces of Ukraine",
    },
    "Перейти до балів": {"ru": "Перейти к баллам", "en": "Open points dashboard"},
    "Увійти для накопичення балів": {
        "ru": "Войти, чтобы копить баллы",
        "en": "Sign in to earn points",
    },

    # ===== contacts.html structured data =====
    "TwoComms — контакти": {
        "ru": "TwoComms — контакты",
        "en": "TwoComms — contacts",
    },
    "TwoComms — стріт і мілітарі одяг": {
        "ru": "TwoComms — стрит и милитари одежда",
        "en": "TwoComms — street & military apparel",
    },
    "TwoComms — український онлайн-магазин стрітвір- і мілітарі-одягу: футболки, худі, лонгсліви та кастомний DTF-друк.": {
        "ru": "TwoComms — украинский онлайн-магазин стритвир- и милитари-одежды: футболки, худи, лонгсливы и кастомная DTF-печать.",
        "en": "TwoComms is the Ukrainian online store of streetwear and military-style apparel: t-shirts, hoodies, longsleeves and custom DTF print.",
    },
    "Зв'яжіться з TwoComms: телефон, Telegram, Instagram. Онлайн-консультації та форма зворотного зв'язку.": {
        "ru": "Свяжитесь с TwoComms: телефон, Telegram, Instagram. Онлайн-консультации и форма обратной связи.",
        "en": "Contact TwoComms by phone, Telegram or Instagram. Online consultations and feedback form.",
    },
    "Україна": {"ru": "Украина", "en": "Ukraine"},

    # ===== catalog.html — expanded title/description (SEO Phase 12 finding N) =====
    "Каталог TwoComms — футболки, худі, лонгсліви з принтами": {
        "ru": "Каталог TwoComms — футболки, худи, лонгсливы с принтами",
        "en": "TwoComms catalogue — t-shirts, hoodies and longsleeves with prints",
    },
    "Каталог стріт-мілітарі одягу TwoComms: футболки, худі, лонгсліви з авторськими принтами, DTF-друк, бавовна. Доставка Новою Поштою від 1 дня по Україні.": {
        "ru": "Каталог стрит-милитари одежды TwoComms: футболки, худи, лонгсливы с авторскими принтами, DTF-печать, хлопок. Доставка Новой Почтой от 1 дня по Украине.",
        "en": "TwoComms streetwear and military-inspired apparel: cotton T-shirts, hoodies and long sleeves with original DTF prints. Nova Poshta delivery across Ukraine.",
    },

    # ===== index.html — expanded OG/Twitter description (SEO Phase 12 finding T) =====
    "Футболки, худі та лонгсліви з авторськими принтами. TwoComms — харківський стрітвеар-бренд, українське виробництво, швидка доставка Новою Поштою.": {
        "ru": "Футболки, худи и лонгсливы с авторскими принтами. TwoComms — харьковский стритвир-бренд, украинское производство, быстрая доставка Новой Почтой.",
        "en": "T-shirts, hoodies and longsleeves with original prints. TwoComms is a Kharkiv-based streetwear brand, made in Ukraine, fast Nova Poshta delivery.",
    },
    "Ексклюзивні футболки, худі та лонгсліви від харківського бренду TwoComms. Швидка доставка по Україні.": {
        "ru": "Эксклюзивные футболки, худи и лонгсливы от харьковского бренда TwoComms. Быстрая доставка по Украине.",
        "en": "Exclusive t-shirts, hoodies and longsleeves by Kharkiv-based TwoComms brand. Fast delivery across Ukraine.",
    },

    # ===== product_detail.html — PDP delivery & care H2 (Phase 12 finding L) =====
    "Доставка %(title)s по Україні": {
        "ru": "Доставка %(title)s по Украине",
        "en": "Delivery of %(title)s across Ukraine",
    },
    "Догляд за %(title)s": {
        "ru": "Уход за %(title)s",
        "en": "Care guide for %(title)s",
    },
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17P)


# Phase 17q — partials sweep: category_seo_blocks, product_seo_landing,
# catalog_color_seo, color_filter_chips, product_delivery_modal,
# product_quick_view, product_reviews, home_pagination.
TRANSLATIONS_PHASE_17Q: dict[str, dict[str, str]] = {
    # ===== category_seo_blocks.html =====
    "SEO-блоки категорії": {
        "ru": "SEO-блоки категории",
        "en": "Category SEO blocks",
    },
    "SEO-розділи категорії": {
        "ru": "SEO-разделы категории",
        "en": "Category SEO sections",
    },
    "Найкращі ціни": {"ru": "Лучшие цены", "en": "Best prices"},
    "Ціна": {"ru": "Цена", "en": "Price"},

    # ===== product_seo_landing.html =====
    "Деталі та підбір моделі": {
        "ru": "Детали и подбор модели",
        "en": "Details & fit guide",
    },
    "Розділи товару": {"ru": "Разделы товара", "en": "Product sections"},
    "Топ запити для цієї моделі": {
        "ru": "Топ-запросы для этой модели",
        "en": "Top queries for this model",
    },

    # ===== catalog_color_seo.html =====
    "SEO-опис %(slug)s": {
        "ru": "SEO-описание %(slug)s",
        "en": "SEO description for %(slug)s",
    },
    "Популярні запити цього розділу": {
        "ru": "Популярные запросы этого раздела",
        "en": "Popular queries in this section",
    },
    "каталога": {"ru": "каталога", "en": "catalogue"},

    # ===== color_filter_chips.html =====
    "Фільтр за кольором": {"ru": "Фильтр по цвету", "en": "Filter by colour"},
    "Колір:": {"ru": "Цвет:", "en": "Colour:"},
    "Скинути": {"ru": "Сбросить", "en": "Reset"},

    # ===== product_delivery_modal.html =====
    "Доставка та оплата": {"ru": "Доставка и оплата", "en": "Delivery & payment"},
    "Умови доставки замовлень": {
        "ru": "Условия доставки заказов",
        "en": "Order delivery terms",
    },
    "Нова Пошта": {"ru": "Новая Почта", "en": "Nova Poshta"},
    "Доставка по всій Україні. Вартість від 60 грн, термін доставки 1-3 дні.": {
        "ru": "Доставка по всей Украине. Стоимость от 60 грн, срок доставки 1-3 дня.",
        "en": "Delivery across Ukraine. Price from UAH 60, delivery in 1–3 days.",
    },
    "Самовивіз": {"ru": "Самовывоз", "en": "Self pickup"},
    "Можливий самовивіз з нашого складу. Деталі узгоджуються з менеджером.": {
        "ru": "Возможен самовывоз с нашего склада. Детали согласуются с менеджером.",
        "en": "Self pickup from our warehouse is available. Details are agreed with the manager.",
    },
    "Оплата": {"ru": "Оплата", "en": "Payment"},
    "Можлива оплата картою онлайн, накладним платежем або передоплатою.": {
        "ru": "Возможна оплата картой онлайн, наложенным платежом или предоплатой.",
        "en": "Card online, cash on delivery or prepayment are all accepted.",
    },

    # ===== product_reviews.html =====
    "Досвід клієнтів": {"ru": "Опыт клиентов", "en": "Customer experience"},
    "Відгуки про товар": {"ru": "Отзывы о товаре", "en": "Product reviews"},
    "Кількість опублікованих відгуків": {
        "ru": "Количество опубликованных отзывов",
        "en": "Published review count",
    },
    "відгуків": {"ru": "отзывов", "en": "reviews"},
    "Зведений рейтинг": {"ru": "Сводный рейтинг", "en": "Aggregate rating"},
    "Розподіл оцінок": {
        "ru": "Распределение оценок",
        "en": "Rating distribution",
    },
    "Статус рейтингу": {"ru": "Статус рейтинга", "en": "Rating status"},
    "Рейтинг скоро": {"ru": "Рейтинг скоро", "en": "Rating coming soon"},
    "Поки без відгуків": {"ru": "Пока без отзывов", "en": "No reviews yet"},
    "Після 3 перевірених публікацій.": {
        "ru": "После 3 проверенных публикаций.",
        "en": "After 3 verified publications.",
    },
    "Перший відгук сформує рейтинг.": {
        "ru": "Первый отзыв сформирует рейтинг.",
        "en": "The first review will set the rating.",
    },
    "Ваш статус для відгуку": {
        "ru": "Ваш статус для отзыва",
        "en": "Your status for a review",
    },
    "Підпис відгуку": {"ru": "Подпись отзыва", "en": "Review signature"},
    "Покупка підтверджена": {"ru": "Покупка подтверждена", "en": "Verified purchase"},
    "Гостьовий статус": {"ru": "Гостевой статус", "en": "Guest status"},
    "Гостьовий відгук": {"ru": "Гостевой отзыв", "en": "Guest review"},
    "Увійдіть для профілю": {
        "ru": "Войдите для профиля",
        "en": "Sign in for a profile",
    },
    "Увійти": {"ru": "Войти", "en": "Sign in"},
    "Залишити відгук": {"ru": "Оставить отзыв", "en": "Leave a review"},
    "Поділіться досвідом — модерація 1–2 дні": {
        "ru": "Поделитесь опытом — модерация 1–2 дня",
        "en": "Share your experience — moderation takes 1–2 days",
    },
    "Ваша оцінка": {"ru": "Ваша оценка", "en": "Your rating"},
    "Заголовок": {"ru": "Заголовок", "en": "Title"},
    "Наприклад: сіла краще, ніж очікував": {
        "ru": "Например: села лучше, чем ожидал",
        "en": "For example: fits better than expected",
    },
    "Фото до відгуку": {"ru": "Фото к отзыву", "en": "Review photo"},
    "Ваш досвід *": {"ru": "Ваш опыт *", "en": "Your experience *"},
    "Посадка, тканина, друк, доставка, що сподобалось або що варто знати іншим покупцям": {
        "ru": "Посадка, ткань, печать, доставка, что понравилось или что стоит знать другим покупателям",
        "en": "Fit, fabric, print, delivery — what you liked or what other shoppers should know",
    },
    "Імʼя для публікації *": {
        "ru": "Имя для публикации *",
        "en": "Display name *",
    },
    "з профілю": {"ru": "из профиля", "en": "from profile"},
    "для звʼязку": {"ru": "для связи", "en": "for contact"},
    "Надіслати на модерацію": {
        "ru": "Отправить на модерацию",
        "en": "Submit for moderation",
    },
    "Після перевірки відгук зʼявиться на сторінці. Для зареєстрованих покупців статус покупки підтягується автоматично.": {
        "ru": "После проверки отзыв появится на странице. Для зарегистрированных покупателей статус покупки подтягивается автоматически.",
        "en": "Once approved the review appears on the page. Purchase status is auto-detected for registered shoppers.",
    },
    "Список відгуків": {"ru": "Список отзывов", "en": "Review list"},
    "Зареєстрований користувач": {
        "ru": "Зарегистрированный пользователь",
        "en": "Registered user",
    },
    "Статуси відгуку": {"ru": "Статусы отзыва", "en": "Review statuses"},
    "Зареєстрований": {"ru": "Зарегистрированный", "en": "Registered"},
    "Гість": {"ru": "Гость", "en": "Guest"},
    "Купив товар": {"ru": "Купил товар", "en": "Verified buyer"},
    "Покупка не підтверджена": {
        "ru": "Покупка не подтверждена",
        "en": "Purchase not verified",
    },
    "Фото відгуку": {"ru": "Фото отзыва", "en": "Review photo"},

    # Pluralised forms in product_reviews.html (blocktrans count)
    "на основі %(counter)s відгуку": {
        "ru": "на основе %(counter)s отзыва",
        "en": "based on %(counter)s review",
    },
    "на основі %(counter)s відгуків": {
        "ru": "на основе %(counter)s отзывов",
        "en": "based on %(counter)s reviews",
    },
    "%(counter)s зірка": {"ru": "%(counter)s звезда", "en": "%(counter)s star"},
    "%(counter)s зірок": {"ru": "%(counter)s звёзд", "en": "%(counter)s stars"},
    "%(rating)s з 5": {"ru": "%(rating)s из 5", "en": "%(rating)s of 5"},
    "Фото відгуку %(author)s": {
        "ru": "Фото отзыва %(author)s",
        "en": "Review photo by %(author)s",
    },
    "Корисно: %(count)s": {
        "ru": "Полезно: %(count)s",
        "en": "Helpful: %(count)s",
    },

    # ===== home_pagination.html =====
    "Навігація по новинках": {
        "ru": "Навигация по новинкам",
        "en": "New arrivals pagination",
    },
    "Попередня": {"ru": "Предыдущая", "en": "Previous"},
    "Наступна": {"ru": "Следующая", "en": "Next"},
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17Q)


# ===========================================================================
# Phase 17R (2026-05-15) — bulk customer-facing strings from product/catalog/
# co-op/404/builder/wholesale templates and brand copy.
# ===========================================================================
TRANSLATIONS_PHASE_17R: dict[str, dict[str, str]] = {
    # ===== Currency / units =====
    "грн": {"ru": "грн", "en": "UAH"},
    "грн/шт": {"ru": "грн/шт", "en": "UAH/pc"},
    "0 грн / шт": {"ru": "0 грн / шт", "en": "0 UAH / pc"},
    "шт": {"ru": "шт", "en": "pcs"},
    "б.": {"ru": "б.", "en": "pts"},

    # ===== Product detail =====
    "Розмір": {"ru": "Размер", "en": "Size"},
    "Колір": {"ru": "Цвет", "en": "Color"},
    "Кількість": {"ru": "Количество", "en": "Quantity"},
    "Зменшити": {"ru": "Уменьшить", "en": "Decrease"},
    "Збільшити": {"ru": "Увеличить", "en": "Increase"},
    "Немає в наявності": {"ru": "Нет в наличии", "en": "Out of stock"},
    "Додати в кошик": {"ru": "Добавить в корзину", "en": "Add to cart"},
    "Додати до обраних": {"ru": "Добавить в избранное", "en": "Add to favorites"},
    "Дізнатись про бали": {"ru": "Узнать о баллах", "en": "Learn about points"},
    "Інформація про доставку": {"ru": "Информация о доставке", "en": "Delivery info"},
    "Поділитися:": {"ru": "Поделиться:", "en": "Share:"},
    "Поділитися у Facebook": {"ru": "Поделиться в Facebook", "en": "Share on Facebook"},
    "Поділитися у Twitter": {"ru": "Поделиться в Twitter", "en": "Share on Twitter"},
    "Поділитися у Telegram": {"ru": "Поделиться в Telegram", "en": "Share on Telegram"},
    "Скопіювати посилання": {"ru": "Скопировать ссылку", "en": "Copy link"},
    "Опис": {"ru": "Описание", "en": "Description"},
    "Розмірна сітка": {"ru": "Размерная сетка", "en": "Size guide"},
    "Догляд": {"ru": "Уход", "en": "Care"},
    "Опис товару буде додано найближчим часом.": {
        "ru": "Описание товара будет добавлено в ближайшее время.",
        "en": "Product description will be added soon.",
    },
    "Таблиця розмірів": {"ru": "Таблица размеров", "en": "Size chart"},
    "Груди (см)": {"ru": "Грудь (см)", "en": "Chest (cm)"},
    "Талія (см)": {"ru": "Талия (см)", "en": "Waist (cm)"},
    "Стегна (см)": {"ru": "Бёдра (см)", "en": "Hips (cm)"},
    "Як виміряти:": {"ru": "Как измерить:", "en": "How to measure:"},
    "• Груди: навколо найширшої частини грудей": {
        "ru": "• Грудь: вокруг самой широкой части груди",
        "en": "• Chest: around the widest part of the chest",
    },
    "• Талія: навколо найвужчої частини талії": {
        "ru": "• Талия: вокруг самой узкой части талии",
        "en": "• Waist: around the narrowest part of the waist",
    },
    "• Стегна: навколо найширшої частини стегон": {
        "ru": "• Бёдра: вокруг самой широкой части бёдер",
        "en": "• Hips: around the widest part of the hips",
    },
    "Матеріал:": {"ru": "Материал:", "en": "Material:"},
    "Матеріал": {"ru": "Материал", "en": "Material"},
    "100% бавовна преміум-якості": {
        "ru": "100% хлопок премиум-качества",
        "en": "100% premium-quality cotton",
    },
    "100%% бавовна преміум-якості": {
        "ru": "100%% хлопок премиум-качества",
        "en": "100%% premium-quality cotton",
    },
    "Стирання:": {"ru": "Стирка:", "en": "Washing:"},
    "Машинне або ручне стирання при 30°C, не відбілювати": {
        "ru": "Машинная или ручная стирка при 30°C, не отбеливать",
        "en": "Machine or hand wash at 30°C, do not bleach",
    },
    "Прасування:": {"ru": "Глажка:", "en": "Ironing:"},
    "Прасування на середній температурі, краще з вивороту": {
        "ru": "Глажка на средней температуре, лучше с изнанки",
        "en": "Iron on medium heat, preferably inside out",
    },
    "Вам також сподобається": {"ru": "Вам также понравится", "en": "You may also like"},
    "Колір {{ hex }}": {"ru": "Цвет {{ hex }}", "en": "Color {{ hex }}"},

    # ===== 404 page =====
    "404 — Щось пішло не так": {
        "ru": "404 — Что-то пошло не так",
        "en": "404 — Something went wrong",
    },
    "Упс, глухий кут": {"ru": "Упс, тупик", "en": "Oops, dead end"},
    "(Щось ми не туди припливли...)": {
        "ru": "(Что-то мы не туда заплыли...)",
        "en": "(Looks like we drifted off course...)",
    },
    "Назад": {"ru": "Назад", "en": "Back"},
    "ОК": {"ru": "ОК", "en": "OK"},

    # ===== Catalog / counters =====
    "%(counter)s товар": {"ru": "%(counter)s товар", "en": "%(counter)s product"},
    "%(counter)s товар знайдено": {
        "ru": "%(counter)s товар найдено",
        "en": "%(counter)s product found",
    },
    "%(counter)s бал": {"ru": "%(counter)s балл", "en": "%(counter)s point"},
    "%(counter)s відгук": {"ru": "%(counter)s отзыв", "en": "%(counter)s review"},
    "%(counter)s зірка": {"ru": "%(counter)s звезда", "en": "%(counter)s star"},
    "Рейтинг товару, %(counter)s відгук": {
        "ru": "Рейтинг товара, %(counter)s отзыв",
        "en": "Product rating, %(counter)s review",
    },
    "за запитом «%(q)s»": {
        "ru": "по запросу «%(q)s»",
        "en": "for query \"%(q)s\"",
    },
    "Категорія %(cat)s з авторськими принтами від TwoComms.": {
        "ru": "Категория %(cat)s с авторскими принтами от TwoComms.",
        "en": "%(cat)s category with original prints by TwoComms.",
    },
    "%(cat)s з авторськими принтами від TwoComms.": {
        "ru": "%(cat)s с авторскими принтами от TwoComms.",
        "en": "%(cat)s with original prints by TwoComms.",
    },
    "%(cat)s з авторськими принтами від TwoComms. Стріт & мілітарі одяг.": {
        "ru": "%(cat)s с авторскими принтами от TwoComms. Стрит & милитари одежда.",
        "en": "%(cat)s with original prints by TwoComms. Street & military apparel.",
    },

    # ===== Categories =====
    "Футболка": {"ru": "Футболка", "en": "T-shirt"},
    "Футболки": {"ru": "Футболки", "en": "T-shirts"},
    "Футболки:": {"ru": "Футболки:", "en": "T-shirts:"},
    "Худі": {"ru": "Худи", "en": "Hoodie"},
    "Худі [фліс]": {"ru": "Худи [флис]", "en": "Hoodie [fleece]"},
    "Худі [фліс] опт": {"ru": "Худи [флис] опт", "en": "Hoodie [fleece] wholesale"},
    "[фліс]": {"ru": "[флис]", "en": "[fleece]"},
    "Лонгслів": {"ru": "Лонгслив", "en": "Longsleeve"},

    # ===== Brand / about / pro_brand =====
    "TwoComms — не крапка. Продовження.": {
        "ru": "TwoComms — не точка. Продолжение.",
        "en": "TwoComms — not a period. A continuation.",
    },
    "не крапка.": {"ru": "не точка.", "en": "not a period."},
    "Характер. Харків. Продовження.": {
        "ru": "Характер. Харьков. Продолжение.",
        "en": "Character. Kharkiv. Continuation.",
    },
    "Харків": {"ru": "Харьков", "en": "Kharkiv"},
    "ХАРКІВ": {"ru": "ХАРЬКОВ", "en": "KHARKIV"},
    "Київ": {"ru": "Киев", "en": "Kyiv"},
    "Харків як ДНК": {"ru": "Харьков как ДНК", "en": "Kharkiv as DNA"},
    "Харківський бренд одягу": {
        "ru": "Харьковский бренд одежды",
        "en": "Kharkiv apparel brand",
    },
    "Місто як характер": {"ru": "Город как характер", "en": "City as character"},
    "Код Харкова": {"ru": "Код Харькова", "en": "Kharkiv code"},
    "TwoComms не можна відділити від Харкова.": {
        "ru": "TwoComms нельзя отделить от Харькова.",
        "en": "TwoComms is inseparable from Kharkiv.",
    },
    "Харківський код і дисципліна залишаються основою кожної нової речі.": {
        "ru": "Харьковский код и дисциплина остаются основой каждой новой вещи.",
        "en": "The Kharkiv code and discipline stay the backbone of every new piece.",
    },
    "TwoComms — одяг з характером, кодом і харківським походженням": {
        "ru": "TwoComms — одежда с характером, кодом и харьковским происхождением",
        "en": "TwoComms — apparel with character, code and Kharkiv origin",
    },
    "Що означає назва TwoComms": {
        "ru": "Что означает название TwoComms",
        "en": "What the name TwoComms means",
    },
    "У назві TwoComms закладено дві коми.": {
        "ru": "В названии TwoComms заложены две запятые.",
        "en": "The name TwoComms contains two commas.",
    },
    "Знак для своїх": {"ru": "Знак для своих", "en": "A mark for our own"},
    "Знак": {"ru": "Знак", "en": "Mark"},
    "Ми знаємо свій знак.": {"ru": "Мы знаем свой знак.", "en": "We know our mark."},
    "Ми знаємо, звідки ми.": {
        "ru": "Мы знаем, откуда мы.",
        "en": "We know where we come from.",
    },
    "Ми не ставимо крапку.": {
        "ru": "Мы не ставим точку.",
        "en": "We don't put a period.",
    },
    "Не шум. Не роль.": {
        "ru": "Не шум. Не роль.",
        "en": "Not noise. Not a role.",
    },
    "Сенси": {"ru": "Смыслы", "en": "Meanings"},
    "Сенсовий принт TwoComms Довіряй своїй божевільній ідеї": {
        "ru": "Смысловой принт TwoComms Доверяй своей безумной идее",
        "en": "TwoComms statement print: Trust your crazy idea",
    },
    "Сенсовий принт TwoComms Рабів до раю не пускають": {
        "ru": "Смысловой принт TwoComms Рабов в рай не пускают",
        "en": "TwoComms statement print: No slaves in heaven",
    },
    "Довіряй своїй божевільній ідеї": {
        "ru": "Доверяй своей безумной идее",
        "en": "Trust your crazy idea",
    },
    "Рабів до раю не пускають": {
        "ru": "Рабов в рай не пускают",
        "en": "No slaves are allowed in heaven",
    },
    "Код свободи": {"ru": "Код свободы", "en": "Code of freedom"},
    "Одяг з кодом": {"ru": "Одежда с кодом", "en": "Apparel with a code"},
    "Куди ми йдемо": {"ru": "Куда мы идём", "en": "Where we are going"},
    "Якість, яка доводить ідею": {
        "ru": "Качество, которое доказывает идею",
        "en": "Quality that backs the idea",
    },
    "Візуальна мова": {"ru": "Визуальный язык", "en": "Visual language"},
    "Власна візуальна мова не розчиняється в трендах і випадкових жестах.": {
        "ru": "Собственный визуальный язык не растворяется в трендах и случайных жестах.",
        "en": "An original visual language does not dissolve into trends or random gestures.",
    },
    "Форма, яка тримає людину": {
        "ru": "Форма, которая держит человека",
        "en": "Form that holds the wearer",
    },
    "Форма без зайвого шуму: річ має сидіти впевнено і не сковувати рух.": {
        "ru": "Форма без лишнего шума: вещь должна сидеть уверенно и не сковывать движения.",
        "en": "Form without noise: the piece must sit confidently and not restrict movement.",
    },
    "Принт має бути частиною речі, а не випадковою картинкою на поверхні.": {
        "ru": "Принт должен быть частью вещи, а не случайной картинкой на поверхности.",
        "en": "A print must be part of the piece, not a random image on the surface.",
    },
    "Кожен дроп продовжує ідею бренду, а не запускає її заново.": {
        "ru": "Каждый дроп продолжает идею бренда, а не запускает её заново.",
        "en": "Each drop continues the brand idea instead of restarting it.",
    },
    "Річ, що тримає стан": {
        "ru": "Вещь, которая держит состояние",
        "en": "A piece that holds a state",
    },
    "Ключові характеристики бренду": {
        "ru": "Ключевые характеристики бренда",
        "en": "Key brand traits",
    },

    # ===== Cooperation page =====
    "Опт": {"ru": "Опт", "en": "Wholesale"},
    "Дроп": {"ru": "Дропшипинг", "en": "Dropshipping"},
    "Опт / повний магазин": {
        "ru": "Опт / полный магазин",
        "en": "Wholesale / full store",
    },
    "Опт і дропшипінг": {"ru": "Опт и дропшипинг", "en": "Wholesale & dropshipping"},
    "Бренд-партнерство": {"ru": "Бренд-партнёрство", "en": "Brand partnership"},
    "Брендування / мерч": {"ru": "Брендирование / мерч", "en": "Branding / merch"},
    "Для бренду / команди / події": {
        "ru": "Для бренда / команды / события",
        "en": "For brand / team / event",
    },
    "Для брендів, подій, команд і корпоративного мерчу зі своїм принтом.": {
        "ru": "Для брендов, событий, команд и корпоративного мерча со своим принтом.",
        "en": "For brands, events, teams and corporate merch with your own print.",
    },
    "Оптова партія": {"ru": "Оптовая партия", "en": "Wholesale batch"},
    "Тестова партія": {"ru": "Тестовая партия", "en": "Trial batch"},
    "Тестова партія: комплектація": {
        "ru": "Тестовая партия: комплектация",
        "en": "Trial batch: composition",
    },
    "Тестова партія: товар": {
        "ru": "Тестовая партия: товар",
        "en": "Trial batch: item",
    },
    "Оптові закупівлі та дропшипінг TwoComms": {
        "ru": "Оптовые закупки и дропшипинг TwoComms",
        "en": "TwoComms wholesale and dropshipping",
    },
    "TwoComms — оптові закупівлі та дропшипінг українського стріт одягу": {
        "ru": "TwoComms — оптовые закупки и дропшипинг украинской стрит-одежды",
        "en": "TwoComms — wholesale and dropshipping of Ukrainian streetwear",
    },
    "Співпраця з TwoComms — дропшипінг, опт і бренд-партнерства": {
        "ru": "Сотрудничество с TwoComms — дропшипинг, опт и бренд-партнёрства",
        "en": "Partnerships with TwoComms — dropshipping, wholesale and brand collabs",
    },
    "Оптовий прайс-лист TwoComms — гуртові ціни на одяг": {
        "ru": "Оптовый прайс-лист TwoComms — оптовые цены на одежду",
        "en": "TwoComms wholesale price list — wholesale apparel prices",
    },
    "Оптові умови та прайс": {
        "ru": "Оптовые условия и прайс",
        "en": "Wholesale terms and price list",
    },
    "Скачайте актуальний прайс-лист оптових цін на футболки та худі.": {
        "ru": "Скачайте актуальный прайс-лист оптовых цен на футболки и худи.",
        "en": "Download the current wholesale price list for tees and hoodies.",
    },
    "Спеціальні ціни для худі з флісом. М'який та зручний матеріал.": {
        "ru": "Специальные цены на худи с флисом. Мягкий и удобный материал.",
        "en": "Special prices on fleece hoodies. Soft and comfortable material.",
    },
    "М'який фліс": {"ru": "Мягкий флис", "en": "Soft fleece"},
    "Поширені питання про оптові закупівлі": {
        "ru": "Частые вопросы про оптовые закупки",
        "en": "Wholesale FAQ",
    },
    "Що ще подивитися перед запуском опта": {
        "ru": "Что ещё посмотреть перед запуском опта",
        "en": "What else to review before launching wholesale",
    },
    "Що ще подивитися перед заявкою": {
        "ru": "Что ещё посмотреть перед заявкой",
        "en": "What else to review before applying",
    },
    "Швидкі маршрути для партнерів": {
        "ru": "Быстрые маршруты для партнёров",
        "en": "Quick routes for partners",
    },
    "Прозорі та вигідні умови для всіх типів співпраці": {
        "ru": "Прозрачные и выгодные условия для всех типов сотрудничества",
        "en": "Transparent and rewarding terms for every form of cooperation",
    },
    "Оберіть найкращий варіант для вашого бізнесу": {
        "ru": "Выберите лучший вариант для вашего бизнеса",
        "en": "Pick the best option for your business",
    },
    "Вигідні оптові тарифи": {
        "ru": "Выгодные оптовые тарифы",
        "en": "Competitive wholesale rates",
    },
    "Висока маржинальність": {"ru": "Высокая маржа", "en": "High margin"},
    "Високі комісії": {"ru": "Высокие комиссии", "en": "High commissions"},
    "Максимальна вигода": {"ru": "Максимальная выгода", "en": "Maximum benefit"},
    "Грошова винагорода": {"ru": "Денежное вознаграждение", "en": "Cash reward"},
    "Готові медіаматеріали та персональний менеджер 24/7": {
        "ru": "Готовые медиаматериалы и персональный менеджер 24/7",
        "en": "Ready media kit and a personal manager 24/7",
    },
    "Оперативне відвантаження зі складу по всій Україні": {
        "ru": "Оперативная отгрузка со склада по всей Украине",
        "en": "Fast shipping from the warehouse across Ukraine",
    },
    "Зручне формування накладної прямо на сайті": {
        "ru": "Удобное оформление накладной прямо на сайте",
        "en": "Convenient waybill creation right on the site",
    },
    "Прямий контакт з командою": {
        "ru": "Прямой контакт с командой",
        "en": "Direct contact with the team",
    },
    "Контакти без прихованих умов": {
        "ru": "Контакты без скрытых условий",
        "en": "Contacts with no hidden terms",
    },
    "Довгострокова співпраця": {
        "ru": "Долгосрочное сотрудничество",
        "en": "Long-term cooperation",
    },
    "Фіксовані ціни на футболки та худі за договором": {
        "ru": "Фиксированные цены на футболки и худи по договору",
        "en": "Contract-fixed prices on tees and hoodies",
    },
    "Для блогерів та медіа": {"ru": "Для блогеров и медиа", "en": "For bloggers & media"},
    "Для моделей": {"ru": "Для моделей", "en": "For models"},
    "Для себе": {"ru": "Для себя", "en": "For yourself"},
    "Для бренду / команди / події": {
        "ru": "Для бренда / команды / события",
        "en": "For brand / team / event",
    },

    # ===== Wholesale form =====
    "Email отримувача": {"ru": "Email получателя", "en": "Recipient email"},
    "Імʼя/компанія отримувача": {
        "ru": "Имя/компания получателя",
        "en": "Recipient name/company",
    },
    "Назва бренду або @instagram": {
        "ru": "Название бренда или @instagram",
        "en": "Brand name or @instagram",
    },
    "@username або +380...": {
        "ru": "@username или +380...",
        "en": "@username or +380...",
    },
    "Назва магазину / Instagram": {
        "ru": "Название магазина / Instagram",
        "en": "Store name / Instagram",
    },
    "Номер телефону": {"ru": "Номер телефона", "en": "Phone number"},
    "Скільки речей потрібно?": {
        "ru": "Сколько вещей нужно?",
        "en": "How many items do you need?",
    },
    "Куди надіслати результат?": {
        "ru": "Куда отправить результат?",
        "en": "Where to send the result?",
    },
    "Якщо потрібні особливі розміри або примітки": {
        "ru": "Если нужны особые размеры или примечания",
        "en": "If you need special sizes or notes",
    },
    "Бот відправить заявку в Telegram": {
        "ru": "Бот отправит заявку в Telegram",
        "en": "The bot will send the application to Telegram",
    },
    "Зберегти чернетку і відкрити Telegram": {
        "ru": "Сохранить черновик и открыть Telegram",
        "en": "Save draft and open Telegram",
    },
    "Контакт + кнопка нижче. Чорнетка відразу зберігається локально.": {
        "ru": "Контакт + кнопка ниже. Черновик сразу сохраняется локально.",
        "en": "Contact + button below. The draft is saved locally instantly.",
    },
    "Префікс +380 підставимо автоматично.": {
        "ru": "Префикс +380 подставим автоматически.",
        "en": "We will add the +380 prefix automatically.",
    },
    "Один клік — і розмір зафіксовано.": {
        "ru": "Один клик — и размер зафиксирован.",
        "en": "One click and the size is locked in.",
    },
    "Один розмір": {"ru": "Один размер", "en": "One size"},

    # ===== Builder / custom print =====
    "Створи річ, <br>що&nbsp;говорить <span>за тебе</span>": {
        "ru": "Создай вещь, <br>которая&nbsp;говорит <span>за тебя</span>",
        "en": "Create a piece <br>that&nbsp;speaks <span>for you</span>",
    },
    "твій стиль, твої правила.": {
        "ru": "твой стиль, твои правила.",
        "en": "your style, your rules.",
    },
    "У тебе є готовий файл чи тільки ідея?": {
        "ru": "У тебя есть готовый файл или только идея?",
        "en": "Do you have a ready file or just an idea?",
    },
    "Маю готовий файл": {"ru": "Есть готовый файл", "en": "I have a ready file"},
    "Опиши ідею — менеджер перевірить, чи можемо зробити.": {
        "ru": "Опиши идею — менеджер проверит, сможем ли мы её сделать.",
        "en": "Describe the idea — the manager will confirm whether we can do it.",
    },
    "Потрібен дизайн з нуля": {
        "ru": "Нужен дизайн с нуля",
        "en": "I need a design from scratch",
    },
    "Потрібно допрацювати файл": {
        "ru": "Нужно доработать файл",
        "en": "I need to refine the file",
    },
    "Уточню з менеджером": {
        "ru": "Уточню с менеджером",
        "en": "I'll clarify with the manager",
    },
    "1. Формат": {"ru": "1. Формат", "en": "1. Format"},
    "5. Макет": {"ru": "5. Макет", "en": "5. Design"},
    "Формат": {"ru": "Формат", "en": "Format"},
    "Макет": {"ru": "Макет", "en": "Design"},
    "Макет / дизайн": {"ru": "Макет / дизайн", "en": "Design"},
    "Цей браузер": {"ru": "Этот браузер", "en": "This browser"},
    "Чорний": {"ru": "Чёрный", "en": "Black"},
    "Чорний, Кайот": {"ru": "Чёрный, Койот", "en": "Black, Coyote"},
    "Будь-який колір з палітри": {
        "ru": "Любой цвет из палитры",
        "en": "Any color from the palette",
    },
    "Будь-який колір та дизайн": {
        "ru": "Любой цвет и дизайн",
        "en": "Any color and design",
    },
    "Обери свій колір або будь-який з палітри": {
        "ru": "Выбери свой цвет или любой из палитры",
        "en": "Pick your color or any from the palette",
    },
    "Сцена показує відтінок максимально близько до реального.": {
        "ru": "Сцена показывает оттенок максимально близко к реальному.",
        "en": "The stage shows the shade as close to the real one as possible.",
    },
    "Покажемо вибраний режим прямо на сцені.": {
        "ru": "Покажем выбранный режим прямо на сцене.",
        "en": "We'll show the selected mode right on the stage.",
    },
    "Поки що нічого не додано — оберіть виріб, щоб побачити прорахунок.": {
        "ru": "Пока ничего не добавлено — выберите изделие, чтобы увидеть расчёт.",
        "en": "Nothing added yet — choose an item to see the calculation.",
    },
    "Ціну побачите після першого вибору": {
        "ru": "Цену увидите после первого выбора",
        "en": "The price will appear after the first selection",
    },
    "Ціна оновлюється в реальному часі.": {
        "ru": "Цена обновляется в реальном времени.",
        "en": "The price updates in real time.",
    },
    "Як річ сидітиме на тілі.": {
        "ru": "Как вещь будет сидеть на теле.",
        "en": "How the piece will fit the body.",
    },
    "Торкніться мітки, щоб подивитися placement на виробі.": {
        "ru": "Коснитесь метки, чтобы посмотреть размещение на изделии.",
        "en": "Tap the marker to view the placement on the garment.",
    },
    "Перед, спинка, рукави або будь-яка інша зона": {
        "ru": "Перед, спинка, рукава или любая другая зона",
        "en": "Front, back, sleeves or any other zone",
    },
    "Зі спини": {"ru": "Со спины", "en": "Back view"},
    "Спина": {"ru": "Спина", "en": "Back"},
    "Рукави": {"ru": "Рукава", "en": "Sleeves"},
    "Лівий рукав": {"ru": "Левый рукав", "en": "Left sleeve"},
    "Правий рукав": {"ru": "Правый рукав", "en": "Right sleeve"},
    "Текст для лівого рукава": {"ru": "Текст для левого рукава", "en": "Text for the left sleeve"},
    "Текст для правого рукава": {"ru": "Текст для правого рукава", "en": "Text for the right sleeve"},
    "Другий рукав рахується як окрема платна зона.": {
        "ru": "Второй рукав считается как отдельная платная зона.",
        "en": "The second sleeve counts as a separate paid zone.",
    },
    "Текст, символи або невеликі деталі.": {
        "ru": "Текст, символы или небольшие детали.",
        "en": "Text, symbols or small details.",
    },
    "Класичне розміщення для головного принта.": {
        "ru": "Классическое размещение для главного принта.",
        "en": "Classic placement for the main print.",
    },
    "Більший формат для сильного візуалу.": {
        "ru": "Бóльший формат для сильного визуала.",
        "en": "A bigger format for a strong visual.",
    },
    "Апгрейди, які роблять річ більш зібраною.": {
        "ru": "Апгрейды, которые делают вещь более собранной.",
        "en": "Upgrades that make the piece feel more polished.",
    },
    "Від легкої бази до щільнішого варіанту.": {
        "ru": "От лёгкой базы до более плотного варианта.",
        "en": "From a light base to a denser option.",
    },
    "Подарункова упаковка": {"ru": "Подарочная упаковка", "en": "Gift packaging"},
    "Додамо крафт-пакування, листівку й приберемо цінники.": {
        "ru": "Добавим крафт-упаковку, открытку и уберём ценники.",
        "en": "We'll add craft packaging, a postcard and remove price tags.",
    },
    "Текст побажання (необовʼязково)": {
        "ru": "Текст пожелания (необязательно)",
        "en": "Wish text (optional)",
    },
    "Наприклад: TWOCOMMS CLUB": {
        "ru": "Например: TWOCOMMS CLUB",
        "en": "For example: TWOCOMMS CLUB",
    },
    "Наприклад: EST. 2014": {
        "ru": "Например: EST. 2014",
        "en": "For example: EST. 2014",
    },
    "Друк": {"ru": "Печать", "en": "Print"},
    "Свій одяг": {"ru": "Своя одежда", "en": "Your own apparel"},
    "Худі, футболки, лонгсліви та ваші речі під DTF-друк.": {
        "ru": "Худи, футболки, лонгсливы и ваши вещи под DTF-печать.",
        "en": "Hoodies, tees, longsleeves and your own items for DTF print.",
    },
    "Худі, футболки, лонгсліви та інший одяг на вибір": {
        "ru": "Худи, футболки, лонгсливы и другая одежда на выбор",
        "en": "Hoodies, tees, longsleeves and other apparel to choose from",
    },
    "Худі, футболка та лонгслів з кастомним друком TwoComms": {
        "ru": "Худи, футболка и лонгслив с кастомной печатью TwoComms",
        "en": "Hoodie, T-shirt and longsleeve with custom TwoComms print",
    },
    "Матеріал, колір, крій, стан виробу та все, що важливо для прорахунку": {
        "ru": "Материал, цвет, крой, состояние изделия и всё, что важно для расчёта",
        "en": "Material, color, cut, item condition and anything else relevant for the quote",
    },
    "Зміна кількості (±)": {"ru": "Изменение количества (±)", "en": "Quantity change (±)"},
    "Мінімальне замовлення по моделі — від 8 шт. і кратно 8.": {
        "ru": "Минимальный заказ по модели — от 8 шт. и кратно 8.",
        "en": "Minimum order per model — from 8 pcs and in multiples of 8.",
    },

    # ===== Quantities / tiers =====
    "8-15 шт": {"ru": "8-15 шт", "en": "8-15 pcs"},
    "8–15": {"ru": "8–15", "en": "8–15"},
    "16-31 шт": {"ru": "16-31 шт", "en": "16-31 pcs"},
    "16–31": {"ru": "16–31", "en": "16–31"},
    "32-63 шт": {"ru": "32-63 шт", "en": "32-63 pcs"},
    "32–63": {"ru": "32–63", "en": "32–63"},
    "64-99 шт": {"ru": "64-99 шт", "en": "64-99 pcs"},
    "64–99": {"ru": "64–99", "en": "64–99"},
    "100+": {"ru": "100+", "en": "100+"},
    "100+ шт": {"ru": "100+ шт", "en": "100+ pcs"},
    "грн (за 8-15/16-31/32-63/64-99/100+ шт.)": {
        "ru": "грн (за 8-15/16-31/32-63/64-99/100+ шт.)",
        "en": "UAH (per 8-15/16-31/32-63/64-99/100+ pcs)",
    },
    "15-30%% від продажів": {"ru": "15-30%% от продаж", "en": "15-30%% of sales"},

    # ===== Promo / loyalty =====
    "Aктивні та використані промокоди зʼявляться тут": {
        "ru": "Активные и использованные промокоды появятся здесь",
        "en": "Active and used promo codes will appear here",
    },
    "Історія використаних промокодів": {
        "ru": "История использованных промокодов",
        "en": "Used promo codes history",
    },
    "Поки що немає операцій з балами": {
        "ru": "Пока нет операций с баллами",
        "en": "No point transactions yet",
    },
    "1 бал = 1 гривня з вашої покупки": {
        "ru": "1 балл = 1 гривна с вашей покупки",
        "en": "1 point = 1 UAH from your purchase",
    },
    "Бали можна використовувати для отримання знижок": {
        "ru": "Баллы можно использовать для получения скидок",
        "en": "Points can be used to get discounts",
    },
    "Бали нараховуються за кожну покупку в нашому магазині": {
        "ru": "Баллы начисляются за каждую покупку в нашем магазине",
        "en": "Points are credited for every purchase in our store",
    },
    "Накопичуйте бали за покупки та використовуйте їх для отримання знижок": {
        "ru": "Накапливайте баллы за покупки и используйте их для получения скидок",
        "en": "Earn points for purchases and use them for discounts",
    },
    "Використовуйте ваші бали для отримання знижок та підтримки ЗСУ": {
        "ru": "Используйте ваши баллы для получения скидок и поддержки ВСУ",
        "en": "Use your points for discounts and to support the UA Army",
    },
    "Або пожертвувати на підтримку ЗСУ": {
        "ru": "Или пожертвовать на поддержку ВСУ",
        "en": "Or donate to support the UA Army",
    },
    "Обмінюйте бали на промокоди або жертвуйте на ЗСУ.": {
        "ru": "Обменивайте баллы на промокоды или жертвуйте на ВСУ.",
        "en": "Exchange points for promo codes or donate to the UA Army.",
    },
    "Купівля за бали": {"ru": "Покупка за баллы", "en": "Purchase with points"},
    "Баланс:": {"ru": "Баланс:", "en": "Balance:"},

    # ===== Misc UI =====
    "о": {"ru": "из", "en": "of"},
    "Кому": {"ru": "Кому", "en": "To"},
    "Коли": {"ru": "Когда", "en": "When"},
    "Хто це (якщо інший)": {"ru": "Кто это (если другой)", "en": "Who is it (if different)"},
    "Хто виконав": {"ru": "Кто выполнил", "en": "Performed by"},
    "Я є учасником бойових дій": {
        "ru": "Я являюсь участником боевых действий",
        "en": "I am a combat veteran",
    },
    "Документ УБД": {"ru": "Документ УБД", "en": "Combat veteran document"},
    "Може допомогти у відновленні акаунту або зв'язку": {
        "ru": "Может помочь в восстановлении аккаунта или связи",
        "en": "May help with account recovery or contact",
    },
    "Потрібен для перевірки підписки у випадку конкурсів": {
        "ru": "Нужен для проверки подписки в случае конкурсов",
        "en": "Required to verify subscription for giveaways",
    },
    "Назва профілю": {"ru": "Название профиля", "en": "Profile name"},
    "Адреса": {"ru": "Адрес", "en": "Address"},
    "Місце реєстрації": {"ru": "Место регистрации", "en": "Place of registration"},
    "Будь-який колір з палітри": {
        "ru": "Любой цвет из палитры",
        "en": "Any color from the palette",
    },
    "Прямий перехід до актуальної картки товару.": {
        "ru": "Прямой переход к актуальной карточке товара.",
        "en": "Direct link to the up-to-date product page.",
    },
    "Діє з": {"ru": "Действует с", "en": "Valid from"},
    "Діє до": {"ru": "Действует до", "en": "Valid until"},
    "Діє до:": {"ru": "Действует до:", "en": "Valid until:"},
    "Діяв до:": {"ru": "Действовал до:", "en": "Was valid until:"},
    "Період повернення": {"ru": "Период возврата", "en": "Return period"},
    "Термін дії минув": {"ru": "Срок действия истёк", "en": "Expired"},
    "Вичерпано": {"ru": "Исчерпано", "en": "Used up"},
    "Активний": {"ru": "Активный", "en": "Active"},
    "Активно": {"ru": "Активно", "en": "Active"},
    "Неактивно": {"ru": "Неактивно", "en": "Inactive"},
    "Невідомо": {"ru": "Неизвестно", "en": "Unknown"},
    "Недоступно": {"ru": "Недоступно", "en": "Unavailable"},
    "Тип": {"ru": "Тип", "en": "Type"},
    "Назва": {"ru": "Название", "en": "Name"},
    "Звіт": {"ru": "Отчёт", "en": "Report"},
    "Звіти": {"ru": "Отчёты", "en": "Reports"},
    "Стан": {"ru": "Состояние", "en": "Status"},
    "Статус": {"ru": "Статус", "en": "Status"},
    "Користувач": {"ru": "Пользователь", "en": "User"},
    "Адміністратор": {"ru": "Администратор", "en": "Administrator"},
    "Модератор": {"ru": "Модератор", "en": "Moderator"},
    "Без QA": {"ru": "Без QA", "en": "Without QA"},
    "Помилка": {"ru": "Ошибка", "en": "Error"},
    "Здорово": {"ru": "Здорово", "en": "Healthy"},
    "Аномалія": {"ru": "Аномалия", "en": "Anomaly"},
    "Деградація": {"ru": "Деградация", "en": "Degradation"},
    "Ескалація": {"ru": "Эскалация", "en": "Escalation"},
    "Працює": {"ru": "Работает", "en": "Working"},
    "Зупинено": {"ru": "Остановлено", "en": "Stopped"},
    "Виконано": {"ru": "Выполнено", "en": "Completed"},
    "Виконується": {"ru": "Выполняется", "en": "In progress"},
    "Завершено": {"ru": "Завершено", "en": "Finished"},
    "Заплановано": {"ru": "Запланировано", "en": "Scheduled"},
    "Створено": {"ru": "Создано", "en": "Created"},
    "Скасовано": {"ru": "Отменено", "en": "Cancelled"},
    "Закрито": {"ru": "Закрыто", "en": "Closed"},
    "Закрита": {"ru": "Закрыта", "en": "Closed"},
    "Відкрито": {"ru": "Открыто", "en": "Open"},
    "Перевірено": {"ru": "Проверено", "en": "Verified"},
    "Підтверджено": {"ru": "Подтверждено", "en": "Confirmed"},
    "Схвалено": {"ru": "Одобрено", "en": "Approved"},
    "Скориговано": {"ru": "Скорректировано", "en": "Adjusted"},
    "Виплачено": {"ru": "Выплачено", "en": "Paid out"},
    "Надіслано": {"ru": "Отправлено", "en": "Sent"},
    "Показано": {"ru": "Показано", "en": "Shown"},
    "Перенесено": {"ru": "Перенесено", "en": "Rescheduled"},
    "Дубль": {"ru": "Дубль", "en": "Duplicate"},
    "Backlog": {"ru": "Бэклог", "en": "Backlog"},
    "В роботі": {"ru": "В работе", "en": "In progress"},
    "Чернетка": {"ru": "Черновик", "en": "Draft"},
    "Архів": {"ru": "Архив", "en": "Archive"},
    "Пауза": {"ru": "Пауза", "en": "Paused"},
    "Свято": {"ru": "Праздник", "en": "Holiday"},
    "Лікарняний": {"ru": "Больничный", "en": "Sick leave"},
    "Відпустка": {"ru": "Отпуск", "en": "Vacation"},
    "Робочий день": {"ru": "Рабочий день", "en": "Working day"},
    "Вихідний": {"ru": "Выходной", "en": "Day off"},
    "Форс-мажор": {"ru": "Форс-мажор", "en": "Force majeure"},
    "Нова": {"ru": "Новая", "en": "New"},
    "Новий": {"ru": "Новый", "en": "New"},
    "Інший": {"ru": "Другой", "en": "Other"},
    "Інше": {"ru": "Другое", "en": "Other"},
    "Меньше": {"ru": "Меньше", "en": "Less"},
    "Без телефону": {"ru": "Без телефона", "en": "Without phone"},
    "Телефон": {"ru": "Телефон", "en": "Phone"},
    "Каталог": {"ru": "Каталог", "en": "Catalog"},
    "Категорія": {"ru": "Категория", "en": "Category"},

    # ===== Push / iOS =====
    "iPhone та iPad: як увімкнути push правильно": {
        "ru": "iPhone и iPad: как включить push правильно",
        "en": "iPhone & iPad: how to enable push correctly",
    },
    "У Safari або іншому браузері відкрийте меню “Поділитися”.": {
        "ru": "В Safari или другом браузере откройте меню «Поделиться».",
        "en": "In Safari or another browser open the \"Share\" menu.",
    },
    "Натисніть “На екран Додому”, а потім відкрийте TwoComms з іконки.": {
        "ru": "Нажмите «На экран Домой», затем откройте TwoComms с иконки.",
        "en": "Tap \"Add to Home Screen\", then open TwoComms from the icon.",
    },
    "Після запуску як вебзастосунку дозвольте системні push-сповіщення.": {
        "ru": "После запуска как веб-приложения разрешите системные push-уведомления.",
        "en": "After launching as a web app, allow system push notifications.",
    },
    "Перевіряємо стан push…": {"ru": "Проверяем состояние push…", "en": "Checking push status…"},
    "Увімкнути в цьому браузері": {"ru": "Включить в этом браузере", "en": "Enable in this browser"},
    "Вимкнути в цьому браузері": {"ru": "Отключить в этом браузере", "en": "Disable in this browser"},
    "Усі активні підписки": {"ru": "Все активные подписки", "en": "All active subscriptions"},
    "Активна сесія": {"ru": "Активная сессия", "en": "Active session"},
    "Недійсна підписка": {"ru": "Недействительная подписка", "en": "Invalid subscription"},
    "Недійсний": {"ru": "Недействительный", "en": "Invalid"},

    # ===== Brand intro on cooperation =====
    "Іван Петренко": {"ru": "Иван Петренко", "en": "Ivan Petrenko"},
    "Синіло Артем Віталійович": {
        "ru": "Синило Артём Витальевич",
        "en": "Artem Synilo",
    },
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17R)


# ============================================================
# Phase 17z — pro_brand / cooperation / wholesale / custom_print /
# user_points / promocodes / add-print / profile / search.
# ============================================================
TRANSLATIONS_PHASE_17Z: dict[str, dict[str, str]] = {
    # ----- 404 / generic -----
    "Повернутися на головну": {"ru": "Вернуться на главную", "en": "Return to home"},

    # ----- add-print (UGC) -----
    "Запропонувати принт — TwoComms": {
        "ru": "Предложить принт — TwoComms",
        "en": "Suggest a print — TwoComms",
    },
    "Запропонувати принт": {"ru": "Предложить принт", "en": "Suggest a print"},
    "Сторінка для пропозицій принтів від спільноти TwoComms. Надішліть ідею, файл або посилання та отримайте винагороду після модерації. Службовий UGC-розділ, не основна сторінка кастомного друку.": {
        "ru": "Страница для предложений принтов от сообщества TwoComms. Отправьте идею, файл или ссылку и получите вознаграждение после модерации. Служебный UGC-раздел, не основная страница кастомной печати.",
        "en": "Page for print suggestions from the TwoComms community. Send an idea, file or link and receive a reward after moderation. Service UGC section, not the main custom print page.",
    },
    "Надішліть ідею принта для TwoComms, щоб отримати промокод або бали після модерації. Для замовлення свого принта використовуйте окремий конфігуратор кастомного друку.": {
        "ru": "Отправьте идею принта для TwoComms, чтобы получить промокод или баллы после модерации. Для заказа своего принта используйте отдельный конфигуратор кастомной печати.",
        "en": "Send a print idea for TwoComms to get a promo code or points after moderation. To order your own print use the dedicated custom print configurator.",
    },
    "UGC-сторінка для ідей принтів TwoComms. Для кастомного друку на замовлення використовуйте окремий конфігуратор бренду.": {
        "ru": "UGC-страница для идей принтов TwoComms. Для кастомной печати на заказ используйте отдельный конфигуратор бренда.",
        "en": "UGC page for TwoComms print ideas. For custom print on demand use the brand's dedicated configurator.",
    },
    "Поділись ідеєю — отримай винагороду": {
        "ru": "Поделись идеей — получи вознаграждение",
        "en": "Share an idea — get rewarded",
    },
    "Унікальні принти з брендовим написом або логотипом можуть принести <strong>бали</strong> або <strong>промокод 200–1000 грн</strong>.": {
        "ru": "Уникальные принты с брендовой надписью или логотипом могут принести <strong>баллы</strong> или <strong>промокод 200–1000 грн</strong>.",
        "en": "Unique prints with brand wording or logo can earn you <strong>points</strong> or a <strong>promo code worth 200–1000 UAH</strong>.",
    },
    "Промокод 200–1000 грн": {"ru": "Промокод 200–1000 грн", "en": "Promo code 200–1000 UAH"},
    "Для унікальних принтів з брендовим написом або логотипом.": {
        "ru": "Для уникальных принтов с брендовой надписью или логотипом.",
        "en": "For unique prints with brand wording or logo.",
    },
    "Бали до профілю": {"ru": "Баллы в профиль", "en": "Points to your profile"},
    "Накопичуйте бали і обмінюйте їх на знижки або добрі справи.": {
        "ru": "Накапливайте баллы и обменивайте их на скидки или добрые дела.",
        "en": "Accumulate points and exchange them for discounts or good deeds.",
    },
    "Зв’язок з нами": {"ru": "Связь с нами", "en": "Get in touch"},
    "Вкажіть <a href=\"%(profile_setup_url)s\">Telegram/Instagram</a> у профілі — ми зв’яжемось.": {
        "ru": "Укажите <a href=\"%(profile_setup_url)s\">Telegram/Instagram</a> в профиле — мы свяжемся.",
        "en": "Add <a href=\"%(profile_setup_url)s\">Telegram/Instagram</a> to your profile — we'll reach out.",
    },
    "Надіслати пропозицію принта": {
        "ru": "Отправить предложение принта",
        "en": "Submit a print proposal",
    },
    "до 10 МБ": {"ru": "до 10 МБ", "en": "up to 10 MB"},
    "Антиспам 1 хв": {"ru": "Антиспам 1 мин", "en": "Anti-spam 1 min"},
    "Занадто часто. Спробуйте ще раз за хвилину.": {
        "ru": "Слишком часто. Попробуйте снова через минуту.",
        "en": "Too frequent. Try again in a minute.",
    },
    "Помилка:": {"ru": "Ошибка:", "en": "Error:"},
    "Перетягніть зображення сюди або натисніть для вибору": {
        "ru": "Перетащите изображение сюда или нажмите для выбора",
        "en": "Drag an image here or click to select",
    },
    "Очистити": {"ru": "Очистить", "en": "Clear"},
    "Або посилання на зображення": {
        "ru": "Или ссылка на изображение",
        "en": "Or a link to the image",
    },
    "Опис / примітки": {"ru": "Описание / заметки", "en": "Description / notes"},
    "Опишіть ідею принта та деталі": {
        "ru": "Опишите идею принта и детали",
        "en": "Describe your print idea and details",
    },
    "Вкажіть <a href=\"%(profile_setup_url)s\">Telegram або Instagram у налаштуваннях профілю</a>, щоб ми могли зв’язатися.": {
        "ru": "Укажите <a href=\"%(profile_setup_url)s\">Telegram или Instagram в настройках профиля</a>, чтобы мы могли связаться.",
        "en": "Add <a href=\"%(profile_setup_url)s\">Telegram or Instagram to your profile settings</a> so we can contact you.",
    },
    "Надіслати": {"ru": "Отправить", "en": "Send"},
    "Співпраця для дизайнерів": {
        "ru": "Сотрудничество для дизайнеров",
        "en": "Collaboration for designers",
    },
    "Мої принти на розгляді": {"ru": "Мои принты на рассмотрении", "en": "My prints under review"},
    "Останні 10": {"ru": "Последние 10", "en": "Last 10"},
    "+%(pts)s балів": {"ru": "+%(pts)s баллов", "en": "+%(pts)s points"},
    "Промокод:": {"ru": "Промокод:", "en": "Promo code:"},
    "Поки немає заявок": {"ru": "Пока нет заявок", "en": "No submissions yet"},
    "Авторизація потрібна": {"ru": "Требуется авторизация", "en": "Sign-in required"},
    "Авторизуйтеся, щоб мати можливість запропонувати принт та накопичувати бали.": {
        "ru": "Авторизуйтесь, чтобы предлагать принты и накапливать баллы.",
        "en": "Sign in to be able to suggest prints and earn points.",
    },
    "Будь ласка, завантажте зображення або введіть посилання на зображення": {
        "ru": "Пожалуйста, загрузите изображение или введите ссылку на изображение",
        "en": "Please upload an image or enter an image URL",
    },
    "Відправляємо...": {"ru": "Отправляем...", "en": "Sending..."},
    "Принт успішно відправлено на розгляд!": {
        "ru": "Принт успешно отправлен на рассмотрение!",
        "en": "Print submitted for review!",
    },
    "Балів": {"ru": "Баллов", "en": "Points"},
    "балів": {"ru": "баллов", "en": "points"},
    "Помилка відправки:": {"ru": "Ошибка отправки:", "en": "Submission error:"},
    "Невідома помилка": {"ru": "Неизвестная ошибка", "en": "Unknown error"},
    "Помилка відправки форми": {"ru": "Ошибка отправки формы", "en": "Form submission error"},
    "Зареєструватись": {"ru": "Зарегистрироваться", "en": "Sign up"},

    # ----- pro_brand.html -----
    "Про бренд TwoComms": {"ru": "О бренде TwoComms", "en": "About TwoComms brand"},
    "Продовження.": {"ru": "Продолжение.", "en": "A continuation."},
    "Дивитись каталог": {"ru": "Смотреть каталог", "en": "Browse catalog"},
    "Зрозуміти бренд": {"ru": "Понять бренд", "en": "Understand the brand"},
    "Український streetwear": {"ru": "Украинский streetwear", "en": "Ukrainian streetwear"},
    "Що таке TwoComms": {"ru": "Что такое TwoComms", "en": "What TwoComms is"},
    "Походження": {"ru": "Происхождение", "en": "Origins"},
    "Принти": {"ru": "Принты", "en": "Prints"},
    "Код бренду": {"ru": "Код бренда", "en": "Brand code"},
    "Назва бренду": {"ru": "Название бренда", "en": "Brand name"},
    "Походження бренду": {"ru": "Происхождение бренда", "en": "Brand origin"},
    "Візуальна історія": {"ru": "Визуальная история", "en": "Visual story"},
    "ДНК Бренду": {"ru": "ДНК бренда", "en": "Brand DNA"},
    "Харків у ДНК TwoComms": {
        "ru": "Харьков в ДНК TwoComms",
        "en": "Kharkiv in TwoComms DNA",
    },
    "Принти як": {"ru": "Принты как", "en": "Prints as"},
    "візуальні коди": {"ru": "визуальные коды", "en": "visual codes"},
    "Код ідеї": {"ru": "Код идеи", "en": "Idea code"},
    "Харківський код TwoComms у принті про характер міста": {
        "ru": "Харьковский код TwoComms в принте о характере города",
        "en": "Kharkiv code of TwoComms in a print about the city's character",
    },
    "Контроль": {"ru": "Контроль", "en": "Control"},
    "Маршрут покупки": {"ru": "Маршрут покупки", "en": "Purchase journey"},
    "Повага після покупки": {"ru": "Уважение после покупки", "en": "Respect after the purchase"},
    "14 днів для готових товарів": {
        "ru": "14 дней для готовых товаров",
        "en": "14 days for ready-made items",
    },
    "Для кого TwoComms": {"ru": "Для кого TwoComms", "en": "Who TwoComms is for"},
    "Наступний контур": {"ru": "Следующий контур", "en": "Next contour"},
    "Ми продовжуємо.": {"ru": "Мы продолжаем.", "en": "We continue."},
    "Відповіді": {"ru": "Ответы", "en": "Answers"},
    "Запитання про бренд": {"ru": "Вопросы о бренде", "en": "Brand questions"},
    "Фінальний крок": {"ru": "Финальный шаг", "en": "Final step"},
    "Обери свою форму продовження": {
        "ru": "Выбери свою форму продолжения",
        "en": "Choose your form of continuation",
    },
    "Колір %(hex)s": {"ru": "Цвет %(hex)s", "en": "Color %(hex)s"},
    "Швидка навігація": {"ru": "Быстрая навигация", "en": "Quick navigation"},
    "Перейти": {"ru": "Перейти", "en": "Go"},
    "Відкрити розділ": {"ru": "Открыть раздел", "en": "Open section"},
    "Підтверджені сітки по категоріях": {
        "ru": "Подтверждённые сетки по категориям",
        "en": "Confirmed grids by category",
    },
    "Поради по посадці": {"ru": "Советы по посадке", "en": "Fit tips"},
    "Інші візуальні схеми": {"ru": "Другие визуальные схемы", "en": "Other visual schemes"},
    "Свіжі товарні сторінки": {"ru": "Свежие карточки товаров", "en": "Fresh product pages"},
    "Поширені запитання": {"ru": "Часто задаваемые вопросы", "en": "Frequently asked questions"},

    # ----- user_points.html -----
    "Мої бали": {"ru": "Мои баллы", "en": "My points"},
    "Поточний баланс": {"ru": "Текущий баланс", "en": "Current balance"},
    "Всього зароблено": {"ru": "Всего заработано", "en": "Total earned"},
    "Всього витрачено": {"ru": "Всего потрачено", "en": "Total spent"},
    "Заробляйте бали": {"ru": "Зарабатывайте баллы", "en": "Earn points"},
    "Отримуйте бали за кожну покупку. Кількість балів залежить від товару.": {
        "ru": "Получайте баллы за каждую покупку. Количество зависит от товара.",
        "en": "Earn points on every purchase. The amount depends on the item.",
    },
    "Використовуйте бали": {"ru": "Используйте баллы", "en": "Use your points"},
    "Історія балів": {"ru": "История баллов", "en": "Points history"},
    "Операція з балами": {"ru": "Операция с баллами", "en": "Points operation"},
    "Зробіть першу покупку, щоб почати заробляти бали!": {
        "ru": "Сделайте первую покупку, чтобы начать зарабатывать баллы!",
        "en": "Make your first purchase to start earning points!",
    },
    "Зароблено:": {"ru": "Заработано:", "en": "Earned:"},
    "Витрачено:": {"ru": "Потрачено:", "en": "Spent:"},
    "Бали не знайдено": {"ru": "Баллы не найдены", "en": "Points not found"},
    "Об'єкт UserPoints не створено для користувача": {
        "ru": "Объект UserPoints не создан для пользователя",
        "en": "UserPoints object not created for user",
    },
    "Ваш баланс балів": {"ru": "Ваш баланс баллов", "en": "Your points balance"},
    "Вартість:": {"ru": "Стоимость:", "en": "Cost:"},
    "Пожертвувати": {"ru": "Пожертвовать", "en": "Donate"},
    "Недостатньо балів": {"ru": "Недостаточно баллов", "en": "Not enough points"},
    "Підтримка ЗСУ": {"ru": "Поддержка ВСУ", "en": "UA Army support"},
    "опціонально": {"ru": "опционально", "en": "optional"},

    # ----- profile setup -----
    "Аватар користувача - TwoComms": {
        "ru": "Аватар пользователя - TwoComms",
        "en": "User avatar - TwoComms",
    },
    "Фото профілю": {"ru": "Фото профиля", "en": "Profile photo"},
    "Натисніть на фото для завантаження (JPG, PNG до 5 МБ)": {
        "ru": "Нажмите на фото для загрузки (JPG, PNG до 5 МБ)",
        "en": "Click the photo to upload (JPG, PNG up to 5 MB)",
    },
    "Основна інформація": {"ru": "Основная информация", "en": "Main info"},
    "Переприв'язати Telegram": {"ru": "Перепривязать Telegram", "en": "Relink Telegram"},
    "Отримувати статуси": {"ru": "Получать статусы", "en": "Receive status updates"},
    "Статус УБД": {"ru": "Статус УБД", "en": "Combatant (UBD) status"},
    "Отримайте спеціальні умови та знижки": {
        "ru": "Получите особые условия и скидки",
        "en": "Get special terms and discounts",
    },
    "Завантажте фото посвідчення УБД": {
        "ru": "Загрузите фото удостоверения УБД",
        "en": "Upload a photo of your UBD certificate",
    },
    "Скасувати": {"ru": "Отменить", "en": "Cancel"},
    "Push-сповіщення": {"ru": "Push-уведомления", "en": "Push notifications"},
    "Нові дропи та спецпропозиції": {
        "ru": "Новые дропы и спецпредложения",
        "en": "New drops and special offers",
    },
    "Статуси замовлення та доставки": {
        "ru": "Статусы заказа и доставки",
        "en": "Order and delivery status",
    },
    "Зберегти push-налаштування": {
        "ru": "Сохранить push-настройки",
        "en": "Save push settings",
    },

    # ----- search results -----
    "Результати пошуку TwoComms": {
        "ru": "Результаты поиска TwoComms",
        "en": "TwoComms search results",
    },
    "Купити %(cat)s в TwoComms. Якісний стріт & мілітарі одяг з ексклюзивним дизайном і швидкою доставкою по Україні.": {
        "ru": "Купить %(cat)s в TwoComms. Качественная стрит & милитари одежда с эксклюзивным дизайном и быстрой доставкой по Украине.",
        "en": "Buy %(cat)s at TwoComms. Quality street & military apparel with exclusive design and fast shipping across Ukraine.",
    },
    "стріт одяг, мілітарі одяг, футболки, худі, лонгсліви, TwoComms, ексклюзивний дизайн, якісний одяг, каталог одягу": {
        "ru": "стрит одежда, милитари одежда, футболки, худи, лонгсливы, TwoComms, эксклюзивный дизайн, качественная одежда, каталог одежды",
        "en": "street apparel, military apparel, t-shirts, hoodies, longsleeves, TwoComms, exclusive design, quality clothing, apparel catalog",
    },
    "Результати пошуку TwoComms за товарами каталогу.": {
        "ru": "Результаты поиска TwoComms по товарам каталога.",
        "en": "TwoComms search results across catalog items.",
    },
    "Повний каталог стріт та мілітарі одягу TwoComms.": {
        "ru": "Полный каталог стрит и милитари одежды TwoComms.",
        "en": "Full TwoComms street & military apparel catalog.",
    },
    "Каталог стріт та мілітарі одягу TwoComms.": {
        "ru": "Каталог стрит и милитари одежды TwoComms.",
        "en": "TwoComms street & military apparel catalog.",
    },
    "Каталог TwoComms": {"ru": "Каталог TwoComms", "en": "TwoComms catalog"},
    "Результати пошуку: «%(q)s»": {
        "ru": "Результаты поиска: «%(q)s»",
        "en": "Search results: \"%(q)s\"",
    },

    # ----- cooperation -----
    "Співпраця з TwoComms": {"ru": "Сотрудничество с TwoComms", "en": "Cooperation with TwoComms"},
    "Співпраця з": {"ru": "Сотрудничество с", "en": "Cooperation with"},
    "Преміум товари": {"ru": "Премиум-товары", "en": "Premium goods"},
    "Надійність": {"ru": "Надёжность", "en": "Reliability"},
    "Перевірені партнери": {"ru": "Проверенные партнёры", "en": "Verified partners"},
    "24/7 консультації": {"ru": "Консультации 24/7", "en": "24/7 consultations"},
    "Дізнатися більше": {"ru": "Узнать больше", "en": "Learn more"},
    "Типи співпраці": {"ru": "Типы сотрудничества", "en": "Cooperation types"},
    "Без вкладень у товар": {"ru": "Без вложений в товар", "en": "No upfront stock investment"},
    "Автоматична обробка замовлень": {
        "ru": "Автоматическая обработка заказов",
        "en": "Automated order processing",
    },
    "Підтримка 24/7": {"ru": "Поддержка 24/7", "en": "24/7 support"},
    "Почати співпрацю": {"ru": "Начать сотрудничество", "en": "Start the partnership"},
    "Оптові продажі": {"ru": "Оптовые продажи", "en": "Wholesale sales"},
    "Старт від 8 одиниць на замовлення": {
        "ru": "Старт от 8 единиц на заказ",
        "en": "Start from 8 units per order",
    },
    "Перейти до оптових умов": {"ru": "Перейти к оптовым условиям", "en": "View wholesale terms"},
    "Ексклюзивні колекції": {"ru": "Эксклюзивные коллекции", "en": "Exclusive collections"},
    "Спільний маркетинг": {"ru": "Совместный маркетинг", "en": "Joint marketing"},
    "Персональний менеджер": {"ru": "Персональный менеджер", "en": "Personal manager"},
    "Обговорити проект": {"ru": "Обсудить проект", "en": "Discuss the project"},
    "Для дизайнерів": {"ru": "Для дизайнеров", "en": "For designers"},
    "Швидка реалізація": {"ru": "Быстрая реализация", "en": "Fast turnaround"},
    "Підтримка команди": {"ru": "Поддержка команды", "en": "Team support"},
    "Оплата речами": {"ru": "Оплата вещами", "en": "Payment in goods"},
    "Безкоштовні товари": {"ru": "Бесплатные товары", "en": "Complimentary items"},
    "Розвиток портфоліо": {"ru": "Развитие портфолио", "en": "Portfolio growth"},
    "Стати моделлю": {"ru": "Стать моделью", "en": "Become a model"},
    "Ексклюзивний контент": {"ru": "Эксклюзивный контент", "en": "Exclusive content"},
    "Готові почати співпрацю?": {
        "ru": "Готовы начать сотрудничество?",
        "en": "Ready to start cooperating?",
    },
    "Зв'яжіться з нами в Telegram для обговорення деталей": {
        "ru": "Свяжитесь с нами в Telegram для обсуждения деталей",
        "en": "Reach us on Telegram to discuss the details",
    },
    "Загальні питання": {"ru": "Общие вопросы", "en": "General questions"},
    "Технічна підтримка": {"ru": "Техническая поддержка", "en": "Technical support"},
    "Поширені питання про співпрацю": {
        "ru": "Часто задаваемые вопросы о сотрудничестве",
        "en": "Cooperation FAQ",
    },

    # ----- custom_print / configurator -----
    "Кастомний принт для брендів": {
        "ru": "Кастомный принт для брендов",
        "en": "Custom print for brands",
    },
    "DTF-конфігуратор кастомного одягу — TwoComms": {
        "ru": "DTF-конфигуратор кастомной одежды — TwoComms",
        "en": "DTF custom apparel configurator — TwoComms",
    },
    "Кастомний DTF-друк на одязі": {
        "ru": "Кастомная DTF-печать на одежде",
        "en": "Custom DTF print on apparel",
    },
    "Кастомний одяг TwoComms": {
        "ru": "Кастомная одежда TwoComms",
        "en": "TwoComms custom apparel",
    },
    "Почати створення": {"ru": "Начать создание", "en": "Start creating"},
    "Написати нам у Telegram": {"ru": "Написать нам в Telegram", "en": "Message us on Telegram"},
    "Переваги кастомного друку": {
        "ru": "Преимущества кастомной печати",
        "en": "Benefits of custom print",
    },
    "Преміум якість друку": {"ru": "Премиум-качество печати", "en": "Premium print quality"},
    "Будь-який тип одягу": {"ru": "Любой тип одежды", "en": "Any garment type"},
    "Приклад кастомного одягу TwoComms": {
        "ru": "Пример кастомной одежды TwoComms",
        "en": "Example of TwoComms custom apparel",
    },
    "ваш принт": {"ru": "ваш принт", "en": "your print"},
    "Приклади кольорів": {"ru": "Примеры цветов", "en": "Color examples"},
    "20+ кольорів": {"ru": "20+ цветов", "en": "20+ colors"},
    "Перейти до вибору кольору": {"ru": "Перейти к выбору цвета", "en": "Go to color picker"},
    "Можливості конфігуратора": {
        "ru": "Возможности конфигуратора",
        "en": "Configurator capabilities",
    },
    "Тип одягу": {"ru": "Тип одежды", "en": "Garment type"},
    "Прогрес конфігурації": {"ru": "Прогресс конфигурации", "en": "Configuration progress"},
    "Виріб на сцені": {"ru": "Изделие на сцене", "en": "Garment on stage"},
    "Виріб і зона друку": {"ru": "Изделие и зона печати", "en": "Garment and print area"},
    "Сторона виробу": {"ru": "Сторона изделия", "en": "Garment side"},
    "Спереди": {"ru": "Спереди", "en": "Front"},
    "Доступні зони друку": {"ru": "Доступные зоны печати", "en": "Available print zones"},
    "Зони друку на сцені": {"ru": "Зоны печати на сцене", "en": "Print zones on stage"},
    "Виріб": {"ru": "Изделие", "en": "Garment"},
    "Оберіть виріб, щоб побачити деталі.": {
        "ru": "Выберите изделие, чтобы увидеть детали.",
        "en": "Pick a garment to view the details.",
    },
    "Активні зони": {"ru": "Активные зоны", "en": "Active zones"},
    "Без додаткових деталей.": {"ru": "Без дополнительных деталей.", "en": "No extra details."},
    "Базова конфігурація": {"ru": "Базовая конфигурация", "en": "Base configuration"},
    "Для кого збираємо?": {"ru": "Для кого собираем?", "en": "Who is it for?"},
    "2. Виріб": {"ru": "2. Изделие", "en": "2. Garment"},
    "Обери основу": {"ru": "Выбери основу", "en": "Pick the base"},
    "На чому буде твій дизайн?": {"ru": "На чём будет твой дизайн?", "en": "What will host your design?"},
    "Налаштування": {"ru": "Настройки", "en": "Settings"},
    "3. Налаштування": {"ru": "3. Настройки", "en": "3. Settings"},
    "Налаштуй вигляд і відчуття": {"ru": "Настрой вид и ощущение", "en": "Tune look and feel"},
    "Тканина": {"ru": "Ткань", "en": "Fabric"},
    "Колір виробу": {"ru": "Цвет изделия", "en": "Garment color"},
    "Деталі худі": {"ru": "Детали худи", "en": "Hoodie details"},
    "Далі до зон": {"ru": "Дальше к зонам", "en": "Next: zones"},
    "Зони друку": {"ru": "Зоны печати", "en": "Print zones"},
    "4. Зони друку": {"ru": "4. Зоны печати", "en": "4. Print zones"},
    "Де буде принт?": {"ru": "Где будет принт?", "en": "Where will the print go?"},
    "Спереду": {"ru": "Спереди", "en": "Front"},
    "Далі до макета": {"ru": "Дальше к макету", "en": "Next: layout"},
    "Бриф / завдання": {"ru": "Бриф / задание", "en": "Brief / task"},
    "Далі до кількості": {"ru": "Дальше к количеству", "en": "Next: quantity"},
    "6. Кількість і розміри": {"ru": "6. Количество и размеры", "en": "6. Quantity & sizes"},
    "B2B знижка": {"ru": "B2B скидка", "en": "B2B discount"},
    "Уточнити розміри з менеджером": {
        "ru": "Уточнить размеры с менеджером",
        "en": "Clarify sizes with the manager",
    },
    "Примітка по розмірах": {"ru": "Примечание по размерам", "en": "Size note"},
    "Опис свого виробу": {"ru": "Описание своего изделия", "en": "Describe your item"},
    "Далі до подарунку": {"ru": "Дальше к подарку", "en": "Next: gift"},
    "Подарунок": {"ru": "Подарок", "en": "Gift"},
    "7. Подарунок": {"ru": "7. Подарок", "en": "7. Gift"},
    "Потрібна подарункова упаковка?": {
        "ru": "Нужна подарочная упаковка?",
        "en": "Need gift wrap?",
    },
    "Додати упаковку": {"ru": "Добавить упаковку", "en": "Add gift wrap"},
    "Контакт": {"ru": "Контакт", "en": "Contact"},
    "8. Контакт і фінал": {"ru": "8. Контакт и финал", "en": "8. Contact & final"},
    "Ім'я для менеджера": {"ru": "Имя для менеджера", "en": "Name for the manager"},
    "Бренд / Instagram": {"ru": "Бренд / Instagram", "en": "Brand / Instagram"},
    "Канал звʼязку": {"ru": "Канал связи", "en": "Contact channel"},
    "Передзамовлення зі снимком конфігурації": {
        "ru": "Предзаказ со снимком конфигурации",
        "en": "Pre-order with configuration snapshot",
    },
    "Надіслати менеджеру": {"ru": "Отправить менеджеру", "en": "Send to manager"},
    "Поширені питання про кастомний друк": {
        "ru": "Часто задаваемые вопросы о кастомной печати",
        "en": "Custom print FAQ",
    },
    "Умови": {"ru": "Условия", "en": "Terms"},
    "Подивіться, як відбувається відправка, оплата та підтвердження замовлень по Україні.": {
        "ru": "Посмотрите, как происходит отправка, оплата и подтверждение заказов по Украине.",
        "en": "See how shipping, payment and order confirmation work across Ukraine.",
    },

    # ----- promocodes -----
    "Активні промокоди": {"ru": "Активные промокоды", "en": "Active promo codes"},
    "Промокод готовий до використання": {
        "ru": "Промокод готов к использованию",
        "en": "Promo code ready to use",
    },
    "Використані промокоди": {"ru": "Использованные промокоды", "en": "Used promo codes"},
    "Використано": {"ru": "Использовано", "en": "Used"},
    "Дата використання:": {"ru": "Дата использования:", "en": "Used on:"},
    "Замовлення:": {"ru": "Заказ:", "en": "Order:"},
    "Сума замовлення:": {"ru": "Сумма заказа:", "en": "Order total:"},
    "Промокод використано успішно": {
        "ru": "Промокод успешно использован",
        "en": "Promo code applied successfully",
    },
    "Поки що немає промокодів": {"ru": "Пока нет промокодов", "en": "No promo codes yet"},
    "Перейти до каталогу": {"ru": "Перейти в каталог", "en": "Go to catalog"},
    "Відділення №1, вул. Хрещатик, 1": {
        "ru": "Отделение №1, ул. Крещатик, 1",
        "en": "Branch No. 1, 1 Khreshchatyk St.",
    },
    "10%% знижка на наступне замовлення": {
        "ru": "10%% скидка на следующий заказ",
        "en": "10%% off your next order",
    },

    # ----- wholesale -----
    "Оптові закупівлі та дропшипінг одягу — TwoComms": {
        "ru": "Оптовые закупки и дропшиппинг одежды — TwoComms",
        "en": "Wholesale and dropshipping apparel — TwoComms",
    },
    "Оптові продажі та дропшипінг TwoComms": {
        "ru": "Оптовые продажи и дропшиппинг TwoComms",
        "en": "TwoComms wholesale and dropshipping",
    },
    "Оптові закупівлі": {"ru": "Оптовые закупки", "en": "Wholesale purchases"},
    "Напишіть нам для співпраці в Telegram": {
        "ru": "Напишите нам для сотрудничества в Telegram",
        "en": "Message us on Telegram for cooperation",
    },
    "Сформувати замовлення": {"ru": "Сформировать заказ", "en": "Build order"},
    "Завантажити прайс (Excel)": {
        "ru": "Скачать прайс (Excel)",
        "en": "Download price list (Excel)",
    },
    "Оптові ціни TwoComms": {"ru": "Оптовые цены TwoComms", "en": "TwoComms wholesale prices"},
    "Завантажити прайс-лист (XLSX)": {
        "ru": "Скачать прайс-лист (XLSX)",
        "en": "Download price list (XLSX)",
    },
    "Умови опту:": {"ru": "Условия опта:", "en": "Wholesale terms:"},
    "Худі:": {"ru": "Худи:", "en": "Hoodies:"},
    "TwoComms логотип": {"ru": "TwoComms логотип", "en": "TwoComms logo"},
    "TwoComms логотип - декоративний": {
        "ru": "TwoComms логотип — декоративный",
        "en": "TwoComms logo — decorative",
    },
    "Система ціноутворення": {"ru": "Система ценообразования", "en": "Pricing system"},
    "Дроп ціни - TwoComms": {"ru": "Дроп-цены — TwoComms", "en": "Dropship prices — TwoComms"},
    "Дроп ціни": {"ru": "Дроп-цены", "en": "Dropship prices"},
    "Класичні футболки": {"ru": "Классические футболки", "en": "Classic t-shirts"},
    "Футболки опт - TwoComms": {
        "ru": "Футболки опт — TwoComms",
        "en": "Wholesale t-shirts — TwoComms",
    },
    "Футболки опт": {"ru": "Футболки опт", "en": "Wholesale t-shirts"},
    "Мінімальне замовлення": {"ru": "Минимальный заказ", "en": "Minimum order"},
    "Середній обсяг": {"ru": "Средний объём", "en": "Medium volume"},
    "Великий обсяг": {"ru": "Большой объём", "en": "Large volume"},
    "Оптовий обсяг": {"ru": "Оптовый объём", "en": "Wholesale volume"},
    "Худі опт - TwoComms": {"ru": "Худи опт — TwoComms", "en": "Wholesale hoodies — TwoComms"},
    "Категорії товарів": {"ru": "Категории товаров", "en": "Product categories"},
    "Всі товари": {"ru": "Все товары", "en": "All items"},
    "Всі товари категорія - TwoComms": {
        "ru": "Категория «Все товары» — TwoComms",
        "en": "All products category — TwoComms",
    },
    "Худі категорія - TwoComms": {
        "ru": "Категория «Худи» — TwoComms",
        "en": "Hoodies category — TwoComms",
    },
    "Футболки категорія - TwoComms": {
        "ru": "Категория «Футболки» — TwoComms",
        "en": "T-shirts category — TwoComms",
    },
    "Наші товари": {"ru": "Наши товары", "en": "Our products"},
    "Якісний стріт & мілітарі одяг з ексклюзивними принтами": {
        "ru": "Качественная стрит & милитари одежда с эксклюзивными принтами",
        "en": "Quality street & military apparel with exclusive prints",
    },
    "Оптові ціни:": {"ru": "Оптовые цены:", "en": "Wholesale prices:"},
    "Кастомний друк для партій": {
        "ru": "Кастомная печать для партий",
        "en": "Custom print for batches",
    },
    "Зв'язок з менеджером": {"ru": "Связь с менеджером", "en": "Reach the manager"},
    "Завантажуємо розділ…": {"ru": "Загружаем раздел…", "en": "Loading section…"},
    "Соцмережа": {"ru": "Соцсеть", "en": "Social network"},
    "Продовжити покупки": {"ru": "Продолжить покупки", "en": "Continue shopping"},
    "Оплатити через mono checkout": {
        "ru": "Оплатить через mono checkout",
        "en": "Pay via mono checkout",
    },
    "Оплатити через": {"ru": "Оплатить через", "en": "Pay via"},
    "Функція формування замовлень в розробці. Звертайтесь через Telegram: https://t.me/twocomms": {
        "ru": "Функция формирования заказов в разработке. Свяжитесь через Telegram: https://t.me/twocomms",
        "en": "Order generation feature is in development. Reach us via Telegram: https://t.me/twocomms",
    },

    # ----- product_detail_new generic UI -----
    "грн / шт": {"ru": "грн / шт", "en": "UAH / pc"},
    "грн/шт": {"ru": "грн/шт", "en": "UAH/pc"},
    "Дізнатись про бали": {"ru": "Узнать о баллах", "en": "Learn about points"},
    "б.": {"ru": "б.", "en": "pt"},
    "Розмір": {"ru": "Размер", "en": "Size"},
    "Колір": {"ru": "Цвет", "en": "Color"},
    "Зменшити": {"ru": "Уменьшить", "en": "Decrease"},
    "Збільшити": {"ru": "Увеличить", "en": "Increase"},
    "Додати в кошик": {"ru": "Добавить в корзину", "en": "Add to cart"},
    "Немає в наявності": {"ru": "Нет в наличии", "en": "Out of stock"},
    "Додати до обраних": {"ru": "Добавить в избранное", "en": "Add to favorites"},

    # ----- B2B / cart -----
    "B2B знижка:": {"ru": "B2B скидка:", "en": "B2B discount:"},

    # ----- pro_brand additional misc -----
    "TwoComms — харківський бренд одягу про характер, стійкість і продовження": {
        "ru": "TwoComms — харьковский бренд одежды о характере, стойкости и продолжении",
        "en": "TwoComms — Kharkiv apparel brand about character, resilience and continuation",
    },
    "TwoComms — український streetwear / military-adjacent бренд одягу з Харкова. Речі з кодом, сенсовими принтами, якісною базою і філософією: не крапка, а продовження.": {
        "ru": "TwoComms — украинский streetwear / military-adjacent бренд одежды из Харькова. Вещи с кодом, смысловыми принтами, качественной базой и философией: не точка, а продолжение.",
        "en": "TwoComms — Ukrainian streetwear / military-adjacent apparel brand from Kharkiv. Items with code, meaningful prints, a quality base and the philosophy of continuation, not a full stop.",
    },
    "TwoComms — харківський бренд одягу, streetwear / military-adjacent естетика": {
        "ru": "TwoComms — харьковский бренд одежды, streetwear / military-adjacent эстетика",
        "en": "TwoComms — Kharkiv apparel brand, streetwear / military-adjacent aesthetic",
    },
    "TwoComms — не крапка. Продовження.": {
        "ru": "TwoComms — не точка. Продолжение.",
        "en": "TwoComms — not a full stop. A continuation.",
    },
    "Одяг з кодом для людей, які тримають форму зсередини. Харківський streetwear / military-adjacent бренд TwoComms.": {
        "ru": "Одежда с кодом для людей, которые держат форму изнутри. Харьковский streetwear / military-adjacent бренд TwoComms.",
        "en": "Apparel with code for people who hold their form from within. The Kharkiv streetwear / military-adjacent brand TwoComms.",
    },
    "TwoComms — одяг з характером, кодом і харківським походженням": {
        "ru": "TwoComms — одежда с характером, кодом и харьковским происхождением",
        "en": "TwoComms — apparel with character, code and Kharkiv origin",
    },
    "TwoComms — український streetwear / military-adjacent бренд одягу з Харкова, створений навколо ідеї продовження після критичної точки: не крапка, а продовження.": {
        "ru": "TwoComms — украинский streetwear / military-adjacent бренд одежды из Харькова, созданный вокруг идеи продолжения после критической точки: не точка, а продолжение.",
        "en": "TwoComms — Ukrainian streetwear / military-adjacent apparel brand from Kharkiv built around the idea of continuation after a critical point: not a full stop, a continuation.",
    },
    "Харківський бренд одягу": {
        "ru": "Харьковский бренд одежды",
        "en": "Kharkiv apparel brand",
    },
    "не крапка.": {"ru": "не точка.", "en": "not a full stop."},
    "Харківський бренд streetwear / military-adjacent одягу про характер, внутрішню дисципліну і рух далі після критичної точки.": {
        "ru": "Харьковский бренд streetwear / military-adjacent одежды о характере, внутренней дисциплине и движении после критической точки.",
        "en": "Kharkiv streetwear / military-adjacent apparel brand about character, inner discipline and continuing forward after a critical point.",
    },
    "Ми створюємо речі з кодом: сенсові принти, щільна база, чиста посадка, харківський характер і філософія продовження. TwoComms — це не військовий мерч і не патріотичний шум. Це одяг для людей, які тримають форму зсередини.": {
        "ru": "Мы создаём вещи с кодом: смысловые принты, плотная база, чистая посадка, харьковский характер и философия продолжения. TwoComms — это не военный мерч и не патриотический шум. Это одежда для людей, которые держат форму изнутри.",
        "en": "We make items with code: meaningful prints, dense base, clean fit, Kharkiv character and the philosophy of continuation. TwoComms is not military merch nor patriotic noise. It is apparel for people who hold their form from within.",
    },
    "Ключові характеристики бренду": {
        "ru": "Ключевые характеристики бренда",
        "en": "Key brand traits",
    },
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17Z)


# ============================================================
# Phase 17z2 — extended public-facing copy: long body of
# pro_brand / wholesale / cooperation / custom_print + plurals
# and counter strings.
# ============================================================
TRANSLATIONS_PHASE_17Z2: dict[str, dict[str, str]] = {
    # ----- Plurals / counters -----
    "%(counter)s відгук": {"ru": "%(counter)s отзывов", "en": "%(counter)s reviews"},
    "%(counter)s бал": {"ru": "%(counter)s баллов", "en": "%(counter)s points"},
    "%(counter)s товар": {"ru": "%(counter)s товаров", "en": "%(counter)s items"},
    "%(counter)s товар знайдено": {
        "ru": "Найдено %(counter)s товаров",
        "en": "%(counter)s items found",
    },
    "%(counter)s зірка": {"ru": "%(counter)s звёзд", "en": "%(counter)s stars"},
    "Рейтинг товару, %(counter)s відгук": {
        "ru": "Рейтинг товара, %(counter)s отзывов",
        "en": "Product rating, %(counter)s reviews",
    },
    "на основі %(counter)s відгуку": {
        "ru": "на основе %(counter)s отзывов",
        "en": "based on %(counter)s reviews",
    },
    "за запитом «%(q)s»": {"ru": "по запросу «%(q)s»", "en": "for query \"%(q)s\""},

    # ----- pro_brand body / manifesto -----
    "База": {"ru": "База", "en": "Base"},
    "Щільна тканина, яка тримає силует і дає речі фізичну вагу.": {
        "ru": "Плотная ткань, которая держит силуэт и даёт вещи физический вес.",
        "en": "Dense fabric that holds the silhouette and gives the item physical weight.",
    },
    "Перед відправкою ми перевіряємо вигляд, друк, базу і загальне відчуття речі.": {
        "ru": "Перед отправкой мы проверяем вид, печать, базу и общее ощущение вещи.",
        "en": "Before shipping we check the look, the print, the base and the overall feel.",
    },
    "Нормальний зв'язок": {"ru": "Нормальная связь", "en": "Normal connection"},
    "Позиція": {"ru": "Позиция", "en": "Position"},
    "Зв’язок із фронтом": {"ru": "Связь с фронтом", "en": "Frontline connection"},
    "TwoComms має реальний зв’язок із військовим контекстом, але не продає війну як естетику. Для нас фронт — не декорація і не рекламний важіль. Це джерело відповідальності, пам’яті, стандарту і поваги до людей, які знають ціну речам.": {
        "ru": "TwoComms имеет реальную связь с военным контекстом, но не продаёт войну как эстетику. Для нас фронт — не декорация и не рекламный рычаг. Это источник ответственности, памяти, стандарта и уважения к людям, которые знают цену вещам.",
        "en": "TwoComms has a real connection with the military context, but it does not sell war as aesthetics. For us the front is not decor or a marketing lever. It is a source of responsibility, memory, standard and respect for people who know the value of things.",
    },
    "Цей зв’язок проявляється в походженні бренду, окремих принтах, колабораціях, підтримці ЗСУ і ставленні до якості. Ми не використовуємо військовий досвід як позу. Ми переводимо його в дисципліну, сенс і речі, які не мають бути випадковими.": {
        "ru": "Эта связь проявляется в происхождении бренда, отдельных принтах, коллаборациях, поддержке ВСУ и отношении к качеству. Мы не используем военный опыт как позу. Мы переводим его в дисциплину, смысл и вещи, которые не должны быть случайными.",
        "en": "This connection shows in the brand's origin, particular prints, collaborations, support of the Armed Forces of Ukraine, and our attitude to quality. We do not use military experience as a pose. We translate it into discipline, meaning, and items that must not be random.",
    },
    "Аудиторія": {"ru": "Аудитория", "en": "Audience"},
    "TwoComms не намагається подобатися всім. Це бренд для людей, яким близькі характер, український контекст, внутрішня дисципліна і речі з причиною існування.": {
        "ru": "TwoComms не пытается нравиться всем. Это бренд для людей, которым близки характер, украинский контекст, внутренняя дисциплина и вещи с причиной существования.",
        "en": "TwoComms does not try to please everyone. It is a brand for people who relate to character, Ukrainian context, inner discipline and items that exist for a reason.",
    },
    "Його можуть носити військові, ветерани, волонтери, цивільні, харків’яни і ті, хто підтримує Україну з різних міст і країн. Головне не статус. Головне — здатність зчитати сенс і носити його без зайвого шуму.": {
        "ru": "Его могут носить военные, ветераны, волонтёры, гражданские, харьковчане и те, кто поддерживает Украину из разных городов и стран. Главное не статус. Главное — способность считать смысл и носить его без лишнего шума.",
        "en": "It can be worn by service members, veterans, volunteers, civilians, Kharkivites and those who support Ukraine from different cities and countries. Status is not the point. The point is the ability to read the meaning and carry it without unnecessary noise.",
    },
    "Основа незмінна: сенс, форма, якість і продовження. Система розширюватиметься: від футболок і худі до складніших силуетів, аксесуарів та речей із тихим, архітектурним характером.": {
        "ru": "Основа неизменна: смысл, форма, качество и продолжение. Система будет расширяться: от футболок и худи к более сложным силуэтам, аксессуарам и вещам с тихим, архитектурным характером.",
        "en": "The core stays the same: meaning, form, quality and continuation. The system will keep expanding: from t-shirts and hoodies to more complex silhouettes, accessories and items with a quiet, architectural character.",
    },
    "Розширюємо бренд як систему: не шумом, а послідовними кроками, які тримають характер, знак і внутрішню дисципліну.": {
        "ru": "Расширяем бренд как систему: не шумом, а последовательными шагами, которые держат характер, знак и внутреннюю дисциплину.",
        "en": "We grow the brand as a system: not through noise but through consistent steps that hold the character, the mark and inner discipline.",
    },
    "Від базових форм до тихіших складніших силуетів, аксесуарів і цілісної системи.": {
        "ru": "От базовых форм к более тихим сложным силуэтам, аксессуарам и целостной системе.",
        "en": "From basic forms to quieter, more complex silhouettes, accessories and a holistic system.",
    },
    "Видимі відповіді для людей і для пошуку: що означає назва, як TwoComms пов’язаний із Харковом, і чому це не зводиться лише до військового мерчу.": {
        "ru": "Видимые ответы для людей и для поиска: что означает название, как TwoComms связан с Харьковом и почему это не сводится только к военному мерчу.",
        "en": "Visible answers for people and search engines: what the name means, how TwoComms is connected with Kharkiv, and why it isn't just military merch.",
    },
    "Кожна річ TwoComms починається з ідеї, але має закінчуватися на людині. Знайди код, який відгукується, і носи стан, який хочеш тримати.": {
        "ru": "Каждая вещь TwoComms начинается с идеи, но должна заканчиваться на человеке. Найди код, который откликается, и носи состояние, которое хочешь держать.",
        "en": "Every TwoComms item starts with an idea but must end on a person. Find a code that resonates and carry the state you want to hold.",
    },

    # ----- product detail extra -----
    "Матеріал: 95%% бавовна, 5%% еластан — преміум якість, щільність 190 г/м²": {
        "ru": "Материал: 95%% хлопок, 5%% эластан — премиум-качество, плотность 190 г/м²",
        "en": "Material: 95%% cotton, 5%% elastane — premium quality, 190 g/m² density",
    },

    # ----- wholesale long copy -----
    "Оптовий одяг TwoComms для магазинів і мереж: футболки та худі від 8 одиниць, персональні ціни, швидке відвантаження й підтримка менеджера.": {
        "ru": "Оптовая одежда TwoComms для магазинов и сетей: футболки и худи от 8 единиц, персональные цены, быстрая отгрузка и поддержка менеджера.",
        "en": "TwoComms wholesale apparel for stores and chains: T-shirts and hoodies from 8 units, tailored pricing, fast dispatch and dedicated manager support.",
    },
    "оптові закупівлі одягу, футболки оптом, худі оптом, оптовий одяг TwoComms, дропшипінг одягу, оптовий прайс, український бренд одягу, партнерська програма TwoComms": {
        "ru": "оптовые закупки одежды, футболки оптом, худи оптом, оптовая одежда TwoComms, дропшиппинг одежды, оптовый прайс, украинский бренд одежды, партнёрская программа TwoComms",
        "en": "wholesale apparel, t-shirts wholesale, hoodies wholesale, TwoComms wholesale apparel, apparel dropshipping, wholesale price list, Ukrainian apparel brand, TwoComms partnership program",
    },
    "Отримайте вигідні умови для магазинів та дропшиперів: старт від 8 одиниць, фіксовані ціни за договором, швидке відвантаження та підтримка менеджера бренду TwoComms.": {
        "ru": "Получите выгодные условия для магазинов и дропшиперов: старт от 8 единиц, фиксированные цены по договору, быстрая отгрузка и поддержка менеджера бренда TwoComms.",
        "en": "Get favorable terms for stores and dropshippers: start from 8 units, fixed contract prices, fast shipping and TwoComms brand manager support.",
    },
    "Підключайтесь до партнерської програми TwoComms: опт і дропшипінг українського стріт одягу з гнучкими умовами та підтримкою.": {
        "ru": "Подключайтесь к партнёрской программе TwoComms: опт и дропшиппинг украинского стрит-одежды с гибкими условиями и поддержкой.",
        "en": "Join the TwoComms partnership program: wholesale and dropshipping of Ukrainian streetwear with flexible terms and support.",
    },
    "Оптові закупівлі та дропшипінг стріт-одягу TwoComms: футболки, худі, аксесуари. Старт від 8 одиниць, персональні прайси, швидке відвантаження та підтримка менеджера.": {
        "ru": "Оптовые закупки и дропшиппинг стрит-одежды TwoComms: футболки, худи, аксессуары. Старт от 8 единиц, персональные прайсы, быстрая отгрузка и поддержка менеджера.",
        "en": "Wholesale and dropshipping of TwoComms streetwear: t-shirts, hoodies, accessories. Start from 8 units, personal price lists, fast shipping and manager support.",
    },
    "Вигідні ціни для дропшипінгу та оптових закупівель.<br>": {
        "ru": "Выгодные цены для дропшиппинга и оптовых закупок.<br>",
        "en": "Favorable prices for dropshipping and wholesale.<br>",
    },
    "Фіксовані ціни незалежно від кількості замовлення. Ідеально для дропшипінгу та роздрібної торгівлі.": {
        "ru": "Фиксированные цены независимо от количества в заказе. Идеально для дропшиппинга и розничной торговли.",
        "en": "Fixed prices regardless of order quantity. Ideal for dropshipping and retail.",
    },
    "Вигідні ціни залежно від обсягу замовлення. Чим більше замовляєте, тим менше платите за одиницю.": {
        "ru": "Выгодные цены в зависимости от объёма заказа. Чем больше заказываете, тем меньше платите за единицу.",
        "en": "Favorable prices based on order volume. The more you order, the less you pay per unit.",
    },
    "Дроп:": {"ru": "Дроп:", "en": "Drop:"},
    "Дроп ціна:": {"ru": "Дроп-цена:", "en": "Drop price:"},
    "90%% Бавовна, 10%% Поліестер<br>": {
        "ru": "90%% хлопок, 10%% полиэстер<br>",
        "en": "90%% cotton, 10%% polyester<br>",
    },
    "95%% Бавовна, 5%% Еластан<br>": {
        "ru": "95%% хлопок, 5%% эластан<br>",
        "en": "95%% cotton, 5%% elastane<br>",
    },
    "Оптовий SEO layer": {"ru": "Оптовый SEO-блок", "en": "Wholesale SEO layer"},
    "Тут зібрані ключові відповіді для магазинів, шоурумів, дропшиперів і команд, які планують запускати продажі або закупівлі через TwoComms на основному домені.": {
        "ru": "Здесь собраны ключевые ответы для магазинов, шоурумов, дропшиперов и команд, которые планируют запускать продажи или закупки через TwoComms на основном домене.",
        "en": "Here are key answers for stores, showrooms, dropshippers and teams planning to launch sales or purchases through TwoComms on the main domain.",
    },
    "Якщо вам потрібно не просто скачати прайс, а швидко узгодити умови, зразки або паралельний кастомний сценарій, переходьте в суміжні money pages без зайвого пошуку по сайту.": {
        "ru": "Если вам нужно не просто скачать прайс, а быстро согласовать условия, образцы или параллельный кастомный сценарий, переходите на смежные money-страницы без лишнего поиска по сайту.",
        "en": "If you need more than the price list — quick alignment on terms, samples or a parallel custom scenario — jump straight into the adjacent money pages without searching the site.",
    },
    "Перевірте логістику, умови відправки, дедлайни та базові правила оформлення.": {
        "ru": "Проверьте логистику, условия отправки, дедлайны и базовые правила оформления.",
        "en": "Check logistics, shipping terms, deadlines and basic order rules.",
    },
    "Швидкий перехід у канал, де можна узгодити асортимент, документи й запуск продажів.": {
        "ru": "Быстрый переход в канал, где можно согласовать ассортимент, документы и запуск продаж.",
        "en": "Quickly jump into the channel where you can align assortment, documents and launching sales.",
    },

    # ----- cooperation long copy -----
    "Співпраця з TwoComms: дропшипінг, оптові закупівлі, бренд-колаборації та проєкти з дизайнерами, блогерами й медіа. Оберіть формат партнерства.": {
        "ru": "Сотрудничество с TwoComms: дропшиппинг, оптовые закупки, бренд-коллаборации и проекты с дизайнерами, блогерами и медиа. Выберите формат партнёрства.",
        "en": "Partner with TwoComms through dropshipping, wholesale, brand collaborations, and projects with designers, bloggers or media. Choose a partnership format.",
    },
    "TwoComms відкриває партнерські маршрути для магазинів, брендів, дизайнерів, блогерів та медіа.": {
        "ru": "TwoComms открывает партнёрские маршруты для магазинов, брендов, дизайнеров, блогеров и медиа.",
        "en": "TwoComms opens partnership routes for stores, brands, designers, bloggers and media.",
    },
    "Сторінка для партнерств, дропшипінгу, оптових закупівель, бренд-колаборацій і співпраці з дизайнерами або медіа.": {
        "ru": "Страница для партнёрств, дропшиппинга, оптовых закупок, бренд-коллабораций и сотрудничества с дизайнерами или медиа.",
        "en": "Page for partnerships, dropshipping, wholesale, brand collaborations and cooperation with designers or media.",
    },
    "Приєднуйтесь до нашої мережі партнерів та розвивайте свій бізнес разом з нами": {
        "ru": "Присоединяйтесь к нашей сети партнёров и развивайте свой бизнес вместе с нами",
        "en": "Join our partner network and grow your business with us",
    },
    "Продавайте наші товари без вкладень у склад. Ми відправляємо замовлення вашим клієнтам.": {
        "ru": "Продавайте наши товары без вложений в склад. Мы отправляем заказы вашим клиентам.",
        "en": "Sell our products without stock investment. We ship orders to your customers.",
    },
    "Комісія:": {"ru": "Комиссия:", "en": "Commission:"},
    "Працюємо напряму з магазинами та мережами: гнучкі прайси, швидке відвантаження зі складу та зручні онлайн-інструменти для оформлення накладних.": {
        "ru": "Работаем напрямую с магазинами и сетями: гибкие прайсы, быстрая отгрузка со склада и удобные онлайн-инструменты для оформления накладных.",
        "en": "We work directly with stores and chains: flexible prices, fast warehouse shipping and convenient online tools for invoice processing.",
    },
    "Створюйте ексклюзивні колекції разом з нами. Для великих брендів та дизайнерів.": {
        "ru": "Создавайте эксклюзивные коллекции вместе с нами. Для больших брендов и дизайнеров.",
        "en": "Build exclusive collections with us. For large brands and designers.",
    },
    "Ваші ідеї можуть стати реальністю! Створюйте принти та отримуйте винагороду за кожен успішний продаж.": {
        "ru": "Ваши идеи могут стать реальностью! Создавайте принты и получайте вознаграждение за каждую успешную продажу.",
        "en": "Your ideas can become real! Create prints and earn a reward for every successful sale.",
    },
    "Демонструйте наші товари та отримуйте винагороду за кожну фотосесію. Станьте частиною нашої команди!": {
        "ru": "Демонстрируйте наши товары и получайте вознаграждение за каждую фотосессию. Станьте частью нашей команды!",
        "en": "Showcase our items and earn a reward for every photo shoot. Become part of our team!",
    },
    "Співпрацюйте з нами для створення якісного контенту та розвивайте свій бренд разом з TwoComms.": {
        "ru": "Сотрудничайте с нами для создания качественного контента и развивайте свой бренд вместе с TwoComms.",
        "en": "Collaborate with us to create quality content and grow your brand together with TwoComms.",
    },
    "Якщо ви вибираєте між дропшипінгом, оптовим запуском, бренд-проєктом або контентною колаборацією, почніть з цих відповідей і переходьте в потрібний сценарій без зайвого шуму.": {
        "ru": "Если вы выбираете между дропшиппингом, оптовым запуском, бренд-проектом или контентной коллаборацией, начните с этих ответов и переходите в нужный сценарий без лишнего шума.",
        "en": "If you're choosing between dropshipping, wholesale launch, brand project or content collaboration, start with these answers and move into the right scenario without noise.",
    },
    "Ці сторінки закривають найчастіші next step сценарії: оптові закупівлі, кастомний друк для партій, а також прямий контакт з командою для нестандартних колаборацій.": {
        "ru": "Эти страницы закрывают самые частые next-step сценарии: оптовые закупки, кастомная печать для партий и прямой контакт с командой для нестандартных коллабораций.",
        "en": "These pages cover the most common next-step scenarios: wholesale, custom print for batches, and direct contact with the team for non-standard collaborations.",
    },
    "Для магазинів, шоурумів, мереж і дропшиперів, яким потрібні обсяги та комерційні умови.": {
        "ru": "Для магазинов, шоурумов, сетей и дропшиперов, которым нужны объёмы и коммерческие условия.",
        "en": "For stores, showrooms, chains and dropshippers who need volume and commercial terms.",
    },
    "Для мерчу, команд, подій і партій зі своїм дизайном або приватним лейблом.": {
        "ru": "Для мерча, команд, событий и партий со своим дизайном или частным лейблом.",
        "en": "For merch, teams, events and batches with custom design or a private label.",
    },
    "Переходьте до контактів, якщо потрібен нестандартний формат співпраці або швидкий запуск.": {
        "ru": "Переходите в контакты, если нужен нестандартный формат сотрудничества или быстрый запуск.",
        "en": "Go to contacts if you need a non-standard cooperation format or a fast launch.",
    },

    # ----- custom_print copy -----
    "Кастомний DTF-друк на футболках, худі та лонгслівах. Оберіть основу й зони друку, додайте макет, дізнайтеся вартість і оформіть замовлення.": {
        "ru": "Кастомная DTF-печать на футболках, худи и лонгсливах. Выберите основу и зоны печати, добавьте макет, узнайте стоимость и оформите заказ.",
        "en": "Custom DTF printing on T-shirts, hoodies and long sleeves. Choose a garment and print zones, upload artwork, see the price and place your order.",
    },
    "dtf друк, кастомний принт, друк на худі, друк на футболках, кастомний одяг, twocomms, dtf конфігуратор": {
        "ru": "dtf печать, кастомный принт, печать на худи, печать на футболках, кастомная одежда, twocomms, dtf конфигуратор",
        "en": "dtf print, custom print, print on hoodies, print on t-shirts, custom apparel, twocomms, dtf configurator",
    },
    "Збери річ під свій дизайн: обери основу, тканину, зони друку й макет. Конструктор покаже вигляд і вартість.": {
        "ru": "Соберите вещь под свой дизайн: выберите основу, ткань, зоны печати и макет. Конструктор покажет вид и стоимость.",
        "en": "Build an item around your design: pick the base, fabric, print zones and layout. The configurator shows the look and the price.",
    },
    "Кастомний DTF-друк на худі, футболках, лонгслівах. Збери виріб під свій дизайн за лічені хвилини.": {
        "ru": "Кастомная DTF-печать на худи, футболках, лонгсливах. Соберите изделие под свой дизайн за считанные минуты.",
        "en": "Custom DTF print on hoodies, t-shirts, longsleeves. Build an item around your design in minutes.",
    },
    "Кастомний DTF-конфігуратор для худі, футболок, лонгслівів. Обери основу, тканину, зони друку й макет.": {
        "ru": "Кастомный DTF-конфигуратор для худи, футболок, лонгсливов. Выберите основу, ткань, зоны печати и макет.",
        "en": "Custom DTF configurator for hoodies, t-shirts, longsleeves. Pick the base, fabric, print zones and layout.",
    },
    "Кастомний одяг та аксесуари під ваші ідеї. Будь-який дизайн, будь-який колір, преміальна якість без компромісів.": {
        "ru": "Кастомная одежда и аксессуары под ваши идеи. Любой дизайн, любой цвет, премиальное качество без компромиссов.",
        "en": "Custom apparel and accessories tailored to your ideas. Any design, any color, premium quality with no compromise.",
    },
    "Три чорні речі на подіумі з логотипами TwoComms: худі, футболка та лонгслів.": {
        "ru": "Три чёрные вещи на подиуме с логотипами TwoComms: худи, футболка и лонгслив.",
        "en": "Three black items on a podium with TwoComms logos: hoodie, t-shirt and longsleeve.",
    },
    "Преміальні тканини та унікальна термотканина, що змінює відтінок від температури.": {
        "ru": "Премиальные ткани и уникальная термоткань, меняющая оттенок от температуры.",
        "en": "Premium fabrics and a unique thermal fabric that shifts shade with temperature.",
    },
    "Змінити": {"ru": "Изменить", "en": "Change"},
    "Один виріб для себе чи партія для команди/бренду — від цього залежить ціна, розміри й наступні кроки.": {
        "ru": "Одна вещь для себя или партия для команды/бренда — от этого зависят цена, размеры и следующие шаги.",
        "en": "A single item for yourself or a batch for a team/brand — that defines the price, the sizes and the next steps.",
    },
    "Обери посадку, тканину, колір і деталі. Доплати видно одразу — без сюрпризів у фіналі.": {
        "ru": "Выберите посадку, ткань, цвет и детали. Доплаты видны сразу — без сюрпризов в финале.",
        "en": "Pick the fit, fabric, color and details. Surcharges are visible immediately — no surprises at checkout.",
    },
    "Обери одну або кілька зон. Перший друк входить у базову ціну, додаткові зони додаються окремо.": {
        "ru": "Выберите одну или несколько зон. Первая печать входит в базовую цену, дополнительные зоны добавляются отдельно.",
        "en": "Pick one or several zones. The first print is included in the base price; additional zones are added separately.",
    },
    "Перший друк входить у базову ціну. Додаткові зони можна вмикати і вимикати прямо на сцені.": {
        "ru": "Первая печать входит в базовую цену. Дополнительные зоны можно включать и выключать прямо на сцене.",
        "en": "The first print is included in the base price. Extra zones can be toggled on and off directly on the stage.",
    },
    "Нестандартна зона": {"ru": "Нестандартная зона", "en": "Custom zone"},
    "Завантаж макет або коротко опиши, що хочеш отримати. Ми підкажемо, якщо файл треба підготувати до друку.": {
        "ru": "Загрузите макет или кратко опишите, что хотите получить. Мы подскажем, если файл нужно подготовить к печати.",
        "en": "Upload artwork or briefly describe what you want. We'll let you know if the file needs prep before printing.",
    },
    "Спочатку оберіть зони друку — для кожної зʼявиться окреме поле завантаження.": {
        "ru": "Сначала выберите зоны печати — для каждой появится отдельное поле загрузки.",
        "en": "First pick the print zones — each will get its own upload field.",
    },
    "Опишіть ідею, стиль або референси. Якщо потрібен дизайн — це обовʼязково.": {
        "ru": "Опишите идею, стиль или референсы. Если нужен дизайн — это обязательно.",
        "en": "Describe the idea, style or references. If a design is required, this field is mandatory.",
    },
    "Вкажи кількість і розміри. Якщо ще не знаєш точні розміри — можна уточнити з менеджером.": {
        "ru": "Укажите количество и размеры. Если ещё не знаете точные размеры — можно уточнить с менеджером.",
        "en": "Specify quantity and sizes. If you don't know the exact sizes yet, you can clarify them with the manager.",
    },
    "Введіть текст для листівки. Або залиште пустим — ми покладемо чисту листівку для вашого почерку.": {
        "ru": "Введите текст для открытки. Или оставьте пустым — мы вложим чистую открытку для вашего почерка.",
        "en": "Enter text for the card. Or leave it empty — we'll include a blank card for your handwriting.",
    },
    "Додай контакт, щоб ми могли підтвердити деталі. Можеш додати конфігурацію в кошик або одразу передати менеджеру.": {
        "ru": "Добавьте контакт, чтобы мы могли подтвердить детали. Можно добавить конфигурацию в корзину или сразу передать менеджеру.",
        "en": "Add a contact so we can confirm the details. You can drop the configuration into the cart or pass it directly to the manager.",
    },
    "Ваше імʼя": {"ru": "Ваше имя", "en": "Your name"},
    "Telegram / WhatsApp / телефон.": {
        "ru": "Telegram / WhatsApp / телефон.",
        "en": "Telegram / WhatsApp / phone.",
    },
    "Цей блок закриває часті заперечення до заявки: чи можна зробити один виріб, що робити без готового файлу, як працювати зі своїм одягом і як запускати партію для бренду чи команди.": {
        "ru": "Этот блок закрывает частые возражения к заявке: можно ли сделать одну вещь, что делать без готового файла, как работать со своей одеждой и как запускать партию для бренда или команды.",
        "en": "This block addresses common objections before submitting a request: can we make a single item, what to do without a ready file, how to work with your own apparel and how to launch a batch for a brand or team.",
    },
    "Важливо: готові товари належної якості можна повернути або обміняти протягом 14 днів згідно із законодавством України, але кастомний одяг, виготовлений за індивідуальним замовленням, не підлягає поверненню чи обміну, якщо його виконано належним чином.": {
        "ru": "Важно: готовые товары надлежащего качества можно вернуть или обменять в течение 14 дней согласно законодательству Украины, но кастомная одежда, изготовленная по индивидуальному заказу, не подлежит возврату или обмену, если она выполнена надлежащим образом.",
        "en": "Important: ready-made items of proper quality can be returned or exchanged within 14 days under Ukrainian law, but custom apparel made to individual order is not subject to return or exchange when produced properly.",
    },
    "Це допоміжний внутрішній хаб навколо кастомного друку: опт, контакти та умови оформлення, які часто потрібні до підтвердження макета або тиражу.": {
        "ru": "Это вспомогательный внутренний хаб вокруг кастомной печати: опт, контакты и условия оформления, которые часто нужны до утверждения макета или тиража.",
        "en": "This is an internal hub around custom print: wholesale, contacts and ordering terms that are often required before approving artwork or a run.",
    },
    "Окрема сторінка для брендів, команд, магазинів і тих, кому потрібні партії або партнерський формат.": {
        "ru": "Отдельная страница для брендов, команд, магазинов и тех, кому нужны партии или партнёрский формат.",
        "en": "A dedicated page for brands, teams, stores and those who need batches or a partnership format.",
    },
    "Зв'язок": {"ru": "Связь", "en": "Contact"},

    # pro_brand long copy
    "Сильний сенс не працює на слабкій базі. Тому TwoComms починається не з принта, а з речі: тканини, посадки, друку, швів і контролю.": {
        "ru": "Сильный смысл не работает на слабой базе. Поэтому TwoComms начинается не с принта, а с вещи: ткани, посадки, печати, швов и контроля.",
        "en": "Strong meaning doesn't work on a weak base. That's why TwoComms starts not from a print but from the item itself: fabric, fit, print, seams and control.",
    },
    "Ми не робимо принт на випадковій основі. Якщо річ говорить про характер, вона сама має мати характер: щільність, чисту форму, витриманий друк і відчуття предмета, який не купили випадково.": {
        "ru": "Мы не делаем принт на случайной основе. Если вещь говорит о характере, она сама должна иметь характер: плотность, чистую форму, выдержанную печать и ощущение предмета, который не купили случайно.",
        "en": "We don't put prints on a random base. If an item speaks of character, it must have character itself: density, clean form, restrained print and the feel of an object you didn't buy by chance.",
    },
}
TRANSLATIONS.update(TRANSLATIONS_PHASE_17Z2)


# Phase 17i fix: original `[^"]*` failed on multi-line msgid blocks
# whose continuation strings contain escaped quotes (e.g. ``href=\"…\"`` in
# the catalogue SEO paragraphs). The dotted alternative
# ``(?:[^"\\]|\\.)*`` walks escape sequences as a unit so the regex stops
# only at unescaped closing quotes.
_PO_STRING = r'"(?:[^"\\]|\\.)*"'

_PO_ENTRY_RE = re.compile(
    rf'(^msgid (?:{_PO_STRING}\s*)+\n)((?:msgstr (?:{_PO_STRING}\s*)+\n)+)',
    re.MULTILINE,
)


def _decode_po_string(block: str) -> str:
    """Concatenate consecutive ``"..."`` lines from a PO field block.

    PO files are UTF-8; we only need to translate backslash escapes
    (``\\n``, ``\\t``, ``\\"``, ``\\\\``). Using ``unicode_escape`` would
    corrupt non-ASCII bytes — handle escapes by hand instead.
    """
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    raw = "".join(parts)
    return (
        raw.replace("\\\\", "\x00")  # protect literal backslashes
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace("\x00", "\\")
    )


def _encode_po_string(s: str) -> str:
    """Encode a string for a single-line PO value.

    PO files use C-style escapes; real newline / tab / CR characters
    cannot appear inside a quoted string. Translate them to backslash
    sequences so multi-line msgstr values (e.g. address blocks with
    embedded ``\\n``) compile correctly under ``msgfmt``.
    """
    return (
        s.replace("\\", "\\\\")
        .replace('"', r"\"")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )


_BLOCK_RE = re.compile(
    rf"((?:^#.*\n)*)(^msgid (?:{_PO_STRING}\s*)+\n)((?:msgstr (?:{_PO_STRING}\s*)+\n)+)",
    re.MULTILINE,
)


def patch_po(path: Path, lang: str) -> tuple[int, int]:
    """Fill empty / fuzzy msgstr for known msgids. Returns (filled, matched).

    Also strips ``#, fuzzy`` flags from blocks we override and removes
    the speculative ``#| msgid ...`` previous-msgid hints, since our
    explicit translation supersedes them.
    """
    text = path.read_text(encoding="utf-8")
    filled = 0
    seen = 0

    def repl(m: re.Match) -> str:
        nonlocal filled, seen
        comments = m.group(1)
        msgid_block = m.group(2)
        msgstr_block = m.group(3)
        msgid_val = _decode_po_string(msgid_block)
        msgstr_val = _decode_po_string(msgstr_block)
        entry = TRANSLATIONS.get(msgid_val)
        if not entry or not entry.get(lang):
            return m.group(0)
        seen += 1
        is_fuzzy = bool(re.search(r"^#, .*fuzzy", comments, re.MULTILINE))
        # Phase 17d.6 — also override msgstr that equals msgid (Django's
        # makemessages auto-copies msgid into msgstr for some entries;
        # those placeholder values should be replaced with our explicit
        # translation when one exists).
        is_placeholder = msgstr_val == msgid_val
        if msgstr_val.strip() and not is_fuzzy and not is_placeholder:
            return m.group(0)
        new_val = entry[lang]
        filled += 1
        # Remove fuzzy flag and previous-msgid speculation lines.
        cleaned_comments = []
        for line in comments.splitlines(keepends=True):
            if line.startswith("#|"):
                continue
            if line.startswith("#,") and "fuzzy" in line:
                stripped = re.sub(r",?\s*fuzzy", "", line)
                if stripped.strip() in ("#", "#,"):
                    continue
                cleaned_comments.append(stripped)
                continue
            cleaned_comments.append(line)
        return (
            "".join(cleaned_comments)
            + msgid_block
            + f'msgstr "{_encode_po_string(new_val)}"\n'
        )

    new_text = _BLOCK_RE.sub(repl, text)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
    return filled, seen


def main() -> None:
    for lang in ("ru", "en"):
        po = LOCALE_DIR / lang / "LC_MESSAGES" / "django.po"
        if not po.exists():
            print(f"[skip] {po} not found")
            continue
        filled, seen = patch_po(po, lang)
        print(f"[{lang}] filled {filled} / matched {seen} / known {len(TRANSLATIONS)}")


if __name__ == "__main__":
    main()
