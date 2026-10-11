"""Middleware edycji: blokada całego rekordu nadrzędnego, strona konfliktu i odzyskiwanie wpisanych danych."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from authors.models import Author, AuthorNote
from core.audiobook_models import Audiobook
from core.middleware import aggregate, recover_submitted_values
from core.models import UserActivity
from fantazmaty.testing import create_superuser, edit_token
from people.models import Person, Vacation
from texts.models import Anthology, Review, ReviewAssignment, Reviewers, Text, TextNote, TextTranslation

from .support import TemporaryActivitySpoolMixin


def admin_change(model, obj_id):
    return reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_change', args=[obj_id])


class SubmittedValuesTests(SimpleTestCase):
    def test_content_fields_are_recovered_including_formsets_but_never_secrets(self):
        data = QueryDict(mutable=True)
        data.setlist('notes-0-content', ['Pierwsza', 'Druga'])
        data['title'] = 'Tytuł'
        for secret in ('_edit_version', 'csrfmiddlewaretoken', 'password', 'notes-0-password'):
            data[secret] = 'tajne'
        self.assertEqual(recover_submitted_values(data),
                         [('notes-0-content', 'Pierwsza'), ('notes-0-content', 'Druga'), ('title', 'Tytuł')])


class AggregateTests(TestCase):
    """Edycja rekordu podrzędnego w panelu blokuje i wersjonuje cały rekord nadrzędny."""

    @classmethod
    def setUpTestData(cls):
        book = Anthology.objects.create(title='Antologia')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=book)
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.test')
        cls.review = Review.objects.create(title='Zgłoszenie', anthology=book, author=cls.author,
                                           author_first_name='Jan', author_last_name='Autor', length=100)

    def test_admin_children_resolve_to_their_parent_record(self):
        reviewer = get_user_model().objects.create_user('recenzent', 'recenzent@example.test')
        cases = (
            (Audiobook.objects.create(text=self.text), (Text, self.text.pk)),
            (TextNote.objects.create(text=self.text, content='Notatka'), (Text, self.text.pk)),
            (AuthorNote.objects.create(author=self.author, content='Notatka'), (Author, self.author.pk)),
            (ReviewAssignment.objects.create(review=self.review, user=reviewer, position=1),
             (Review, self.review.pk)),
            (Reviewers.objects.get_or_create(review=self.review)[0], (Review, self.review.pk)),
            (TextTranslation.objects.create(text=self.text), (Text, self.text.pk)),
        )
        for child, parent in cases:
            with self.subTest(model=type(child).__name__):
                self.assertEqual(aggregate(resolve(admin_change(type(child), child.pk))), parent)

    def test_non_numeric_admin_object_id_is_not_locked(self):
        self.assertEqual(aggregate(resolve(admin_change(Text, 'abc'))), (None, None))


class EditingRequestTests(TemporaryActivitySpoolMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.person = Person.objects.create(first_name='Anna', last_name='Zespół', email='anna@example.test')
        book = Anthology.objects.create(title='Antologia')
        cls.book = book
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=book)
        cls.other_text = Text.objects.create(title='Inny tekst', length=100, anthology=book)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_unknown_address_is_an_ordinary_not_found(self):
        self.assertEqual(self.client.get('/nie-ma-takiej-strony/').status_code, 404)

    def test_admin_post_without_version_shows_conflict_with_submitted_text(self):
        vacation = Vacation.objects.create(person=self.person, start_date=timezone.localdate())
        for url in (admin_change(Person, self.person.pk), admin_change(Vacation, vacation.pk)):
            with self.subTest(url=url):
                response = self.client.post(url, {'first_name': 'Nowe imię', 'password': 'tajne'})
                self.assertContains(response, 'Rekord został zmieniony', status_code=409)
                self.assertContains(response, 'Nowe imię', status_code=409)
                self.assertNotContains(response, 'tajne', status_code=409)
        self.person.refresh_from_db()
        self.assertEqual(self.person.first_name, 'Anna')

    def test_note_moved_to_another_text_meanwhile_is_a_conflict(self):
        note = TextNote.objects.create(text=self.text, content='Notatka')
        moved = [(Text, self.text.pk), (Text, self.other_text.pk)]
        with patch('core.middleware.aggregate', side_effect=moved):
            response = self.client.post(admin_change(TextNote, note.pk), {
                'text': str(self.other_text.pk), 'content': 'Zmieniona notatka',
                '_edit_version': edit_token(self.admin, self.text),
            })
        self.assertContains(response, 'Zmieniona notatka', status_code=409)
        note.refresh_from_db()
        self.assertEqual(note.content, 'Notatka')

    def test_refused_admin_changes_get_the_admin_answer_not_a_conflict(self):
        """Bez prawa do zmiany rekordu nie ma strony konfliktu (ani podglądu aktualnych danych)."""
        entry = UserActivity.objects.create(actor='a', method='GET', action='Pulpit', path='/', status_code=200)
        response = self.client.post(admin_change(UserActivity, entry.pk), {'action': 'Zmiana'})
        self.assertEqual(response.status_code, 403)
        staff = get_user_model().objects.create_user('personel', 'personel@example.test', is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(admin_change(Anthology, self.book.pk), {'title': 'Zmiana'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('admin:login'), response['Location'])
