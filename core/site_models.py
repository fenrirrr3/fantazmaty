"""Strona WWW: działy ekipy publikowane na fantazmaty.pl (zasilane z CMS-a przez endpoint z kluczem)."""
from django.core.exceptions import ValidationError
from django.db import models

from core.edit_versions import VersionedQuerySet
from core.site_note import clean_note

DEFAULT_PERSON = 'Dawid Wiktorski'


class SiteTeamSection(models.Model):
    """Dział na stronie „Ekipa”. Kotwica na stronie to ``ekipa-<slug>``."""
    objects = VersionedQuerySet.as_manager()
    slug = models.SlugField('identyfikator', unique=True,
                            help_text='Kotwica na stronie: ekipa-<identyfikator>. Małe litery, cyfry i myślniki.')
    title = models.CharField('tytuł', max_length=100)
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    is_enabled = models.BooleanField('włączony', default=True,
                                     help_text='Wyłączony dział nie trafia na stronę.')
    show_count = models.BooleanField('liczba osób w nagłówku', default=True)
    coordinator_roles = models.ManyToManyField('people.Role', blank=True, related_name='+',
                                               verbose_name='role koordynatorów',
                                               help_text='Osoby z tymi rolami trafiają do linii „Koordynacja”.')
    note = models.TextField('notka', blank=True,
                            help_text='Akapit zamiast listy osób (np. Ilustracje). Dozwolone znaczniki: '
                                      '<a href="https://…">, <strong>, <em>; pozostałe są usuwane.')
    in_navigation = models.BooleanField('w nawigacji „Przejdź do działu”', default=True)
    navigation_label = models.CharField('etykieta w nawigacji', max_length=100, blank=True,
                                        help_text='Pusta – tytuł działu.')

    class Meta:
        verbose_name = 'dział na stronie WWW'
        verbose_name_plural = 'Strona WWW – działy ekipy'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title

    @property
    def anchor(self):
        return f'ekipa-{self.slug}'

    def clean(self):
        super().clean()
        self.note = clean_note(self.note)


class SiteTeamGroup(models.Model):
    """Podgrupa listy działu: z ról (automatycznie) albo z ręcznie wpisanych nazwisk."""
    objects = VersionedQuerySet.as_manager()

    class Source(models.TextChoices):
        ROLES = 'roles', 'z ról'
        MANUAL = 'manual', 'ręcznie'

    section = models.ForeignKey(SiteTeamSection, on_delete=models.CASCADE, related_name='groups',
                                verbose_name='dział')
    title = models.CharField('tytuł podgrupy', max_length=100, blank=True,
                             help_text='Pusty przy jednej podgrupie – lista bez nagłówka.')
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    source = models.CharField('źródło', max_length=10, choices=Source.choices, default=Source.ROLES)
    roles = models.ManyToManyField('people.Role', blank=True, related_name='+', verbose_name='role',
                                   help_text='Aktywne, niezewnętrzne osoby z którąkolwiek z ról.')
    manual_names = models.TextField('nazwiska', blank=True,
                                    help_text='Jedno imię i nazwisko w wierszu, w podanej kolejności.')

    class Meta:
        verbose_name = 'podgrupa działu'
        verbose_name_plural = 'podgrupy działu'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title or f'{self.section} – lista'

    @property
    def manual_list(self):
        return [line.strip() for line in self.manual_names.splitlines() if line.strip()]

    def clean(self):
        super().clean()
        if self.source == self.Source.MANUAL and not self.manual_list:
            raise ValidationError({'manual_names': 'Ręczna podgrupa wymaga co najmniej jednego nazwiska.'})


class SiteTeamSettings(models.Model):
    """Ustawienia strony „Ekipa” (jeden rekord, pk=1) i ślad ostatniego pobrania przez stronę."""
    objects = VersionedQuerySet.as_manager()
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    lead_editor = models.CharField('redaktor prowadzący', max_length=200, default=DEFAULT_PERSON)
    site_person = models.CharField('strona internetowa', max_length=200, default=DEFAULT_PERSON)
    last_fetched_at = models.DateTimeField('ostatnie pobranie przez stronę', null=True, blank=True, editable=False)
    last_fetch_version = models.CharField('wersja ostatniego pobrania', max_length=40, blank=True, editable=False)

    class Meta:
        verbose_name = 'ustawienia strony WWW'
        verbose_name_plural = 'Strona WWW – ustawienia'
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='single_site_team_settings')]

    def __str__(self):
        return 'Strona WWW – ekipa'

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]
