"""MySQL 8: zmiana porównań tekstu z kontrolą konfliktów przed DDL.

Nie zmienia wartości danych. Nie dotyczy binarnych identyfikatorów sesji.
DDL MySQL nie jest transakcyjne; operacja jest wznawialna po błędzie.
"""
from django.db import migrations

COLLATION = 'utf8mb4_0900_as_ci'


def forwards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'mysql':
        return
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute('SHOW COLLATION LIKE %s', [COLLATION])
        if not cursor.fetchone():
            raise RuntimeError('Wymagany MySQL 8 z kolacją utf8mb4_0900_as_ci.')
        tables = set(connection.introspection.table_names(cursor))
        targets = []
        for model in apps.get_models():
            table = model._meta.db_table
            if table not in tables or model._meta.proxy or model._meta.app_label not in {
                'authors', 'texts', 'workflow', 'people', 'core', 'illustrations', 'auth', 'contenttypes'
            }:
                continue
            targets.append(table)
        targets = sorted(set(targets))
        # Sprawdź WSZYSTKIE unikalne indeksy przed pierwszą zmianą tabeli.
        for table in targets:
            cursor.execute('SELECT COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', [table])
            collated = {name for name, collation in cursor.fetchall() if collation}
            constraints = connection.introspection.get_constraints(cursor, table)
            for name, constraint in constraints.items():
                columns = constraint.get('columns', [])
                if not constraint.get('unique') or not any(c in collated for c in columns):
                    continue
                expressions = [f'CONVERT({quote(c)} USING utf8mb4) COLLATE {COLLATION}' if c in collated else quote(c) for c in columns]
                nonnull = ' AND '.join(f'{quote(c)} IS NOT NULL' for c in columns)
                cursor.execute(f'SELECT 1 FROM {quote(table)} WHERE {nonnull} GROUP BY {", ".join(expressions)} HAVING COUNT(*) > 1 LIMIT 1')
                if cursor.fetchone():
                    raise RuntimeError(f'Konflikt wartości bez rozróżniania wielkości liter: tabela {table}, indeks {name}. Rozwiąż duplikaty przed ponowieniem migracji.')
        for table in targets:
            cursor.execute(f'ALTER TABLE {quote(table)} CONVERT TO CHARACTER SET utf8mb4 COLLATE {COLLATION}')
        cursor.execute(f'ALTER DATABASE {quote(connection.settings_dict["NAME"])} CHARACTER SET utf8mb4 COLLATE {COLLATION}')


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ('core', '0003_recruitment'), ('texts', '0003_extract'),
        ('authors', '0002_blacklistedauthor'), ('people', '0002_person_previous_data'),
        ('workflow', '0002_mysql_verifier_uniqueness'), ('illustrations', '0001_initial'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]
    operations = [migrations.RunPython(forwards)]
