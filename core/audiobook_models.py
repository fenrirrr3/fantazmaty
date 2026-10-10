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


def resolve_contact(name, email, contact=None):
    """Return the single profile for a narrator or sound engineer, creating it only when new.

    A typed name never renames an existing profile (that is done on the profile itself).
    """
    name = ' '.join((name or '').split())
    email = (email or '').strip().lower()
    if not name:
        return None
    key = contact_key(name)
    if contact is not None and contact_key(contact.name) == key and email in ('', contact.email):
        return contact
    if contact is not None and contact_key(contact.name) == key and not contact.email:
        if not AudioContributor.objects.filter(email__iexact=email).exclude(pk=contact.pk).exists():
            contact.email = email
            contact.save(update_fields=['email'])
            return contact
    if email:
        owners = list(AudioContributor.objects.filter(email__iexact=email))
        if owners:
            same = [person for person in owners if contact_key(person.name) == key]
            if len(same) == 1:
                return same[0]
            raise ValidationError(f'Adres {email} ma już kontakt „{owners[0].name}”. Wybierz go z podpowiedzi albo popraw adres.')
    similar = [person for person in AudioContributor.objects.only('pk', 'name', 'email') if contact_key(person.name) == key]
    if email:
        without_email = [person for person in similar if not person.email]
        if len(without_email) == 1 and len(similar) == 1:
            person = without_email[0]
            person.email = email
            person.save(update_fields=['email'])
            return person
        if similar and not without_email:
            # Same name, different address: another person.
            return AudioContributor.objects.create(name=name, email=email)
    if len(similar) == 1:
        return similar[0]
    if len(similar) > 1:
        raise ValidationError(f'W bazie jest kilka kontaktów „{name}”. Wybierz właściwy z podpowiedzi.')
    return AudioContributor.objects.create(name=name, email=email)


class AudioContributor(models.Model):
    objects = VersionedQuerySet.as_manager()
    name = models.CharField('imię i nazwisko', max_length=255)
    email = models.EmailField('adres e-mail', blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='audio_contacts', verbose_name='powiązane konto (opcjonalne)')

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
            if not self.pk and not self.user_id and self.email:
                from django.contrib.auth import get_user_model
                matches = list(get_user_model().objects.filter(email__iexact=self.email).values_list('pk', flat=True)[:2])
                if len(matches) == 1:
                    self.user_id = matches[0]
            super().save(*args, **kwargs)
            for role in ('narrator', 'engineer'):
                Audiobook.objects.filter(**{f'{role}_contact': self}).update(
                    **{f'{role}_name': self.name, f'{role}_email': self.email})


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
    active_stage = models.OneToOneField('core.AudiobookStage', on_delete=models.PROTECT, null=True, blank=True,
        editable=False, related_name='active_for', verbose_name='trwający etap')
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
    recording_started_at = models.DateField('rozpoczęcie nagrywania', null=True, blank=True)
    proofreading_started_at = models.DateField('rozpoczęcie korekty', null=True, blank=True)
    corrections_started_at = models.DateField('rozpoczęcie nanoszenia poprawek', null=True, blank=True)
    editing_started_at = models.DateField('rozpoczęcie montażu', null=True, blank=True)
    awaiting_publication_started_at = models.DateField('oczekiwanie na publikację od', null=True, blank=True)
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
        from django.db import transaction
        with transaction.atomic():
            fields = kwargs.get('update_fields')
            for role in ('narrator', 'engineer'):
                if fields is not None and not set(fields) & {f'{role}_name', f'{role}_email', f'{role}_contact'}:
                    continue
                contact = getattr(self, f'{role}_contact')
                name = getattr(self, f'{role}_name')
                if contact is None and ' '.join(name.split()):
                    contact = resolve_contact(name, getattr(self, f'{role}_email'))
                # One profile per person: the audiobook only mirrors the profile data.
                setattr(self, f'{role}_contact', contact)
                setattr(self, f'{role}_name', contact.name if contact else '')
                setattr(self, f'{role}_email', contact.email if contact else '')
                if fields is not None:
                    fields = set(fields) | {f'{role}_contact', f'{role}_name', f'{role}_email'}
            if fields is not None:
                kwargs['update_fields'] = fields
            return super().save(*args, **kwargs)

    @property
    def awaiting_next_stage(self):
        return self.status not in (self.Status.PENDING, self.Status.PUBLISHED) and not self.active_stage_id

    @property
    def status_label(self):
        from core.audiobook_services import status_text
        return status_text(self)

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
        for name, email in (('narrator_name', 'narrator_email'), ('engineer_name', 'engineer_email')):
            setattr(self, name, ' '.join(getattr(self, name).split()))
            if getattr(self, email) and not getattr(self, name):
                errors[name] = 'Przy adresie e-mail podaj również imię i nazwisko.'
        if errors:
            raise ValidationError(errors)


class AudiobookStage(models.Model):
    objects = VersionedQuerySet.as_manager()
    text = models.ForeignKey('texts.Text', on_delete=models.PROTECT, related_name='audiobook_stages', verbose_name='tekst')
    stage_type = models.CharField('etap', max_length=24, choices=Audiobook.Status.choices)
    started_at = models.DateField('data rozpoczęcia', null=True, blank=True)
    ended_at = models.DateField('data zakończenia', null=True, blank=True)
    is_completed = models.BooleanField('zakończony', default=False)
    performer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='audiobook_stage_history', verbose_name='wykonawca korekty')
    import_key = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)

    class Meta:
        verbose_name = 'etap audiobooka'
        verbose_name_plural = 'Historia etapów audiobooków'
        ordering = ('pk',)
        constraints = [models.CheckConstraint(condition=models.Q(ended_at__isnull=True)
            | models.Q(started_at__isnull=True) | models.Q(ended_at__gte=models.F('started_at')),
            name='audio_stage_end_after_start')]

    def __str__(self):
        return f'{self.text} – {self.get_stage_type_display()}'
