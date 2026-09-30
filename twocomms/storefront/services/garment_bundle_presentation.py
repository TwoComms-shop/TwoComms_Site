"""Localized, explicit offer copy; prices are supplied by the server quote."""
from django.utils.translation import get_language

COPY = {'uk': {'eyebrow': 'РАЗОМ ВИГІДНІШЕ',
        'title': 'Футболка до худі',
        'intro': 'Той самий принт — особлива ціна на пару.',
        'other_intro': 'Теплий шар і улюблена футболка — разом вигідніше.',
        'same': 'Той самий принт',
        'other': 'Інший принт',
        'classic': 'Класична',
        'oversize': 'Оверсайз',
        'choose': 'Обрати футболку',
        'choose_hoodie': 'Обрати худі',
        'other_choice': 'Обрати інший принт',
        'terms': 'Одна футболка за спеціальною ціною до одного худі. Умови 225 — окремо.',
        'automatic': 'Вигода застосовується автоматично в кошику.',
        'terms_more': 'На речі в комплекті промокоди не додаються. Розмір футболки обираєш окремо.',
        'currency': 'грн',
        'from_': 'від',
        'saving': 'Вигода комплекту',
        'included': 'Вигоду комплекту вже враховано',
        'remaining': 'До цього худі можна додати футболку вигідніше.',
        'cart_title': 'Доповни худі футболкою',
        'size': 'Розмір футболки',
        'fit': 'Посадка',
        'color': 'Колір',
        'select_size': 'Обери розмір футболки',
        'add_pair': 'Додати комплект',
        'add_tee': 'Додати футболку',
        'total': 'Разом',
        'close': 'Закрити',
        'back': 'До вибору розміру',
        'loading': 'Підбираємо футболки…',
        'error': 'Не вдалося завантажити. Спробуй ще раз.',
        'retry': 'Спробувати ще раз',
        'added': 'Додано в кошик',
        'submitting': 'Додаємо…',
        'hoodie': 'Худі',
        'tee': 'Футболка',
        'checkout': 'Перейти до оформлення',
        'recommend': 'До худі — футболка за спеціальною ціною',
        'recommend_sub': 'Той самий принт — від 850 грн. Інший — від 900 грн.',
        'view': 'Переглянути варіанти',
        'pair_note': 'Додамо одне худі та одну футболку. Параметри худі — як обрано на сторінці.',
        'tee_note': 'Додамо тільки футболку до худі у твоєму кошику.',
        'rules_title': 'Як працює комплект',
        'empty': 'Зараз немає доступних футболок для цього комплекту.'},
 'ru': {'eyebrow': 'ВМЕСТЕ ВЫГОДНЕЕ',
        'title': 'Футболка к худи',
        'intro': 'Тот же принт — особая цена на пару.',
        'other_intro': 'Тёплый слой и любимая футболка — вместе выгоднее.',
        'same': 'Тот же принт',
        'other': 'Другой принт',
        'classic': 'Классическая',
        'oversize': 'Оверсайз',
        'choose': 'Выбрать футболку',
        'choose_hoodie': 'Выбрать худи',
        'other_choice': 'Выбрать другой принт',
        'terms': 'Одна футболка по специальной цене к одному худи. Условия 225 — отдельно.',
        'automatic': 'Выгода применяется автоматически в корзине.',
        'terms_more': 'На вещи в комплекте промокоды не добавляются. Размер футболки выбираешь отдельно.',
        'currency': 'грн',
        'from_': 'от',
        'saving': 'Выгода комплекта',
        'included': 'Выгода комплекта уже учтена',
        'remaining': 'К этому худи можно добавить футболку выгоднее.',
        'cart_title': 'Дополни худи футболкой',
        'size': 'Размер футболки',
        'fit': 'Посадка',
        'color': 'Цвет',
        'select_size': 'Выбери размер футболки',
        'add_pair': 'Добавить комплект',
        'add_tee': 'Добавить футболку',
        'total': 'Вместе',
        'close': 'Закрыть',
        'back': 'К выбору размера',
        'loading': 'Подбираем футболки…',
        'error': 'Не удалось загрузить. Попробуй ещё раз.',
        'retry': 'Попробовать ещё раз',
        'added': 'Добавлено в корзину',
        'submitting': 'Добавляем…',
        'hoodie': 'Худи',
        'tee': 'Футболка',
        'checkout': 'Перейти к оформлению',
        'recommend': 'К худи — футболка по специальной цене',
        'recommend_sub': 'Тот же принт — от 850 грн. Другой — от 900 грн.',
        'view': 'Посмотреть варианты',
        'pair_note': 'Добавим одно худи и одну футболку. Параметры худи — как выбрано на странице.',
        'tee_note': 'Добавим только футболку к худи в твоей корзине.',
        'rules_title': 'Как работает комплект',
        'empty': 'Сейчас нет доступных футболок для этого комплекта.'},
 'en': {'eyebrow': 'BETTER TOGETHER',
        'title': 'A tee for your hoodie',
        'intro': 'Matching print. A better price together.',
        'other_intro': 'A warm layer and your favourite tee, for less together.',
        'same': 'Matching print',
        'other': 'Another print',
        'classic': 'Classic',
        'oversize': 'Oversized',
        'choose': 'Choose a tee',
        'choose_hoodie': 'Choose a hoodie',
        'other_choice': 'Choose another print',
        'terms': 'One specially priced tee per hoodie. The 225 offer is separate.',
        'automatic': 'Savings apply automatically in your cart.',
        'terms_more': 'Promo codes do not stack on bundle items. Choose the tee size separately.',
        'currency': 'UAH',
        'from_': 'from',
        'saving': 'Bundle savings',
        'included': 'Your bundle savings are included',
        'remaining': 'Add a specially priced tee to this hoodie.',
        'cart_title': 'Complete your hoodie with a tee',
        'size': 'Tee size',
        'fit': 'Fit',
        'color': 'Colour',
        'select_size': 'Choose a tee size',
        'add_pair': 'Add the bundle',
        'add_tee': 'Add the tee',
        'total': 'Together',
        'close': 'Close',
        'back': 'Back to sizes',
        'loading': 'Finding your tees…',
        'error': 'Could not load. Please try again.',
        'retry': 'Try again',
        'added': 'Added to cart',
        'submitting': 'Adding…',
        'hoodie': 'Hoodie',
        'tee': 'T-shirt',
        'checkout': 'Go to checkout',
        'recommend': 'Add a specially priced tee to your hoodie',
        'recommend_sub': 'Matching print from UAH 850. Another print from UAH 900.',
        'view': 'Explore options',
        'pair_note': 'We’ll add one hoodie and one tee. Hoodie options match your selections on the page.',
        'tee_note': 'We’ll add only the tee to the hoodie in your cart.',
        'rules_title': 'How the bundle works',
        'empty': 'No tees are available for this bundle right now.'}}

