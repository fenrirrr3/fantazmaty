from django.db.models.signals import post_save
from django.dispatch import receiver

from texts.models import Anthology, Text

from .models import Illustration
from .services import sync_required_illustrations


@receiver(post_save, sender=Anthology)
def create_illustrations_for_anthology(
    sender,
    instance,
    using,
    raw=False,
    **kwargs,
):
    if raw:
        return
    if (
        instance.status == Anthology.Status.IN_PREPARATION
        and instance.has_illustrations
        and not instance.is_translated
        and not instance.is_novel
    ):
        sync_required_illustrations(
            anthology=instance, using=using,
        )


@receiver(post_save, sender=Text)
def create_illustration_for_text(
    sender,
    instance,
    using,
    raw=False,
    **kwargs,
):
    if raw:
        return
    qualifies_for_illustration = (
        instance.anthology_id is not None
        and instance.anthology.status
        == Anthology.Status.IN_PREPARATION
        and instance.anthology.has_illustrations
        and not instance.anthology.is_translated
        and not instance.anthology.is_novel
    )

    from texts.production import active_production_texts
    if qualifies_for_illustration and active_production_texts(Text.objects.using(using).filter(pk=instance.pk)).exists():
        Illustration.objects.using(using).get_or_create(
            text=instance,
            defaults={
                "status": Illustration.Status.UNASSIGNED,
            },
        )


from django.db.models.signals import pre_delete
from django.db.models.deletion import ProtectedError
from .models import Illustrator


@receiver(pre_delete, sender=Illustrator)
def protect_illustrator_credits(sender, instance, using, **kwargs):
    credits = list(instance.illustrations.using(using).all())
    if credits:
        raise ProtectedError('Ilustrator ma przypisane teksty. Wyłącz jego aktywność zamiast usuwać wpis.', credits)
