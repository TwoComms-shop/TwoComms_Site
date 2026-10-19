"""Phase 17d.7 — per-language overrides for support page content.

This module ships full RU and EN replacements for the user-visible
parts of ``SUPPORT_PAGE_DEFINITIONS`` (in ``support_content.py``) and
the related FAQ item lists. Ukrainian (default) is the canonical
source; RU and EN are flat dictionary overrides applied on top of a
deepcopied page dict in ``_build_page_context``.

Supported override keys per page:

    * page_title, meta_title, meta_description, meta_keywords
    * hero_kicker, hero_title, hero_intro, hero_meta
    * intro_links (full list replacement; preserve url_name/url_kwargs)
    * sections (full list replacement)
    * faq_items (full list replacement)
    * cta (full dict replacement)

Usage in views/static_pages.py — after deepcopy of the page dict:

    from .support_translations import apply_language_overrides
    page = apply_language_overrides(page, page_key, request.LANGUAGE_CODE)
"""

from __future__ import annotations

from typing import Any, Dict
from copy import deepcopy

from django.utils.translation import override

from storefront.support_content import HELP_FAQ_ITEMS, SUPPORT_PAGE_DEFINITIONS


# ---------------------------------------------------------------------------
# Russian overrides
# ---------------------------------------------------------------------------