def bundle_copy(language=None):
    return COPY.get((language or get_language() or "uk").split("-")[0], COPY["uk"])

COPY['uk']['select_hoodie'] = 'Спочатку обери доступний розмір худі на сторінці товару.'
COPY['ru']['select_hoodie'] = 'Сначала выбери доступный размер худи на странице товара.'
COPY['en']['select_hoodie'] = 'First, choose an available hoodie size on the product page.'

COPY['uk'].update(invalid_bundle='Некоректний комплект', select_one='Обери одну річ для комплекту', excluded='Ця річ не бере участі в комплекті', refresh_selection='Онови вибір комплекту', add_hoodie='Додай худі для цього комплекту', offer_unavailable='Цей комплект зараз недоступний. Онови вибір.', adding='Комплект уже додається. Онови кошик.', line_limit='Забагато речей в одній позиції')
COPY['ru'].update(invalid_bundle='Некорректный комплект', select_one='Выбери одну вещь для комплекта', excluded='Эта вещь не участвует в комплекте', refresh_selection='Обнови выбор комплекта', add_hoodie='Добавь худи для этого комплекта', offer_unavailable='Этот комплект сейчас недоступен. Обнови выбор.', adding='Комплект уже добавляется. Обнови корзину.', line_limit='Слишком много вещей в одной позиции')
COPY['en'].update(invalid_bundle='Invalid bundle', select_one='Choose one item for the bundle', excluded='This item is not included in the bundle offer', refresh_selection='Refresh your bundle selection', add_hoodie='Add a hoodie for this bundle', offer_unavailable='This bundle is currently unavailable. Refresh your selection.', adding='The bundle is being added. Refresh your cart.', line_limit='Too many units of one item')


def garment_bundle_text(key, language=None):
    return bundle_copy(language)[key]

