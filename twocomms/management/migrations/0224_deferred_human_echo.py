import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0223_selection_correction_operation")]

    operations = [
        migrations.AddField(
            model_name="igdeferredecho", name="competing_human_part_ids",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="igdeferredecho", name="matched_human_part",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="deferred_echoes", to="management.humanreplypart"),
        ),
        migrations.AlterField(
            model_name="igdeferredecho", name="state",
            field=models.CharField(default="waiting_receipt", max_length=20, choices=[
                ("waiting_receipt", "Очікує квитанцію"), ("ambiguous", "Потрібна звірка"),
                ("own", "Підтверджене власне повідомлення"),
                ("manager_pending", "Очікує запису менеджера"),
                ("manager_applied", "Менеджера зафіксовано"),
                ("human_pending", "Очікує квитанцію ручної відповіді"),
                ("human_applied", "Ручну відповідь зафіксовано"),
            ]),
        ),
    ]
