from django.db import migrations, models

import accounts.models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0028_normalize_userprofile_pay_type')]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='ubd_doc',
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to=accounts.models.private_ubd_document_path,
                verbose_name='Фото посвідчення УБД',
            ),
        ),
    ]
