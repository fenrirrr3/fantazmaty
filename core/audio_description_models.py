from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction, router
from core.edit_versions import VersionedQuerySet


class AudioDescription(models.Model):
    class Stage(models.TextChoices):
        WRITING = "writing", "Pisanie AD"
        CONSULTATION = "consultation", "Konsultacja"
        PROOFREADING = "proofreading", "Korekta audiodeskrypcji"
        COMPLETED = "completed", "Zakończone"

    objects = VersionedQuerySet.as_manager()
    content = models.TextField("treść audiodeskrypcji", blank=True)
    stage = models.CharField(
        "etap prac", max_length=20, choices=Stage.choices, default=Stage.WRITING
    )
    anthology = models.OneToOneField(
        "texts.Anthology",
        on_delete=models.CASCADE,
        related_name="audio_description",
        verbose_name="antologia",
    )
    controllers = models.ManyToManyField(
        "people.Person",
        blank=True,
        related_name="controlled_audio_descriptions",
        verbose_name="konsultacja – osoby do kontroli",
    )

    class Meta:
        verbose_name = "audiodeskrypcja"
        verbose_name_plural = "audiodeskrypcje"
        ordering = ("anthology__title", "pk")

    def __str__(self):
        return f"Audiodeskrypcja – {self.anthology}"

    def clean(self):
        super().clean()
        if self.stage == self.Stage.COMPLETED and self.anthology_id:
            if not self.anthology.production_tasks.filter(
                task_type="audio_description", assigned_to__isnull=False
            ).exists():
                raise ValidationError(
                    "Zakończenie wymaga przypisania osoby piszącej audiodeskrypcję."
                )

    def save(self, *args, **kwargs):
        from texts.models import Anthology, AnthologyTask

        alias = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        fields = kwargs.get("update_fields")
        sync_stage = fields is None or "stage" in fields
        with transaction.atomic(using=alias):
            Anthology.objects.using(alias).select_for_update().get(pk=self.anthology_id)
            task = (
                AnthologyTask.objects.using(alias)
                .filter(anthology_id=self.anthology_id, task_type="audio_description")
                .first()
            )
            if task and task.status == "not_applicable":
                # Zadanie „Nie dotyczy”: treść można zapisywać, status zadania się nie zmienia.
                task, sync_stage = None, False
            if self._state.adding and task and task.status == "ready":
                self.stage = self.Stage.COMPLETED
            if (
                sync_stage
                and self.stage == self.Stage.COMPLETED
                and task
                and not task.assigned_to_id
            ):
                raise ValidationError(
                    {"stage": "Zakończenie wymaga przypisania osoby piszącej audiodeskrypcję."}
                )
            super().save(*args, **kwargs)
            if task and sync_stage:
                status = (
                    "ready"
                    if self.stage == self.Stage.COMPLETED
                    else ("commissioned" if task.assigned_to_id else "not_commissioned")
                )
                if task.status != status:
                    task.status = status
                    task.full_clean()
                    task.save(using=alias, update_fields=["status"])


class AudioDescriptionNote(models.Model):
    objects = VersionedQuerySet.as_manager()
    description = models.ForeignKey(
        AudioDescription,
        on_delete=models.CASCADE,
        related_name="notes",
        verbose_name="audiodeskrypcja",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        editable=False,
        verbose_name="autor uwagi",
    )
    author_name = models.CharField("podpis autora", max_length=255, editable=False)
    content = models.TextField("uwaga")
    created_at = models.DateTimeField("data wpisania", auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "uwaga do audiodeskrypcji"
        verbose_name_plural = "uwagi do audiodeskrypcji"

    def __str__(self):
        return f"{self.author_name} – {self.description}"
