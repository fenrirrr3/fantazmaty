"""Panel recenzji: przydziały recenzentów (także archiwalne z datami), zapis zgłoszenia, dane autora i notatki do tekstów."""
from datetime import date, datetime, timedelta

from django.contrib.admin import site
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_member, create_superuser
from people.models import Person
from texts.admin import ReviewAdmin, ReviewAssignmentAdminForm, TextNoteAdmin
from texts.archive_dates import validate_archive_dates
from texts.models import MAX_REVIEWERS, Anthology, Review, ReviewAssignment, Text, TextNote

YES = ReviewAssignment.Opinion.YES
READING = ReviewAssignment.Opinion.READING


def admin_date(value):
    return value.strftime('%d.%m.%Y')


def admin_datetime(value):
    return timezone.localtime(value).strftime('%d.%m.%Y %H:%M:%S')


class ArchiveDatesTests(SimpleTestCase):
    def test_archive_dates_order_and_future(self):
        now = timezone.now()
        today = timezone.localdate()
        naive_past = datetime.now() - timedelta(days=3)
        cases = [
            (now - timedelta(days=2), today - timedelta(days=1), {}),
            (naive_past, today, {}),
            (None, None, {}),
            (None, today - timedelta(days=400), {}),
            (now + timedelta(hours=1), None, {'assigned_at'}),
            (naive_past + timedelta(days=10), None, {'assigned_at'}),
            (now - timedelta(days=1), today - timedelta(days=5), {'opinion_changed_at'}),
            (None, today + timedelta(days=1), {'opinion_changed_at'}),
            (now + timedelta(days=2), today + timedelta(days=1), {'assigned_at', 'opinion_changed_at'}),
        ]
        for assigned, opinion, expected in cases:
            with self.subTest(assigned=assigned, opinion=opinion):
                if not expected:
                    validate_archive_dates(assigned, opinion)
                    continue
                with self.assertRaises(ValidationError) as error:
                    validate_archive_dates(assigned, opinion)
                self.assertEqual(set(error.exception.message_dict), expected)


