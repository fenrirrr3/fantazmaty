"""Pierwsza edycja: Mikołajki 2027 (koniec zapisów do zmiany w panelu admina)."""
import datetime
from zoneinfo import ZoneInfo

from django.db import migrations


def create(apps, schema_editor):
    Edition = apps.get_model('mikolajki', 'Edition')
    deadline = datetime.datetime(2027, 11, 30, 23, 59, tzinfo=ZoneInfo('Europe/Warsaw'))
    Edition.objects.using(schema_editor.connection.alias).get_or_create(year=2027, defaults={'signup_deadline': deadline})


class Migration(migrations.Migration):

    dependencies = [
        ('mikolajki', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
