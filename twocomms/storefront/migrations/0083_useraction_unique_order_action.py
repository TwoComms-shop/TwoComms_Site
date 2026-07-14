from django.db import migrations, models
from django.db.models import Count


def validate_order_action_uniqueness(apps, schema_editor):
    """Fail clearly instead of silently deleting ambiguous analytics rows."""
    UserAction = apps.get_model('storefront', 'UserAction')
    duplicate = (
        UserAction.objects.using(schema_editor.connection.alias)
        .filter(order_id__isnull=False)
        .values('action_type', 'order_id')
        .annotate(row_count=Count('id'))
        .filter(row_count__gt=1)
        .order_by('action_type', 'order_id')
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            'Cannot add uniq_user_action_type_order: duplicate '
            f"action_type={duplicate['action_type']!r}, "
            f"order_id={duplicate['order_id']}, rows={duplicate['row_count']}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('storefront', '0082_align_product_seo_titles_with_h1'),
    ]

    operations = [
        migrations.RunPython(
            validate_order_action_uniqueness,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='useraction',
            constraint=models.UniqueConstraint(
                fields=('action_type', 'order_id'),
                name='uniq_user_action_type_order',
            ),
        ),
    ]