_RU_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "about": {
        "page_title": "О бренде — TwoComms",
        "meta_title": "TwoComms — харьковский бренд одежды о характере, стойкости и продолжении",
        "meta_description": "TwoComms — украинский streetwear / military-adjacent бренд из Харькова. Одежда с кодом, смысловыми принтами, реальным происхождением и философией: не точка, а продолжение.",
        "meta_keywords": "TwoComms, харьковский бренд одежды, украинский streetwear бренд, military-adjacent одежда, одежда со смыслом, смысловые принты, две запятые, Харьков",
        "hero_kicker": "Brand",
        "hero_title": "TwoComms — не точка. Продолжение.",
        "hero_intro": "TwoComms — харьковский streetwear / military-adjacent бренд о характере, смысловых принтах, внутренней дисциплине и движении дальше после критической точки.",
    },
    "delivery": {
        "page_title": "Доставка и оплата — TwoComms",
        "meta_title": "Доставка и оплата — TwoComms",
        "meta_description": "Условия доставки и оплаты TwoComms: сроки по Украине, международные отправления, оплата, наложенный платёж и отслеживание заказа.",
        "meta_keywords": "TwoComms доставка, оплата, наложенный платёж, mono checkout, международная доставка",
        "hero_kicker": "Delivery & Payment",
        "hero_title": "Доставка и оплата",
        "hero_intro": "Здесь только то, что нужно для логистики и оплаты: сроки, сценарии по Украине, международные отправления и дальнейший маршрут после checkout.",
        "hero_meta": [
            "1-5 дней по Украине",
            "mono checkout",
            "tracking после отправки",
        ],
        "intro_links": [
            {"label": "Отслеживание заказа", "url_name": "order_tracking"},
            {"label": "Возврат и обмен", "url_name": "returns"},
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Купить футболку", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "tshirts"}},
            {"label": "Купить худи", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "hoodie"}},
            {"label": "Купить лонгслив", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "long-sleeve"}},
        ],
        "sections": [
            {
                "eyebrow": "Украина",
                "title": "Базовый сценарий доставки",
                "cards": [
                    {"title": "Сроки", "text": "Ориентир по Украине — 1-5 рабочих дней после подготовки заказа."},
                    {"title": "Оплата", "text": "Доступны полная онлайн-оплата, mono checkout и отдельные сценарии с частичной предоплатой."},
                    {"title": "Логистика", "text": "Стоимость доставки определяется перевозчиком, форматом посылки и направлением."},
                ],
            },
            {
                "eyebrow": "Международная",
                "title": "Что учитывать для отправлений за границу",
                "bullets": [
                    "Срок и стоимость зависят от страны и перевозчика.",
                    "Для международных заказов может действовать полная оплата до отправки.",
                    "Уточнения по международной логистике лучше делать до оплаты, а не после checkout.",
                ],
            },
            {
                "eyebrow": "После оплаты",
                "title": "Куда двигаться дальше",
                "links": [
                    {"title": "Отслеживание заказа", "text": "TTN, статусы и логика после передачи посылки перевозчику.", "url_name": "order_tracking"},
                    {"title": "FAQ", "text": "Быстрые ответы по смежным вопросам сервиса.", "url_name": "faq"},
                    {"title": "Возврат и обмен", "text": "Отдельный маршрут для вопросов уже после получения заказа.", "url_name": "returns"},
                ],
            },
        ],
        "faq_items": [
            {"question": "Сколько идёт доставка по Украине?", "answer": "Ориентировочный срок — 1-5 рабочих дней после подготовки заказа к отправке."},
            {"question": "Можно ли оплатить при получении?", "answer": "Да, для части заказов доступен сценарий с частичной предоплатой и наложенным платежом. Окончательный вариант зависит от типа отправления."},
            {"question": "Как отследить заказ?", "answer": "После передачи посылки перевозчику вы получаете номер отправления и можете сверять статус в кабинете или на странице отслеживания."},
            {"question": "Возможна ли доставка за границу?", "answer": "Да, международные отправления возможны. Условия, стоимость и сроки зависят от страны назначения и логистического сценария."},
        ],
        "cta": {
            "title": "Нужен короткий маршрут после оформления?",
            "text": "Откройте tracking или FAQ. Это самые короткие точки входа для большинства послепродажных вопросов.",
            "primary": {"label": "Отследить заказ", "url_name": "order_tracking"},
            "secondary": {"label": "Открыть FAQ", "url_name": "faq"},
        },
    },
    "help_center": {
        "page_title": "Помощь — TwoComms",
        "meta_title": "Помощь — условия заказа, оплата и поддержка | TwoComms",
        "meta_description": "Центр помощи TwoComms: как оформить заказ, как работают доставка, оплата, баллы, промокоды, кастомная печать и сервисные обращения.",
        "meta_keywords": "TwoComms помощь, условия заказа, поддержка клиентов, доставка, оплата",
        "hero_kicker": "Support Center",
        "hero_title": "Помощь и базовые правила сайта",
        "hero_intro": "Это service-manual без лишних повторов: коротко об оформлении, оплате, доставке, лояльности, кастомных заявках и дальнейшей поддержке.",
        "hero_meta": ["manual-формат", "без дублирования FAQ", "быстрая навигация по сервису"],
        "intro_links": [
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Доставка", "url_name": "delivery"},
            {"label": "Размерная сетка", "url_name": "size_guide"},
            {"label": "Контакты", "url_name": "contacts"},
        ],
        "sections": [
            {
                "eyebrow": "1",
                "title": "Оформление заказа",
                "paragraphs": [
                    "Покупатель самостоятельно выбирает товар, размер и цвет, добавляет позиции в корзину и переходит к checkout.",
                    "После заполнения контактов, доставки и оплаты заказ фиксируется в системе и поступает в обработку.",
                ],
                "bullets": [
                    "Актуальная цена и скидка показываются в карточке товара и в корзине.",
                    "Для нестандартных или кастомных задач может потребоваться дополнительное согласование.",
                ],
            },
            {
                "eyebrow": "2",
                "title": "Оплата, доставка и статусы",
                "paragraphs": [
                    "Для большинства заказов доступна полная онлайн-оплата, а для отдельных сценариев — частичная предоплата.",
                    "После отправки заказа вы получаете номер отправления и можете перейти к отслеживанию.",
                ],
                "links": [
                    {"title": "Доставка и оплата", "text": "Отдельная страница только о логистике и способах оплаты.", "url_name": "delivery"},
                    {"title": "Отслеживание заказа", "text": "Статусы, TTN и действия после отправки.", "url_name": "order_tracking"},
                ],
            },
            {
                "eyebrow": "3",
                "title": "Баллы, промокоды и аккаунт",
                "bullets": [
                    "После авторизации в кабинете видны заказы, бонусы и промокоды.",
                    "Баллы накапливаются и могут конвертироваться в скидки по действующим правилам.",
                    "Повторные покупки удобнее вести через аккаунт, а не через внешние сообщения.",
                ],
            },
            {
                "eyebrow": "4",
                "title": "Кастомная печать и сервисные обращения",
                "paragraphs": [
                    "Для готовых товаров действует сервисный маршрут возврата и обмена: надлежащее качество можно вернуть или обменять в течение 14 дней с момента получения согласно законодательству Украины.",
                    "Для кастомных задач есть отдельный сценарий: до запуска согласовываются файл или бриф, параметры и стоимость, а кастомная одежда после надлежащего выполнения возврату и обмену не подлежит. Если есть производственный брак или отклонение от согласованного макета, обращение рассматриваем отдельно.",
                    "Для сервисных ситуаций после покупки используйте страницы возврата, FAQ или контакты в зависимости от запроса.",
                ],
                "links": [
                    {"title": "Кастомный принт", "text": "Отдельный маршрут для печати, брендированных заявок и нестандартных запросов.", "url_name": "custom_print"},
                    {"title": "Возврат и обмен", "text": "Что подготовить и как быстрее передать сервисное обращение.", "url_name": "returns"},
                ],
            },
        ],
        "cta": {
            "title": "Нужен быстрый ответ?",
            "text": "Для частых вопросов откройте FAQ. Если нужна живая коммуникация, переходите в контакты.",
            "primary": {"label": "Открыть FAQ", "url_name": "faq"},
            "secondary": {"label": "Контакты", "url_name": "contacts"},
        },
    },
    "faq": {
        "page_title": "FAQ — TwoComms",
        "meta_title": "FAQ — оплата, доставка, размеры и сервис | TwoComms",
        "meta_description": "FAQ TwoComms: ответы об оформлении заказа, оплате, доставке, размерах, возврате, баллах, кастомной печати и уходе за одеждой.",
        "meta_keywords": "TwoComms FAQ, вопросы и ответы, оплата, доставка, размеры, возврат",
        "hero_kicker": "FAQ",
        "hero_title": "Частые вопросы о покупке и сервисе",
        "hero_intro": "Это единый полный FAQ-хаб. Если нужны короткие ответы без длинных инструкций — начните отсюда.",
        "hero_meta": ["одна точка ответов", "оплата / доставка / размеры", "без дублирования на других страницах"],
        "intro_links": [
            {"label": "Помощь", "url_name": "help_center"},
            {"label": "Доставка", "url_name": "delivery"},
            {"label": "Возврат", "url_name": "returns"},
            {"label": "Размерная сетка", "url_name": "size_guide"},
        ],
        "sections": [
            {
                "eyebrow": "Перед покупкой",
                "title": "Что чаще всего проверяют",
                "cards": [
                    {"title": "Доставка и оплата", "text": "Сроки, оплата, наложенный платёж и международные отправления.", "url_name": "delivery"},
                    {"title": "Размерная сетка", "text": "Подтверждённые замеры худи и базовых футболок.", "url_name": "size_guide"},
                    {"title": "Помощь", "text": "Короткий manual по корзине, бонусам и сервисным сценариям.", "url_name": "help_center"},
                ],
            },
        ],
        "faq_items": [
            {"question": "Как оформить заказ на TwoComms?", "answer": "Выберите товар, размер и цвет, добавьте позицию в корзину, заполните контакты, доставку и оплату. После этого заказ поступает в обработку."},
            {"question": "Какие способы оплаты доступны?", "answer": "На сайте доступны полная онлайн-оплата, mono checkout и отдельные сценарии с частичной предоплатой. Точный вариант зависит от формата заказа."},
            {"question": "Сколько идёт доставка по Украине?", "answer": "Базовый ориентир для отправлений по Украине — 1-5 рабочих дней после подготовки заказа к логистике."},
            {"question": "Как отследить заказ?", "answer": "После отправки вы получаете номер отправления. Также статус можно проверять в личном кабинете или через страницу отслеживания."},
            {"question": "Какие условия возврата и обмена?", "answer": "Готовые товары надлежащего качества можно вернуть или обменять в течение 14 дней с момента получения согласно законодательству Украины, если сохранён товарный вид, бирки и нет следов использования. Кастомная одежда, изготовленная по индивидуальному заказу, возврату или обмену не подлежит при надлежащем выполнении."},
            {"question": "Есть ли таблица размеров?", "answer": "Да. Для подтверждённых категорий мы показываем отдельные garment measurements и короткие подсказки по посадке."},
            {"question": "Как работают баллы и промокоды?", "answer": "После покупок и отдельных действий в аккаунте начисляются баллы. Их можно конвертировать в промокоды согласно актуальным правилам программы."},
            {"question": "Можно ли заказать кастомный принт?", "answer": "Да. Для этого есть отдельный сценарий кастомной печати: вы передаёте файл или описание, а менеджер согласует детали, формат и стоимость."},
            {"question": "Как ухаживать за одеждой TwoComms?", "answer": "Рекомендуем стирать вещи наизнанку, не использовать агрессивные отбеливатели и придерживаться щадящего режима сушки. Подробнее это собрано на отдельной странице."},
        ],
        "cta": {
            "title": "Не нашли ответ в FAQ?",
            "text": "Перейдите в help-центр или напишите нам напрямую, если ваш запрос не вписывается в типовые сценарии.",
            "primary": {"label": "Открыть помощь", "url_name": "help_center"},
            "secondary": {"label": "Контакты", "url_name": "contacts"},
        },
    },
    "size_guide": {
        "page_title": "Размерная сетка — TwoComms",
        "meta_title": "Размерная сетка и советы по посадке | TwoComms",
        "meta_description": "Размерная сетка TwoComms: подтверждённые garment measurements, советы по посадке, как снимать мерки и когда обращаться в поддержку.",
        "meta_keywords": "TwoComms размерная сетка, hoodie size guide, футболка size guide, как выбрать размер",
        "hero_kicker": "Fit Guide",
        "hero_title": "Размерная сетка и советы по посадке",
        "hero_intro": "Страница построена как fit-хаб: подтверждённые таблицы для ключевых категорий, короткий способ сверки со своей вещью и понятный маршрут, если нужна помощь.",
        "hero_meta": ["garment measurements", "подтверждённые категории", "без перегрузки"],
        "intro_links": [
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Помощь", "url_name": "help_center"},
            {"label": "Контакты", "url_name": "contacts"},
            {"label": "Футболки", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "tshirts"}},
            {"label": "Худи", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "hoodie"}},
            {"label": "Лонгсливы", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "long-sleeve"}},
        ],
        "sections": [
            {
                "eyebrow": "Как выбирать",
                "title": "Начинайте со своей вещи, а не с абстрактного роста",
                "bullets": [
                    "Положите свою футболку или худи на ровную поверхность и снимите мерки с изделия.",
                    "Сравните ширину, длину, плечи и рукав с таблицей нужной категории.",
                    "Если колеблетесь между двумя размерами — выбирайте по желаемой посадке, а не только по привычке.",
                ],
            },
            {
                "eyebrow": "Когда писать нам",
                "title": "Обращайтесь в поддержку до оплаты, если",
                "bullets": [
                    "нужен более точный фит для нестандартной фигуры или stylized fit;",
                    "важна конкретная длина или запас под второй слой;",
                    "для категории ещё нет отдельной подтверждённой таблицы.",
                ],
            },
        ],
        "cta": {
            "title": "Нужна более точная подсказка по размеру?",
            "text": "Опишите модель, желаемый fit и свой привычный размер. Так мы быстрее подскажем без лишних сообщений.",
            "primary": {"label": "Написать нам", "url_name": "contacts"},
            "secondary": {"label": "Открыть FAQ", "url_name": "faq"},
        },
    },
    "care_guide": {
        "page_title": "Уход за одеждой — TwoComms",
        "meta_title": "Уход за одеждой TwoComms",
        "meta_description": "Как ухаживать за одеждой TwoComms: стирка, сушка, глажка и базовые советы для сохранения формы и принта.",
        "meta_keywords": "уход за одеждой, стирка футболок, стирка худи, уход за принтом",
        "hero_kicker": "Care",
        "hero_title": "Уход за одеждой",
        "hero_intro": "Короткие правила ухода без перегрузки: как стирать, сушить и хранить вещи, чтобы они дольше держали форму и вид.",
        "intro_links": [
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Возврат", "url_name": "returns"},
        ],
        "sections": [
            {
                "title": "Базовые правила",
                "bullets": [
                    "Стирайте вещи наизнанку при умеренной температуре.",
                    "Не используйте агрессивные отбеливатели и жёсткие режимы сушки.",
                    "Гладьте аккуратно и не перегревайте зоны с принтом.",
                ],
            },
            {
                "title": "Что продлевает ресурс вещи",
                "bullets": [
                    "Сортируйте тёмные и светлые вещи отдельно.",
                    "Не оставляйте влажную одежду сложенной надолго после стирки.",
                    "Для принтов и деликатных поверхностей избегайте грубого механического трения.",
                ],
            },
        ],
        "cta": {
            "title": "Есть вопрос по конкретной вещи?",
            "text": "Напишите нам, если нужна подсказка именно по ткани, принту или особенностям ухода.",
            "primary": {"label": "Контакты", "url_name": "contacts"},
            "secondary": {"label": "FAQ", "url_name": "faq"},
        },
    },
    "order_tracking": {
        "page_title": "Отслеживание заказа — TwoComms",
        "meta_title": "Отслеживание заказа, статусы и TTN | TwoComms",
        "meta_description": "Как отследить заказ TwoComms: личный кабинет, номер отправления, этапы обработки и поддержка.",
        "meta_keywords": "TwoComms отслеживание заказа, статус заказа, TTN, мои заказы",
        "hero_kicker": "Order Status",
        "hero_title": "Отслеживание заказа",
        "hero_intro": "Отдельный маршрут для статусов, TTN и короткого пояснения этапов после оформления покупки.",
        "intro_links": [
            {"label": "Мои заказы", "url_name": "my_orders"},
            {"label": "Помощь", "url_name": "help_center"},
            {"label": "Контакты", "url_name": "contacts"},
        ],
        "sections": [
            {
                "title": "Где смотреть статус",
                "links": [
                    {"title": "Личный кабинет", "text": "После входа в аккаунт откройте «Мои заказы» и проверьте актуальный этап.", "url_name": "my_orders"},
                    {"title": "Номер отправления", "text": "После отгрузки ориентируйтесь на TTN или номер перевозчика."},
                    {"title": "Поддержка", "text": "Если нужно уточнение, подготовьте номер заказа и обратитесь к нам.", "url_name": "contacts"},
                ],
            },
            {
                "title": "Что означают этапы",
                "bullets": [
                    "Оформлено: заказ создан и ждёт подтверждения или оплаты.",
                    "В обработке: мы проверяем склад, параметры или уточняем детали.",
                    "Отправлено: посылка уже передана перевозчику.",
                    "Завершено: заказ успешно получен.",
                ],
            },
        ],
        "cta": {
            "title": "Нужно быстро проверить заказ?",
            "text": "Если статус не обновляется или нужно пояснение, переходите в контакты с номером заказа.",
            "primary": {"label": "Контакты", "url_name": "contacts"},
            "secondary": {"label": "Мои заказы", "url_name": "my_orders"},
        },
    },
    "site_map_page": {
        "page_title": "Карта сайта — TwoComms",
        "meta_title": "Карта сайта TwoComms — каталог, поддержка и бренд",
        "meta_description": "Карта сайта TwoComms с маршрутами по каталогу, поддержке, бренду, FAQ, размерам, контактам и сервисным страницам.",
        "meta_keywords": "карта сайта twocomms, навигация сайта, каталог, поддержка, бренд",
        "hero_kicker": "Site Map",
        "hero_title": "Карта сайта и быстрая навигация",
        "hero_intro": "Человеческая карта сайта без дублирования содержимого support-страниц. Она показывает основные маршруты и помогает быстро найти нужный кластер.",
        "intro_links": [
            {"label": "Каталог", "url_name": "catalog"},
            {"label": "Помощь", "url_name": "help_center"},
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Контакты", "url_name": "contacts"},
        ],
        "sections": [
            {
                "title": "Основные разделы магазина",
                "links": [
                    {"title": "Главная", "text": "Быстрый старт, категории и актуальные акценты.", "url_name": "home"},
                    {"title": "Каталог", "text": "Все товары и категории.", "url_name": "catalog"},
                    {"title": "Кастомный принт", "text": "Отдельный сервис для печати и нестандартных заявок.", "url_name": "custom_print"},
                    {"title": "Сотрудничество", "text": "Опт, партнёрства и бренд-запросы.", "url_name": "cooperation"},
                ],
            },
            {
                "title": "Поддержка и сервис",
                "links": [
                    {"title": "Помощь", "text": "Базовый service-manual.", "url_name": "help_center"},
                    {"title": "FAQ", "text": "Полный хаб с ответами.", "url_name": "faq"},
                    {"title": "Размерная сетка", "text": "Подтверждённые таблицы и fit-советы.", "url_name": "size_guide"},
                    {"title": "Отслеживание заказа", "text": "Статусы, TTN и дальнейшие действия.", "url_name": "order_tracking"},
                ],
            },
        ],
        "cta": {
            "title": "Нужен самый короткий маршрут?",
            "text": "Для покупки начинайте с каталога, а для сервисных вопросов — с help-центра или FAQ.",
            "primary": {"label": "Каталог", "url_name": "catalog"},
            "secondary": {"label": "Помощь", "url_name": "help_center"},
        },
    },
    "news": {
        "page_title": "Новости — TwoComms",
        "meta_title": "Новости, новинки и апдейты бренда | TwoComms",
        "meta_description": "Новости TwoComms: новинки каталога, апдейты бренда, сервисные обновления и маршруты к актуальному ассортименту.",
        "meta_keywords": "TwoComms новости, новинки бренда, релизы, новая одежда",
        "hero_kicker": "Updates",
        "hero_title": "Новости и апдейты бренда",
        "hero_intro": "Страница работает как чистый brand-updates хаб: что нового в каталоге, куда смотреть дальше и как быстро перейти к актуальным релизам.",
        "intro_links": [
            {"label": "Каталог", "url_name": "catalog"},
            {"label": "О бренде", "url_name": "about"},
            {"label": "Главная", "url_name": "home"},
        ],
        "sections": [
            {
                "title": "Что здесь найти",
                "cards": [
                    {"title": "Новинки каталога", "text": "Свежие позиции и актуальные публикации товаров.", "url_name": "catalog"},
                    {"title": "Апдейты бренда", "text": "Фокус на эстетике, направлении и новых акцентах TwoComms.", "url_name": "about"},
                    {"title": "Сервисные обновления", "text": "Когда меняются важные маршруты по доставке или поддержке.", "url_name": "help_center"},
                ],
            },
        ],
        "cta": {
            "title": "Хотите сразу перейти к актуальному?",
            "text": "Чаще всего вся новостная логика ведёт в каталог или на главную с актуальными акцентами.",
            "primary": {"label": "Каталог", "url_name": "catalog"},
            "secondary": {"label": "Главная", "url_name": "home"},
        },
    },
    "returns": {
        "page_title": "Возврат и обмен — TwoComms",
        "meta_title": "Возврат и обмен товаров | TwoComms",
        "meta_description": "Возврат и обмен TwoComms: готовые товары надлежащего качества можно вернуть или обменять в течение 14 дней, кастомная одежда имеет отдельные условия.",
        "meta_keywords": "TwoComms возврат, обмен товара, сервис после покупки",
        "hero_kicker": "After Purchase",
        "hero_title": "Возврат и обмен",
        "hero_intro": "Готовые товары надлежащего качества можно вернуть или обменять в течение 14 дней с момента получения согласно законодательству Украины. Кастомная одежда, изготовленная по индивидуальному заказу, не подлежит возврату или обмену, если выполнена надлежащим образом и соответствует согласованным параметрам.",
        "intro_links": [
            {"label": "Помощь", "url_name": "help_center"},
            {"label": "Контакты", "url_name": "contacts"},
            {"label": "Футболки", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "tshirts"}},
            {"label": "Худи", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "hoodie"}},
            {"label": "Лонгсливы", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "long-sleeve"}},
        ],
        "sections": [
            {
                "title": "Для каких заказов действует возврат и обмен",
                "bullets": [
                    "готовые товары надлежащего качества можно вернуть или обменять в течение 14 календарных дней с момента получения;",
                    "для сервисного обращения вещь должна сохранять товарный вид, бирки и не иметь следов использования;",
                    "кастомная одежда, изготовленная по индивидуальному заказу, возврату или обмену не подлежит, если выполнена надлежащим образом и соответствует согласованным параметрам.",
                ],
            },
            {
                "title": "Что подготовить для обращения",
                "bullets": [
                    "номер заказа;",
                    "короткое описание ситуации;",
                    "фото товара, если это помогает быстрее оценить вопрос.",
                ],
            },
            {
                "title": "Как проходит сервисный процесс",
                "paragraphs": [
                    "Для готовых товаров мы проверяем условия возврата или обмена и согласовываем дальнейшие шаги через официальные каналы TwoComms.",
                    "Если вопрос касается кастомного изделия, производственного брака или отклонения от согласованного макета, ситуацию рассматриваем отдельно после проверки деталей заказа.",
                ],
                "bullets": [
                    "вы передаёте обращение через официальные каналы;",
                    "мы проверяем детали и согласовываем дальнейший сценарий;",
                    "после этого получаете чёткое продолжение без лишних переадресаций.",
                ],
            },
        ],
        "faq_items": [
            {"question": "Какие товары можно вернуть или обменять?", "answer": "Готовые товары надлежащего качества можно вернуть или обменять в течение 14 дней с момента получения согласно законодательству Украины, если сохранён товарный вид, бирки и нет следов использования."},
            {"question": "Можно ли вернуть кастомную одежду?", "answer": "Кастомная одежда, изготовленная по индивидуальному заказу, не подлежит возврату или обмену, если выполнена надлежащим образом и соответствует согласованным параметрам. Если есть производственный брак или отклонение от согласованного макета, обратитесь к нам через контакты — мы рассмотрим ситуацию отдельно."},
            {"question": "Что подготовить для сервисного обращения?", "answer": "Номер заказа, короткое описание ситуации и при необходимости фото товара или проблемы. Это поможет быстрее определить правильный сценарий."},
        ],
        "cta": {
            "title": "Нужно передать сервисный запрос или проверить случай по кастому?",
            "text": "Начните с контактов и сразу добавьте номер заказа, описание ситуации и фото. Это заметно сокращает время на уточнение.",
            "primary": {"label": "Контакты", "url_name": "contacts"},
            "secondary": {"label": "Помощь", "url_name": "help_center"},
        },
    },
    "privacy_policy": {
        "page_title": "Политика конфиденциальности — TwoComms",
        "meta_title": "Политика конфиденциальности | TwoComms",
        "meta_description": "Политика конфиденциальности TwoComms: какие данные используются для заказов, аккаунта, поддержки и технической работы сайта.",
        "meta_keywords": "TwoComms политика конфиденциальности, персональные данные, cookies",
        "hero_kicker": "Privacy",
        "hero_title": "Политика конфиденциальности",
        "hero_intro": "Кратко о том, какие данные нужны для работы заказов, аккаунта, сервисной коммуникации и базовой технической аналитики сайта.",
        "intro_links": [
            {"label": "Условия использования", "url_name": "terms_of_service"},
            {"label": "Контакты", "url_name": "contacts"},
        ],
        "sections": [
            {
                "title": "Для чего используются данные",
                "bullets": [
                    "для оформления, доставки и сопровождения заказа;",
                    "для авторизации и работы личного кабинета;",
                    "для сервисных уведомлений и улучшения работы сайта.",
                ],
            },
            {
                "title": "Коммуникация и техническая аналитика",
                "paragraphs": [
                    "Часть данных используется для подтверждения заказов, уточнений по доставке и поддержки клиента.",
                    "Технические инструменты аналитики нужны для оценки работы страниц и сервисных сценариев.",
                ],
            },
        ],
        "cta": {
            "title": "Нужно уточнение по данным?",
            "text": "Опишите свой запрос через контакты максимально конкретно — так мы быстрее дадим корректный ответ.",
            "primary": {"label": "Контакты", "url_name": "contacts"},
            "secondary": {"label": "Условия использования", "url_name": "terms_of_service"},
        },
    },
    "terms_of_service": {
        "page_title": "Условия использования — TwoComms",
        "meta_title": "Условия использования сайта | TwoComms",
        "meta_description": "Условия использования сайта TwoComms: базовые правила оформления заказов, работы аккаунта и сервисного взаимодействия.",
        "meta_keywords": "TwoComms условия использования, правила сайта, оформление заказа",
        "hero_kicker": "Terms",
        "hero_title": "Условия использования сайта",
        "hero_intro": "Краткий набор базовых правил: как работает сайт, аккаунт, оформление заказов и сервисные страницы.",
        "intro_links": [
            {"label": "Политика конфиденциальности", "url_name": "privacy_policy"},
            {"label": "Помощь", "url_name": "help_center"},
        ],
        "sections": [
            {
                "title": "Использование сайта",
                "bullets": [
                    "пользователь самостоятельно проверяет корректность контактных и адресных данных;",
                    "оформление заказа запускает стандартный сервисный процесс магазина;",
                    "часть функций доступна только после авторизации.",
                ],
            },
            {
                "title": "Сервисное взаимодействие",
                "bullets": [
                    "для поддержки и послепродажных обращений используйте официальные каналы сайта;",
                    "для быстрого решения запроса полезно сразу добавлять номер заказа;",
                    "дополнительные правила могут уточняться на отдельных support-страницах.",
                ],
            },
        ],
        "cta": {
            "title": "Нужна связанная сервисная информация?",
            "text": "Перейдите в help-центр, если нужен более практичный manual по оплате, доставке или поддержке.",
            "primary": {"label": "Открыть помощь", "url_name": "help_center"},
            "secondary": {"label": "Политика конфиденциальности", "url_name": "privacy_policy"},
        },
    },
}


