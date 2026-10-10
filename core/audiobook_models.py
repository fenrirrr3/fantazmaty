"""Audiobook production is independent of the editorial workflow."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.edit_versions import VersionedQuerySet
from core.audiobook_validators import validate_youtube_url, validate_hearthis_url


class Audiobook(models.Model):
    objects = VersionedQuerySet.as_manager()

    class Status(models.TextChoices):
        PENDING = 'pending', 'Do nagrania'
        RECORDING = 'recording', 'Trwa nagrywanie'
        PROOFREADING = 'proofreading', 'Korekta'
        CORRECTIONS = 'corrections', 'Nanoszenie poprawek'
        EDITING = 'editing', 'Montaż'
        AWAITING_PUBLICATION = 'awaiting_publication', 'Czeka na publikację'
        PUBLISHED = 'published', 'Opublikowane'

    text = models.OneToOneField('texts.Text', on_delete=models.PROTECT, related_name='audiobook', verbose_name='tekst')
    status = models.CharField('status audiobooka', max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True)
    narrator_name = models.CharField('lektor – imię i nazwisko', max_length=255, blank=True)
    narrator_email = models.EmailField('e-mail lektora', blank=True)
    engineer_name = models.CharField('dźwiękowiec – imię i nazwisko', max_length=255, blank=True)
    engineer_email = models.EmailField('e-mail dźwiękowca', blank=True)
    proofreader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='proofread_audiobooks', verbose_name='korektor audiobooka')
    recording_started_at = models.DateField('rozpoczęcie nagrywania', null=True, blank=True)
    proofreading_started_at = models.DateField('rozpoczęcie korekty', null=True, blank=True)
    corrections_started_at = models.DateField('rozpoczęcie nanoszenia poprawek', null=True, blank=True)
    editing_started_at = models.DateField('rozpoczęcie montażu', null=True, blank=True)
    awaiting_publication_started_at = models.DateField('oczekiwanie na publikację od', null=True, blank=True)
    premiere_date = models.DateField('data premiery', null=True, blank=True)
    youtube_url = models.URLField('YouTube', max_length=1000, blank=True, validators=[validate_youtube_url])
    hearthis_url = models.URLField('HearThis', max_length=1000, blank=True, validators=[validate_hearthis_url])

    class Meta:
        verbose_name = 'audiobook'
        verbose_name_plural = 'Audiobooki – przypisania i publikacje'
        ordering = ('text__anthology__title', 'text__title', 'pk')

    def __str__(self):
        return str(self.text)

    def clean(self):
        super().clean()
        errors = {}
        if self.text_id and self.text.anthology_id and self.text.anthology.is_novel:
            errors['text'] = 'Rozdziały powieści nie należą do audiobooków.'
        previous = type(self).objects.filter(pk=self.pk).values('proofreader_id', 'text_id').first() if self.pk else None
        if previous and previous['text_id'] != self.text_id:
            errors['text'] = 'Nie można przepiąć historii audiobooka do innego tekstu.'
        if self.proofreader_id and (not previous or previous['proofreader_id'] != self.proofreader_id):
            from core.audiobook_forms import eligible_proofreaders
            if not eligible_proofreaders().filter(pk=self.proofreader_id).exists():
                errors['proofreader'] = 'Wybierz aktywne konto członka zespołu z rolą Korektor audiobooków.'
        for name, email in (('narrator_name', 'narrator_email'), ('engineer_name', 'engineer_email')):
            setattr(self, name, ' '.join(getattr(self, name).split()))
            if getattr(self, email) and not getattr(self, name):
                errors[name] = 'Przy adresie e-mail podaj również imię i nazwisko.'
        if errors:
            raise ValidationError(errors)
