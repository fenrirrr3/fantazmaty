"""Social Media: planowanie wpisów promocyjnych o opowiadaniach i innych wpisów."""
from django.conf import settings
from django.core.validators import URLValidator
from django.db import models

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
