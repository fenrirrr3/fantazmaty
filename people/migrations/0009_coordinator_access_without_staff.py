from django.conf import settings
from django.db import migrations
from django.db.models import Q


def migrate_access(apps, schema_editor):
    User=apps.get_model(settings.AUTH_USER_MODEL); Person=apps.get_model('people','Person'); Role=apps.get_model('people','Role')
    db=schema_editor.connection.alias
    # Preserve legacy coordinator group memberships as the canonical profile roles.
    for person in Person.objects.using(db).exclude(user_id=None).iterator(chunk_size=500):
        user=User.objects.using(db).get(pk=person.user_id)
        for group in user.groups.using(db).filter(Q(name__iexact='Koordynator') | Q(name__istartswith='Koordynator ')):
            role,_=Role.objects.using(db).get_or_create(name=group.name)
            person.roles.add(role)
    # Admin access is exclusively superuser; the flag no longer implies CMS authority.
    User.objects.using(db).filter(is_superuser=False).update(is_staff=False)

class Migration(migrations.Migration):
    dependencies=[('people','0008_person_author_profile'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[migrations.RunPython(migrate_access,migrations.RunPython.noop)]
