"""Small, explicit three-language copy contract for brigade commerce."""
from django.utils.translation import get_language


COPY = {
    'uk': {
        'eyebrow': '225 ОШП · КОМАНДА СІРКО', 'title': 'Дві речі. Один код.',
        'set_price': 'Комплект', 'separate': 'Окремо',
        'intro': 'Поєднай футболку та худі 225 — збережи 225 грн на комплекті.',
        'saving': 'Вигода комплекту', 'currency': 'грн',
        'tee': 'Футболка', 'hoodie': 'Худі', 'pair': 'У комплекті',
        'choose_tee': 'Обрати футболку', 'choose_hoodie': 'Обрати худі',
        'rules': 'Також: від 2 футболок — −80 грн на кожну; від 2 худі — −145 грн на кожне.',
        'automatic': 'Автоматично в кошику. Розміри й кольори можна поєднувати.',
        'no_stack': 'Умови серії 225 не поєднуються з промокодами. Інші речі в кошику можуть брати участь у промо.',
        'card': '225 · вигідніше комплектом',
        'full_payment': 'Речі із серії «Бригади» оформлюємо за повною оплатою. Якщо така річ є в кошику, ця умова діє на все замовлення.',
        'payment_title': 'Для серії «Бригади» — повна оплата',
        'payment_short': 'Ця умова діє на все замовлення з річчю із серії «Бригади».',
        'cart_title': 'Збери свій комплект 225',
        'applied': 'Вигоду 225 вже враховано',
        'add_tee': 'Додай футболку 225: −80 грн на футболку та −145 грн на худі в парі.',
        'add_hoodie': 'Додай худі 225: −145 грн на худі та −80 грн на футболку в парі.',
        'discounted_units': 'За умовами 225', 'regular_units': 'Звичайна ціна',
        'from': 'від',
    },
    'ru': {
        'eyebrow': '225 ОШП · КОМАНДА СІРКО', 'title': 'Две вещи. Один код.',
        'set_price': 'Комплект', 'separate': 'По отдельности',
        'intro': 'Соедини футболку и худи 225 — сэкономь 225 грн на комплекте.',
        'saving': 'Выгода комплекта', 'currency': 'грн',
        'tee': 'Футболка', 'hoodie': 'Худи', 'pair': 'В комплекте',
        'choose_tee': 'Выбрать футболку', 'choose_hoodie': 'Выбрать худи',
        'rules': 'Также: от 2 футболок — −80 грн на каждую; от 2 худи — −145 грн на каждое.',
        'automatic': 'Автоматически в корзине. Размеры и цвета можно сочетать.',
        'no_stack': 'Условия серии 225 не суммируются с промокодами. Другие вещи в корзине могут участвовать в промо.',
        'card': '225 · выгоднее комплектом',
        'full_payment': 'Вещи из серии «Бригады» оформляем по полной оплате. Если такая вещь есть в корзине, это условие действует на весь заказ.',
        'payment_title': 'Для серии «Бригады» — полная оплата',
        'payment_short': 'Это условие действует на весь заказ с вещью из серии «Бригады».',
        'cart_title': 'Собери свой комплект 225',
        'applied': 'Выгода 225 уже учтена',
        'add_tee': 'Добавь футболку 225: −80 грн на футболку и −145 грн на худи в паре.',
        'add_hoodie': 'Добавь худи 225: −145 грн на худи и −80 грн на футболку в паре.',
        'discounted_units': 'По условиям 225', 'regular_units': 'Обычная цена',
        'from': 'от',
    },
    'en': {
        'eyebrow': '225 REGIMENT · SIRKO TEAM', 'title': 'Two pieces. One code.',
        'set_price': 'The set', 'separate': 'Separately',
        'intro': 'Pair a 225 tee with a 225 hoodie and save UAH 225 on the set.',
        'saving': 'Set savings', 'currency': 'UAH',
        'tee': 'T-shirt', 'hoodie': 'Hoodie', 'pair': 'In a set',
        'choose_tee': 'Choose a T-shirt', 'choose_hoodie': 'Choose a hoodie',
        'rules': 'Also: buy 2+ tees and save UAH 80 each; buy 2+ hoodies and save UAH 145 each.',
        'automatic': 'Applied automatically in your cart. Mix sizes and colours.',
        'no_stack': '225 series offers cannot be combined with promo codes. Other items in your cart may qualify for a promo.',
        'card': '225 · save with a set',
        'full_payment': 'Items in the Brigades series require full payment. When your cart contains one, this applies to the entire order.',
        'payment_title': 'Brigades series: full payment',
        'payment_short': 'This applies to the entire order containing an item from the Brigades series.',
        'cart_title': 'Build your 225 set',
        'applied': 'Your 225 savings are included',
        'add_tee': 'Add a 225 tee: save UAH 80 on the tee and UAH 145 on the paired hoodie.',
        'add_hoodie': 'Add a 225 hoodie: save UAH 145 on the hoodie and UAH 80 on the paired tee.',
        'discounted_units': '225 offer', 'regular_units': 'Regular price',
        'from': 'from',
    },
}


def brigade_copy(language=None):
    code = (language or get_language() or 'uk').split('-')[0]
    return COPY.get(code, COPY['uk'])


def brigade_text(key, language=None):
    return brigade_copy(language)[key]
