"""Osoba jest źródłem ról, imion, e-maila logowania i możliwości logowania; konto tylko za nią podąża.

Grupy kont nie są już lustrem ról (uprawnienia wynikają z ról osoby). Grupy starego konta są czytane
jednorazowo, gdy powstaje dla niego osoba.
"""
from contextvars import ContextVar
from django.contrib.auth import get_user_model
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver
from people.accounts import account_changes, apply_account_changes
from people.models import Person, Role
from people.roles import is_coordinator_role_name, is_general_coordinator_name

syncing = ContextVar('syncing_team_identity', default=False)


def sync_person(person, *, copy_groups=False):
    if syncing.get():
        return
    token = syncing.set(True)
    try:
        if person.user_id and copy_groups:
            for name in person.user.groups.values_list("name", flat=True):
                if is_general_coordinator_name(name):
                    person.legacy_coordinator_access = True
                    Person.objects.filter(pk=person.pk).update(legacy_coordinator_access=True)
                    continue
                person.roles.add(Role.objects.get_or_create(name=name)[0])
        names = list(person.roles.values_list("name", flat=True))
        coordinator = person.legacy_coordinator_access or any(map(is_coordinator_role_name, names))
        Person.objects.filter(pk=person.pk).update(is_coordinator=coordinator)
        if person.user_id:
            get_user_model().objects.filter(pk=person.user_id).update(
                first_name=person.first_name, last_name=person.last_name
            )
        return coordinator
    finally:
        syncing.reset(token)


@receiver(pre_save, sender=Person)
def prepare_account_sync(sender, instance, raw=False, using=None, update_fields=None, **kwargs):
    """Zmiany konta liczone przed zapisem – kolizja e-maila przerywa zapis osoby (ValidationError)."""
    instance._account_changes = {}
    if raw or not instance.user_id:
        return
    # Poprzedni stan wiersza: people.models.remember_person_row.
    instance._account_changes = account_changes(instance, using=using, update_fields=update_fields,
                                                previous=instance._previous_row)


@receiver(post_save, sender=Person)
def profile_saved(sender, instance, raw=False, created=False, using=None, **kwargs):
    if not raw:
        sync_person(instance, copy_groups=created)
        apply_account_changes(instance.user_id, getattr(instance, '_account_changes', {}), using)
        instance._account_changes = {}


@receiver(m2m_changed, sender=Person.roles.through)
def profile_roles_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if syncing.get():
        return
    if reverse:
        if action == "pre_clear":
            instance._cms_people = list(instance.people.values_list("pk", flat=True))
        ids = getattr(instance, "_cms_people", []) if action == "post_clear" else pk_set or ()
        if action.startswith("post_"):
            for person in Person.objects.filter(pk__in=ids):
                sync_person(person)
    elif action.startswith("post_"):
        # The flag mirrors roles; it must not re-add a deliberately removed role.
        instance.is_coordinator = sync_person(instance)


@receiver(pre_save, sender=get_user_model())
def normalize_account_email(sender, instance, **kwargs):
    instance.email = (instance.email or "").strip().lower()
    if instance.pk and not getattr(instance, "_cms_edit_profile_name", False):
        person = Person.objects.filter(user_id=instance.pk).first()
        if person:
            instance.first_name, instance.last_name = person.first_name, person.last_name


@receiver(post_save, sender=Role)
@receiver(post_delete, sender=Role)
def changed_role(sender, instance, **kwargs):
    # Osoby roli sprzed zmiany: people.models.remember_role_people.
    for person in Person.objects.filter(pk__in=instance._role_people):
        sync_person(person)
