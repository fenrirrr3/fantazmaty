"""Modele i formularze tekstów: reguły walidacji, etykiety, czarna lista współautorów, słownik tagów,
formularz okładki i dane Ekstraktów – przypadki dotąd nieobjęte testami."""
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author, AuthorNote, BlacklistEntry
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustrator
from people.models import Person
from texts.blacklist import apply_blacklist
from texts.cover_forms import CoverAssignmentForm
from texts.extract_volume_models import ExtractVolume
from texts.models import (Anthology, AnthologyTask, Extract, Review, ReviewAssignment, Reviewers, Text, TextNote,
                          VocabularyTerm)
from texts.vocabulary import merge_plan, refresh_dictionary


class AnthologyAndTextRulesTests(TestCase):
    def test_extract_anthology_flags_are_validated(self):
        extracts = Anthology.objects.create(title='Ekstrakty', is_extracts=True)
        ExtractVolume.objects.create(anthology=extracts, number=1)
        cases = {
            'Ekstrakty bez tłumaczeń': (Anthology(title='Ekstrakty tłumaczone', is_extracts=True, is_translated=True),
                                        'is_translated'),
            'tom z importu pozostaje Ekstraktami': (extracts, 'is_extracts'),
        }
        extracts.is_extracts = False
        for case, (book, field) in cases.items():
            with self.subTest(case), self.assertRaises(ValidationError) as error:
                book.full_clean()
            self.assertIn(field, error.exception.message_dict)
        self.assertTrue(Anthology.objects.get(pk=extracts.pk).is_extracts)

    def test_chapter_number_only_for_novels_and_chapter_title_follows_number(self):
        book = Anthology.objects.create(title='Zwykła')
        with self.assertRaises(ValidationError) as error:
            Text(title='Opowiadanie', length=10, anthology=book, chapter_number=3).full_clean()
        self.assertIn('Numer rozdziału dotyczy wyłącznie powieści.', error.exception.message_dict['chapter_number'])
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1)
        chapter.chapter_number = 2
        chapter.save(update_fields=['chapter_number'])
        chapter.refresh_from_db()
        self.assertEqual((chapter.chapter_number, chapter.title), (2, 'Rozdział 2'))

    def test_author_emails_skip_authors_without_address(self):
        text = Text.objects.create(title='Wspólny', length=10)
        text.authors.add(Author.objects.create(first_name='Jan', last_name='Adres', email='jan@example.com'),
                         Author.objects.create(first_name='Ola', last_name='Bez', email=None))
        self.assertEqual(text.author_emails, 'jan@example.com')

    def test_task_date_correction_with_unrelated_fields_keeps_given_date(self):
        task = Anthology.objects.create(title='Zadania').production_tasks.get(task_type='typesetting')
        task.commissioned_at = date(2024, 5, 1)
        task.save(update_fields=['commissioned_at'])
        task.refresh_from_db()
        self.assertEqual((task.commissioned_at, task.status), (date(2024, 5, 1), AnthologyTask.Status.NOT_COMMISSIONED))

    def test_note_labels_accept_naive_dates(self):
        """Notatka z datą bez strefy (np. dane spoza ORM) – etykieta pokazuje datę bez przeliczania."""
        naive = datetime(2026, 3, 1, 9, 5)
        text = Text.objects.create(title='Opowiadanie', length=10)
        author = Author.objects.create(first_name='Jan', last_name='Notowany')
        cases = {
            'notatka do tekstu': (TextNote(text=text, content='x', created_at=naive), 'Opowiadanie'),
            'notatka o autorze': (AuthorNote(author=author, content='x', created_at=naive), str(author)),
        }
        for case, (note, prefix) in cases.items():
            with self.subTest(case):
                self.assertTrue(str(note).startswith(prefix))
                self.assertTrue(str(note).endswith('01.03.2026 09:05'))


class ReviewModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Nabór')
        cls.reviewer = create_member('rev-model', 'Recenzent')

    def review(self, **values):
        data = dict(title='Zgłoszenie', anthology=self.book, length=100, email='a@example.com',
                    author_first_name='Jan', author_last_name='Autor')
        data.update(values)
        return Review.objects.create(**data)

    def test_statistics_scope_and_hidden_status_label(self):
        visible = self.review(title='Widoczne')
        hidden = self.review(title='Ukryte', is_hidden=True, status=Review.Status.REJECTED,
                             decision_at=timezone.localdate() + timedelta(days=10))
        archived_hidden = self.review(title='Archiwum', is_hidden=True, old_reviews=True)
        self.assertEqual(set(Review.objects.for_statistics()), {visible, archived_hidden})
        self.assertEqual(hidden.display_status,
                         f'Ukryty – odrzucenie zaplanowane na {hidden.decision_at:%d.%m.%Y}')
        self.assertEqual(visible.display_status, 'Do recenzji')

    def test_general_notes_label(self):
        review = self.review(title='Latarnik')
        self.assertEqual(str(Reviewers.objects.create(review=review)), 'Oceny: Latarnik')

    def test_historical_reviewer_profile_rules(self):
        archived = self.review(title='Archiwum', old_reviews=True)
        current = self.review(title='Bieżące')
        former = Person.objects.create(first_name='Dawny', last_name='Recenzent')
        assignment = ReviewAssignment(review=archived, historical_person=former, position=1)
        assignment.full_clean()
        self.assertEqual(assignment.reviewer_display_name, 'Dawny Recenzent')
        cases = {
            'profil historyczny poza archiwum': ReviewAssignment(review=current, historical_person=former, position=1),
            'profil innego konta': ReviewAssignment(review=archived, historical_person=former, user=self.reviewer,
                                                    position=2),
        }
        for case, invalid in cases.items():
            with self.subTest(case), self.assertRaises(ValidationError) as error:
                invalid.clean()
            self.assertIn('historical_person', error.exception.message_dict)

    def test_assignment_save_tracks_opinion_change_and_ignores_empty_update(self):
        assignment = ReviewAssignment.objects.create(review=self.review(), user=self.reviewer, position=1)
        ReviewAssignment.objects.filter(pk=assignment.pk).update(opinion_changed_at=None)
        assignment.opinion = ReviewAssignment.Opinion.YES
        assignment.save(update_fields=[])
        assignment.refresh_from_db()
        self.assertEqual((assignment.opinion, assignment.opinion_changed_at), (ReviewAssignment.Opinion.READING, None))
        assignment.opinion = ReviewAssignment.Opinion.YES
        assignment.save()
        assignment.refresh_from_db()
        self.assertEqual((assignment.opinion, assignment.opinion_changed_at),
                         (ReviewAssignment.Opinion.YES, timezone.localdate()))


class ReviewBlacklistTests(TestCase):
    def test_blacklisted_coauthor_hides_review(self):
        book = Anthology.objects.create(title='Nabór')
        clean = Author.objects.create(first_name='Ola', last_name='Czysta', email='ola@example.com')
        banned = Author.objects.create(first_name='Zła', last_name='Osoba', email='zla@example.com', is_blacklisted=True)
        listed = Author.objects.create(first_name='Wpis', last_name='Listy', email='wpis@example.com')
        BlacklistEntry.objects.create(name='Wpis Listy', email='wpis@example.com')
        cases = {'współautor bez zastrzeżeń': (clean, False), 'współautor z czarnej listy': (banned, True),
                 'współautor z wpisu czarnej listy': (listed, True)}
        for case, (coauthor, hidden) in cases.items():
            with self.subTest(case):
                review = Review.objects.create(title=case, anthology=book, length=10, email='glowny@example.com',
                                               author_first_name='Główny', author_last_name='Autor')
                review.coauthors.add(coauthor)
                apply_blacklist(review)
                self.assertEqual((review.is_hidden, review.status == Review.Status.REJECTED), (hidden, hidden))


class ExtractRecordTests(TestCase):
    def test_extract_dates_label_and_partial_save(self):
        author = Author.objects.create(first_name='Ewa', last_name='Ekstrakt', email='ewa@example.com')
        extract = Extract(author=author, full_name='Ewa Ekstrakt', email='ewa@example.com', title='Jeden; Dwa',
                          recruitment='Ekstrakty  2026', submission_dates=' ; ')
        with self.assertRaises(ValidationError) as error:
            extract.full_clean()
        self.assertEqual(error.exception.message_dict['submission_dates'], ['Wpisz co najmniej jedną datę nadesłania.'])
        extract.submission_dates = '2026-02-01'
        extract.save()
        self.assertEqual(str(extract), 'Ekstrakty 2026 – Ewa Ekstrakt')
        extract.accepted_titles = 'Dwa'
        extract.save(update_fields=['accepted_titles'])
        extract.refresh_from_db()
        self.assertEqual((extract.accepted_titles, extract.status), ('Dwa', Extract.Status.ACCEPTED))


