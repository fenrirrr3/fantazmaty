from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from .models import Person, Role
from people.coordinator_access import revoke_coordinator, revoking
from people.roles import coordinator_role_q, is_coordinator_role_name


# CMS coordination does not confer Django-admin access. Existing role/flag
# revocation handlers below remain the common explicit revocation operation.



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
            affected = list(people.values_list('pk', flat=True)) if Role.objects.filter(pk=instance.pk).filter(coordinator_role_q()).exists() else []
        else:
            removed = instance.roles.all() if action == 'pre_clear' else instance.roles.filter(pk__in=pk_set)
            affected = [instance.pk] if removed.filter(coordinator_role_q()).exists() else []
        instance._removed_coordinator_people = affected
    elif action in ('post_remove', 'post_clear'):
        for person in Person.objects.filter(pk__in=getattr(instance, '_removed_coordinator_people', [])):
            if not person.legacy_coordinator_access and not person.roles.filter(coordinator_role_q()).exists():
                revoke_coordinator(person)


@receiver(pre_save, sender=Role)
@receiver(pre_delete, sender=Role)
def remember_coordinator_members(sender, instance, **kwargs):
    old_name = sender.objects.filter(pk=instance.pk).values_list('name', flat=True).first() if instance.pk else None
    if not old_name or not is_coordinator_role_name(old_name):
        instance._coordinator_members = []
    else:
        instance._coordinator_members = list(instance.people.values_list('pk', flat=True))


@receiver(post_save, sender=Role)
@receiver(post_delete, sender=Role)
def revoke_lost_is_coordinator_role_name(sender, instance, signal, **kwargs):
    if revoking.get() or (signal is post_save and is_coordinator_role_name(instance.name)):
        return
    for person in Person.objects.filter(pk__in=getattr(instance, '_coordinator_members', [])):
        if not person.legacy_coordinator_access and not person.roles.filter(coordinator_role_q()).exists():
            revoke_coordinator(person)
