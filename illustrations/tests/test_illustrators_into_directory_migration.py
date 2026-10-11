"""Migracja illustrations 0009 (wszyscy ilustratorzy w spisie): dane ilustratorów z osób zespołu
i ręcznie wpisani ilustratorzy trafiają do spisu bez nadpisywania istniejących wpisów.

Test zostaje do czasu wdrożenia migracji. Baza jest cofana raz dla całej klasy (do stanu sprzed
migracji), a każdy test wywołuje funkcję migracji na modelach historycznych.
"""
import contextlib
import io
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

BEFORE = [('illustrations', '0008_illustrator_pseudonym'), ('people', '0015_retire_generic_coordinator')]
forward = import_module('illustrations.migrations.0009_illustrators_into_directory').forward


def migrate_to_latest():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


@tag('migration', 'slow')
class IllustratorsIntoDirectoryMigrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(migrate_to_latest)
        MigrationExecutor(connection).migrate(BEFORE)
        # Rzeczywisty stan bazy: inne aplikacje zachowują swoje niezależne późniejsze migracje.
        cls.old = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps

    def setUp(self):
        get = self.old.get_model
        self.Person, self.Role = get('people', 'Person'), get('people', 'Role')
        self.Illustrator, self.Illustration = get('illustrations', 'Illustrator'), get('illustrations', 'Illustration')
        self.role = self.Role.objects.create(name='Ilustrator')
        self.book = get('texts', 'Anthology').objects.create(title='Antologia migracji', has_illustrations=True)
        self.Text = get('texts', 'Text')

    def run_migration(self):
        with contextlib.redirect_stdout(io.StringIO()):
            forward(self.old, None)

    def person(self, first, last, email=None, *, role=True, **fields):
        person = self.Person.objects.create(first_name=first, last_name=last, email=email, **fields)
        if role:
            person.roles.add(self.role)
        return person

    def manual(self, name, email='', status='delivered'):
        text = self.Text.objects.create(title=f'Tekst {name}', anthology=self.book, length=10)
        illustration, _ = self.Illustration.objects.get_or_create(text=text)
        self.Illustration.objects.filter(pk=illustration.pk).update(
            manual_illustrator_name=name, manual_illustrator_email=email, status=status)
        return illustration

    def entries(self, first, last=''):
        return self.Illustrator.objects.filter(first_name=first, last_name=last)

    def snapshot(self):
        return (sorted(self.Illustrator.objects.values_list(
                    'pk', 'first_name', 'last_name', 'email', 'portfolio', 'preferences', 'covers', 'is_active')),
                sorted(self.Illustration.illustrators.through.objects.values_list('illustration_id', 'illustrator_id')))

    def test_team_illustrator_data_fills_only_empty_fields_or_creates_entry(self):
        existing = self.Illustrator.objects.create(first_name='Anna', last_name='Spisowa', email='ANNA@example.test',
                                                   preferences='Ze spisu')
        by_name = self.Illustrator.objects.create(first_name='Bartek', last_name='Nowy', email='bartek@example.test',
                                                  portfolio='https://example.test/stare')
        self.person('Anna', 'Zespołowa', 'anna@example.test', illustrator_portfolio='https://example.test/anna',
                    illustrator_preferences='Z konta', illustrator_covers=True)
        self.person('Bartek', 'Nowy', illustrator_portfolio='https://example.test/nowe', illustrator_preferences='Smoki')
        self.person('Cezary', 'Aktywny', 'cezary@example.test', illustrator_portfolio='https://example.test/c',
                    illustrator_active=True)
        self.person('Dorota', 'Bezroli', role=False, illustrator_preferences='Portrety', illustrator_active=True)
        self.person('Edward', 'Pominięty', 'edward@example.test', role=False)
        self.person('Ewa', 'Nieaktywna', illustrator_active=False)
        accounts = self.Person.objects.count()
        self.run_migration()

        existing.refresh_from_db()
        self.assertEqual((existing.first_name, existing.last_name, existing.email), ('Anna', 'Spisowa', 'ANNA@example.test'))
        self.assertEqual((existing.portfolio, existing.preferences, existing.covers),
                         ('https://example.test/anna', 'Ze spisu', True))
        by_name.refresh_from_db()
        self.assertEqual((by_name.portfolio, by_name.preferences, by_name.email),
                         ('https://example.test/stare', 'Smoki', 'bartek@example.test'))
        new = self.entries('Cezary', 'Aktywny').get()
        self.assertEqual((new.email, new.portfolio, new.is_active),
                         ('cezary@example.test', 'https://example.test/c', True))
        # Dane ilustratora bez roli: wpis powstaje, ale nieaktywny; bez roli i bez danych – pominięty.
        self.assertFalse(self.entries('Dorota', 'Bezroli').get().is_active)
        self.assertFalse(self.entries('Edward', 'Pominięty').exists())
        self.assertFalse(self.entries('Ewa', 'Nieaktywna').get().is_active)
        self.assertEqual(self.Illustrator.objects.count(), 5)
        self.assertEqual(self.Person.objects.count(), accounts)
        before = self.snapshot()
        self.run_migration()
        self.assertEqual(self.snapshot(), before)

    def test_manual_illustrators_join_directory_and_are_assigned(self):
        graphos = self.Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', pseudonym='Graphos')
        mailed = self.Illustrator.objects.create(first_name='Inna', last_name='Osoba', email='wspolny@example.test')
        for _ in range(2):
            self.Illustrator.objects.create(first_name='Jan', last_name='Kowalski')
        by_pseudonym = self.manual(' graphos ')
        by_email = self.manual('Zupełnie Inne Nazwisko', 'WSPOLNY@example.test', 'assigned')
        active = self.manual('Zofia  Ręczna', 'zofia@example.test', 'in_corrections')
        same_name = self.manual('zofia ręczna')
        delivered = self.manual('Stary Wpis', status='delivered')
        ambiguous = self.manual('Jan Kowalski', status='assigned')
        self.run_migration()

        def artists(illustration):
            return list(illustration.illustrators.values_list('pk', flat=True))

        self.assertEqual(artists(by_pseudonym), [graphos.pk])
        # Ten sam e-mail co wpis w spisie: przypisanie do niego zamiast drugiego wpisu z tym adresem.
        self.assertEqual(artists(by_email), [mailed.pk])
        self.assertEqual(self.Illustrator.objects.filter(email__iexact='wspolny@example.test').count(), 1)
        zofia = self.entries('Zofia', 'Ręczna').get()
        self.assertEqual((zofia.email, zofia.is_active), ('zofia@example.test', True))
        self.assertEqual(artists(active), [zofia.pk])
        self.assertEqual(artists(same_name), [zofia.pk])
        old = self.entries('Stary', 'Wpis').get()
        self.assertEqual((old.email, old.is_active, artists(delivered)), (None, False, [old.pk]))
        # Niejednoznaczne imię i nazwisko: nowy wpis, istniejących się nie zgaduje.
        self.assertEqual(self.entries('Jan', 'Kowalski').count(), 3)
        self.assertTrue(self.entries('Jan', 'Kowalski').get(pk=artists(ambiguous)[0]).is_active)
        self.assertFalse(self.Illustration.objects.exclude(manual_illustrator_name='').exists())
        self.assertFalse(self.Illustration.objects.exclude(manual_illustrator_email='').exists())
        before = self.snapshot()
        self.run_migration()
        self.assertEqual(self.snapshot(), before)

    def test_team_member_and_manual_entry_with_same_email_share_one_entry(self):
        """Ten sam adres przy osobie zespołu i przy ręcznym wpisie nie łamie unikalności e-maila."""
        self.person('Anna', 'Konto', 'dzielony@example.test', illustrator_portfolio='https://example.test/a')
        illustration = self.manual('Anna Ręczna', 'Dzielony@Example.test', 'assigned')
        self.run_migration()
        entry = self.Illustrator.objects.get(email__iexact='dzielony@example.test')
        self.assertEqual((entry.first_name, entry.last_name), ('Anna', 'Konto'))
        self.assertEqual(list(illustration.illustrators.all()), [entry])

    def test_team_member_matching_several_entries_by_name_is_added_once(self):
        """Osoba zespołu bez e-maila pasująca do kilku wpisów nie dostaje nowego wpisu (także przy ponownym przebiegu)."""
        for _ in range(2):
            self.Illustrator.objects.create(first_name='Jan', last_name='Kowalski')
        self.person('Jan', 'Kowalski', illustrator_portfolio='https://example.test/jan')
        self.run_migration()
        count = self.entries('Jan', 'Kowalski').count()
        self.run_migration()
        self.assertEqual(self.entries('Jan', 'Kowalski').count(), count)
