"""Migracja core 0071 (rekrutacja bez pola „dział”): zgłoszenie bez rozpoznanej roli dostaje dawny dział jako rolę
(albo „Inny pomysł”), a cofnięcie odtwarza dział z jedynej roli.

Test zostaje do czasu wdrożenia migracji. Baza jest cofana do stanu sprzed migracji tylko dla tej klasy.
"""
import contextlib
import io

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

BEFORE = [('core', '0070_useractivity_created_at_default')]
AFTER = [('core', '0071_recruitment_remove_department')]


def migrate(targets):
    executor = MigrationExecutor(connection)
    with contextlib.redirect_stdout(io.StringIO()):
        executor.migrate(targets)
    return MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps


def migrate_to_latest():
    migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@tag('migration', 'slow')
class RecruitmentDepartmentMigrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(migrate_to_latest)

    def test_department_becomes_role_and_back(self):
        """Puste lub nierozpoznane role: dział, jeśli to znana rola, inaczej „other”; rozpoznane role zostają.
        Cofnięcie: dział = jedyna rola, jeśli była działem, inaczej „other”."""
        old = migrate(BEFORE).get_model('core', 'Recruitment')
        cases = {
            # opis: (role przed, dział, role po migracji, dział po cofnięciu)
            'mail spoza formularza – Inny pomysł': ([], 'other', ['other'], 'other'),
            'ręcznie, dział redakcji': ([], 'editors', ['editors'], 'editors'),
            'ręcznie, dział spoza formularza': ([], 'sound', ['sound'], 'sound'),
            'nierozpoznana rola': (['nieznana'], 'designers', ['designers'], 'designers'),
            'nieznany dział': ([], 'dawny', ['other'], 'other'),
            'rola z formularza bez działu': (['promotion'], 'editors', ['promotion'], 'other'),
            'kilka ról': (['editors', 'proofreaders'], 'other', ['editors', 'proofreaders'], 'other'),
        }
        ids = {case: old.objects.create(email=f'{i}@example.test', mail_roles=roles, department=department).pk
               for i, (case, (roles, department, *_)) in enumerate(cases.items())}
        new = migrate(AFTER).get_model('core', 'Recruitment')
        self.assertNotIn('department', {field.name for field in new._meta.get_fields()})
        roles = dict(new.objects.values_list('pk', 'mail_roles'))
        old = migrate(BEFORE).get_model('core', 'Recruitment')
        departments = dict(old.objects.values_list('pk', 'department'))
        for case, (*_, expected_roles, expected_department) in cases.items():
            with self.subTest(case):
                self.assertEqual(roles[ids[case]], expected_roles)
                self.assertEqual(departments[ids[case]], expected_department)
