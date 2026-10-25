"""Zestawy danych do testów procesu tekstów."""
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.edit_versions import version_of
from fantazmaty.testing import create_member
from people import roles
from people.models import Person
from texts.models import Anthology, Text
from workflow.admin_status import set_admin_status
from workflow.models import WorkflowStage
from workflow.services import (claim_ready_for_editing, claim_stage, complete_stage, current_stage_queryset,
                               resume_editing, send_to_first_verification, send_to_second_verification)

User = get_user_model()
StageType = WorkflowStage.StageType


def start_reserved_first_verification(text, user, started_at=None):
    """Rozpoczęcie oczekującej pierwszej weryfikacji – jak przycisk „Rozpocznij” na stronie tekstu."""
    from django.core.exceptions import ValidationError
    from core.services.texts import start_assigned_stage
    stage = WorkflowStage.objects.current_cycle().filter(
        text=text, stage_type=StageType.FIRST_VERIFICATION, is_completed=False, started_at__isnull=True,
    ).order_by('-pk').first()
    if stage is None:
        raise ValidationError('Pierwsza weryfikacja nie oczekuje na rozpoczęcie.')
    return start_assigned_stage(user=user, stage_id=stage.pk, started_at=started_at or timezone.localdate())


class WorkflowTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.editor = create_member("redaktor", roles.EDITOR)
        cls.other_editor = create_member("drugi_redaktor", roles.EDITOR)
        cls.verifier_1 = create_member("weryfikator_1", roles.VERIFIER)
        cls.verifier_2 = create_member("weryfikator_2", roles.VERIFIER)
        cls.proofreader = create_member("korektor", roles.PROOFREADER)
        cls.other_proofreader = create_member("drugi_korektor", roles.PROOFREADER)
        cls.coordinator = create_member("koordynator", roles.COORDINATOR)
        cls.proofreading_coordinator = create_member("koordynator_korekty", roles.PROOFREADING_COORDINATOR)

        cls.superuser = User.objects.create_superuser(
            username="superadmin",
            email="superadmin@example.com",
            password="Testowe-haslo-123!",
        )

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.text = Text.objects.create(
            title="Tekst do testów workflow",
            length=25000,
        )
        self.initial_stage = WorkflowStage.objects.create(
            text=self.text,
            workflow_cycle=1,
            stage_type=StageType.READY_FOR_EDITING,
        )

    def stage(self, stage_type):
        return current_stage_queryset(self.text).get(
            stage_type=stage_type,
            is_completed=False,
        )

    def begin_editing(self):
        return claim_ready_for_editing(
            self.text,
            self.editor,
            started_at=self.today,
        )

    def finish_first_verification(self):
        self.begin_editing()
        claim_stage(
            self.text,
            StageType.FIRST_VERIFICATION,
            self.verifier_1,
        )
        send_to_first_verification(
            self.text,
            self.editor,
            ended_at=self.today,
        )
        stage = start_reserved_first_verification(
            self.text,
            self.verifier_1,
            started_at=self.today,
        )
        complete_stage(stage, self.verifier_1, self.today)

    def finish_second_verification(self):
        self.finish_first_verification()
        resume_editing(
            self.text,
            self.editor,
            started_at=self.today,
        )
        send_to_second_verification(
            self.text,
            self.editor,
            started_at=self.today,
        )
        stage = claim_stage(
            self.text,
            StageType.SECOND_VERIFICATION,
            self.verifier_2,
        )
        complete_stage(stage, self.verifier_2, self.today)


class StatusAssignmentFixtures(TestCase):
    """Superuser „admin”, dwie osoby zespołu bez ról („member” – Jan Test, „other” – Anna Test), antologia
    „Test” z tekstem „Tekst”; dane wspólne dla klasy, dziennik aktywności w katalogu tymczasowym."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin', 'admin@example.com', 'test')
        cls.member = User.objects.create_user('member', 'member@example.com', 'test')
        Person.objects.create(user=cls.member, first_name='Jan', last_name='Test', email=cls.member.email)
        cls.other = User.objects.create_user('other', 'other@example.com', 'test')
        Person.objects.create(user=cls.other, first_name='Anna', last_name='Test', email=cls.other.email)
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.today = timezone.localdate()

    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)

    def status(self, kind, text=None):
        text = text or self.text
        return set_admin_status(text.pk, kind, self.admin, version_of(text))