class VocabularyTests(TestCase):
    def test_term_rules(self):
        genre = VocabularyTerm.objects.create(kind='genre', name='Fantasy')
        tag = VocabularyTerm.objects.create(kind='tag', name='magia')
        self.assertEqual(str(genre), 'Fantasy')
        renamed = VocabularyTerm.objects.get(pk=tag.pk)
        renamed.name = 'czary'
        cases = {
            'za długi gatunek': (VocabularyTerm(kind='genre', name='g' * 101), 'najwyżej 100 znaków'),
            'zmiana nazwy bez scalenia': (renamed, 'przez scalenie w słowniku'),
            'alias innego rodzaju': (VocabularyTerm(kind='tag', name='fantastyka', canonical=genre),
                                     'tego samego rodzaju'),
        }
        for case, (term, message) in cases.items():
            with self.subTest(case), self.assertRaisesMessage(ValidationError, message):
                term.clean()
        self.assertEqual(VocabularyTerm.objects.get(pk=tag.pk).name, 'magia')

    def test_dictionary_refresh_skips_too_long_tags_and_merge_skips_unrelated_texts(self):
        Text.objects.create(title='Długi tag', length=10, tags='smok, ' + 'x' * 256)
        Text.objects.create(title='Inne tagi', length=10, tags='wampiry')
        refresh_dictionary()
        self.assertEqual(set(VocabularyTerm.objects.filter(kind='tag').values_list('name', flat=True)), {'smok', 'wampiry'})
        source = VocabularyTerm.objects.get(name='smok')
        target = VocabularyTerm.objects.create(kind='tag', name='smoki')
        changes, _digest = merge_plan(source, target)
        self.assertEqual([(change['before'], change['after']) for change in changes], [('smok, ' + 'x' * 256, 'smoki, ' + 'x' * 256)])

    def test_vocabulary_list_offers_merge_link(self):
        term = VocabularyTerm.objects.create(kind='tag', name='magia')
        self.client.force_login(create_superuser('vocab-admin'))
        response = self.client.get(reverse('admin:texts_vocabularyterm_changelist'))
        self.assertContains(response, reverse('core:vocabulary_merge', args=[term.pk]))


class CoverFormTests(TestCase):
    def test_new_person_and_legacy_name_rules(self):
        artist = Illustrator.objects.create(first_name='Anna', last_name='Rysuje')
        legacy = Anthology.objects.create(title='Dawna okładka', cover_author='Dawny Podpis')
        cases = {
            'samo nazwisko': (Anthology.objects.create(title='Nowa'), {'cover_status': 'in_progress',
                                                                        'new_cover_last_name': 'Nowak'},
                              'new_cover_first_name', 'Podaj imię nowej osoby.'),
            'usunięcie podpisu i wybór osoby': (legacy, {'cover_status': 'in_progress', 'clear_legacy_cover': 'on',
                                                         'cover_illustrator': artist.pk},
                                                'clear_legacy_cover', 'odznacz usuwanie przypisania'),
        }
        for case, (book, data, field, message) in cases.items():
            with self.subTest(case):
                form = CoverAssignmentForm(data, instance=book)
                self.assertFalse(form.is_valid())
                self.assertIn(message, ' '.join(form.errors[field]))

    def test_person_created_meanwhile_is_reused_or_conflict_reported(self):
        """Nowa osoba dodana równolegle po walidacji: jeden wpis – użyj go; kilka – odmów zapisu."""
        cases = {'jeden równoległy wpis': 1, 'kilka równoległych wpisów': 2}
        for case, created in cases.items():
            with self.subTest(case):
                book = Anthology.objects.create(title=case)
                form = CoverAssignmentForm({'cover_status': 'in_progress', 'new_cover_first_name': 'Ewa',
                                            'new_cover_last_name': f'Równoległa {created}'}, instance=book)
                self.assertTrue(form.is_valid(), form.errors)
                people = [Illustrator.objects.create(first_name='Ewa', last_name=f'Równoległa {created}',
                                                     pseudonym='' if n == 0 else f'Ewa Równoległa {created}')
                          for n in range(created)]
                if created == 1:
                    form.save()
                    book.refresh_from_db()
                    self.assertEqual(book.cover_illustrator, people[0])
                else:
                    with self.assertRaisesMessage(ValidationError, 'Powstało kilka pasujących wpisów'):
                        form.save()
                    book.refresh_from_db()
                    self.assertIsNone(book.cover_illustrator_id)
                self.assertEqual(Illustrator.objects.filter(last_name=f'Równoległa {created}').count(), created)
