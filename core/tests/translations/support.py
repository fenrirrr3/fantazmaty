"""Wspólne dane testów tłumaczeń."""
from tempfile import TemporaryDirectory

from django.utils import timezone

from authors.models import Author
from core.models import AudioContributor, Recruitment
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustrator
from texts.models import Anthology, ForeignAuthor, Review, Text, Translator
from workflow.completed_import import import_completed_workflow
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class TranslatedBookData:
    """Antologia tłumaczona z tekstem w redakcji obok zwykłej antologii z krajowym autorem."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('tr-admin')
        cls.member = create_member('tr-member', 'Redaktor')
        cls.manager = create_member('tr-manager', 'Koordynator redakcji')
        cls.normal = Anthology.objects.create(title='Księga krajowa', has_illustrations=True)
        cls.book = Anthology.objects.create(title='TRANSLATED UNIQUE ANTHOLOGY', is_translated=True,
                                            has_illustrations=True)
        cls.author = ForeignAuthor.objects.create(first_name='Autor', last_name='Oryginału',
                                                  email='author-private@example.test')
        cls.translator = Translator.objects.create(first_name='Anna', last_name='Żak',
                                                   email='translator-private@example.test')
        cls.other = Translator.objects.create(first_name='Beata', last_name='Adamska', email='')
        cls.text = Text.objects.create(title='TRANSLATED UNIQUE STORY', length=500, anthology=cls.book)
        cls.text.translation.foreign_authors.add(cls.author)
        cls.text.translation.translators.add(cls.translator)
        cls.regular = Text.objects.create(title='Tekst zwykły', length=100, anthology=cls.normal)
        cls.domestic = Author.objects.create(first_name='Polski', last_name='Autor', email=None)
        cls.regular.authors.add(cls.domestic)
        assignment = WorkflowRoleAssignment.objects.create(text=cls.text, role='editor', assigned_to=cls.member)
        cls.stage = WorkflowStage.objects.create(text=cls.text, stage_type='editing', assignment=assignment,
                                                 started_at=timezone.localdate())
        cls.review = Review.objects.create(title='TRANSLATED UNIQUE REVIEW', anthology=cls.book, length=500,
                                           author_first_name='Autor', author_last_name='Oryginału',
                                           email='review-private@example.test')
        # Kontakty spoza procesu tekstów: strony antologii i pulpit renderują się także z nimi.
        AudioContributor.objects.create(name='Nawigator lektor', email='secret-audio@example.test')
        Illustrator.objects.create(first_name='Nawigator', last_name='Plastyk', is_active=False)
        Recruitment.objects.create(first_name='Nawigator', last_name='Kandydat', email='secret-candidate@example.test')

    def setUp(self):
        super().setUp()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        setting = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.client.force_login(self.admin)


class CompletedTranslationData:
    """Ukończony przekład i ukończony tekst krajowy z historią pracy jednego redaktora."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('translation-admin')
        cls.member = create_member('translation-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Przekłady ukończone', is_translated=True)
        cls.normal_book = Anthology.objects.create(title='Polskie ukończone')
        cls.foreign = ForeignAuthor.objects.create(first_name='Jane', last_name='Żak', pseudonym='JZ')
        # Ten sam klucz główny u autora krajowego i zagranicznego – filtry nie mogą ich mylić.
        cls.domestic = Author.objects.create(pk=cls.foreign.pk, first_name='Polska', last_name='Autorka',
                                             email=None)
        cls.translator = Translator.objects.create(first_name='Anna', last_name='Kowalska', language='francuski',
                                                   email='private@example.test')
        cls.text = Text.objects.create(title='Gotowy przekład', anthology=cls.book, length=20)
        cls.text.translation.foreign_authors.add(cls.foreign)
        cls.text.translation.translators.add(cls.translator)
        cls.normal = Text.objects.create(title='Gotowy krajowy', anthology=cls.normal_book, length=20)
        cls.normal.authors.add(cls.domestic)
        for text in (cls.text, cls.normal):
            import_completed_workflow(text_id=text.pk, stages=[
                {'stage_type': 'editing', 'assigned_to_id': cls.member.pk},
                {'stage_type': 'editing', 'assigned_to_id': cls.member.pk}], next_stage='ready',
                preserve_executions=True)
