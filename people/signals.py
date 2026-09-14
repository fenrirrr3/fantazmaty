from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Q
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from .models import Person, Role


def coordinator_query():
    return Q(name__iexact="Koordynator") | Q(name__istartswith="Koordynator ")


def promote_person(person, using):
    if person.user_id and (person.is_coordinator or person.roles.using(using).filter(coordinator_query()).exists()):
        get_user_model().objects.using(using).filter(pk=person.user_id).update(is_staff=True)


@receiver(post_save, sender=Person)
def person_saved(sender, instance, using, raw=False, **kwargs):
    if not raw:
        promote_person(instance, using)


@receiver(m2m_changed, sender=Person.roles.through)
def roles_changed(sender, instance, action, reverse, pk_set, using, **kwargs):
    if action != "post_add":
        return
    if reverse:
        for person in Person.objects.using(using).filter(pk__in=pk_set):
            promote_person(person, using)
    else:
        promote_person(instance, using)


@receiver(post_save, sender=Role)
def role_renamed(sender, instance, using, raw=False, **kwargs):
    if not raw:
        for person in instance.people.using(using).all():
            promote_person(person, using)


@receiver(m2m_changed, sender=get_user_model().groups.through)
def groups_changed(sender, instance, action, reverse, pk_set, using, **kwargs):
    if action != "post_add":
        return
    users = get_user_model().objects.using(using).filter(pk__in=pk_set) if reverse else get_user_model().objects.using(using).filter(pk=instance.pk)
    users.filter(groups__in=Group.objects.using(using).filter(coordinator_query())).update(is_staff=True)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def account_saved(sender, instance, using, raw=False, **kwargs):
    if raw or instance.is_staff:
        return
    if (instance.groups.using(using).filter(coordinator_query()).exists() or
        Person.objects.using(using).filter(user_id=instance.pk).filter(
            Q(is_coordinator=True) | Q(roles__name__iexact="Koordynator") | Q(roles__name__istartswith="Koordynator ")).exists()):
        sender.objects.using(using).filter(pk=instance.pk).update(is_staff=True)
        instance.is_staff = True


@receiver(post_save, sender=Group)
def group_renamed(sender, instance, using, raw=False, **kwargs):
    if not raw and Group.objects.using(using).filter(pk=instance.pk).filter(coordinator_query()).exists():
        get_user_model().objects.using(using).filter(groups=instance).update(is_staff=True)
