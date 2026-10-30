"""Migracja workflow 0019: dodatkowe wykonania zapisane przed polem is_additional dostają znacznik i numer
wykonania liczony od prac (wyk. 2 zamiast wyk. 3); inne nieaktualne przydziały zostają bez zmian.

Test zostaje do czasu wdrożenia migracji. Baza jest cofana do stanu sprzed migracji tylko dla tej klasy.
"""
import contextlib
import datetime
import io

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

BEFORE = [('workflow', '0018_additional_execution')]
AFTER = [('workflow', '0019_mark_additional_executions')]
DAY = datetime.date(2026, 10, 1)


def migrate(targets):
    with contextlib.redirect_stdout(io.StringIO()):
        MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps


def migrate_to_latest():
    migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@tag('migration', 'slow')
class AdditionalExecutionMigrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(migrate_to_latest)

    def test_old_extra_executions_are_marked_and_renumbered(self):
        apps = migrate(BEFORE)
        User = apps.get_model('auth', 'User')
        Text = apps.get_model('texts', 'Text')
        A = apps.get_model('workflow', 'WorkflowRoleAssignment')
        S = apps.get_model('workflow', 'WorkflowStage')
        R = apps.get_model('workflow', 'WorkflowRepetition')
        users = [User.objects.create(username=f'u{i}') for i in range(6)]
        text = Text.objects.create(title='Tekst', length=100)

        def assignment(role, user, number, *, current, undated=True):
            row = A.objects.create(text=text, role=role, assigned_to=user, execution_number=number, is_current=current)
            if undated:
                A.objects.filter(pk=row.pk).update(assigned_at=None)
            return row

        def stage(kind, row, number, iteration, *, current, released=True, completed=True):
            return S.objects.create(text=text, stage_type=kind, assignment=row, execution_number=number,
                                    iteration=iteration, is_current=current, is_released=released,
                                    is_completed=completed, started_at=DAY, ended_at=DAY if completed else None)

        # W1: przekazanie pracy zostawiło dwa przydziały, jedna praca; stara wersja zapisała dodatkową jako wyk. 3.
        assignment('verifier_1', users[0], 1, current=False, undated=False)
        stage('first_verification', assignment('verifier_1', users[1], 2, current=True, undated=False), 2, 1,
              current=True)
        S.objects.filter(stage_type='first_verification').update(execution_number=1)
        old_extra = stage('first_verification', assignment('verifier_1', users[2], 3, current=False), 3, 2,
                          current=False, released=False)
        # K1: praca historyczna (etap bieżący) i wcześniejsze wykonanie sprzed powtórzenia – bez zmian.
        historical = stage('first_proofreading', assignment('proofreader_1', users[3], 1, current=False), 1, 1,
                           current=True)
        before_repeat = stage('second_proofreading', assignment('proofreader_2', users[4], 1, current=False), 1, 1,
                              current=False, released=False)
        repeated = assignment('proofreader_2', users[5], 2, current=True, undated=False)
        R.objects.create(text=text, selected_stages=['second_proofreading'], previous_stage_ids=[before_repeat.pk],
                         previous_assignment_ids=[before_repeat.assignment_id])
        stage('second_proofreading', repeated, 2, 2, current=True, completed=False)

        apps = migrate(AFTER)
        A = apps.get_model('workflow', 'WorkflowRoleAssignment')
        S = apps.get_model('workflow', 'WorkflowStage')
        self.assertEqual(list(A.objects.filter(is_additional=True).values_list('pk', flat=True)),
                         [old_extra.assignment_id])
        self.assertEqual(S.objects.get(pk=old_extra.pk).execution_number, 2)
        for unchanged in (historical, before_repeat):
            with self.subTest(stage=unchanged.stage_type):
                self.assertEqual(S.objects.get(pk=unchanged.pk).execution_number, unchanged.execution_number)
