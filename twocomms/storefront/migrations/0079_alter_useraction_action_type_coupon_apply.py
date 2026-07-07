# W1-4д (TECH-023): add 'coupon_apply' to UserAction.ACTION_TYPES choices.
# Choices-only change — no database schema alteration on most backends.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('storefront', '0078_qrdevicegrant'),
    ]

    operations = [
        migrations.AlterField(
            model_name='useraction',
            name='action_type',
            field=models.CharField(choices=[('page_view', 'Перегляд сторінки'), ('product_view', 'Перегляд товару'), ('add_to_cart', 'Додано в кошик'), ('remove_from_cart', 'Видалено з кошика'), ('initiate_checkout', 'Початок оформлення'), ('lead', 'Лід (передплата)'), ('purchase', 'Покупка'), ('search', 'Пошук'), ('coupon_apply', 'Застосування промокоду'), ('custom_print_start', 'Кастомний принт: старт'), ('custom_print_step_enter', 'Кастомний принт: вхід у крок'), ('custom_print_step_complete', 'Кастомний принт: завершення кроку'), ('custom_print_add_to_cart', 'Кастомний принт: додано в кошик'), ('custom_print_send_to_manager', 'Кастомний принт: відправлено менеджеру'), ('custom_print_safe_exit', 'Кастомний принт: safe exit'), ('custom_print_moderation_result', 'Кастомний принт: результат модерації'), ('survey_start', 'Опитування: старт'), ('survey_answer', 'Опитування: відповідь'), ('survey_back', 'Опитування: назад'), ('survey_skip', 'Опитування: пропуск'), ('survey_close', 'Опитування: закриття'), ('survey_complete', 'Опитування: завершення'), ('survey_dismiss', 'Опитування: приховано з головної'), ('click', 'Клік'), ('scroll', 'Прокрутка'), ('time_on_page', 'Час на сторінці')], db_index=True, max_length=50, verbose_name='Тип дії'),
        ),
    ]