class ReviewAdminData(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.reviewer = create_member('rev-one', 'Recenzent')
        cls.second = create_member('rev-two', 'Recenzent')
        cls.book = Anthology.objects.create(title='Nabór admina')

    def setUp(self):
        self.client.force_login(self.admin)

    def make_review(self, **values):
        data = dict(title='Zgłoszenie', anthology=self.book, length=100, genre='fantasy', email='autor@example.com',
                    author_first_name='Jan', author_last_name='Autor')
        data.update(values)
        return Review.objects.create(**data)

    def archived(self, **values):
        return self.make_review(old_reviews=True, status=Review.Status.ACCEPTED, **values)

    @staticmethod
    def url(review):
        return reverse('admin:texts_review_change', args=[review.pk])

    def post_change(self, review, **changes):
        url = self.url(review)
        data = admin_form_data(self.client, url, 'review_form', **changes)
        return self.client.post(url, {k: v for k, v in data.items() if '__prefix__' not in k})


class ReviewAssignmentAdminFormTests(ReviewAdminData):
    """Walidacja konta recenzenta w formularzu przydziału (bieżąca recenzja)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        users = get_user_model().objects
        cls.inactive = create_member('rev-inactive', 'Recenzent')
        users.filter(pk=cls.inactive.pk).update(is_active=False)
        cls.inactive.refresh_from_db()
        cls.no_profile = users.create_user('rev-no-profile')
        cls.former = create_member('rev-former', 'Recenzent')
        Person.objects.filter(user=cls.former).update(is_active=False)
        cls.external = create_member('rev-external', 'Recenzent')
        Person.objects.filter(user=cls.external).update(is_external=True)

    def user_errors(self, instance, user):
        form = ReviewAssignmentAdminForm(instance=instance, data={
            'position': instance.position or 2, 'user': user.pk if user else '', 'historical_person': '',
            'opinion': instance.opinion, 'notes': instance.notes})
        form.is_valid()
        return ' '.join(form.errors.get('user', []))

    def test_account_rules_for_new_and_existing_assignments(self):
        review = self.make_review()
        reading = ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1)
        rated = ReviewAssignment.objects.create(review=review, user=self.second, position=2, opinion=YES)
        noted = ReviewAssignment.objects.create(review=review, user=self.inactive, position=3, notes='Uwagi')
        orphan = ReviewAssignment.objects.create(review=review, user=None, position=4, opinion=YES)
        new = ReviewAssignment(review=review)
        newcomer = create_member('rev-new', 'Recenzent')
        cases = [
            ('nowy bez konta', new, None, 'Wybierz recenzenta'),
            ('nowy – konto nieaktywne', new, self.inactive, 'nieaktywne'),
            ('nowy – bez profilu osoby', new, self.no_profile, 'aktywnego zespołu'),
            ('nowy – profil nieaktywny', new, self.former, 'aktywnego zespołu'),
            ('nowy – osoba zewnętrzna', new, self.external, 'aktywnego zespołu'),
            ('nowy – aktywny członek', new, newcomer, ''),
            ('usunięcie konta z przydziału', reading, None, 'Aby zwolnić miejsce'),
            ('ocena po usuniętym koncie', orphan, None, ''),
            ('zamiana przy czytaniu', reading, newcomer, ''),
            ('zamiana po oddaniu opinii', rated, newcomer, 'istniejącej opinii'),
            ('zamiana przy zapisanych uwagach', noted, newcomer, 'istniejącej opinii'),
            # Bez zmiany konta jego późniejsza dezaktywacja nie blokuje zapisu.
            ('to samo nieaktywne konto', noted, self.inactive, ''),
        ]
        for label, instance, user, expected in cases:
            with self.subTest(label):
                errors = self.user_errors(instance, user)
                if expected:
                    self.assertIn(expected, errors)
                else:
                    self.assertEqual(errors, '')

    def test_archive_accepts_any_account_choice(self):
        review = self.archived()
        for user in (None, self.inactive, self.no_profile):
            with self.subTest(user=user):
                self.assertEqual(self.user_errors(ReviewAssignment(review=review), user), '')


class ArchivedAssignmentAdminTests(ReviewAdminData):
    """Archiwum: superuser poprawia przydziały i daty w inline; bieżące przydziały są tylko do odczytu."""

    def setUp(self):
        super().setUp()
        self.review = self.archived()
        self.item = ReviewAssignment.objects.create(review=self.review, user=self.reviewer, position=1, opinion=YES)

    def row(self, index, **values):
        return {f'assignments-{index}-{name}': value for name, value in values.items()}

    def new_rows(self, *rows):
        data = {'assignments-TOTAL_FORMS': str(1 + len(rows))}
        for index, values in enumerate(rows, 1):
            defaults = dict(id='', review=self.review.pk, historical_person='', opinion=YES, notes='',
                            archive_assigned_at='', archive_opinion_changed_at='')
            data.update(self.row(index, **{**defaults, **values}))
        return data

    def formset_errors(self, response):
        self.assertEqual(response.status_code, 200)
        formset = response.context['inline_admin_formsets'][0].formset
        return ' '.join([*formset.non_form_errors(), *(str(form.errors) for form in formset.forms)])

    def test_dates_are_validated_and_saved(self):
        now = timezone.now()
        today = timezone.localdate()
        past = now - timedelta(days=30)
        invalid = [
            ({'archive_assigned_at': admin_datetime(now + timedelta(days=1))}, 'przydzielenia nie może być w przyszłości'),
            ({'archive_assigned_at': admin_datetime(past), 'archive_opinion_changed_at': admin_date(today - timedelta(days=40))},
             'nie może być wcześniejsza'),
            ({'archive_opinion_changed_at': admin_date(today + timedelta(days=1))}, 'opinii nie może być w przyszłości'),
        ]
        for values, message in invalid:
            with self.subTest(message):
                response = self.post_change(self.review, **self.row(0, **values))
                self.assertIn(message, self.formset_errors(response))
        saved = self.post_change(self.review, **self.row(0, archive_assigned_at=admin_datetime(past),
                                                         archive_opinion_changed_at=admin_date(today - timedelta(days=2))))
        self.assertEqual(saved.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assigned_at.replace(microsecond=0), past.replace(microsecond=0))
        self.assertEqual(self.item.opinion_changed_at, today - timedelta(days=2))
        # Puste pola oznaczają nieznaną datę historyczną.
        self.assertEqual(self.post_change(self.review, **self.row(
            0, archive_assigned_at='', archive_opinion_changed_at='')).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual((self.item.assigned_at, self.item.opinion_changed_at), (None, None))

    def test_formset_rejects_duplicates_limit_and_rows_without_reviewer(self):
        third = create_member('rev-three', 'Recenzent')
        cases = [
            # Duplikaty wykrywa już kontrola unikalności formsetu (ograniczenia modelu).
            ('to samo miejsce', [{'position': 1, 'user': self.second.pk}], 'zduplikowane dane w position'),
            ('ta sama osoba', [{'position': 2, 'user': self.reviewer.pk}], 'zduplikowane dane w user'),
            ('bez konta i profilu', [{'position': 2, 'user': ''}], 'Wybierz konto lub historyczny profil'),
            # Miejsca są unikalne i mają numery 1–MAX, więc siódmego recenzenta nie da się dodać.
            ('ponad limit', [{'position': MAX_REVIEWERS + 1, 'user': self.second.pk}],
             f'mniejsza lub równa {MAX_REVIEWERS}'),
        ]
        for label, rows, message in cases:
            with self.subTest(label):
                response = self.post_change(self.review, **self.new_rows(*rows))
                self.assertIn(message, self.formset_errors(response))
        self.assertEqual(self.review.assignments.count(), 1)
        past = timezone.now() - timedelta(days=10)
        historical = Person.objects.create(first_name='Dawny', last_name='Członek')
        response = self.post_change(self.review, **self.new_rows(
            {'position': 2, 'user': '', 'historical_person': historical.pk, 'archive_assigned_at': admin_datetime(past)},
            {'position': 3, 'user': third.pk, 'opinion': 'no'}))
        self.assertEqual(response.status_code, 302)
        added = self.review.assignments.get(position=2)
        self.assertEqual((added.user_id, added.historical_person_id), (None, historical.pk))
        self.assertEqual(added.assigned_at.replace(microsecond=0), past.replace(microsecond=0))
        self.assertIsNone(added.opinion_changed_at)
        self.assertEqual(self.review.assignments.get(position=3).user, third)

    def test_model_validates_archive_dates_only_in_archive(self):
        future = timezone.now() + timedelta(days=1)
        with self.assertRaises(ValidationError) as error:
            ReviewAssignment(review=self.review, user=self.second, position=2, assigned_at=future).full_clean()
        self.assertIn('assigned_at', error.exception.message_dict)
        current = self.make_review(title='Bieżące')
        ReviewAssignment(review=current, user=self.second, position=2, assigned_at=future).full_clean()

    def test_current_review_assignments_are_read_only(self):
        current = self.make_review(title='Bieżące')
        ReviewAssignment.objects.create(review=current, user=self.reviewer, position=1)
        rendered = admin_form_data(self.client, self.url(current), 'review_form')
        self.assertIn('assignments-0-notes', rendered)
        for name in ('user', 'position', 'opinion', 'archive_assigned_at'):
            self.assertNotIn(f'assignments-0-{name}', rendered)
        response = self.post_change(current, **{'assignments-0-user': self.second.pk, 'assignments-0-notes': 'Zmiana'})
        self.assertEqual(response.status_code, 302)
        item = current.assignments.get()
        self.assertEqual((item.user, item.notes), (self.reviewer, 'Zmiana'))


class ReviewAdminSaveTests(ReviewAdminData):
    def confirmed_change(self, review, **changes):
        """Zapis zmiany autora: pierwsza próba pokazuje ostrzeżenie o czarnej liście, druga je potwierdza."""
        response = self.post_change(review, **changes)
        self.assertEqual(response.status_code, 200)
        form = response.context['adminform'].form
        self.assertIn('czarnej liście', ' '.join(form.submission_warnings))
        return self.post_change(review, confirm_submission_warnings='on',
                                submission_warnings_token=form.data['submission_warnings_token'], **changes)

    def test_new_review_takes_identity_from_author_profile(self):
        author = Author.objects.create(first_name='Ewa', last_name='Profil', email='ewa@example.com')
        url = reverse('admin:texts_review_add')
        data = admin_form_data(self.client, url, 'review_form', title='Nowe zgłoszenie', anthology=self.book.pk,
                               genre='fantasy', length='500', author=author.pk, author_first_name='Inne',
                               author_last_name='Dane', email='inny@example.com')
        response = self.client.post(url, {k: v for k, v in data.items() if '__prefix__' not in k})
        self.assertEqual(response.status_code, 302)
        review = Review.objects.get(title='Nowe zgłoszenie')
        self.assertEqual((review.author_first_name, review.author_last_name, review.email),
                         ('Ewa', 'Profil', 'ewa@example.com'))
        self.assertEqual((review.status, review.decision_at, review.is_hidden), (Review.Status.NEW, None, False))

    def test_switching_to_blacklisted_author_hides_review_and_clears_unfinished_reading(self):
        review = self.make_review()
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1)
        done = ReviewAssignment.objects.create(review=review, user=self.second, position=2, opinion=YES)
        banned = Author.objects.create(first_name='Zakazany', last_name='Autor', email='zakazany@example.com',
                                       is_blacklisted=True)
        response = self.confirmed_change(review, author=banned.pk)
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertTrue(review.is_hidden)
        self.assertEqual(review.status, Review.Status.REJECTED)
        self.assertGreater(review.decision_at, timezone.localdate())
        self.assertEqual(list(review.assignments.all()), [done])

    def test_archived_identity_change_keeps_decision_and_assignments(self):
        decided = date(2020, 5, 1)
        review = self.archived(decision_at=decided)
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion=READING)
        banned = Author.objects.create(first_name='Zakazany', last_name='Autor', email='zakazany@example.com',
                                       is_blacklisted=True)
        response = self.confirmed_change(review, author=banned.pk, status=Review.Status.REJECTED)
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual((review.status, review.decision_at, review.is_hidden),
                         (Review.Status.REJECTED, decided, False))
        self.assertEqual(review.author, banned)
        self.assertEqual(review.assignments.count(), 1)


class AuthorDetailsViewTests(ReviewAdminData):
    def test_superuser_gets_private_author_data_with_blacklist_warning(self):
        author = Author.objects.create(first_name='Anna', last_name='Dane', email='anna@example.com',
                                       pseudonym='A.D.', phone_number='500600700', is_blacklisted=True)
        url = reverse('admin:texts_review_author_details', args=[author.pk])
        response = self.client.get(url)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.assertEqual(response.json(), {
            'first_name': 'Anna', 'last_name': 'Dane', 'email': 'anna@example.com', 'is_blacklisted': True,
            'pseudonym': 'A.D.', 'phone_number': '500600700', 'warning': 'Autor znajduje się na czarnej liście.'})
        Author.objects.filter(pk=author.pk).update(is_blacklisted=False)
        self.assertEqual(self.client.get(url).json()['warning'], '')
        self.assertEqual(self.client.get(reverse('admin:texts_review_author_details', args=[author.pk + 99])).status_code, 404)
        # Pole autora w formularzu zna adres tego widoku.
        page = self.client.get(self.url(self.make_review()))
        self.assertIn('data-author-details-url', page.content.decode())

    def test_only_superuser_reads_author_data(self):
        author = Author.objects.create(first_name='Anna', last_name='Dane', email='anna@example.com')
        url = reverse('admin:texts_review_author_details', args=[author.pk])
        staff = get_user_model().objects.create_user('review-staff', is_staff=True)
        for user in (staff, self.reviewer):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(b'anna@example.com', response.content)
        request = RequestFactory().get(url)
        request.user = staff
        with self.assertRaises(PermissionDenied):
            ReviewAdmin(Review, site).author_details_view(request, author.pk)


class TextNoteAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('notes-admin')
        cls.other_admin = create_superuser('notes-other')
        cls.book = Anthology.objects.create(title='Notatki')
        cls.text = Text.objects.create(title='Opowiadanie', anthology=cls.book, length=100)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_note_author_is_the_creator_and_stays_on_edit(self):
        url = reverse('admin:texts_textnote_add')
        response = self.client.post(url, {'text': self.text.pk, 'content': 'Ważna uwaga', 'is_important': 'on',
                                          '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        note = TextNote.objects.get()
        self.assertEqual(note.author, self.admin)
        self.client.force_login(self.other_admin)
        change = reverse('admin:texts_textnote_change', args=[note.pk])
        data = admin_form_data(self.client, change, 'textnote_form', content='Poprawiona uwaga')
        self.assertEqual(self.client.post(change, data).status_code, 302)
        note.refresh_from_db()
        self.assertEqual((note.content, note.author), ('Poprawiona uwaga', self.admin))
        request = RequestFactory().post(url)
        request.user = get_user_model().objects.create_user('notes-staff', is_staff=True)
        with self.assertRaises(PermissionDenied):
            TextNoteAdmin(TextNote, site).save_model(request, TextNote(text=self.text, content='x'), None, False)

    def test_bulk_delete_removes_only_selected_notes(self):
        notes = [TextNote.objects.create(text=self.text, author=self.admin, content=f'Uwaga {n}') for n in range(3)]
        response = self.client.post(reverse('admin:texts_textnote_changelist'), {
            'action': 'delete_selected', '_selected_action': [notes[0].pk, notes[1].pk], 'post': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(TextNote.objects.values_list('pk', flat=True)), [notes[2].pk])

    def test_label_names_text_author_and_local_time(self):
        created = timezone.make_aware(datetime(2026, 3, 1, 9, 5))
        named = get_user_model().objects.create_user('nazwa', first_name='Ola', last_name='Nowak')
        login_only = get_user_model().objects.create_user('tylko-login')
        cases = [(named, 'Opowiadanie – Ola Nowak – 01.03.2026 09:05'),
                 (login_only, 'Opowiadanie – tylko-login – 01.03.2026 09:05'),
                 (None, 'Opowiadanie – nieznany autor – 01.03.2026 09:05')]
        for author, label in cases:
            with self.subTest(label):
                note = TextNote.objects.create(text=self.text, author=author, content='x')
                TextNote.objects.filter(pk=note.pk).update(created_at=created)
                note.refresh_from_db()
                self.assertEqual(str(note), label)
        self.assertEqual(str(TextNote(text=self.text, content='x')), 'Opowiadanie – nieznany autor – bez daty')
