import django.db.models.deletion
from django.db import migrations, models


def capture_base_snapshots(apps, schema_editor):
    Delivery = apps.get_model("management", "IgUgcRewardDelivery")
    alias = schema_editor.connection.alias
    for row in Delivery.objects.using(alias).select_related("reward__promo_code").iterator():
        if row.reward.discount_percent not in (5, 10):
            raise RuntimeError("Unsupported historical UGC base percentage")
        row.generation = 1
        row.discount_percent_snapshot = row.reward.discount_percent
        row.promo_code_snapshot = row.reward.promo_code.code
        row.valid_until_snapshot = row.reward.promo_code.valid_until
        row.save(update_fields=["generation", "discount_percent_snapshot", "promo_code_snapshot", "valid_until_snapshot"])


class Migration(migrations.Migration):
    dependencies = [("management", "0226_align_system_prompt_default")]
    operations = [
        migrations.AlterField(
            model_name="igugcrewarddelivery", name="reward",
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="deliveries", to="management.igugcreward"),
        ),
        migrations.AddField(model_name="igugcrewarddelivery", name="generation", field=models.PositiveSmallIntegerField(default=1)),
        migrations.AddField(model_name="igugcrewarddelivery", name="discount_percent_snapshot", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="igugcrewarddelivery", name="promo_code_snapshot", field=models.CharField(blank=True, default="", max_length=20)),
        migrations.AddField(model_name="igugcrewarddelivery", name="valid_until_snapshot", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="igugcrewarddelivery", name="invitation_snapshot", field=models.JSONField(blank=True, default=dict)),
        migrations.RunPython(capture_base_snapshots, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="igugcrewarddelivery", constraint=models.UniqueConstraint(fields=("reward", "generation"), name="ig_ugc_delivery_generation")),
        migrations.AddConstraint(model_name="igugcrewarddelivery", constraint=models.CheckConstraint(condition=models.Q(generation__in=(1, 2)), name="ig_ugc_delivery_generation_range")),
        migrations.AddConstraint(model_name="igugcrewarddelivery", constraint=models.CheckConstraint(condition=models.Q(discount_percent_snapshot__in=(5, 10, 15)), name="ig_ugc_delivery_percent_range")),
    ]
