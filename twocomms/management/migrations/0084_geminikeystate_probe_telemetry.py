from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0083_igpollcursor")]

    operations = [
        migrations.AddField(model_name="geminikeystate", name="last_probe_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="geminikeystate", name="last_probe_status", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="geminikeystate", name="last_probe_model", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="geminikeystate", name="last_probe_latency_ms", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="geminikeystate", name="last_probe_finish_reason", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="geminikeystate", name="last_probe_http_code", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="geminikeystate", name="last_probe_error", field=models.CharField(blank=True, max_length=120)),
    ]
