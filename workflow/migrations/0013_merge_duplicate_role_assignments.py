"""Keep the migration history while retiring automatic assignment merging.

Already applied databases remain valid; fresh databases preserve all assignments.
Removing this migration would break dependencies and recorded migration history.
Previously merged data cannot be reconstructed by reversing this marker.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('workflow', '0012_restore_retired_team_assignments')]
    operations = []
