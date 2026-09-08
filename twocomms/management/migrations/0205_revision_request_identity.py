from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0204_manual_resume_revision_origin")]

    operations = [
        migrations.AddField(
            model_name="geminirequest",
            name="source_execution_key",
            field=models.CharField(max_length=64, blank=True, default="", db_default=""),
        ),
        # Install the replacement invariant before removing the legacy mutex.
        migrations.AddConstraint(
            model_name="geminirequest",
            constraint=models.UniqueConstraint(
                fields=("source_message_id", "lane", "source_execution_key"),
                name="gem_req_source_exec_uniq",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="geminirequest", name="gem_req_source_lane_uniq",
        ),
    ]
