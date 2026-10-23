from django.shortcuts import render, redirect
from django.http import JsonResponse, Http404
from django.contrib.auth.decorators import user_passes_test
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


def staff_required(view_func):
    return user_passes_test(lambda u: u.is_staff)(view_func)

# ==================== OFFLINE STORES STUBS ====================


@staff_required
def admin_offline_stores(request): return render(request, 'admin/stub.html')
@staff_required
def admin_offline_store_create(request): return redirect('admin_offline_stores')
@staff_required
def admin_offline_store_edit(request, pk): return redirect('admin_offline_stores')
@staff_required
def admin_offline_store_toggle(request, pk): return redirect('admin_offline_stores')
@staff_required
def admin_offline_store_delete(request, pk): return redirect('admin_offline_stores')

# Store Management


@staff_required
def admin_store_management(request, store_id): return render(request, 'admin/stub.html')
@staff_required
def admin_store_add_product_to_order(request, store_id): return JsonResponse({'status': 'error'})
@staff_required
def admin_store_get_order_items(request, store_id, order_id): return JsonResponse({'items': []})
@staff_required
def admin_store_get_product_colors(request, store_id, product_id): return JsonResponse({'colors': []})
@staff_required
def admin_store_remove_product_from_order(request, store_id, order_id, item_id): return JsonResponse({'status': 'ok'})
@staff_required
def admin_store_add_products_to_store(request, store_id): return redirect('admin_store_management', store_id=store_id)
@staff_required
def admin_store_generate_invoice(request, store_id): return redirect('admin_store_management', store_id=store_id)
@staff_required
def admin_store_update_product(request, store_id, product_id): return JsonResponse({'status': 'ok'})
@staff_required
def admin_store_mark_product_sold(request, store_id, product_id): return JsonResponse({'status': 'ok'})
@staff_required
def admin_store_remove_product(request, store_id, product_id): return JsonResponse({'status': 'ok'})

# ==================== PRINT PROPOSALS STUBS ====================


@staff_required
def admin_print_proposal_update_status(request): return JsonResponse({'status': 'ok'})
@staff_required
def admin_print_proposal_award_points(request): return JsonResponse({'status': 'ok'})
@staff_required
def admin_print_proposal_award_promocode(request): return JsonResponse({'status': 'ok'})

# ==================== API & DEBUG STUBS ====================


@staff_required
def debug_media(request): return JsonResponse({'status': 'ok'})
@staff_required
def debug_media_page(request): return render(request, 'admin/stub.html')
@staff_required
def debug_product_images(request): return JsonResponse({'status': 'ok'})
@staff_required
def dev_grant_admin(request): return redirect('home')

# ==================== STATIC PAGES STUBS ====================

# SEO molecular-upgrade US-4 (2026-05-16) — wholesale FAQ expanded from
# 3 to 8 questions covering pricing tiers, dropshipping, custom merch,
# production timelines, accounting docs, volunteer pricing and customer
# refusals. Mirrors keyword universe from audit/06 (опт, дропшипінг,
# мерч для команд, корпоративний друк) and feeds the FAQPage schema on
# /wholesale/.
WHOLESALE_FAQ_ITEMS = [
    {
        'question': _('Який мінімальний обсяг оптового замовлення у TwoComms?'),
        'answer': _('Оптові закупівлі — партіями по 8, 16 і далі кратно 8. Склад партії та кращі умови погоджуємо з менеджером індивідуально.'),
    },
    {
        'question': _('Як визначається ціна оптової партії?'),
        'answer': _('Для опту діють індивідуально погоджені умови партії. Універсальної відсоткової шкали немає; остаточну ціну підтверджує менеджер до замовлення.'),
    },
    {
        'question': _('Чим відрізняються умови дропшипінгу?'),
        'answer': _('Індивідуальна знижка 10–15% від поточної повної ціни на сайті. Конкретну знижку погоджує менеджер.'),
    },
    {
        'question': _('Коли можна обговорити нові партнерські умови?'),
        'answer': _('Від 8 фактичних продажів на місяць обговорюємо нові умови. Зміни не активуються автоматично.'),
    },
    {
        'question': _('Хто оформлює ТТН?'),
        'answer': _('ТТН може оформити партнер або TwoComms. Для ТТН партнера відділення відправника погоджуємо заздалегідь.'),
    },
    {
        'question': _('Яка оплата потрібна до відправлення?'),
        'answer': _('Якщо відправляє TwoComms, до відправлення потрібна повна оплата на IBAN або передоплата 200 грн. Деталі погоджуємо з менеджером.'),
    },
    {
        'question': _('Чи можна оформити договір або замовити свій принт?'),
        'answer': _('Договір можливий за погодженням із менеджером. Для власного дизайну скористайтеся сторінкою «Кастомний принт»; склад, вартість і строки партії уточнюємо окремо.'),
    },
    {
        'question': _('Як погодити оптове замовлення?'),
        'answer': _('Напишіть у Telegram TwoComms: https://t.me/twocomms. Форма та прайс допомагають підготувати запит; ціни, наявність і відправлення підтверджує менеджер.'),
    },
]

