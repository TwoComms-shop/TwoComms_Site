from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("storefront", "0099_publish_veteran_fund_stage_three_report")]
    operations = [migrations.AddField(
        model_name="customprintlead", name="telegram_delivery_json",
        field=models.JSONField(blank=True, default=dict, verbose_name="Результати доставки Telegram"),
    ), migrations.AlterField(
        model_name="customprintleadattachment", name="attachment_role",
        field=models.CharField(choices=[("design", "Макет / дизайн"), ("reference", "Референс"), ("gift_reference", "Зображення для коробки")], default="design", max_length=20, verbose_name="Роль файлу"),
    )]
