# Rekrutacja bez dawnego pola „dział”: role zgłoszenia są tylko w mail_roles.
# Zgłoszenie bez rozpoznanej roli dostaje dawny dział (jeśli to znana rola) albo „Inny pomysł”,
# więc lista ról i decyzje zgłoszeń się nie zmieniają.

from django.db import migrations

# Stałe z chwili migracji (bez importów z kodu aplikacji): klucze role_choices()
# i wartości dawnego Recruitment.Department.
KNOWN_ROLES = ('editors', 'proofreaders', 'verifiers', 'reviewers', 'narrators', 'audio_proofreaders',
               'illustrators', 'typesetters', 'post_layout', 'promotion', 'other', 'sound', 'designers')
DEPARTMENTS = ('editors', 'proofreaders', 'verifiers', 'reviewers', 'post_layout', 'audio_proofreaders',
               'narrators', 'sound', 'illustrators', 'typesetters', 'designers', 'other')


def _known(mail_roles):
    roles = mail_roles if isinstance(mail_roles, list) else []
    return [role for role in roles if isinstance(role, str) and role in KNOWN_ROLES]


def roles_from_department(apps, schema_editor):
    Recruitment = apps.get_model('core', 'Recruitment')
    rows = Recruitment.objects.using(schema_editor.connection.alias)
    for pk, mail_roles, department in list(rows.values_list('pk', 'mail_roles', 'department')):
        if not _known(mail_roles):
            rows.filter(pk=pk).update(mail_roles=[department if department in KNOWN_ROLES else 'other'])


def department_from_roles(apps, schema_editor):
    Recruitment = apps.get_model('core', 'Recruitment')
    rows = Recruitment.objects.using(schema_editor.connection.alias)
    by_department = {}
    for pk, mail_roles in rows.values_list('pk', 'mail_roles').iterator():
        roles = set(_known(mail_roles))
        department = roles.pop() if len(roles) == 1 else 'other'
        if department in DEPARTMENTS and department != 'other':
            by_department.setdefault(department, []).append(pk)
    for department, pks in by_department.items():
        for start in range(0, len(pks), 500):
            rows.filter(pk__in=pks[start:start + 500]).update(department=department)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0070_useractivity_created_at_default'),
    ]

    operations = [
        migrations.RunPython(roles_from_department, department_from_roles),
        migrations.RemoveField(model_name='recruitment', name='department'),
    ]