# Pair savings are calculated by the quote, never hard-coded in customer copy.
COPY['uk'].update({'hoodie_discount':'На худі в комплекті', 'tee_in_pair':'Футболка в комплекті', 'cart_extra':'До суми кошика', 'pair_saving':'Вигода на двох речах', 'pair_note':'Додамо худі з обраними на сторінці параметрами та футболку. На обидві речі діє вигода комплекту.', 'tee_note':'Додамо футболку та перерахуємо ціну худі в кошику.', 'terms_more':'Промокоди на речі в комплекті не додаються. Розмір футболки обираєш окремо.'})
COPY['ru'].update({'hoodie_discount':'На худи в комплекте', 'tee_in_pair':'Футболка в комплекте', 'cart_extra':'К сумме корзины', 'pair_saving':'Выгода на двух вещах', 'pair_note':'Добавим худи с выбранными на странице параметрами и футболку. На обе вещи действует выгода комплекта.', 'tee_note':'Добавим футболку и пересчитаем цену худи в корзине.', 'terms_more':'Промокоды на вещи в комплекте не добавляются. Размер футболки выбираешь отдельно.'})
COPY['en'].update({'hoodie_discount':'Off the hoodie in this set', 'tee_in_pair':'Tee price in the set', 'cart_extra':'Added to your cart total', 'pair_saving':'Savings on both items', 'pair_note':'Adds the hoodie with your selected options and a tee. Bundle savings apply to both items.', 'tee_note':'Adds the tee and reduces the price of the hoodie already in your cart.', 'terms_more':'Promo codes do not stack on bundled items. Choose the tee size separately.'})

COPY['uk'].update({'goods_saving':'Економія на речах', 'shipping_free':'Доставка безкоштовна', 'shipping_paid':'Доставка — за тарифами перевізника.', 'shipping_after_discount':'За сумою після знижок', 'payable':'До сплати', 'pending_estimate':'Очікує погодження', 'promo_included':'Промокод враховано', 'shipping_separate':'Доставка безкоштовна від 3000 грн після знижок; до цієї суми — за тарифами перевізника.'})
COPY['ru'].update({'goods_saving':'Экономия на вещах', 'shipping_free':'Доставка бесплатная', 'shipping_paid':'Доставка — по тарифам перевозчика.', 'shipping_after_discount':'По сумме после скидок', 'payable':'К оплате', 'pending_estimate':'Ожидает согласования', 'promo_included':'Промокод учтён', 'shipping_separate':'Доставка бесплатная от 3000 грн после скидок; до этой суммы — по тарифам перевозчика.'})
COPY['en'].update({'goods_saving':'Savings on the items', 'shipping_free':'Free shipping', 'shipping_paid':'Shipping at the carrier’s rates.', 'shipping_after_discount':'Based on the total after discounts', 'payable':'Payable total', 'pending_estimate':'Awaiting approval', 'promo_included':'Promo code applied', 'shipping_separate':'Free shipping from 3000 UAH after discounts; below this amount, carrier rates apply.'})

COPY['uk'].update({'brigade_title':'Комплект для 225 ОШП', 'brigade_eyebrow':'225 ОШП · КОМАНДА СІРКО', 'brigade_kind':'Футболка 225 ОШП', 'brigade_terms':'Дві футболки, два худі або худі + футболка 225 — вигідніше разом. Замовлення з цією серією — за повною оплатою.', 'size_guide':'Розмірна сітка', 'pair_note':'Худі з обраними параметрами + футболка.', 'select_hoodie':'Обери розмір худі, щоб зібрати комплект.'})
COPY['ru'].update({'brigade_title':'Комплект для 225 ОШП', 'brigade_eyebrow':'225 ОШП · КОМАНДА СІРКО', 'brigade_kind':'Футболка 225 ОШП', 'brigade_terms':'Две футболки, два худи или худи + футболка 225 — выгоднее вместе. Заказы с этой серией — по полной оплате.', 'size_guide':'Размерная сетка', 'pair_note':'Худи с выбранными параметрами + футболка.', 'select_hoodie':'Выбери размер худи, чтобы собрать комплект.'})
COPY['en'].update({'brigade_title':'A set for the 225th', 'brigade_eyebrow':'225 REGIMENT · SIRKO TEAM', 'brigade_kind':'225th Regiment tee', 'brigade_terms':'Two tees, two hoodies, or a hoodie + tee from the 225th collection qualify. Orders with this collection require full payment.', 'size_guide':'Size guide', 'pair_note':'Your selected hoodie + a tee.', 'select_hoodie':'Choose a hoodie size to build your set.'})
