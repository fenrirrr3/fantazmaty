"""Published-volume credits are separate from individual miniature workflow."""

from django.core.exceptions import ValidationError
from django.db import models


class ExtractVolume(models.Model):
    anthology = models.OneToOneField(
        "texts.Anthology", on_delete=models.PROTECT, related_name="extract_volume"
    )
    number = models.PositiveSmallIntegerField(
        "tom", unique=True, choices=((1, "1"), (2, "2"), (3, "3"))
    )

    class Meta:
        verbose_name = "tom Ekstraktów"
        verbose_name_plural = "tomy Ekstraktów"

    def __str__(self):
        return str(self.anthology)


class ExtractVolumeCredit(models.Model):
    anthology = models.ForeignKey(
        "texts.Anthology", on_delete=models.PROTECT, related_name="extract_credits"
    )
    person = models.ForeignKey(
        "people.Person",
        on_delete=models.PROTECT,
        related_name="extract_credits",
        verbose_name="osoba",
    )
    role = models.CharField("wykonana praca / etap", max_length=100)
    source_name = models.CharField("podpis w stopce", max_length=255, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "wykonana praca nad Ekstraktami"
        verbose_name_plural = "wykonane prace nad Ekstraktami"
        ordering = ("anthology_id", "position", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("anthology", "person", "role"), name="unique_extract_volume_credit"
            )
        ]

    def clean(self):
        super().clean()
        if (
            self.anthology_id
            and not ExtractVolume.objects.filter(anthology_id=self.anthology_id).exists()
        ):
            raise ValidationError(
                {"anthology": "Te przypisania dotyczą wyłącznie tomów Ekstraktów."}
            )

    def __str__(self):
        return f"{self.anthology} – {self.role}: {self.person}"


class ExtractTextLink(models.Model):
    extract = models.ForeignKey(
        "texts.Extract", on_delete=models.PROTECT, related_name="imported_texts"
    )
    text = models.OneToOneField(
        "texts.Text", on_delete=models.PROTECT, related_name="extract_origin"
    )
    title_key = models.CharField(max_length=255)
    source_title = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("extract", "title_key"), name="unique_extract_accepted_title"
            )
        ]
        verbose_name = "powiązanie miniatury"
        verbose_name_plural = "powiązania miniatur"
