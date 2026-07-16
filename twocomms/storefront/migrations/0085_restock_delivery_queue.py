from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("storefront", "0084_restocksubscription"),
    ]

    operations = [
        migrations.AlterField(
            model_name="restocksubscription",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Очікує підтвердження"),
                    ("active", "Очікує наявності"),
                    ("sending", "Повідомлення надсилається"),
                    ("notified", "Клієнта повідомлено"),
                    ("closed", "Закрито"),
                    ("failed", "Помилка повідомлення"),
                ],
                db_index=True,
                default="active",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="restocksubscription",
            name="delivery_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="restocksubscription",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="restocksubscription",
            name="next_attempt_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="restocksubscription",
            name="notification_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
