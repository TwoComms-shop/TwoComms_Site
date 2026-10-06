import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0221_gemini_attempt_dispatch_manifest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="HumanReplyPart",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation_id_snapshot", models.UUIDField()),
                ("actor_id_snapshot", models.PositiveBigIntegerField()),
                ("context_message_id_snapshot", models.PositiveBigIntegerField()),
                ("permission_epoch", models.PositiveBigIntegerField()),
                ("recipient_igsid", models.CharField(max_length=64)),
                ("provider_namespace", models.CharField(max_length=128)),
                ("ordinal", models.PositiveSmallIntegerField()),
                ("part_count", models.PositiveSmallIntegerField()),
                ("plan_version", models.CharField(default="human-delivery-plan.v1", max_length=40)),
                ("plan_digest", models.CharField(max_length=64)),
                ("payload", models.JSONField(default=dict)),
                ("payload_digest", models.CharField(max_length=64)),
                ("state", models.CharField(choices=[("planned", "Planned"), ("claimed", "Claimed"), ("provider_started", "Provider started"), ("sent", "Sent"), ("definite_failed", "Definite failure"), ("unknown", "Unknown"), ("cancelled", "Cancelled")], db_index=True, default="planned", max_length=16)),
                ("claim_token", models.CharField(blank=True, default="", max_length=64)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("lease_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("provider_started_at", models.DateTimeField(blank=True, null=True)),
                ("provider_message_id", models.CharField(blank=True, default=None, max_length=255, null=True)),
                ("response_digest", models.CharField(blank=True, default="", max_length=64)),
                ("terminal_at", models.DateTimeField(blank=True, null=True)),
                ("failure_code", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="human_reply_parts", to="management.igclient")),
                ("command", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="delivery_parts", to="management.humanreplycommand")),
            ],
            options={
                "ordering": ["command_id", "ordinal"],
                "indexes": [models.Index(fields=["state", "lease_until", "id"], name="ig_human_part_due")],
                "constraints": [
                    models.UniqueConstraint(fields=("command", "ordinal"), name="ig_human_part_ordinal"),
                    models.UniqueConstraint(fields=("provider_namespace", "provider_message_id"), name="ig_human_part_receipt"),
                    models.CheckConstraint(condition=models.Q(part_count__gte=1), name="ig_human_part_count"),
                    models.CheckConstraint(condition=models.Q(ordinal__lt=models.F("part_count")), name="ig_human_part_bounds"),
                ],
            },
        ),
        migrations.CreateModel(
            name="HumanReplyPrivateDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("kind", models.CharField(choices=[("reply_draft", "Reply draft"), ("internal_note", "Internal note")], max_length=16)),
                ("recipient_igsid", models.CharField(max_length=64)),
                ("provider_namespace", models.CharField(max_length=128)),
                ("context_digest", models.CharField(max_length=64)),
                ("reset_floor", models.PositiveBigIntegerField(default=1)),
                ("source_permission_epoch", models.PositiveBigIntegerField(default=0)),
                ("text", models.TextField()),
                ("text_hash", models.CharField(max_length=64)),
                ("version", models.PositiveBigIntegerField(default=1)),
                ("state", models.CharField(choices=[("open", "Open"), ("consumed", "Consumed"), ("archived", "Archived")], default="open", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("actor", models.ForeignKey(db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ig_human_private_documents", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="human_private_documents", to="management.igclient")),
                ("consumed_command", models.OneToOneField(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="private_document", to="management.humanreplycommand")),
                ("context_message", models.ForeignKey(db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="human_private_documents", to="management.instagrambotmessage")),
            ],
            options={
                "ordering": ["-updated_at", "-id"],
                "indexes": [models.Index(fields=["client", "actor", "kind", "state"], name="ig_human_doc_scope")],
                "constraints": [models.CheckConstraint(condition=models.Q(version__gte=1), name="ig_human_doc_version")],
            },
        ),
    ]
