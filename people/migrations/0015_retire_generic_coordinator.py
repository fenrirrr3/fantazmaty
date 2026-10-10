from django.db import migrations


def retire(apps, schema_editor):
    db = schema_editor.connection.alias
    Person = apps.get_model('people', 'Person')
    Role = apps.get_model('people', 'Role')
    Group = apps.get_model('auth', 'Group')
    users_model = apps.get_model('auth', 'User')
    generic = Role.objects.using(db).filter(name__iexact='Koordynator')
    ids = set(Person.objects.using(db).filter(roles__in=generic).values_list('pk', flat=True))
    ids.update(Person.objects.using(db).filter(is_coordinator=True).exclude(
        roles__name__istartswith='Koordynator ').values_list('pk', flat=True))
    for group in Group.objects.using(db).filter(name__iexact='Koordynator'):
        users = users_model.objects.using(db).filter(groups=group)
        ids.update(Person.objects.using(db).filter(user__in=users).values_list('pk', flat=True))
        permissions = list(group.permissions.using(db).all())
        # Preserve explicit Django permissions as well as CMS access.
        for user in users:
            user.user_permissions.add(*permissions)
        group.delete()
    Person.objects.using(db).filter(pk__in=ids).update(legacy_coordinator_access=True, is_coordinator=True)
    generic.delete()


def restore(apps, schema_editor):
    db = schema_editor.connection.alias
    Person = apps.get_model('people', 'Person')
    Role = apps.get_model('people', 'Role')
    Group = apps.get_model('auth', 'Group')
    role, _ = Role.objects.using(db).get_or_create(name='Koordynator')
    group, _ = Group.objects.using(db).get_or_create(name='Koordynator')
    for person in Person.objects.using(db).filter(legacy_coordinator_access=True):
        person.roles.add(role)
        if person.user_id:
            person.user.groups.add(group)


class Migration(migrations.Migration):
    dependencies = [('people', '0014_contacts_and_team_status')]
    operations = [migrations.RunPython(retire, restore)]
