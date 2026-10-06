"""Book-level metadata and vocabulary shared with existing text fields."""
import hashlib
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def term_key(value):
    normalized = ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


class VocabularyTerm(models.Model):
    class Kind(models.TextChoices):
        TAG = 'tag', 'Tag'
        GENRE = 'genre', 'Gatunek'

    kind = models.CharField('rodzaj', max_length=8, choices=Kind.choices)
    name = models.CharField('nazwa', max_length=255)
    key = models.CharField(max_length=64, editable=False)
    canonical = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
                                  related_name='aliases', verbose_name='nazwa docelowa')

    class Meta:
        ordering = ('kind', 'name', 'pk')
        verbose_name = 'hasło słownika'
        verbose_name_plural = 'Słownik tagów i gatunków'
        constraints = [models.UniqueConstraint(fields=('kind', 'key'), name='unique_vocabulary_term')]

    def __str__(self):
        return self.name

    def clean(self):
        self.name = ' '.join(unicodedata.normalize('NFKC', self.name or '').split())
        self.key = term_key(self.name)
        if not self.name or ',' in self.name:
            raise ValidationError({'name': 'Podaj jedną nazwę bez przecinków.'})
        if self.kind == self.Kind.GENRE and len(self.name) > 100:
            raise ValidationError({'name': 'Gatunek może mieć najwyżej 100 znaków.'})
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values('name', 'kind').first()
            if previous and (previous['name'] != self.name or previous['kind'] != self.kind):
                raise ValidationError('Zmianę nazwy wykonaj przez scalenie w słowniku, aby zaktualizować powiązane teksty.')
        if self.canonical_id and (self.canonical_id == self.pk or self.canonical.kind != self.kind or self.canonical.canonical_id):
            raise ValidationError({'canonical': 'Alias musi wskazywać nazwę główną tego samego rodzaju.'})

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class NovelProfile(models.Model):
    anthology = models.OneToOneField('texts.Anthology', on_delete=models.CASCADE, related_name='novel', verbose_name='powieść')
    authors = models.ManyToManyField('authors.Author', blank=True, verbose_name='autorzy')
    genre = models.CharField('gatunek', max_length=100, blank=True)
    tags = models.TextField('tagi', max_length=5000, blank=True)
    content_warnings = models.TextField('trigger warningi', blank=True)
    file_url = models.URLField('folder powieści', max_length=1000, blank=True)
    notes = models.TextField('ustalenia i notatki do całej powieści', blank=True)
    approved_signature = models.CharField(max_length=64, blank=True, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False)

    class Meta:
        verbose_name = 'dane powieści'
        verbose_name_plural = 'Dane powieści'

    def __str__(self):
        return self.anthology.title