# ---------------------------------------------------------------------------
# English overrides
# ---------------------------------------------------------------------------

_EN_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "about": {
        "page_title": "About the brand — TwoComms",
        "meta_title": "TwoComms — Kharkiv-based clothing brand about character, resilience and continuation",
        "meta_description": "TwoComms — a Ukrainian streetwear / military-adjacent brand from Kharkiv. Clothing with code, meaningful prints, real origin and a philosophy: not a full stop, a continuation.",
        "meta_keywords": "TwoComms, Kharkiv clothing brand, Ukrainian streetwear brand, military-adjacent clothing, clothing with meaning, meaningful prints, two commas, Kharkiv",
        "hero_kicker": "Brand",
        "hero_title": "TwoComms — not a full stop. A continuation.",
        "hero_intro": "TwoComms is a Kharkiv-based streetwear / military-adjacent brand about character, meaningful prints, inner discipline, and moving forward after the critical point.",
    },
    "delivery": {
        "page_title": "Delivery & payment — TwoComms",
        "meta_title": "Delivery & payment — TwoComms",
        "meta_description": "TwoComms delivery and payment terms: shipping times in Ukraine, international orders, payment options, cash on delivery, and order tracking.",
        "meta_keywords": "TwoComms delivery, payment, cash on delivery, mono checkout, international shipping",
        "hero_kicker": "Delivery & Payment",
        "hero_title": "Delivery & payment",
        "hero_intro": "Only what you need for logistics and payment: timelines, Ukraine scenarios, international shipping, and the route after checkout.",
        "hero_meta": ["1-5 days within Ukraine", "mono checkout", "tracking after dispatch"],
        "intro_links": [
            {"label": "Order tracking", "url_name": "order_tracking"},
            {"label": "Returns & exchange", "url_name": "returns"},
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Shop t-shirts", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "tshirts"}},
            {"label": "Shop hoodies", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "hoodie"}},
            {"label": "Shop long sleeves", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "long-sleeve"}},
        ],
        "sections": [
            {
                "eyebrow": "Ukraine",
                "title": "Standard delivery scenario",
                "cards": [
                    {"title": "Timelines", "text": "Within Ukraine, expect 1-5 business days after the order is prepared."},
                    {"title": "Payment", "text": "Full online payment, mono checkout, and selected partial-prepayment scenarios are available."},
                    {"title": "Logistics", "text": "Shipping cost is determined by the carrier, parcel format, and destination."},
                ],
            },
            {
                "eyebrow": "International",
                "title": "What to consider for international shipping",
                "bullets": [
                    "Timeframes and cost depend on the country and carrier.",
                    "For international orders, full prepayment may be required before dispatch.",
                    "Clarify international logistics before payment, not after checkout.",
                ],
            },
            {
                "eyebrow": "After payment",
                "title": "Where to go next",
                "links": [
                    {"title": "Order tracking", "text": "TTN, statuses, and the route after the parcel reaches the carrier.", "url_name": "order_tracking"},
                    {"title": "FAQ", "text": "Quick answers to related service questions.", "url_name": "faq"},
                    {"title": "Returns & exchange", "text": "A separate route for questions after you receive the order.", "url_name": "returns"},
                ],
            },
        ],
        "faq_items": [
            {"question": "How long does delivery within Ukraine take?", "answer": "Expect 1-5 business days after the order is prepared for dispatch."},
            {"question": "Can I pay on delivery?", "answer": "Yes, for some orders a partial-prepayment with cash-on-delivery scenario is available. The final option depends on the dispatch type."},
            {"question": "How do I track my order?", "answer": "Once the parcel is handed over to the carrier, you receive a tracking number and can check the status in your account or on the tracking page."},
            {"question": "Do you ship internationally?", "answer": "Yes, international shipments are possible. Conditions, cost, and timelines depend on the destination and logistics scenario."},
        ],
        "cta": {
            "title": "Need a quick route after checkout?",
            "text": "Open tracking or the FAQ — those are the shortest entry points for most post-purchase questions.",
            "primary": {"label": "Track order", "url_name": "order_tracking"},
            "secondary": {"label": "Open FAQ", "url_name": "faq"},
        },
    },
    "help_center": {
        "page_title": "Help — TwoComms",
        "meta_title": "Help — order terms, payment and support | TwoComms",
        "meta_description": "TwoComms help center: how to place an order, how delivery, payment, points, promo codes, custom print, and service requests work.",
        "meta_keywords": "TwoComms help, order terms, customer support, delivery, payment",
        "hero_kicker": "Support Center",
        "hero_title": "Help and site basics",
        "hero_intro": "A service manual with no extra repetition: ordering, payment, delivery, loyalty, custom requests, and follow-up support.",
        "hero_meta": ["manual format", "no FAQ overlap", "fast service navigation"],
        "intro_links": [
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Delivery", "url_name": "delivery"},
            {"label": "Size guide", "url_name": "size_guide"},
            {"label": "Contacts", "url_name": "contacts"},
        ],
        "sections": [
            {
                "eyebrow": "1",
                "title": "Placing an order",
                "paragraphs": [
                    "The customer picks the product, size, and colour, adds items to the cart, and proceeds to checkout.",
                    "After contacts, delivery, and payment are filled in, the order is registered and goes into processing.",
                ],
                "bullets": [
                    "The current price and discount are shown on the product card and in the cart.",
                    "Non-standard or custom tasks may require additional confirmation.",
                ],
            },
            {
                "eyebrow": "2",
                "title": "Payment, delivery, and statuses",
                "paragraphs": [
                    "Full online payment is available for most orders, with partial prepayment for selected scenarios.",
                    "Once the order ships, you receive a tracking number and can move to tracking.",
                ],
                "links": [
                    {"title": "Delivery & payment", "text": "A dedicated page on logistics and payment methods.", "url_name": "delivery"},
                    {"title": "Order tracking", "text": "Statuses, TTN, and actions after dispatch.", "url_name": "order_tracking"},
                ],
            },
            {
                "eyebrow": "3",
                "title": "Points, promo codes and account",
                "bullets": [
                    "Once logged in, you can see your orders, bonuses, and promo codes in your account.",
                    "Points accumulate and can be converted into discounts under the active rules.",
                    "Recurring purchases are easier to manage from the account than through external messages.",
                ],
            },
            {
                "eyebrow": "4",
                "title": "Custom print and service requests",
                "paragraphs": [
                    "Ready-made items have a clear return/exchange route: items of proper quality can be returned or exchanged within 14 days of receipt under Ukrainian law.",
                    "Custom tasks have a dedicated scenario: file or brief, parameters, and price are agreed in advance, and properly made custom clothing isn't subject to return or exchange. Production defects or deviations from the agreed mockup are reviewed separately.",
                    "For post-purchase service situations use the returns, FAQ, or contacts pages depending on the request.",
                ],
                "links": [
                    {"title": "Custom print", "text": "A dedicated track for print, branded requests, and non-standard inquiries.", "url_name": "custom_print"},
                    {"title": "Returns & exchange", "text": "What to prepare and how to submit a service request faster.", "url_name": "returns"},
                ],
            },
        ],
        "cta": {
            "title": "Need a quick answer?",
            "text": "Open the FAQ for common questions. For live communication, head to contacts.",
            "primary": {"label": "Open FAQ", "url_name": "faq"},
            "secondary": {"label": "Contacts", "url_name": "contacts"},
        },
    },
    "faq": {
        "page_title": "FAQ — TwoComms",
        "meta_title": "FAQ — payment, delivery, sizes and service | TwoComms",
        "meta_description": "TwoComms FAQ: answers about checkout, payment, delivery, sizes, returns, points, custom print, and garment care.",
        "meta_keywords": "TwoComms FAQ, questions and answers, payment, delivery, sizes, returns",
        "hero_kicker": "FAQ",
        "hero_title": "Frequently asked questions about purchase and service",
        "hero_intro": "This is the single comprehensive FAQ hub. If you want short answers without long manuals, start here.",
        "hero_meta": ["single answers hub", "payment / delivery / sizes", "no duplication elsewhere"],
        "intro_links": [
            {"label": "Help", "url_name": "help_center"},
            {"label": "Delivery", "url_name": "delivery"},
            {"label": "Returns", "url_name": "returns"},
            {"label": "Size guide", "url_name": "size_guide"},
        ],
        "sections": [
            {
                "eyebrow": "Before purchase",
                "title": "What people check most often",
                "cards": [
                    {"title": "Delivery & payment", "text": "Timelines, payment, cash on delivery, and international shipping.", "url_name": "delivery"},
                    {"title": "Size guide", "text": "Confirmed measurements for hoodies and core tees.", "url_name": "size_guide"},
                    {"title": "Help", "text": "Short manual on the cart, bonuses, and service scenarios.", "url_name": "help_center"},
                ],
            },
        ],
        "faq_items": [
            {"question": "How do I place an order on TwoComms?", "answer": "Pick a product, size, and colour, add it to the cart, fill in contacts, delivery, and payment. The order then goes into processing."},
            {"question": "Which payment methods are available?", "answer": "Full online payment, mono checkout, and selected partial-prepayment scenarios are available. The exact option depends on the order format."},
            {"question": "How long is delivery within Ukraine?", "answer": "The baseline is 1-5 business days after the order is prepared for logistics."},
            {"question": "How do I track my order?", "answer": "After dispatch you get a tracking number. You can also check the status in your account or on the tracking page."},
            {"question": "What are the return and exchange terms?", "answer": "Ready-made items of proper quality can be returned or exchanged within 14 days of receipt under Ukrainian law, provided the items keep their original condition, tags, and have no signs of use. Custom apparel made to individual order isn't subject to return or exchange when properly executed."},
            {"question": "Is there a size chart?", "answer": "Yes. For confirmed categories we publish dedicated garment measurements and short fit hints."},
            {"question": "How do points and promo codes work?", "answer": "Purchases and selected account actions earn points. They can be converted into promo codes under the active program rules."},
            {"question": "Can I order a custom print?", "answer": "Yes. There's a dedicated custom-print track: you share the file or description, and a manager agrees on details, format, and price."},
            {"question": "How do I care for TwoComms clothing?", "answer": "We recommend washing items inside out, avoiding aggressive bleach, and using gentle drying. Details are on the dedicated care page."},
        ],
        "cta": {
            "title": "Didn't find the answer in the FAQ?",
            "text": "Open the help center or message us directly if your request doesn't fit the typical scenarios.",
            "primary": {"label": "Open help", "url_name": "help_center"},
            "secondary": {"label": "Contacts", "url_name": "contacts"},
        },
    },
    "size_guide": {
        "page_title": "Size guide — TwoComms",
        "meta_title": "Size guide and fit tips | TwoComms",
        "meta_description": "TwoComms size guide: confirmed garment measurements, fit tips, how to measure, and when to reach out to support.",
        "meta_keywords": "TwoComms size guide, hoodie size guide, t-shirt size guide, how to choose size",
        "hero_kicker": "Fit Guide",
        "hero_title": "Size guide and fit tips",
        "hero_intro": "A fit hub with confirmed tables for the key categories, a short way to compare against your own piece, and a clear route to support if needed.",
        "hero_meta": ["garment measurements", "confirmed categories", "no overload"],
        "intro_links": [
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Help", "url_name": "help_center"},
            {"label": "Contacts", "url_name": "contacts"},
            {"label": "T-shirts", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "tshirts"}},
            {"label": "Hoodies", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "hoodie"}},
            {"label": "Long sleeves", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "long-sleeve"}},
        ],
        "sections": [
            {
                "eyebrow": "How to choose",
                "title": "Start with your own piece, not an abstract height",
                "bullets": [
                    "Lay a t-shirt or hoodie you own on a flat surface and measure the garment.",
                    "Compare width, length, shoulders, and sleeve against the table for the right category.",
                    "If you're between two sizes, choose by the fit you want, not just out of habit.",
                ],
            },
            {
                "eyebrow": "When to message us",
                "title": "Contact support before payment if",
                "bullets": [
                    "you need a more precise fit for a non-standard build or stylized fit;",
                    "a specific length or layering allowance matters;",
                    "no confirmed table exists for the category yet.",
                ],
            },
        ],
        "cta": {
            "title": "Need a more precise size hint?",
            "text": "Describe the model, the desired fit, and your usual size — we'll respond faster with fewer follow-up messages.",
            "primary": {"label": "Message us", "url_name": "contacts"},
            "secondary": {"label": "Open FAQ", "url_name": "faq"},
        },
    },
    "care_guide": {
        "page_title": "Garment care — TwoComms",
        "meta_title": "Garment care for TwoComms",
        "meta_description": "How to care for TwoComms garments: washing, drying, ironing, and basic tips to keep the shape and the print.",
        "meta_keywords": "garment care, washing t-shirts, washing hoodies, print care",
        "hero_kicker": "Care",
        "hero_title": "Garment care",
        "hero_intro": "Short care rules without overload: how to wash, dry, and store items so they keep their shape and look longer.",
        "intro_links": [
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Returns", "url_name": "returns"},
        ],
        "sections": [
            {
                "title": "Basics",
                "bullets": [
                    "Wash items inside out at moderate temperatures.",
                    "Avoid aggressive bleach and harsh drying programs.",
                    "Iron gently and don't overheat the printed areas.",
                ],
            },
            {
                "title": "What extends the life of a piece",
                "bullets": [
                    "Sort dark and light items separately.",
                    "Don't leave wet clothing folded for long after washing.",
                    "Avoid rough mechanical friction on prints and delicate surfaces.",
                ],
            },
        ],
        "cta": {
            "title": "Have a question about a specific piece?",
            "text": "Message us if you need advice on the fabric, the print, or specific care steps.",
            "primary": {"label": "Contacts", "url_name": "contacts"},
            "secondary": {"label": "FAQ", "url_name": "faq"},
        },
    },
    "order_tracking": {
        "page_title": "Order tracking — TwoComms",
        "meta_title": "Order tracking, statuses and TTN | TwoComms",
        "meta_description": "How to track a TwoComms order: personal account, tracking number, processing stages, and support.",
        "meta_keywords": "TwoComms order tracking, order status, TTN, my orders",
        "hero_kicker": "Order Status",
        "hero_title": "Order tracking",
        "hero_intro": "A dedicated route for statuses, TTN, and a short explanation of post-purchase stages.",
        "intro_links": [
            {"label": "My orders", "url_name": "my_orders"},
            {"label": "Help", "url_name": "help_center"},
            {"label": "Contacts", "url_name": "contacts"},
        ],
        "sections": [
            {
                "title": "Where to see the status",
                "links": [
                    {"title": "Personal account", "text": "Sign in and open My Orders to check the current stage.", "url_name": "my_orders"},
                    {"title": "Tracking number", "text": "Once dispatched, follow the TTN or carrier number."},
                    {"title": "Support", "text": "If you need a clarification, prepare your order number and reach out.", "url_name": "contacts"},
                ],
            },
            {
                "title": "What the stages mean",
                "bullets": [
                    "Placed: the order is created and awaits confirmation or payment.",
                    "Processing: we check stock, parameters, or clarify details.",
                    "Shipped: the parcel is already with the carrier.",
                    "Completed: the order has been received successfully.",
                ],
            },
        ],
        "cta": {
            "title": "Need a quick order check?",
            "text": "If the status isn't updating or you need a clarification, head to contacts with your order number.",
            "primary": {"label": "Contacts", "url_name": "contacts"},
            "secondary": {"label": "My orders", "url_name": "my_orders"},
        },
    },
    "site_map_page": {
        "page_title": "Site map — TwoComms",
        "meta_title": "TwoComms site map — catalog, support, and brand",
        "meta_description": "TwoComms site map with routes across the catalog, support, brand, FAQ, sizes, contacts, and service pages.",
        "meta_keywords": "twocomms site map, site navigation, catalog, support, brand",
        "hero_kicker": "Site Map",
        "hero_title": "Site map and quick navigation",
        "hero_intro": "A human-readable site map without duplicating support-page content. It shows the main routes and helps you find the right cluster fast.",
        "intro_links": [
            {"label": "Catalog", "url_name": "catalog"},
            {"label": "Help", "url_name": "help_center"},
            {"label": "FAQ", "url_name": "faq"},
            {"label": "Contacts", "url_name": "contacts"},
        ],
        "sections": [
            {
                "title": "Main store sections",
                "links": [
                    {"title": "Home", "text": "Quick start, categories, and current highlights.", "url_name": "home"},
                    {"title": "Catalog", "text": "All products and categories.", "url_name": "catalog"},
                    {"title": "Custom print", "text": "A dedicated service for print and non-standard requests.", "url_name": "custom_print"},
                    {"title": "Cooperation", "text": "Wholesale, partnerships, and brand inquiries.", "url_name": "cooperation"},
                ],
            },
            {
                "title": "Support and service",
                "links": [
                    {"title": "Help", "text": "Core service manual.", "url_name": "help_center"},
                    {"title": "FAQ", "text": "Full answers hub.", "url_name": "faq"},
                    {"title": "Size guide", "text": "Confirmed tables and fit tips.", "url_name": "size_guide"},
                    {"title": "Order tracking", "text": "Statuses, TTN, and next steps.", "url_name": "order_tracking"},
                ],
            },
        ],
        "cta": {
            "title": "Need the shortest route?",
            "text": "Start from the catalog for shopping or from the help center / FAQ for service questions.",
            "primary": {"label": "Catalog", "url_name": "catalog"},
            "secondary": {"label": "Help", "url_name": "help_center"},
        },
    },
    "news": {
        "page_title": "News — TwoComms",
        "meta_title": "News, drops, and brand updates | TwoComms",
        "meta_description": "TwoComms news: catalog drops, brand updates, service notices, and routes to the current assortment.",
        "meta_keywords": "TwoComms news, brand drops, releases, new apparel",
        "hero_kicker": "Updates",
        "hero_title": "News and brand updates",
        "hero_intro": "A clean brand-updates hub: what's new in the catalog, where to look next, and how to jump to current drops fast.",
        "intro_links": [
            {"label": "Catalog", "url_name": "catalog"},
            {"label": "About", "url_name": "about"},
            {"label": "Home", "url_name": "home"},
        ],
        "sections": [
            {
                "title": "What you'll find here",
                "cards": [
                    {"title": "Catalog drops", "text": "Fresh pieces and current product publications.", "url_name": "catalog"},
                    {"title": "Brand updates", "text": "Focus on aesthetics, direction, and new TwoComms accents.", "url_name": "about"},
                    {"title": "Service updates", "text": "When important delivery or support routes change.", "url_name": "help_center"},
                ],
            },
        ],
        "cta": {
            "title": "Want to jump straight to what's current?",
            "text": "Most news logic leads to the catalog or the home page with current highlights.",
            "primary": {"label": "Catalog", "url_name": "catalog"},
            "secondary": {"label": "Home", "url_name": "home"},
        },
    },
    "returns": {
        "page_title": "Returns & exchanges — TwoComms",
        "meta_title": "Returns and exchanges | TwoComms",
        "meta_description": "TwoComms returns and exchanges: ready-made items of proper quality can be returned or exchanged within 14 days; custom-made apparel has separate terms.",
        "meta_keywords": "TwoComms returns, exchange, after-sale service",
        "hero_kicker": "After Purchase",
        "hero_title": "Returns & exchanges",
        "hero_intro": "Ready-made items of proper quality can be returned or exchanged within 14 days of receipt under Ukrainian law. Custom apparel made to individual order is not subject to return or exchange when properly executed and matching agreed parameters.",
        "intro_links": [
            {"label": "Help", "url_name": "help_center"},
            {"label": "Contacts", "url_name": "contacts"},
            {"label": "T-shirts", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "tshirts"}},
            {"label": "Hoodies", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "hoodie"}},
            {"label": "Long sleeves", "url_name": "catalog_by_cat", "url_kwargs": {"cat_slug": "long-sleeve"}},
        ],
        "sections": [
            {
                "title": "Which orders are eligible",
                "bullets": [
                    "ready-made items of proper quality can be returned or exchanged within 14 calendar days of receipt;",
                    "for a service request the item must keep its original condition, tags, and have no signs of use;",
                    "custom apparel made to individual order isn't subject to return or exchange when properly executed and matching agreed parameters.",
                ],
            },
            {
                "title": "What to prepare for a request",
                "bullets": [
                    "your order number;",
                    "a short description of the situation;",
                    "a photo of the item if it helps assess the case faster.",
                ],
            },
            {
                "title": "How the service process works",
                "paragraphs": [
                    "For ready-made items we check the return or exchange conditions and agree on the next steps through TwoComms official channels.",
                    "If the question concerns a custom piece, a production defect, or a deviation from the agreed mockup, we review the case separately after checking the order details.",
                ],
                "bullets": [
                    "you submit the request through the official channels;",
                    "we check the details and agree on the next scenario;",
                    "after that you get a clear follow-up with no extra redirects.",
                ],
            },
        ],
        "faq_items": [
            {"question": "Which items can be returned or exchanged?", "answer": "Ready-made items of proper quality can be returned or exchanged within 14 days of receipt under Ukrainian law, provided the items keep their original condition, tags, and have no signs of use."},
            {"question": "Can custom apparel be returned?", "answer": "Custom apparel made to individual order is not subject to return or exchange when properly executed and matching agreed parameters. If there's a production defect or deviation from the agreed mockup, contact us and we'll review the case separately."},
            {"question": "What should I prepare for a service request?", "answer": "Your order number, a short description of the situation, and, if helpful, a photo of the item or the issue. That helps us pick the right scenario faster."},
        ],
        "cta": {
            "title": "Need to submit a service request or check a custom case?",
            "text": "Start from contacts and immediately add your order number, situation description, and photos. That noticeably shortens the clarification time.",
            "primary": {"label": "Contacts", "url_name": "contacts"},
            "secondary": {"label": "Help", "url_name": "help_center"},
        },
    },
    "privacy_policy": {
        "page_title": "Privacy policy — TwoComms",
        "meta_title": "Privacy policy | TwoComms",
        "meta_description": "TwoComms privacy policy: what data we use for orders, account, support, and the technical operation of the site.",
        "meta_keywords": "TwoComms privacy policy, personal data, cookies",
        "hero_kicker": "Privacy",
        "hero_title": "Privacy policy",
        "hero_intro": "A brief overview of the data needed for orders, the account, service communication, and basic technical site analytics.",
        "intro_links": [
            {"label": "Terms of service", "url_name": "terms_of_service"},
            {"label": "Contacts", "url_name": "contacts"},
        ],
        "sections": [
            {
                "title": "What data is used for",
                "bullets": [
                    "to place, deliver, and accompany the order;",
                    "to authenticate and run the personal account;",
                    "for service notifications and to improve the site.",
                ],
            },
            {
                "title": "Communication and technical analytics",
                "paragraphs": [
                    "Part of the data is used to confirm orders, clarify delivery details, and provide customer support.",
                    "Technical analytics tools are needed to evaluate how pages and service scenarios perform.",
                ],
            },
        ],
        "cta": {
            "title": "Need a data clarification?",
            "text": "Describe your request via contacts as specifically as possible so we can respond accurately faster.",
            "primary": {"label": "Contacts", "url_name": "contacts"},
            "secondary": {"label": "Terms of service", "url_name": "terms_of_service"},
        },
    },
    "terms_of_service": {
        "page_title": "Terms of service — TwoComms",
        "meta_title": "Site terms of service | TwoComms",
        "meta_description": "TwoComms site terms of service: basic rules for placing orders, the account, and service interactions.",
        "meta_keywords": "TwoComms terms of service, site rules, ordering",
        "hero_kicker": "Terms",
        "hero_title": "Terms of service",
        "hero_intro": "A short set of basic rules: how the site, account, ordering, and service pages work.",
        "intro_links": [
            {"label": "Privacy policy", "url_name": "privacy_policy"},
            {"label": "Help", "url_name": "help_center"},
        ],
        "sections": [
            {
                "title": "Using the site",
                "bullets": [
                    "the user is responsible for the accuracy of contact and address data;",
                    "placing an order starts the store's standard service process;",
                    "some features are available only after authentication.",
                ],
            },
            {
                "title": "Service interactions",
                "bullets": [
                    "use the site's official channels for support and post-purchase requests;",
                    "include the order number right away to speed up resolution;",
                    "additional rules may be specified on individual support pages.",
                ],
            },
        ],
        "cta": {
            "title": "Need related service information?",
            "text": "Head to the help center for a more practical manual on payment, delivery, or support.",
            "primary": {"label": "Open help", "url_name": "help_center"},
            "secondary": {"label": "Privacy policy", "url_name": "privacy_policy"},
        },
    },
}


