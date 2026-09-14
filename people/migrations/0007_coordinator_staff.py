from django.conf import settings
from django.db import migrations
from django.db.models import Q


def migrate_coordinators(apps, schema_editor):
    using = schema_editor.connection.alias
    Role = apps.get_model("people", "Role")
    Person = apps.get_model("people", "Person")
    User = apps.get_model(settings.AUTH_USER_MODEL)
    Group = apps.get_model("auth", "Group")
    # Preserve coordinator access, remove the obsolete label.
    old_roles = Role.objects.using(using).filter(name__iexact="Koordynator zespołu")
    if old_roles.exists():
        generic, _ = Role.objects.using(using).get_or_create(name="Koordynator")
        for person in Person.objects.using(using).filter(roles__in=old_roles).distinct():
            person.roles.add(generic)
        old_roles.delete()
    old_groups = Group.objects.using(using).filter(name__iexact="Koordynator zespołu")
    if old_groups.exists():
        generic, _ = Group.objects.using(using).get_or_create(name="Koordynator")
        for user in User.objects.using(using).filter(groups__in=old_groups).distinct():
            for group in old_groups:
                if user.groups.filter(pk=group.pk).exists():
                    user.user_permissions.add(*group.permissions.all())
            user.groups.add(generic)
        old_groups.delete()
    people = Person.objects.using(using).filter(Q(is_coordinator=True) | Q(roles__name__iexact="Koordynator") | Q(roles__name__istartswith="Koordynator "))
    User.objects.using(using).filter(Q(pk__in=people.values("user_id")) | Q(groups__name__iexact="Koordynator") | Q(groups__name__istartswith="Koordynator ")).update(is_staff=True)


class Migration(migrations.Migration):
    dependencies = [("people", "0006_add_coordinator_roles"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.RunPython(migrate_coordinators, migrations.RunPython.noop)]
