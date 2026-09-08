from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0205_revision_request_identity")]
    operations = [
        migrations.AddField(model_name="igcustomerturnrevision", name="recovery_state", field=models.CharField(blank=True, db_default="", default="", max_length=16)),
        migrations.AddField(model_name="igcustomerturnrevision", name="recovery_due_at", field=models.DateTimeField(blank=True, db_default=None, db_index=True, null=True)),
        migrations.AddField(model_name="igcustomerturnrevision", name="recovery_code", field=models.CharField(blank=True, db_default="", default="", max_length=64)),
        migrations.AlterField(
            model_name="igcustomerturnrevision", name="origin",
            field=models.CharField(
                choices=[("inbound", "Вхідний хід"), ("auto_refresh", "Автоматичне оновлення"), ("manual_resume", "Ручне повернення боту"), ("outage_recovery", "Відновлення після збою")],
                db_default="inbound", db_index=True, default="inbound", max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="igcustomerturnrevision", name="successor_reason",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=32,
                choices=[("", "Не наступник"), ("publication_changed", "Публікація змінилася"), ("public_policy_inputs_stale", "Публічні правила змінилися"), ("fact_binding_stale", "Факти змінилися"), ("manual_resume", "Авторизоване ручне повернення"), ("outage_recovery", "Відновлення відповіді після збою")],
            ),
        ),
    ]
