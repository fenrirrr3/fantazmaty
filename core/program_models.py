"""Dane programów dokumentów: zapamiętane ustawienia formularzy i słownik zamian Odkurzacza."""
from django.conf import settings
from django.db import models


class ProgramPreferences(models.Model):
    """Ostatnie ustawienia formularzy programów jednej osoby.

    ``data`` ma postać {program: {pole: wartość}}; plik nigdy nie jest zapisywany.
    Wartości są sprawdzane przy odczycie (lista reguł może się zmienić).
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='program_preferences', verbose_name='konto')
    data = models.JSONField('ustawienia', default=dict, blank=True)
    updated_at = models.DateTimeField('zmieniono', auto_now=True)

    class Meta:
        verbose_name = 'ustawienia programów'
        verbose_name_plural = 'Programy – ustawienia użytkowników'

    def __str__(self):
        return f'Ustawienia programów: {self.user}'


class ReplacementEntry(models.Model):
    """Pozycja reguły „Własne zamiany słownikowe” Odkurzacza."""

    class Kind(models.TextChoices):
        WORD = 'word', 'Słowo'
        PATTERN = 'pattern', 'Wyrażenie regularne'

    kind = models.CharField('rodzaj', max_length=10, choices=Kind.choices, default=Kind.WORD)
    source = models.CharField('szukany zapis', max_length=500)
    target = models.CharField('zamiana na', max_length=200)
    is_active = models.BooleanField('aktywna', default=True)
    note = models.CharField('uwagi', max_length=300, blank=True, default='')
    position = models.PositiveIntegerField('kolejność', default=0,
                                           help_text='Mniejsza liczba – wcześniejsze wykonanie.')
    updated_at = models.DateTimeField('zmieniono', auto_now=True)

    class Meta:
        ordering = ('position', 'pk')
        verbose_name = 'pozycja słownika zamian'
        verbose_name_plural = 'Programy – słownik zamian'

    def __str__(self):
        return f'{self.source} → {self.target}'

    def as_replacement(self):
        # Wpis zapisany przez formularz przeszedł już próbę czasu wzorca – Odkurzacz jej nie powtarza.
        return {'kind': self.kind, 'source': self.source, 'target': self.target, 'checked': True}

    @classmethod
    def active_replacements(cls):
        return [entry.as_replacement() for entry in cls.objects.filter(is_active=True).order_by('position', 'pk')]
