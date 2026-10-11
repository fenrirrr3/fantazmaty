"""Wspólne dane testów interfejsu: przekład z pseudonimem autora, kontakty spoza zespołu i kandydat."""
from django.utils import timezone

from core.models import AudioContributor, Recruitment
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustrator
from texts.models import Anthology, ForeignAuthor, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class NavigationDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('nav-admin', '')
        cls.manager = create_member('nav-manager', 'Koordynator redakcji')
        cls.member = create_member('nav-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Przekłady nawigacyjne', is_translated=True)
        cls.text = Text.objects.create(title='Podróż nawigacyjna', anthology=cls.book, length=100)
        cls.author = ForeignAuthor.objects.create(first_name='Secret', last_name='Name', pseudonym='Signature')
        cls.text.translation.foreign_authors.add(cls.author)
        assignment = WorkflowRoleAssignment.objects.create(text=cls.text, role='editor', assigned_to=cls.member)
        cls.stage = WorkflowStage.objects.create(text=cls.text, stage_type='editing', assignment=assignment,
                                                started_at=timezone.localdate())
        cls.audio = AudioContributor.objects.create(name='Nawigator lektor', email='secret-audio@example.test')
        cls.artist = Illustrator.objects.create(first_name='Nawigator', last_name='Plastyk', is_active=False)
        cls.applicant = Recruitment.objects.create(first_name='Nawigator', last_name='Kandydat',
                                                   email='secret-candidate@example.test')
