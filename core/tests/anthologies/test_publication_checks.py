"""Kontrole przed wydaniem i spójności danych: lista kontrolna antologii, stopka okładki, możliwe duplikaty i zapisy kolejek."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from authors.models import Author
from core.models import AnthologyCorrection
from core.supervision import anthology_checklist, anthology_credits, duplicate_candidates, folded, integrity_issues
from fantazmaty.testing import create_member
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment, WorkflowStage


def labels(rows, detail=None):
    return [row['label'] for row in rows if detail is None or detail in row['detail']]


class AnthologyChecklistTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Do wydania')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email=None)

    def text(self, title, stage_type):
        text = Text.objects.create(title=title, anthology=self.book, length=10)
        text.authors.add(self.author)
        text.workflow_stages.update(is_current=False)
        WorkflowStage.objects.create(text=text, stage_type=stage_type)
        return text

    def test_withdrawn_texts_are_skipped_and_book_without_texts_is_reported(self):
        self.text('Wycofany', 'withdrawn')
        rows = anthology_checklist(self.book)
        self.assertNotIn('Wycofany', [row['detail'] for row in rows])
        self.assertIn('Brak tekstów do wydania', labels(rows))
        self.text('Gotowy', 'ready')
        rows = anthology_checklist(self.book)
        self.assertNotIn('Brak tekstów do wydania', labels(rows))
        self.assertEqual(labels(rows, 'Gotowy'), [])

    def test_open_corrections_block_release_until_applied_or_rejected(self):
        self.text('Gotowy', 'ready')
        for status in AnthologyCorrection.Status.values:
            AnthologyCorrection.objects.create(anthology=self.book, story_title=f'Uwaga {status}', status=status)
        corrections = [row['detail'] for row in anthology_checklist(self.book) if row['label'] == 'Uwaga wymaga obsługi']
        self.assertEqual(sorted(corrections), ['Uwaga accepted – Przyjęta', 'Uwaga checking – W sprawdzaniu',
                                               'Uwaga new – Nowa'])

    def test_cover_credit_never_shows_an_email_address(self):
        Anthology.objects.filter(pk=self.book.pk).update(cover_author='ktos@example.test')
        self.book.refresh_from_db()
        cover = [row for row in anthology_credits(self.book) if row['role'] == 'Okładka']
        self.assertEqual([row['name'] for row in cover], ['Brak danych wykonawcy'])

    def test_ready_book_accepts_only_closed_texts(self):
        """Antologię oznaczy się jako gotową dopiero, gdy każdy tekst jest gotowy lub wycofany; do gotowej
        nie przeniesie się niedokończonego tekstu ani nie doda nowego. Gotowej nie sprawdza się ponownie."""
        self.text('Gotowy', 'ready')
        self.text('Wycofany', 'withdrawn')
        open_text = self.text('W redakcji', 'editing')
        self.book.status = Anthology.Status.READY
        with self.assertRaisesMessage(ValidationError, f'Niezakończone teksty: W redakcji (#{open_text.pk}).'):
            self.book.clean()
        open_text.workflow_stages.update(stage_type='ready')
        self.book.clean()
        self.book.save()

        draft = Anthology.objects.create(title='W przygotowaniu')
        for title, stage_type, allowed in (('Gotowy z naboru', 'ready', True), ('Wycofany z naboru', 'withdrawn', True),
                                           ('Redagowany', 'editing', False), ('Nowy', None, False)):
            with self.subTest(title):
                text = Text(title=title, anthology=draft, length=10)
                if stage_type:
                    text.save()
                    text.workflow_stages.update(is_current=False)
                    WorkflowStage.objects.create(text=text, stage_type=stage_type)
                text.anthology = self.book
                if allowed:
                    text.clean()
                else:
                    with self.assertRaisesMessage(ValidationError, 'Nie można przenieść niedokończonego tekstu'):
                        text.clean()
        Text.objects.create(title='Dopisany później', anthology=self.book, length=10)
        self.book.clean()


class DuplicateCandidateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Zbiór')
        cls.author = Author.objects.create(first_name='Ala', last_name='Pisze', email='ala@example.test')
        cls.text = Text.objects.create(title='Łódź na jeziorze', anthology=cls.book, length=10)
        cls.text.authors.add(cls.author)
        cls.review = Review.objects.create(title='Lodz na jeziorze!', anthology=cls.book, length=10,
                                           email='ala@example.test', copied_text=cls.text)

    def test_folding_ignores_case_diacritics_and_punctuation(self):
        self.assertEqual(folded('  Łódź — na JEZIORZE!'), 'lodz na jeziorze')
        self.assertEqual(folded(None), '')

    def test_similar_titles_by_same_author_or_email_are_reported(self):
        found = duplicate_candidates('Łódź na jeziorze.', self.book.pk, [self.author.pk])
        self.assertEqual([row['detail'] for row in found], ['Łódź na jeziorze'])
        found = duplicate_candidates('lodz na jeziorze', self.book.pk, [], email='ALA@example.test')
        self.assertEqual(sorted(row['detail'] for row in found), ['Lodz na jeziorze!', 'Łódź na jeziorze'])
        self.assertEqual(duplicate_candidates('Zupełnie inny tytuł', self.book.pk, [self.author.pk]), [])
        self.assertEqual(duplicate_candidates('Łódź na jeziorze', Anthology.objects.create(title='Inny').pk,
                                              [self.author.pk]), [])

    def test_empty_title_or_anthology_and_the_record_itself_are_not_candidates(self):
        self.assertEqual(duplicate_candidates('!!!', self.book.pk, [self.author.pk]), [])
        self.assertEqual(duplicate_candidates('Łódź na jeziorze', None, [self.author.pk]), [])
        # Edycja recenzji pomija ją samą i tekst, który z niej powstał.
        self.assertEqual(duplicate_candidates('Łódź na jeziorze', self.book.pk, [self.author.pk],
                                              exclude_review_id=self.review.pk, email='ala@example.test'), [])
        found = duplicate_candidates('Łódź na jeziorze', self.book.pk, [], exclude_text_id=self.text.pk,
                                     email='ala@example.test')
        self.assertEqual(found, [])


class IntegrityIssueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('integrity-editor', 'Redaktor')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email=None)
        cls.book = Anthology.objects.create(title='Zbiór kontrolny')

    def text(self, title):
        text = Text.objects.create(title=title, anthology=self.book, length=10)
        text.authors.add(self.author)
        return text

    def started(self, title, user=None):
        text = self.text(title)
        assignment = WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=user or self.editor)
        text.workflow_stages.update(is_current=False)
        WorkflowStage.objects.create(text=text, stage_type='editing', assignment=assignment,
                                     started_at=timezone.localdate())
        return text

    def test_work_assigned_to_deactivated_account_is_reported_until_text_is_closed(self):
        former = create_member('integrity-former', 'Redaktor')
        self.started('Praca w toku', former)
        get_user_model().objects.filter(pk=former.pk).update(is_active=False)
        self.assertIn('Przydział do nieaktywnej osoby', labels(integrity_issues(), 'Praca w toku'))
        closed = self.started('Praca zamknięta', former)
        closed.workflow_stages.update(is_current=False)
        WorkflowStage.objects.create(text=closed, stage_type='ready')
        self.assertNotIn('Przydział do nieaktywnej osoby', labels(integrity_issues(), 'Praca zamknięta'))

    def test_profile_and_account_emails_that_differ_are_reported(self):
        person = self.editor.person_profile
        get_user_model().objects.filter(pk=self.editor.pk).update(email='konto@example.test')
        type(person).objects.filter(pk=person.pk).update(email='KONTO@example.test')
        self.assertNotIn('Różne e-maile powiązanej osoby i konta', labels(integrity_issues()))
        type(person).objects.filter(pk=person.pk).update(email='profil@example.test')
        rows = [row for row in integrity_issues() if row['label'] == 'Różne e-maile powiązanej osoby i konta']
        self.assertEqual(len(rows), 1)
        self.assertIn(str(person), rows[0]['detail'])

    def queue(self, text, **dates):
        run = WorkflowRepetition.objects.create(text=text, **dates)
        return run, labels(integrity_issues(), f'kolejka #{run.pk}')

    def test_queue_states_that_cannot_be_worked_on_are_reported(self):
        text = self.started('Tekst z kolejką')
        now = timezone.now()
        _, found = self.queue(text)
        self.assertEqual(found, ['Otwarta kolejka bez dostępnego etapu'])
        _, found = self.queue(text, completed_at=now, canceled_at=now)
        self.assertEqual(found, ['Kolejka jednocześnie zakończona i anulowana'])
        run, _ = self.queue(text)
        WorkflowStage.objects.filter(text=text, is_current=True).update(repetition=run, queue_position=3)
        found = labels(integrity_issues(), f'kolejka #{run.pk}')
        self.assertIn('Nieprawidłowa numeracja kolejki', found)
        self.assertNotIn('Otwarta kolejka bez dostępnego etapu', found)
        WorkflowRepetition.objects.filter(pk=run.pk).update(completed_at=now)
        self.assertIn('Zakończona kolejka zawiera niewykonane etapy', labels(integrity_issues(), f'kolejka #{run.pk}'))
        WorkflowRepetition.objects.filter(pk=run.pk).update(completed_at=None, canceled_at=now)
        self.assertIn('Anulowana kolejka nadal udostępnia pracę', labels(integrity_issues(), f'kolejka #{run.pk}'))

    def test_open_queue_on_closed_text_is_reported(self):
        text = self.text('Tekst gotowy')
        text.workflow_stages.update(is_current=False)
        WorkflowStage.objects.create(text=text, stage_type='ready')
        _, found = self.queue(text)
        self.assertIn('Otwarta kolejka przy zamkniętym tekście', found)
