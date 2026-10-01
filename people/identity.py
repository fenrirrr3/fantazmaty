"""Person owns team roles and names; account groups are a compatibility mirror."""
from contextvars import ContextVar
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models.signals import m2m_changed, post_save, pre_save, post_delete, pre_delete
from django.dispatch import receiver
from people.models import Person, Role

syncing = ContextVar('syncing_team_identity', default=False)


def sync_person(person, *, copy_groups=False):
    if syncing.get(): return
    token = syncing.set(True)
    try:
        if person.user_id and copy_groups:
            for name in person.user.groups.values_list('name', flat=True):
                person.roles.add(Role.objects.get_or_create(name=name)[0])
        if person.is_coordinator and not person.roles.filter(name__istartswith='Koordynator').exists():
            person.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        names = list(person.roles.values_list('name', flat=True))
        coordinator = any(name.casefold() == 'koordynator' or name.casefold().startswith('koordynator ') for name in names)
        Person.objects.filter(pk=person.pk).update(is_coordinator=coordinator)
        if person.user_id:
            user = get_user_model().objects.get(pk=person.user_id)
            get_user_model().objects.filter(pk=user.pk).update(first_name=person.first_name, last_name=person.last_name)
            groups = [Group.objects.get_or_create(name=name)[0] for name in names]
            user.groups.set(groups)
    finally:
        syncing.reset(token)


@receiver(post_save, sender=Person)
def profile_saved(sender, instance, raw=False, created=False, **kwargs):
    if not raw: sync_person(instance, copy_groups=created)


@receiver(m2m_changed, sender=Person.roles.through)
def profile_roles_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if syncing.get(): return
    if reverse:
        if action == 'pre_clear': instance._cms_people = list(instance.people.values_list('pk', flat=True))
        ids = getattr(instance, '_cms_people', []) if action == 'post_clear' else pk_set or ()
        if action.startswith('post_'):
            for person in Person.objects.filter(pk__in=ids): sync_person(person)
    elif action.startswith('post_'):
        # The flag mirrors roles; it must not re-add a deliberately removed role.
        instance.is_coordinator = instance.roles.filter(name__istartswith='Koordynator').exists()
        sync_person(instance)


@receiver(m2m_changed, sender=get_user_model().groups.through)
def account_groups_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if syncing.get(): return
    if reverse:
        if action == 'pre_clear': instance._cms_accounts = list(instance.user_set.values_list('pk', flat=True))
        ids = getattr(instance, '_cms_accounts', []) if action == 'post_clear' else pk_set or ()
        users = get_user_model().objects.filter(pk__in=ids)
    else:
        users = [instance]
    if not action.startswith('post_'): return
    token = syncing.set(True)
    try:
        for user in users:
            person = Person.objects.filter(user=user).first()
            if person:
                names = list(user.groups.values_list('name', flat=True))
                roles = [Role.objects.get_or_create(name=name)[0] for name in names]
                person.roles.set(roles)
                Person.objects.filter(pk=person.pk).update(is_coordinator=any(name.casefold() == 'koordynator' or name.casefold().startswith('koordynator ') for name in names))
    finally:
        syncing.reset(token)


@receiver(pre_save, sender=get_user_model())
def normalize_account_email(sender, instance, **kwargs):
    instance.email = (instance.email or '').strip().lower()
    if instance.pk and not getattr(instance, '_cms_edit_profile_name', False):
        person = Person.objects.filter(user_id=instance.pk).first()
        if person: instance.first_name, instance.last_name = person.first_name, person.last_name


@receiver(pre_delete, sender=Role)
def remember_role_people(sender, instance, **kwargs):
    instance._cms_role_people = list(instance.people.values_list('pk', flat=True))


@receiver(post_delete, sender=Role)
def deleted_role(sender, instance, **kwargs):
    for person in Person.objects.filter(pk__in=getattr(instance, '_cms_role_people', ())):
        person.is_coordinator = person.roles.filter(name__istartswith='Koordynator').exists()
        sync_person(person)


@receiver(pre_save, sender=Role)
def remember_role_edit(sender, instance, **kwargs):
    instance._cms_role_people = list(instance.people.values_list('pk', flat=True)) if instance.pk else []


@receiver(post_save, sender=Role)
def changed_role(sender, instance, **kwargs):
    for person in Person.objects.filter(pk__in=getattr(instance, '_cms_role_people', ())):
        person.is_coordinator = person.roles.filter(name__istartswith='Koordynator').exists()
        sync_person(person)


@receiver(post_delete, sender=Group)
def deleted_group(sender, instance, **kwargs):
    if syncing.get(): return
    for person in Person.objects.filter(roles__name=instance.name):
        person.roles.remove(*person.roles.filter(name=instance.name))
