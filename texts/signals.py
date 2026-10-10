from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Anthology, AnthologyTask
from .models import Text
from .translations import sync_translations


@receiver(post_save, sender=Anthology, dispatch_uid='texts.create_novel_profile')
def create_novel_profile(sender, instance, using, raw=False, **kwargs):
    if not raw and instance.is_novel:
        from texts.models import NovelProfile
        NovelProfile.objects.using(using).get_or_create(anthology=instance)



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
    using,
    raw=False,
    **kwargs,
):
    if raw:
        return
    for task_type, _label in AnthologyTask.TaskType.choices:
        AnthologyTask.objects.using(using).get_or_create(
            anthology=instance,
            task_type=task_type,
            defaults={
                "status": (
                    AnthologyTask.Status.NOT_COMMISSIONED
                ),
            },
        )


@receiver(post_save, sender=Anthology, dispatch_uid='texts.create_audio_description')
def create_audio_description(sender, instance, using, raw=False, **kwargs):
    if not raw and not instance.is_novel:
        from core.models import AudioDescription
        AudioDescription.objects.using(using).get_or_create(anthology=instance)


@receiver(post_save, sender=AnthologyTask, dispatch_uid='texts.sync_audio_description_stage')
def sync_audio_description_stage(sender, instance, using, raw=False, **kwargs):
    if raw or instance.task_type != 'audio_description':
        return
    from core.models import AudioDescription
    rows = AudioDescription.objects.using(using).filter(anthology_id=instance.anthology_id)
    if instance.status == 'not_applicable':
        return  # audiodeskrypcja niepotrzebna: jej etap zostaje bez zmian
    if instance.status == 'ready':
        rows.exclude(stage='completed').update(stage='completed')
    else:
        # Reopening (admin only) returns to the last working step when work exists,
        # and to the start when the description was never written.
        from django.db.models import Q
        reopened = rows.filter(stage='completed')
        reopened.filter(~Q(content='') | Q(notes__isnull=False)).update(stage='proofreading')
        reopened.update(stage='writing')


@receiver(post_save, sender=Anthology, dispatch_uid='texts.ensure_extract_whole')
def ensure_extract_whole(sender, instance, using, raw=False, **kwargs):
    if not raw:
        from .extract_whole import is_extract_anthology, ensure_whole_text
        if is_extract_anthology(instance):
            whole = ensure_whole_text(instance)
            if whole and instance.status != 'ready':
                instance.status = Anthology.objects.using(using).get(pk=instance.pk).status
