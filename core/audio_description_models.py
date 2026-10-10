from django.db import models
from core.edit_versions import VersionedQuerySet


class AudioDescription(models.Model):
    objects = VersionedQuerySet.as_manager()
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
        verbose_name="osoby do kontroli",
    )

    class Meta:
        verbose_name = "audiodeskrypcja"
        verbose_name_plural = "audiodeskrypcje"
        ordering = ("anthology__title", "pk")

    def __str__(self):
        return f"Audiodeskrypcja – {self.anthology}"
