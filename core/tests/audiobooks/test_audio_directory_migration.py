"""Migracja core 0048 (lektorzy i montaż wyłącznie w spisie): osoby wpisane przy audiobooku bez wpisu spisu
dostają wpis – istniejący (ten sam e-mail albo jednoznacznie to samo imię i nazwisko) albo nowy.

Test zostaje do czasu wdrożenia migracji. Baza jest cofana raz dla całej klasy do stanu sprzed migracji.
"""
import contextlib
import io
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

BEFORE = [('core', '0047_initial_replacements')]
link_typed_people = import_module('core.migrations.0048_audio_directory_without_accounts').link_typed_people


def migrate_to_latest():
    executor = MigrationExecutor(connection)
    with contextlib.redirect_stdout(io.StringIO()):
        executor.migrate(executor.loader.graph.leaf_nodes())


@tag('migration', 'slow')
class AudioDirectoryMigrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(migrate_to_latest)
        MigrationExecutor(connection).migrate(BEFORE)
        cls.old = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps

    def test_typed_people_are_linked_to_directory_entries(self):
        get = self.old.get_model
        Contributor, Audiobook = get('core', 'AudioContributor'), get('core', 'Audiobook')
        book = get('texts', 'Anthology').objects.create(title='Antologia migracji audio')
        by_email = Contributor.objects.create(name='Inny Zapis', email='ewa@example.test')
        by_name = Contributor.objects.create(name='Łucja Lektorka')
        Contributor.objects.create(name='Jan Dwa', email='jan1@example.test')
        Contributor.objects.create(name='Jan Dwa', email='jan2@example.test')
        cases = {
            # opis: (lektor, e-mail, oczekiwany wpis – None oznacza nowy)
            'same email': ('Ewa Głos', 'EWA@example.test', by_email),
            'same name ignoring case and diacritics': ('lucja  LEKTORKA', '', by_name),
            'unknown person': ('Nowa Osoba', 'nowa@example.test', None),
            'several namesakes': ('Jan Dwa', '', None),
        }
        audio = {}
        for i, (case, (name, email, _)) in enumerate(cases.items()):
            text = get('texts', 'Text').objects.create(title=f'Nagranie {i}', anthology=book, length=1)
            audio[case] = Audiobook.objects.create(text=text, narrator_name=name, narrator_email=email)
        before = Contributor.objects.count()
        with contextlib.redirect_stdout(io.StringIO()):
            link_typed_people(self.old, None)
            link_typed_people(self.old, None)  # idempotentna
        self.assertEqual(Contributor.objects.count(), before + 2)
        for case, (name, email, expected) in cases.items():
            with self.subTest(case):
                stored = Audiobook.objects.select_related('narrator_contact').get(pk=audio[case].pk)
                if expected is None:
                    self.assertEqual((stored.narrator_contact.name, stored.narrator_contact.email),
                                     (' '.join(name.split()), email.lower()))
                else:
                    self.assertEqual(stored.narrator_contact_id, expected.pk)
                self.assertEqual(stored.narrator_name, stored.narrator_contact.name)
