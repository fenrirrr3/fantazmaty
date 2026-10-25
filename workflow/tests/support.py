"""Pomocniki testów procesu tekstów używane w kilku plikach tego pakietu."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.edit_versions import version_of
from core.selectors.texts import user_workflow_summary
from fantazmaty.testing.workflow import StatusAssignmentFixtures, WorkflowTestDataMixin
from people.models import Role
from texts.models import Anthology
from workflow.admin_add_stage import add_missing_stage
from workflow.import_context import importing_completed
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.read_queries import classify_work


@contextmanager
def importing():
    """Zapis jak podczas importu archiwum: pozwala utworzyć zakończony etap bez dat."""
    token = importing_completed.set(True)
    try:
        yield
    finally:
        importing_completed.reset(token)


def work_state(user, text):
    """Stan pracy osoby przy tekście: work_active, work_waiting, work_completed (jak w „Moich tekstach”)."""
    states = classify_work(user, timezone.localdate())
    return SimpleNamespace(**{'work_' + name: text.pk in ids for name, ids in states.items()})


def add_roles(user, *names):
    """Dodaje osobie zespołu role (profil osoby musi już istnieć)."""
    for name in names:
        user.person_profile.roles.add(Role.objects.get_or_create(name=name)[0])


class AnthologyWorkflowMixin(WorkflowTestDataMixin):
    """Dane procesu z tekstem w antologii – strony i podsumowania wymagają antologii."""

    def setUp(self):
        super().setUp()
        self.text.anthology = Anthology.objects.create(title='Antologia testowa')
        self.text.save()

    def work(self, user=None):
        """Klasyfikacja pracy osoby (w toku / oczekuje / zakończona) dla tekstu testowego."""
        return work_state(user or self.editor, self.text)

    def detail(self, user=None):
        self.client.force_login(user or self.editor)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def claim_form(self, response, stage):
        """Jedyny formularz „Przejmij” danego etapu na stronie."""
        forms = html.fromstring(response.content).xpath(
            '//form[@action=$url]', url=reverse('core:take_workflow_stage', args=[stage.pk]))
        self.assertEqual(len(forms), 1)
        return forms[0]


class EditingInProgressFixtures(StatusAssignmentFixtures):
    """Tekst „Zabójczyni Snów” w trakcie redakcji; obie osoby zespołu mają role redakcji,
    weryfikacji i korekty."""

    def setUp(self):
        super().setUp()
        for user in (self.member, self.other):
            add_roles(user, 'Redaktor', 'Weryfikator', 'Korektor')
        self.text.title = 'Zabójczyni Snów'
        self.text.save()
        self.editor = WorkflowRoleAssignment.objects.create(
            text=self.text, role='editor', assigned_to=self.member)
        self.editing = WorkflowStage.objects.create(
            text=self.text, stage_type='editing', assignment=self.editor,
            started_at=self.today - timedelta(days=24))

    def reserve(self, performer=None, **kwargs):
        """Rezerwacja pierwszej weryfikacji dodana w adminie."""
        return add_missing_stage(self.text.pk, self.admin, version_of(self.text),
                                 kind='first_verification', performer=performer or self.other, **kwargs)

    def close_editing(self):
        WorkflowStage.objects.filter(pk=self.editing.pk).update(is_completed=True, ended_at=self.today)

    def summary(self, user=None):
        return user_workflow_summary(user or self.member)
