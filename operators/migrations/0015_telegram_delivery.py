from django.db import migrations, models


def create_mutex(apps, schema_editor):
    apps.get_model('operators', 'TelegramPublicationMutex').objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [('operators', '0014_telegramcustomerrequest_unread_messages_count')]
    operations = [
        migrations.CreateModel(name='TelegramPublicationMutex', fields=[('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'))]),
        migrations.CreateModel(name='TelegramIncomingUpdate', fields=[('update_id', models.BigIntegerField(primary_key=True, serialize=False)), ('processed_at', models.DateTimeField(blank=True, null=True))]),
        migrations.RunPython(create_mutex, migrations.RunPython.noop),
    ]
