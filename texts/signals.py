from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Anthology, AnthologyTask


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