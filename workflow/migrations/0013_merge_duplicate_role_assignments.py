from django.db import migrations


def merge(apps, schema_editor):
    from workflow.assignment_merge import merge_duplicates
    report = merge_duplicates(apps, schema_editor.connection.alias)
    print(f"\nScalenie ról: usunięto {report['merged']} powtórzonych przydziałów; "
          f"scalono {len(report['groups'])} ról.")
    for text_id, role, reason in report['skipped']:
        print(f'Bez zmian: tekst {text_id}, rola {role}: {reason}')


class Migration(migrations.Migration):
    dependencies = [('workflow', '0012_restore_retired_team_assignments')]
    operations = [migrations.RunPython(merge, migrations.RunPython.noop, atomic=True)]
