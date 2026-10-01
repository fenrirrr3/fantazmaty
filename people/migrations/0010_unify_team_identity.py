from django.db import migrations


def unify(apps, schema_editor):
    Person = apps.get_model('people', 'Person')
    Role = apps.get_model('people', 'Role')
    User = apps.get_model('auth', 'User')
    Group = apps.get_model('auth', 'Group')
    database = schema_editor.connection.alias
    for person in Person.objects.using(database).select_related('user').iterator(chunk_size=200):
        if person.user_id:
            for name in person.user.groups.values_list('name', flat=True):
                person.roles.add(Role.objects.using(database).get_or_create(name=name)[0])
            # Preserve a meaningful name from either old source, prefer the profile.
            first = person.first_name or person.user.first_name
            last = person.last_name or person.user.last_name
            Person.objects.using(database).filter(pk=person.pk).update(first_name=first, last_name=last)
            User.objects.using(database).filter(pk=person.user_id).update(first_name=first, last_name=last)
        if person.is_coordinator and not person.roles.filter(name__istartswith='Koordynator').exists():
            person.roles.add(Role.objects.using(database).get_or_create(name='Koordynator')[0])
        names = list(person.roles.values_list('name', flat=True))
        Person.objects.using(database).filter(pk=person.pk).update(is_coordinator=any(n.casefold() == 'koordynator' or n.casefold().startswith('koordynator ') for n in names))
        if person.user_id:
            person.user.groups.set([Group.objects.using(database).get_or_create(name=n)[0] for n in names])


class Migration(migrations.Migration):
    dependencies = [('people', '0009_coordinator_access_without_staff'), ('auth', '0012_alter_user_first_name_max_length')]
    operations = [migrations.RunPython(unify, migrations.RunPython.noop)]
