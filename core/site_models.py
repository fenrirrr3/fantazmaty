"""Strona WWW: działy ekipy i katalog opowiadań szukajki publikowane na fantazmaty.pl (endpointy z kluczem)."""
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
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
    manual_coordinators = models.TextField('koordynatorzy wpisani ręcznie', blank=True,
                                           help_text='Po jednym imieniu i nazwisku w wierszu; dopisywani za osobami z ról '
                                                     '(np. koordynator bez konta w CMS-ie).')
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

    @property
    def manual_coordinator_list(self):
        return [line.strip() for line in self.manual_coordinators.splitlines() if line.strip()]

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


def term_lines(value):
    """Wiersze pola tekstowego bez pustych i powtórzeń (porównanie bez wielkości liter), w kolejności zapisu."""
    seen, result = set(), []
    for line in (value or '').splitlines():
        name = ' '.join(line.split())
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


class SiteCatalogAnthology(models.Model):
    """Antologia w katalogu szukajki (fantazmaty.pl/czytaj/szukaj/); może nie mieć odpowiednika w CMS-ie."""
    objects = VersionedQuerySet.as_manager()
    anthology = models.OneToOneField('texts.Anthology', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='site_catalog', verbose_name='antologia w CMS-ie',
                                     help_text='Powiązanie pozwala sprawdzać aktualizacje; puste dla antologii '
                                               'spoza CMS-u.')
    title = models.CharField('nazwa na stronie', max_length=255)
    url = models.URLField('adres antologii', max_length=500, blank=True, validators=[URLValidator(schemes=['https'])],
                          help_text='Strona antologii na fantazmaty.pl (https://…).')
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    is_enabled = models.BooleanField('włączona', default=True,
                                     help_text='Wyłączona antologia nie trafia do szukajki.')
    updated_at = models.DateTimeField('ostatnia zmiana', auto_now=True)

    class Meta:
        verbose_name = 'antologia w katalogu szukajki'
        verbose_name_plural = 'Szukajka WWW – katalog opowiadań'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self.url and not self.url.startswith('https://fantazmaty.pl/'):
            raise ValidationError({'url': 'Adres musi zaczynać się od https://fantazmaty.pl/.'})


class SiteCatalogStory(models.Model):
    """Opowiadanie w katalogu szukajki: tytuł i autor jak na stronie, gatunki i tagi po jednym w wierszu."""
    objects = VersionedQuerySet.as_manager()
    anthology = models.ForeignKey(SiteCatalogAnthology, on_delete=models.CASCADE, related_name='stories',
                                  verbose_name='antologia w katalogu')
    text = models.OneToOneField('texts.Text', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='site_catalog_story', verbose_name='tekst w CMS-ie')
    title = models.CharField('tytuł', max_length=255)
    author = models.CharField('autor', max_length=255, blank=True,
                              help_text='Tak jak na stronie, np. „Kazimierz Kyrcz Jr, Michał Walczak”.')
    genres = models.TextField('gatunki', blank=True, help_text='Jeden gatunek w wierszu.')
    tags = models.TextField('tagi', blank=True, help_text='Jeden tag w wierszu.')
    position = models.PositiveSmallIntegerField('kolejność', default=0)

    class Meta:
        verbose_name = 'opowiadanie w katalogu'
        verbose_name_plural = 'opowiadania w katalogu'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title

    @property
    def genre_list(self):
        return term_lines(self.genres)

    @property
    def tag_list(self):
        return term_lines(self.tags)


class SiteTeamSettings(models.Model):
    """Ustawienia strony WWW (jeden rekord, pk=1): osoby ze strony „Ekipa”, adres szukajki i ślady pobrań."""
    objects = VersionedQuerySet.as_manager()
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    lead_editor = models.CharField('redaktor prowadzący', max_length=200, default=DEFAULT_PERSON)
    site_person = models.CharField('strona internetowa', max_length=200, default=DEFAULT_PERSON)
    last_fetched_at = models.DateTimeField('ostatnie pobranie ekipy przez stronę', null=True, blank=True,
                                           editable=False)
    last_fetch_version = models.CharField('wersja ostatniego pobrania ekipy', max_length=40, blank=True,
                                          editable=False)
    catalog_page_url = models.URLField('adres szukajki na stronie', max_length=500, blank=True,
                                       validators=[URLValidator(schemes=['https'])],
                                       help_text='Np. https://fantazmaty.pl/czytaj/szukaj/ – daje odnośnik '
                                                 '„Odśwież stronę”. Puste – bez odnośnika.')
    catalog_fetched_at = models.DateTimeField('ostatnie pobranie katalogu przez stronę', null=True, blank=True,
                                              editable=False)
    catalog_fetch_version = models.CharField('wersja ostatniego pobrania katalogu', max_length=40, blank=True,
                                             editable=False)

    class Meta:
        verbose_name = 'ustawienia strony WWW'
        verbose_name_plural = 'Strona WWW – ustawienia'
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='single_site_team_settings')]

    def __str__(self):
        return 'Strona WWW – ustawienia'

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def catalog_refresh_url(self):
        """Adres szukajki z ``?fz_odswiez=1`` (pobranie danych od razu przez zalogowanego administratora)."""
        if not self.catalog_page_url:
            return ''
        return self.catalog_page_url + ('&' if '?' in self.catalog_page_url else '?') + 'fz_odswiez=1'
