from django.db import migrations, models


def create_mutex(apps, schema_editor):
    apps.get_model('operators', 'CatalogRebuildMutex').objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [('operators', '0015_telegram_delivery')]
    operations = [
        migrations.CreateModel(name='CatalogRebuildMutex', fields=[('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'))]),
        migrations.RunPython(create_mutex, migrations.RunPython.noop),
    ]
