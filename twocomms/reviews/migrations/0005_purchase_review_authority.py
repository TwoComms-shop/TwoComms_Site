"""Private exact-purchase review proof and append-only reward component."""
import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def ensure_review_authority_innodb(apps, schema_editor):
    if schema_editor.connection.vendor not in {"mysql", "mariadb"}:
        return
    tables = ("reviews_review", "reviews_reviewpurchaseinvitation", "reviews_reviewrewarduplift")
    with schema_editor.connection.cursor() as cursor:
        for table in tables:
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Required review authority table is missing")
            if str(row[0]).lower() == "innodb":
                continue
            # Production Review is currently empty. A nonempty legacy MyISAM
            # table requires an operator-reviewed conversion, not silent DDL.
            if table == "reviews_review":
                from reviews.write_freeze import review_write_freeze_verified
                if not review_write_freeze_verified():
                    raise RuntimeError("Review engine conversion requires verified canonical review write freeze")
                cursor.execute(f"SELECT COUNT(*) FROM {schema_editor.quote_name(table)}")
                if cursor.fetchone()[0]:
                    raise RuntimeError("Nonempty legacy Review engine conversion needs review")
            schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("reviews", "0004_reviewcampaign_reviewsubmissionwindow_and_more"),
        ("management", "0226_align_system_prompt_default"),
        ("orders", "0059_order_delivery_and_dtf_film"),
    ]
    operations = [
        migrations.CreateModel(
            name="ReviewPurchaseInvitation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("client_id_snapshot", models.PositiveBigIntegerField()),
                ("assignment_id", models.PositiveBigIntegerField()),
                ("assignment_version", models.PositiveIntegerField()),
                ("reset_audit_id", models.PositiveBigIntegerField(default=0)),
                ("identity_digest", models.CharField(max_length=64)),
                ("signing_key_id", models.CharField(max_length=24)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
                ("client", models.ForeignKey(blank=True, null=True, db_constraint=False,
                    on_delete=django.db.models.deletion.SET_NULL, related_name="review_invitations", to="management.igclient")),
                ("order", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT, to="orders.order")),
                ("order_item", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT, to="orders.orderitem")),
                ("product", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT, to="storefront.product")),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("client_id_snapshot", "order_item", "assignment_version", "reset_audit_id"), name="review_invite_source_once")]},
        ),
        migrations.AddField(model_name="review", name="purchase_invitation",
            field=models.OneToOneField(blank=True, null=True, editable=False, db_constraint=False,
                on_delete=django.db.models.deletion.PROTECT, related_name="review", to="reviews.reviewpurchaseinvitation")),
        migrations.AddField(model_name="review", name="purchase_proof_version", field=models.PositiveSmallIntegerField(default=0, editable=False)),
        migrations.AddField(model_name="review", name="purchase_content_digest", field=models.CharField(blank=True, editable=False, max_length=64)),
        migrations.AddField(model_name="review", name="purchase_proof_signature", field=models.CharField(blank=True, editable=False, max_length=64)),
        migrations.CreateModel(
            name="ReviewRewardUplift",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("added_percent", models.PositiveSmallIntegerField(default=5)),
                ("proof_snapshot", models.JSONField(default=dict)),
                ("proof_digest", models.CharField(max_length=64)),
                ("signing_key_id", models.CharField(max_length=24)),
                ("proof_signature", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
                ("review", models.OneToOneField(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="reward_uplift", to="reviews.review")),
                ("reward", models.OneToOneField(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="review_uplift", to="management.igugcreward")),
            ],
            options={"constraints": [models.CheckConstraint(condition=models.Q(added_percent=5), name="review_uplift_five_only")]},
        ),
        migrations.RunPython(ensure_review_authority_innodb, migrations.RunPython.noop),
    ]
