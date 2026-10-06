from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Anthology, AnthologyTask


@receiver(post_save, sender=Anthology, dispatch_uid='texts.create_novel_profile')
def create_novel_profile(sender, instance, using, raw=False, **kwargs):
    if not raw and instance.is_novel:
        from texts.models import NovelProfile
        NovelProfile.objects.using(using).get_or_create(anthology=instance)

from .models import Text
from .translations import sync_translations


@receiver(post_save, sender=Anthology, dispatch_uid='texts.sync_anthology_translations')
def sync_anthology_translations(sender, instance, using, raw=False, **kwargs):
    if not raw and instance.is_translated:
        sync_translations(anthology_id=instance.pk, using=using)


@receiver(post_save, sender=Text, dispatch_uid='texts.sync_text_translation')
def sync_text_translation(sender, instance, using, raw=False, **kwargs):
    if not raw and instance.anthology_id and instance.anthology.is_translated:
        sync_translations(text_id=instance.pk, using=using)


@receiver(
    post_save,
    sender=Anthology,
    dispatch_uid="texts.create_anthology_tasks",
)
def create_anthology_tasks(
    sender,
    instance,
    **kwargs,
):
    for task_type, _label in AnthologyTask.TaskType.choices:
        AnthologyTask.objects.get_or_create(
            anthology=instance,
            task_type=task_type,
            defaults={
                "status": (
                    AnthologyTask.Status.NOT_COMMISSIONED
                ),
            },
        )
