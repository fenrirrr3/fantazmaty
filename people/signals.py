from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Q
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from .models import Person, Role


def coordinator_query():
    return Q(name__iexact="Koordynator") | Q(name__istartswith="Koordynator ")


# CMS coordination does not confer Django-admin access. Existing role/flag
# revocation handlers below remain the common explicit revocation operation.

from django.db.models.signals import pre_save
from people.coordinator_access import revoke_coordinator, revoking


@receiver(pre_save, sender=Person)
def track_coordinator_flag(sender, instance, using, **kwargs):
    old = sender.objects.using(using).filter(pk=instance.pk).values_list('is_coordinator', flat=True).first() if instance.pk else False
    instance._revoke_coordinator = bool(old and not instance.is_coordinator)


@receiver(post_save, sender=Person)
def revoke_by_flag(sender, instance, raw=False, **kwargs):
    if not raw and getattr(instance, '_revoke_coordinator', False) and not revoking.get():
        revoke_coordinator(instance)
        instance._revoke_coordinator = False


@receiver(m2m_changed, sender=Person.roles.through)
def revoke_removed_roles(sender, instance, action, reverse, pk_set, using, **kwargs):
    if revoking.get():
        return
    if action in ('pre_remove', 'pre_clear'):
        if reverse:
            people = instance.people.all() if action == 'pre_clear' else Person.objects.filter(pk__in=pk_set)
            affected = list(people.values_list('pk', flat=True)) if Role.objects.filter(pk=instance.pk).filter(coordinator_query()).exists() else []
        else:
            removed = instance.roles.all() if action == 'pre_clear' else instance.roles.filter(pk__in=pk_set)
            affected = [instance.pk] if removed.filter(coordinator_query()).exists() else []
        instance._removed_coordinator_people = affected
    elif action in ('post_remove', 'post_clear'):
        for person in Person.objects.filter(pk__in=getattr(instance, '_removed_coordinator_people', [])):
            if not person.roles.filter(coordinator_query()).exists():
                revoke_coordinator(person)


from django.db.models.signals import pre_delete, post_delete


def coordinator_name(name):
    name = name.casefold()
    return name == 'koordynator' or name.startswith('koordynator ')


@receiver(pre_save, sender=Role)
@receiver(pre_delete, sender=Role)
def remember_coordinator_members(sender, instance, **kwargs):
    old_name = sender.objects.filter(pk=instance.pk).values_list('name', flat=True).first() if instance.pk else None
    if not old_name or not coordinator_name(old_name):
        instance._coordinator_members = []
    elif sender is Role:
        instance._coordinator_members = list(instance.people.values_list('pk', flat=True))
    else:
        instance._coordinator_members = list(Person.objects.filter(user__groups=instance).values_list('pk', flat=True))


@receiver(post_save, sender=Role)
@receiver(post_delete, sender=Role)
def revoke_lost_coordinator_name(sender, instance, signal, **kwargs):
    if revoking.get() or (signal is post_save and coordinator_name(instance.name)):
        return
    for person in Person.objects.filter(pk__in=getattr(instance, '_coordinator_members', [])):
        remaining = person.roles.filter(coordinator_query()).exists() if sender is Role else bool(person.user_id and person.user.groups.filter(coordinator_query()).exists())
        if not remaining:
            revoke_coordinator(person)
