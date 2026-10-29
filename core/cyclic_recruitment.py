"""Nabory cykliczne (Fantazmaty, nanoFantazje): stale otwarte nabory z kolejnymi tomami jako osobnymi antologiami.

Tom należy do naboru, gdy tytuł antologii zaczyna się od nazwy naboru, a po niej jest koniec tytułu, spacja albo
numer („Fantazmaty 6”, „nanoFantazje 1.0”) – numer tomu nie ma znaczenia. Przy zapisie zgłoszenia do tomu naboru
cyklicznego CMS sprawdza, kiedy ten sam autor przysłał poprzedni tekst do dowolnego tomu tego naboru; jeśli przed
upływem ustawionego odstępu – pokazuje ostrzeżenie (texts.services.get_review_submission_warnings).
"""
import calendar
import datetime
import re

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.edit_versions import VersionedQuerySet


class CyclicRecruitment(models.Model):
    class Unit(models.TextChoices):
        WEEKS = 'weeks', 'tygodni'
        MONTHS = 'months', 'miesięcy'

    objects = VersionedQuerySet.as_manager()
    name = models.CharField('nazwa naboru', max_length=50, unique=True, editable=False)
    interval_value = models.PositiveSmallIntegerField(
        'odstęp od ostatniego zgłoszenia', null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(520)],
        help_text='Puste – bez sprawdzania odstępu.')
    interval_unit = models.CharField('jednostka', max_length=10, choices=Unit.choices, default=Unit.MONTHS)

    class Meta:
        ordering = ('name',)
        verbose_name = 'nabór cykliczny'
        verbose_name_plural = 'nabory cykliczne'

    def __str__(self):
        return self.name

    def matches(self, anthology_title):
        """Czy antologia (albo nazwa naboru z tematu maila) należy do tego naboru – bez względu na numer tomu."""
        return bool(re.match(rf'\s*{re.escape(self.name)}(?=$|[\s\d.,:;–-])', anthology_title or '', re.IGNORECASE))

    @property
    def interval_label(self):
        if not self.interval_value:
            return 'bez sprawdzania'
        return f'{self.interval_value} {interval_unit_label(self.interval_value, self.interval_unit)}'

    def window_start(self, today):
        """Pierwszy dzień, od którego wcześniejsze zgłoszenie jest „za świeże” (dzisiaj minus odstęp)."""
        if self.interval_unit == self.Unit.WEEKS:
            return today - datetime.timedelta(weeks=self.interval_value)
        month = today.month - 1 - self.interval_value
        year, month = today.year + month // 12, month % 12 + 1
        return today.replace(year=year, month=month, day=min(today.day, calendar.monthrange(year, month)[1]))


def interval_unit_label(value, unit):
    """Polska odmiana: 1 tydzień / 2 tygodnie / 5 tygodni, 1 miesiąc / 3 miesiące / 6 miesięcy."""
    from core.inflection import count_text
    forms = ('tydzień', 'tygodnie', 'tygodni') if unit == CyclicRecruitment.Unit.WEEKS else (
        'miesiąc', 'miesiące', 'miesięcy')
    return count_text(value, forms).split(' ', 1)[1]


def recruitment_for(anthology_title, recruitments=None):
    """Nabór cykliczny tomu (najdłuższa pasująca nazwa – „nanoFantazje” przed ewentualnym „Fantazje”) albo None."""
    recruitments = CyclicRecruitment.objects.all() if recruitments is None else recruitments
    matching = [item for item in recruitments if item.matches(anthology_title)]
    return max(matching, key=lambda item: len(item.name)) if matching else None


def recent_submission_warning(recruitment, candidates, today=None):
    """Ostrzeżenie, gdy któreś z wcześniejszych zgłoszeń autora (candidates: Review z antologią) do tego naboru
    jest nowsze niż ustawiony odstęp; '' – brak ostrzeżenia."""
    if recruitment is None or not recruitment.interval_value:
        return ''
    today = today or timezone.localdate()
    start = recruitment.window_start(today)
    recent = [review for review in candidates
              if review.created_at and review.created_at > start and review.anthology_id
              and recruitment.matches(review.anthology.title)]
    if not recent:
        return ''
    last = max(recent, key=lambda review: (review.created_at, review.pk))
    from core.inflection import count_text
    days = (today - last.created_at).days
    elapsed = 'dzisiaj' if days == 0 else count_text(days, ('dzień', 'dni', 'dni')) + ' temu'
    return (f'Nabór cykliczny {recruitment.name}: autor przysłał poprzedni tekst {elapsed} '
            f'({last.created_at:%d.%m.%Y}, #{last.pk} „{last.title}”, {last.anthology.title}), a minimalny odstęp '
            f'to {recruitment.interval_label}. Sprawdź przed dodaniem.')


def settings_forms(data=None):
    """Formularze odstępu dla każdego naboru cyklicznego (prefiks = id) na stronie „Dodaj do recenzji”."""
    from django import forms

    class IntervalForm(forms.ModelForm):
        class Meta:
            model = CyclicRecruitment
            fields = ('interval_value', 'interval_unit')
            labels = {'interval_value': 'Odstęp', 'interval_unit': 'Jednostka'}
            widgets = {'interval_value': forms.NumberInput(attrs={'min': 1, 'max': 520, 'inputmode': 'numeric'})}
            error_messages = {'interval_value': {'min_value': 'Podaj liczbę od 1 do 520 albo zostaw puste.',
                                                 'max_value': 'Podaj liczbę od 1 do 520 albo zostaw puste.',
                                                 'invalid': 'Podaj liczbę od 1 do 520 albo zostaw puste.'}}

    return [IntervalForm(data, instance=item, prefix=f'cyclic-{item.pk}') for item in CyclicRecruitment.objects.all()]
