from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0029_private_ubd_document_names"),
    ]

    operations = [
        migrations.AlterField(
            model_name="telegramverificationsession",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("custom_print", "Контакт у формі кастомного принта"),
                    ("profile_link", "Привʼязка профілю"),
                    ("login", "Вхід через Telegram"),
                    ("management_bind", "Привʼязка менеджмент-бота"),
                    ("dropshipper_link", "Привʼязка дропшипера"),
                    ("restock", "Очікування наявності товару"),
                ],
                default="custom_print",
                max_length=32,
                verbose_name="Призначення",
            ),
        ),
    ]
