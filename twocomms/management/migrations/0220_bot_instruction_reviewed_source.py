from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0219_ig_memory_producer")]

    operations = [
        migrations.AddField(
            model_name="botinstruction",
            name="reviewed_source",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
    ]
