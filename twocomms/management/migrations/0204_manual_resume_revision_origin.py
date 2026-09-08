from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0203_ig_deferred_echo")]

    operations = [
        migrations.AlterField(
            model_name="igcustomerturnrevision",
            name="origin",
            field=models.CharField(
                choices=[("inbound", "Вхідний хід"), ("auto_refresh", "Автоматичне оновлення"), ("manual_resume", "Ручне повернення боту")],
                db_default="inbound", db_index=True, default="inbound", max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="igcustomerturnrevision",
            name="successor_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Не наступник"),
                    ("publication_changed", "Публікація змінилася"),
                    ("public_policy_inputs_stale", "Публічні правила змінилися"),
                    ("fact_binding_stale", "Факти змінилися"),
                    ("manual_resume", "Авторизоване ручне повернення"),
                ],
                db_default="", default="", max_length=32,
            ),
        ),
    ]