# SEO molecular-upgrade US-4 — cooperation FAQ expanded from 3 to 7
# questions, including specific routes for stores, bloggers, designers,
# media partners and volunteer projects. Anchors B2B keyword cluster.
COOPERATION_FAQ_ITEMS = [
    {
        'question': _('Які формати співпраці доступні у TwoComms?'),
        'answer': _('Дропшипінг, оптові закупівлі партіями по 8, 16 і далі кратно 8, бренд-проєкти та співпраця з авторами контенту. Формат і можливий договір погоджуємо з менеджером.'),
    },
    {
        'question': _('Яка знижка для дропшипінгу?'),
        'answer': _('Індивідуальна знижка 10–15% від поточної повної ціни на сайті. Конкретну знижку погоджує менеджер.'),
    },
    {
        'question': _('Коли можна обговорити нові партнерські умови?'),
        'answer': _('Від 8 фактичних продажів на місяць обговорюємо нові умови. Зміни не активуються автоматично.'),
    },
    {
        'question': _('Куди звертатися для старту співпраці?'),
        'answer': _('Почніть із Telegram TwoComms: https://t.me/twocomms. Опишіть формат співпраці та товари, які вас цікавлять; менеджер погодить актуальні умови.'),
    },
    {
        'question': _('Чи готовий кабінет партнера до повноцінної роботи?'),
        'answer': _('Кабінет партнера працює у режимі TEST з обмеженими можливостями. Реєстрація та показані розрахунки не активують партнерські умови автоматично; замовлення погоджуйте в Telegram.'),
    },
    {
        'question': _('Як погодити оплату й відправлення?'),
        'answer': _('ТТН може оформити партнер або TwoComms; відділення відправника для ТТН партнера погоджуємо заздалегідь. Якщо відправляє TwoComms, до відправлення потрібна повна оплата на IBAN або передоплата 200 грн.'),
    },
    {
        'question': _('Які фото можна використовувати для продажів?'),
        'answer': _('Можна використовувати публічні фото із сайту та Instagram TwoComms. За власний унікальний контент відповідає партнер; додаткові детальні фото погоджуємо для конкретного запиту.'),
    },
]


def add_print(request): return render(request, 'pages/stub.html')
def delivery_view(request): return render(request, 'pages/delivery.html')  # Assuming template exists
# Use real template for cooperation
def cooperation(request):
    return render(
        request,
        'pages/cooperation.html',
        {
            'faq_items': COOPERATION_FAQ_ITEMS,
            'breadcrumb_items': [
                {'name': _('Головна'), 'url': reverse('home')},
                {'name': _('Співпраця з TwoComms'), 'url': reverse('cooperation')},
            ],
        },
    )

# ==================== WHOLESALE STUBS ====================


def pricelist_redirect(request): return redirect('wholesale_page', permanent=True)
def pricelist_page(request): return redirect('wholesale_page', permanent=True)
def test_pricelist(request): return JsonResponse({'status': 'ok'})
def wholesale_page(request):
    return render(
        request,
        'pages/wholesale.html',
        {
            'faq_items': WHOLESALE_FAQ_ITEMS,
            'breadcrumb_items': [
                {'name': _('Головна'), 'url': reverse('home')},
                {'name': _('Оптові закупівлі та дропшипінг'), 'url': reverse('wholesale_page')},
            ],
        },
    )
def wholesale_order_form(request): return render(request, 'pages/wholesale_order_form.html')
def generate_wholesale_invoice(request): return redirect('wholesale_page')
def download_invoice_file(request, invoice_id): raise Http404
def delete_wholesale_invoice(request, invoice_id): return redirect('wholesale_page')
def check_invoice_approval_status(request, invoice_id): return JsonResponse({'status': 'ok'})
def check_payment_status(request, invoice_id): return JsonResponse({'status': 'ok'})
def debug_invoices(request): return JsonResponse({'status': 'ok'})
def create_wholesale_payment(request): return JsonResponse({'status': 'error'})
def wholesale_payment_webhook(request): return JsonResponse({'status': 'ok'})
def get_user_invoices(request): return JsonResponse({'invoices': []})

# ==================== INVOICE ADMIN STUBS ====================


@staff_required
def admin_update_invoice_status(request, invoice_id): return JsonResponse({'status': 'ok'})
@staff_required
def toggle_invoice_approval(request, invoice_id): return JsonResponse({'status': 'ok'})
@staff_required
def toggle_invoice_payment_status(request, invoice_id): return JsonResponse({'status': 'ok'})
@staff_required
def reset_all_invoices_status(request): return redirect('admin_panel')

# ==================== DROPSHIP ADMIN STUBS ====================


@staff_required
def admin_update_dropship_status(request, order_id): return JsonResponse({'status': 'ok'})
@staff_required
def admin_get_dropship_order(request, order_id): return JsonResponse({'order': {}})
@staff_required
def admin_update_dropship_order(request, order_id): return JsonResponse({'status': 'ok'})
@staff_required
def admin_delete_dropship_order(request, order_id): return JsonResponse({'status': 'ok'})

# ==================== MONOBANK QUICK STUBS ====================


def monobank_create_checkout(request): return redirect('cart')
def monobank_return(request): return redirect('home')
