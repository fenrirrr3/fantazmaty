"""Dane ilustratorów są w spisie ilustratorów (illustrations 0005 i 0009); pola przy osobach zespołu znikają."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('people', '0015_retire_generic_coordinator'),
        ('illustrations', '0009_illustrators_into_directory'),
    ]

    operations = [
        migrations.RemoveField(model_name='person', name=name)
        for name in ('illustrator_active', 'illustrator_portfolio', 'illustrator_preferences', 'illustrator_covers')
    ]