_LANG_OVERRIDES = {"ru": _RU_OVERRIDES, "en": _EN_OVERRIDES}


def apply_language_overrides(page: dict, page_key: str, lang_code: str) -> dict:
    """Mutate ``page`` dict with per-language overrides, return it.

    No-op for Ukrainian (default) or unknown languages / pages. Only
    fields explicitly present in the override dict are replaced.
    """
    overrides = _LANG_OVERRIDES.get(lang_code or "")
    if not overrides:
        return page
    page_overrides = overrides.get(page_key)
    if not page_overrides:
        return page
    for field, value in page_overrides.items():
        page[field] = value

    # The historical RU/EN pages have their own shorter FAQ. Preserve the
    # policy adapter's localized slots, but source owner-reviewed topics from
    # the same gettext content as Ukrainian HTML and FAQ structured data.
    if page_key == "faq":
        reviewed_questions = {
            "Де виробляється одяг TwoComms?",
            "Чому ціна в Instagram відрізняється від сайту?",
            "Який склад поточних футболок і худі?",
            "Чи різняться розміри між футболками, худі та лонгслівами?",
            "Де подивитися фото деталі або заміри?",
            "Як доглядати за одягом TwoComms?",
            "Чи злазить DTF-принт після прання?",
            "Що робити при виявленні браку?",
            "Які умови опту і дропшипінгу?",
            "Чи працюєте з волонтерськими і ветеранськими проєктами?",
        }
        with override("uk"):
            reviewed = [item for item in HELP_FAQ_ITEMS if str(item["question"]) in reviewed_questions]
        with override(lang_code):
            reviewed = [
                {"question": str(item["question"]), "answer": str(item["answer"])}
                for item in reviewed
            ]
        old_care_question = {
            "ru": "Как ухаживать за одеждой TwoComms?",
            "en": "How do I care for TwoComms clothing?",
        }[lang_code]
        page["faq_items"] = [
            deepcopy(item) for item in page.get("faq_items", [])
            if str(item.get("question")) != old_care_question
        ] + reviewed
    elif page_key == "care_guide":
        # The first localized bullet previously said only "moderate" while
        # the HowTo and FAQ publish the confirmed 30 °C recommendation.
        page["sections"] = deepcopy(page["sections"])
        with override(lang_code):
            page["sections"][0]["bullets"][0] = str(SUPPORT_PAGE_DEFINITIONS["care_guide"]["sections"][0]["bullets"][0])
    return page
