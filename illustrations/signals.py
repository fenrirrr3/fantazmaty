from django.db.models.signals import post_save
from django.dispatch import receiver

from texts.models import Anthology, Text

from .models import Illustration
from .services import sync_required_illustrations


@receiver(post_save, sender=Anthology)
def create_illustrations_for_anthology(
    sender,
    instance,
    **kwargs,
):
    if (
        instance.status == Anthology.Status.UNPUBLISHED
        and instance.has_illustrations
    ):
        sync_required_illustrations(
            anthology=instance,
        )


@receiver(post_save, sender=Text)
def create_illustration_for_text(
    sender,
    instance,
    **kwargs,
):
    qualifies_for_illustration = (
        instance.anthology_id is not None
        and instance.anthology.status
        == Anthology.Status.UNPUBLISHED
        and instance.anthology.has_illustrations
    )

    if qualifies_for_illustration:
        Illustration.objects.get_or_create(
            text=instance,
            defaults={
                "status": Illustration.Status.UNASSIGNED,
            },
        )