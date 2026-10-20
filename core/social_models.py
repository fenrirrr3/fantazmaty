"""Social Media: planowanie wpisów promocyjnych o opowiadaniach i innych wpisów."""
from django.conf import settings
from django.core.validators import URLValidator
from django.db import models
from django.utils import timezone

from core.edit_versions import VersionedQuerySet

# Tylko linki http(s) – URLField domyślnie dopuszcza też ftp.
validate_web_url = URLValidator(schemes=('http', 'https'), message='Podaj prawidłowy link zaczynający się od http:// lub https://.')


class SocialMediaStatus(models.TextChoices):
    FREE = 'free', 'Wolne'
    IN_PROGRESS = 'in_progress', 'W przygotowaniu'
    POSTED = 'posted', 'Wrzucone'


class SocialMediaFields(models.Model):
    """Pola wspólne dla wpisów o tekstach i innych wpisów."""
    status = models.CharField('status', max_length=20, choices=SocialMediaStatus.choices,
                              default=SocialMediaStatus.FREE, db_index=True)
    posted_on = models.DateField('data wrzucenia', null=True, blank=True,
                                 help_text='Może to być też data planowana (w przyszłości).')
    url = models.URLField('link', max_length=1000, blank=True, validators=[validate_web_url])
    updated_at = models.DateTimeField('zmieniono', auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='zmienił(a)', editable=False)

    class Meta:
        abstract = True


class TextSocialMedia(SocialMediaFields):
    """Stan promocji opowiadania. Rekord powstaje przy pierwszym zapisie; brak rekordu oznacza „Wolne”."""
    objects = VersionedQuerySet.as_manager()
    text = models.OneToOneField('texts.Text', on_delete=models.CASCADE, related_name='social_media',
                                verbose_name='tekst')

    class Meta:
        verbose_name = 'social media – opowiadanie'
        verbose_name_plural = 'Social Media – opowiadania'
        ordering = ('text__title', 'pk')

    def __str__(self):
        return f'{self.text} – {self.get_status_display()}'


class SocialMediaEntry(SocialMediaFields):
    """Wpis niezwiązany z tekstem (np. zapowiedź naboru)."""
    objects = VersionedQuerySet.as_manager()
    description = models.CharField('krótki opis', max_length=200)
    created_at = models.DateTimeField('utworzono', auto_now_add=True)

    class Meta:
        verbose_name = 'social media – inny wpis'
        verbose_name_plural = 'Social Media – inne wpisy'
        ordering = (models.F('posted_on').desc(nulls_first=True), '-pk')

    def __str__(self):
        return self.description


class SocialMediaQuote(models.Model):
    """Cytat z opowiadania do wpisu promocyjnego; jeden tekst może mieć wiele cytatów od wielu osób.

    Usunięcie tekstu usuwa jego cytaty (CASCADE, jak stan promocji tekstu): cytat bez tekstu nie ma sensu,
    a PROTECT blokowałby usuwanie tekstów z powodu notatek promocyjnych.
    """
    objects = VersionedQuerySet.as_manager()
    text = models.ForeignKey('texts.Text', on_delete=models.CASCADE, related_name='social_quotes', verbose_name='tekst')
    content = models.TextField('cytat', max_length=2000)
    status = models.CharField('status', max_length=20, choices=SocialMediaStatus.choices,
                              default=SocialMediaStatus.FREE, db_index=True)
    posted_on = models.DateField('data wrzucenia', default=timezone.localdate)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='+', verbose_name='dodał(a)', editable=False)
    # Podpis dodającego zostaje, gdy konto zostanie usunięte.
    added_by_name = models.CharField('dodał(a) – podpis', max_length=200, blank=True, editable=False)
    created_at = models.DateTimeField('utworzono', auto_now_add=True)
    updated_at = models.DateTimeField('zmieniono', auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='zmienił(a)', editable=False)

    class Meta:
        verbose_name = 'social media – cytat'
        verbose_name_plural = 'Social Media – cytaty'
        ordering = ('created_at', 'pk')

    def __str__(self):
        content = ' '.join(self.content.split())
        return f'{self.text} – {content[:60]}{"…" if len(content) > 60 else ""}'
