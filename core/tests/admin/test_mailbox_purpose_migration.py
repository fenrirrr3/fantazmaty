"""Migracja core 0055: zaznaczona „wysyłka plików autorom” staje się przeznaczeniem skrzynki (poza skrzynkami importu
i rekrutacji) i wraca do pola przy cofnięciu.

Test zostaje do czasu wdrożenia migracji. Baza jest cofana do stanu sprzed migracji tylko dla tej klasy.
"""
import contextlib
import io

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

BEFORE = [('core', '0054_mailbox_snapshot_and_smtp')]
AFTER = [('core', '0055_mailbox_split_mailing_purpose')]


def migrate(targets):
    executor = MigrationExecutor(connection)
    with contextlib.redirect_stdout(io.StringIO()):
        executor.migrate(targets)
    return MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps


def migrate_to_latest():
    migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@tag('migration', 'slow')
class MailboxPurposeMigrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(migrate_to_latest)

    def test_flag_becomes_purpose_and_back(self):
        """Wysyłkowe skrzynki dostają nowe przeznaczenie; „teksty”, teksty@… i rekrutacja zostają bez zmian.

        Cofnięcie przywraca zaznaczenie tylko skrzynkom wysyłkowym – skrzynki importu tracą je bezpowrotnie.
        """
        old = migrate(BEFORE).get_model('core', 'MailboxConnection')
        cases = {
            # nazwa: (przeznaczenie, login, wysyłka, przeznaczenie po migracji)
            'Wysyłka': ('submissions', 'wysylka@example.test', True, 'split_mailing'),
            'Teksty': ('submissions', 'redakcja@example.test', True, 'submissions'),
            'Import': ('submissions', 'Teksty@example.test', True, 'submissions'),
            'Rekrutacja': ('recruitment', 'rekrutacja@example.test', True, 'recruitment'),
            'Zwykła': ('submissions', 'inna@example.test', False, 'submissions'),
        }
        for name, (purpose, username, flag, _) in cases.items():
            old.objects.create(name=name, purpose=purpose, username=username, split_mailing=flag, host='imap.example.test',
                               smtp_host='smtp.example.test', encrypted_password='x')
        new = migrate(AFTER).get_model('core', 'MailboxConnection')
        self.assertEqual(dict(new.objects.values_list('name', 'purpose')),
                         {name: expected for name, (*_, expected) in cases.items()})
        old = migrate(BEFORE).get_model('core', 'MailboxConnection')
        self.assertEqual(list(old.objects.filter(split_mailing=True).values_list('name', 'purpose')),
                         [('Wysyłka', 'submissions')])
        self.assertEqual(old.objects.filter(purpose='submissions').count(), 4)
