from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0099_opt_out_backfill_cursor"),
    ]

    operations = [
        migrations.AlterField(
            model_name="igconversationanalysissnapshot",
            name="interaction_type",
            field=models.CharField(
                choices=[
                    ("unknown", "Невідомо"),
                    ("reaction_only", "Лише реакція"),
                    ("information_only", "Лише інформація"),
                    ("product_interest", "Інтерес до товару"),
                    ("size_fit_question", "Питання про розмір"),
                    ("custom_print", "Кастомний принт"),
                    ("price_objection", "Заперечення щодо ціни"),
                    ("high_intent", "Високий намір"),
                    ("payment_pending", "Очікує оплату"),
                    ("paid_order_waiting", "Оплачено / очікує товар"),
                    ("no_reply", "Не відповідає"),
                    ("explicit_no_buy", "Явно не купує"),
                    ("opt_out", "Відмовився від повідомлень"),
                    ("spam_abuse", "Спам / образи"),
                    ("manager_observation", "Спостереження менеджера"),
                    ("collaboration", "Співпраця / creator"),
                    ("wholesale_b2b", "Опт / B2B"),
                    ("support_complaint", "Підтримка / скарга"),
                    ("community_casual", "Спільнота / casual"),
                ],
                default="unknown",
                max_length=32,
                db_index=True,
            ),
        ),
    ]
