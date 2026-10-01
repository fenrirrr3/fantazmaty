"""Keep account IDs and FKs; enforce a unique nonempty normalized login email."""
from django.db import migrations


def install(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    connection = schema_editor.connection
    groups = {}
    for pk, email in User.objects.using(connection.alias).values_list('pk', 'email').iterator():
        key = (email or '').strip().lower()
        if key: groups.setdefault(key, []).append(pk)
    conflicts = [ids for ids in groups.values() if len(ids) > 1]
    if conflicts:
        raise RuntimeError('Migracja zatrzymana: powtarzające się e-maile kont. Scal lub popraw konta o ID: ' + str(conflicts) + '. Żadnych kont nie usunięto.')
    table = connection.ops.quote_name(User._meta.db_table)
    if connection.vendor == 'mysql':
        schema_editor.execute(f'ALTER TABLE {table} ADD COLUMN cms_login_email VARCHAR(254) GENERATED ALWAYS AS (NULLIF(LOWER(TRIM(email)), \'\')) STORED, ADD UNIQUE INDEX cms_user_login_email_unique (cms_login_email)')
    elif connection.vendor in ('sqlite', 'postgresql'):
        schema_editor.execute(f'CREATE UNIQUE INDEX cms_user_login_email_unique ON {table} (LOWER(TRIM(email))) WHERE TRIM(email) <> \'\'')
    else:
        raise RuntimeError('Nieobsługiwana baza dla unikalnego adresu konta.')


def uninstall(apps, schema_editor):
    table = schema_editor.connection.ops.quote_name(apps.get_model('auth', 'User')._meta.db_table)
    if schema_editor.connection.vendor == 'mysql':
        schema_editor.execute(f'ALTER TABLE {table} DROP INDEX cms_user_login_email_unique, DROP COLUMN cms_login_email')
    else:
        schema_editor.execute('DROP INDEX cms_user_login_email_unique')


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('core', '0018_mailboxdownload_fingerprint_and_more'), ('auth', '0012_alter_user_first_name_max_length')]
    operations = [migrations.RunPython(install, uninstall)]
