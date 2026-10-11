"""Audiobook production is independent of the editorial workflow."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.edit_versions import VersionedQuerySet
from core.audiobook_validators import validate_youtube_url, validate_hearthis_url, validate_audio_links


def contact_key(value):
    """Loose identity of a typed name: case, spacing and Polish diacritics are ignored."""
    import unicodedata
    value = ' '.join((value or '').split()).casefold().replace('ł', 'l')
    return ''.join(ch for ch in unicodedata.normalize('NFKD', value) if not unicodedata.combining(ch))


class AudioContributor(models.Model):
    """Wpis spisu lektorów i montażystów – niezależny od kont i ról zespołu (jak spis ilustratorów)."""
    objects = VersionedQuerySet.as_manager()
    name = models.CharField('imię i nazwisko', max_length=255)
    email = models.EmailField('adres e-mail', blank=True)
    is_active = models.BooleanField('aktywny', default=True, db_index=True,
        help_text='Wyłączenie ukrywa wpis w aktywnym spisie i przy nowych przypisaniach. Dotychczasowe nagrania pozostają.')

    class Meta:
        verbose_name = 'lektor / montaż'
        verbose_name_plural = 'Lektorzy i montaż'
        ordering = ('name', 'pk')
        constraints = [models.UniqueConstraint(fields=('name', 'email'), name='unique_audio_contact_pair')]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        from django.db import transaction
        with transaction.atomic():
            self.name = ' '.join(self.name.split())
            self.email = self.email.strip().lower()
            super().save(*args, **kwargs)
            for role in ('narrator', 'engineer'):
                Audiobook.objects.filter(**{f'{role}_contact': self}).update(
                    **{f'{role}_name': self.name, f'{role}_email': self.email})


class StageType(models.TextChoices):
    """Etapy pracy nad audiobookiem w stałej kolejności; każdy ma (opcjonalne) daty rozpoczęcia i zakończenia."""
    RECORDING = 'recording', 'Nagrywanie'
    PROOFREADING = 'proofreading', 'Korekta'
    CORRECTIONS = 'corrections', 'Nanoszenie poprawek'
    EDITING = 'editing', 'Montaż'


class Audiobook(models.Model):
    objects = VersionedQuerySet.as_manager()

    class Status(models.TextChoices):
        """Status ustawia koordynator (jak w ilustracjach); zakończenie korekty przestawia go na poprawki."""
        PENDING = 'pending', 'Nieprzypisane'
        RECORDING = 'recording', 'Nagrywanie'
        PROOFREADING = 'proofreading', 'Korekta'
        CORRECTIONS = 'corrections', 'Nanoszenie poprawek'
        EDITING = 'editing', 'Montaż'
        PUBLISHED = 'published', 'Opublikowane'

    text = models.OneToOneField('texts.Text', on_delete=models.PROTECT, related_name='audiobook', verbose_name='tekst')
    active_stage = models.OneToOneField('core.AudiobookStage', on_delete=models.PROTECT, null=True, blank=True,
        editable=False, related_name='active_for', verbose_name='bieżący etap')
    status = models.CharField('status audiobooka', max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True)
    narrator_name = models.CharField('lektor – imię i nazwisko', max_length=255, blank=True)
    narrator_contact = models.ForeignKey(AudioContributor, on_delete=models.PROTECT, null=True, blank=True,
        related_name='narrated_books', verbose_name='profil lektora')
    engineer_contact = models.ForeignKey(AudioContributor, on_delete=models.PROTECT, null=True, blank=True,
        related_name='engineered_books', verbose_name='profil montażysty')
    narrator_email = models.EmailField('e-mail lektora', blank=True)
    engineer_name = models.CharField('montaż – imię i nazwisko', max_length=255, blank=True)
    engineer_email = models.EmailField('e-mail dźwiękowca', blank=True)
    proofreader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='proofread_audiobooks', verbose_name='korektor audiobooka')
    coordinator_notes = models.TextField('uwagi koordynatora', blank=True)
    premiere_date = models.DateField('data premiery', null=True, blank=True)
    youtube_url = models.URLField('YouTube', max_length=1000, blank=True, validators=[validate_youtube_url])
    hearthis_url = models.URLField('HearThis', max_length=1000, blank=True, validators=[validate_hearthis_url])
    additional_links = models.JSONField('linki do kolejnych części', default=list, blank=True,
        validators=[validate_audio_links], help_text='Lista obiektów: service (youtube/hearthis), part (numer części), url.')

    class Meta:
        verbose_name = 'audiobook'
        verbose_name_plural = 'Audiobooki – przypisania i publikacje'
        ordering = ('text__anthology__title', 'text__title', 'pk')

    def __str__(self):
        return str(self.text)

    def save(self, *args, **kwargs):
        fields = kwargs.get('update_fields')
        for role in ('narrator', 'engineer'):
            if fields is not None and not set(fields) & {f'{role}_name', f'{role}_email', f'{role}_contact'}:
                continue
            # Osoba pochodzi wyłącznie ze spisu; audiobook przechowuje kopię jej danych (wyszukiwanie, sortowanie).
            contact = getattr(self, f'{role}_contact')
            setattr(self, f'{role}_name', contact.name if contact else '')
            setattr(self, f'{role}_email', contact.email if contact else '')
            if fields is not None:
                fields = set(fields) | {f'{role}_contact', f'{role}_name', f'{role}_email'}
        if fields is not None:
            kwargs['update_fields'] = fields
        return super().save(*args, **kwargs)

    @property
    def status_label(self):
        return dict(self.Status.choices).get(self.status, self.status)

    @property
    def is_published(self):
        return self.status == self.Status.PUBLISHED

    @property
    def publication_links(self):
        links = []
        for service in ('youtube', 'hearthis'):
            url = getattr(self, f'{service}_url')
            if url:
                links.append({'service': service, 'part': 1, 'url': url})
        links.extend(self.additional_links)
        return [{**link, 'label': ('YouTube' if link['service'] == 'youtube' else 'HearThis')
            + (f" – cz. {link['part']}" if self.additional_links else '')} for link in links]

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
        if errors:
            raise ValidationError(errors)


class AudiobookStage(models.Model):
    """Etap audiobooka. Czeka na rozpoczęcie (brak startu), trwa (start lub start_unknown) albo zakończony.

    Daty i wykonawca są opcjonalne – historyczne nagrania często ich nie mają.
    """
    objects = VersionedQuerySet.as_manager()
    text = models.ForeignKey('texts.Text', on_delete=models.PROTECT, related_name='audiobook_stages', verbose_name='tekst')
    stage_type = models.CharField('etap', max_length=24, choices=StageType.choices)
    position = models.PositiveIntegerField('kolejność', default=0, db_index=True)
    queued_at = models.DateField('oczekuje od', null=True, blank=True)
    started_at = models.DateField('data rozpoczęcia', null=True, blank=True)
    start_unknown = models.BooleanField('rozpoczęty (data nieznana)', default=False,
        help_text='Etap trwał lub trwa, ale data rozpoczęcia nie jest znana.')
    ended_at = models.DateField('data zakończenia', null=True, blank=True)
    is_completed = models.BooleanField('zakończony', default=False)
    performer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='audiobook_stage_history', verbose_name='wykonawca korekty')
    import_key = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)

    class Meta:
        verbose_name = 'etap audiobooka'
        verbose_name_plural = 'Historia etapów audiobooków'
        ordering = ('position', 'pk')
        constraints = [models.CheckConstraint(condition=models.Q(ended_at__isnull=True)
            | models.Q(started_at__isnull=True) | models.Q(ended_at__gte=models.F('started_at')),
            name='audio_stage_end_after_start')]

    def __str__(self):
        return f'{self.text} – {self.get_stage_type_display()}'

    @property
    def is_started(self):
        return bool(self.started_at or self.start_unknown or self.is_completed)
