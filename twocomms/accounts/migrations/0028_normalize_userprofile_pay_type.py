from django.db import migrations, models


def normalize_profile_pay_types(apps, schema_editor):
    UserProfile = apps.get_model('accounts', 'UserProfile')
    UserProfile.objects.filter(pay_type='full').update(pay_type='online_full')
    UserProfile.objects.filter(pay_type='partial').update(pay_type='prepay_200')
    UserProfile.objects.filter(pay_type__in=['cash', 'cash_on_delivery']).update(pay_type='cod')


def reverse_profile_pay_types(apps, schema_editor):
    UserProfile = apps.get_model('accounts', 'UserProfile')
    UserProfile.objects.filter(pay_type='online_full').update(pay_type='full')
    UserProfile.objects.filter(pay_type='prepay_200').update(pay_type='partial')
    UserProfile.objects.filter(pay_type='cod').update(pay_type='full')


class Migration(migrations.Migration):
    dependencies = [('accounts', '0027_userprofile_binotel_internal_number')]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='pay_type',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('online_full', 'Онлайн оплата (повна сума)'),
                    ('prepay_200', 'Передплата 200 грн'),
                    ('cod', 'Оплата при отриманні'),
                ],
                default='online_full',
            ),
        ),
        migrations.RunPython(normalize_profile_pay_types, reverse_profile_pay_types),
    ]
