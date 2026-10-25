"""Panel recenzji: przydziały recenzentów (także archiwalne z datami), formularz i zapis zgłoszenia (powiązanie z tekstem,
ostrzeżenia czarnej listy), lista i usuwanie, dane autora i notatki do tekstów."""
from datetime import date, datetime, time, timedelta

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
from texts.admin import ReviewAdmin, ReviewAdminForm, ReviewAssignmentAdminForm
from texts.archive_dates import validate_archive_dates
from texts.models import MAX_REVIEWERS, Anthology, Review, ReviewAssignment, Text, TextNote
from workflow.models import WorkflowStage

YES = ReviewAssignment.Opinion.YES
READING = ReviewAssignment.Opinion.READING


# Przydziały w danych testowych mają ustaloną datę (południe), a nie domyślne „teraz”: formularz archiwum
# pokazuje datę w czasie lokalnym, a w godzinie zmiany czasu (ostatnia niedziela października, 2:00–3:00)
# taka godzina jest niejednoznaczna i formularz odrzuca odesłane dane.
ASSIGNED_AT = timezone.make_aware(datetime(2026, 3, 10, 12, 0))


def local_noon(days):
    """Południe czasu lokalnego za `days` dni (ujemne – wstecz); nigdy nie wypada w godzinie zmiany czasu."""
    return timezone.make_aware(datetime.combine(timezone.localdate() + timedelta(days=days), time(12)))


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
        self.item = ReviewAssignment.objects.create(review=self.review, user=self.reviewer, position=1, opinion=YES,
                                                    assigned_at=ASSIGNED_AT)

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
        today = timezone.localdate()
        past = local_noon(-30)
        invalid = [
            ({'archive_assigned_at': admin_datetime(local_noon(1))}, 'przydzielenia nie może być w przyszłości'),
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
        self.assertEqual(self.item.assigned_at, past)
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
        past = local_noon(-10)
        historical = Person.objects.create(first_name='Dawny', last_name='Członek')
        response = self.post_change(self.review, **self.new_rows(
            {'position': 2, 'user': '', 'historical_person': historical.pk, 'archive_assigned_at': admin_datetime(past)},
            {'position': 3, 'user': third.pk, 'opinion': 'no'}))
        self.assertEqual(response.status_code, 302)
        added = self.review.assignments.get(position=2)
        self.assertEqual((added.user_id, added.historical_person_id), (None, historical.pk))
        self.assertEqual(added.assigned_at, past)
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


class ReviewAdminFormTests(TestCase):
    """Formularz recenzji w adminie: powiązanie z istniejącym tekstem i ostrzeżenia o współautorach."""

    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Nabór')
        cls.text = Text.objects.create(title='Opowieść', anthology=cls.book, length=1)
        WorkflowStage.objects.create(text=cls.text, stage_type='ready_for_editing')
        cls.linked = Review.objects.create(anthology=cls.book, title='Opowieść', length=1, copied_text=cls.text,
                                           author_first_name='Ala', author_last_name='Autor', email='ala@example.com')
        cls.review = Review.objects.create(anthology=cls.book, title='Druga', length=1, genre='fantasy',
                                           author_first_name='Ewa', author_last_name='Zakazana', email='ewa@example.com')
        cls.banned = Author.objects.create(first_name='Ewa', last_name='Zakazana', email='ewa@example.com',
                                           is_blacklisted=True)

    def form(self, **changes):
        unbound = ReviewAdminForm(instance=self.review)
        data = {}
        for name, field in unbound.fields.items():
            value = unbound[name].value()
            if isinstance(value, list):
                data[name] = value
            elif value not in (None, False):
                data[name] = field.prepare_value(value) if hasattr(field, 'prepare_value') else value
        data.update(changes)
        form = ReviewAdminForm(data, instance=self.review)
        form.is_valid()
        return form

    def test_editing_other_fields_does_not_repeat_identity_warnings(self):
        """Adres z czarnej listy ostrzega przy zmianie danych autora, a nie przy każdej edycji gatunku."""
        form = self.form(genre='horror')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('czarnej liście', str(self.form(title='Nowy tytuł').errors))

    def test_text_already_linked_needs_confirmation_and_cannot_be_shared(self):
        errors = self.form(copied_text=self.text.pk).errors
        self.assertIn('Sprawdź powiązanie', str(errors['confirm_existing_text']))
        self.assertIn('już powiązany z inną recenzją', str(errors['copied_text']))

    def test_blacklisted_coauthor_requires_confirmation(self):
        form = self.form(coauthors=[self.banned.pk])
        self.assertIn('Współautor', str(form.errors['confirm_submission_warnings']))
        self.assertIn('czarnej liście', str(form.errors['confirm_submission_warnings']))


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
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion=READING,
                                        assigned_at=ASSIGNED_AT)
        banned = Author.objects.create(first_name='Zakazany', last_name='Autor', email='zakazany@example.com',
                                       is_blacklisted=True)
        response = self.confirmed_change(review, author=banned.pk, status=Review.Status.REJECTED)
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual((review.status, review.decision_at, review.is_hidden),
                         (Review.Status.REJECTED, decided, False))
        self.assertEqual(review.author, banned)
        self.assertEqual(review.assignments.count(), 1)


class ReviewAdminListAndChangeTests(ReviewAdminData):
    """Lista recenzji (autor, znacznik przeniesienia do tekstów), usuwanie przydziału archiwum w inline
    i zapis do nieistniejącego zgłoszenia."""

    def test_review_list_shows_author_and_transfer_flag(self):
        text = Text.objects.create(title='Opowiadanie', anthology=self.book, length=100)
        self.make_review(title='Przeniesione', copied_text=text, author_pseudonym='Pióro')
        self.make_review(title='Czekające')
        response = self.client.get(reverse('admin:texts_review_changelist'))
        self.assertEqual(response.context['title'], 'Recenzje')
        rows = {row.title: row for row in response.context['cl'].result_list}
        model_admin = response.context['cl'].model_admin
        self.assertEqual({title: model_admin.display_copied_to_text(row) for title, row in rows.items()},
                         {'Przeniesione': True, 'Czekające': False})
        self.assertContains(response, rows['Przeniesione'].author_display_name)

    def test_archived_assignment_can_be_deleted_in_inline(self):
        review = self.archived(title='Archiwalne')
        removed = ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion=YES,
                                                  assigned_at=ASSIGNED_AT)
        kept = ReviewAssignment.objects.create(review=review, user=self.second, position=2,
                                               opinion=ReviewAssignment.Opinion.NO, assigned_at=ASSIGNED_AT)
        data = admin_form_data(self.client, self.url(review), 'review_form')
        index = {data[f'assignments-{i}-id']: i for i in range(2)}
        data[f'assignments-{index[str(removed.pk)]}-DELETE'] = 'on'
        response = self.client.post(self.url(review), {k: v for k, v in data.items() if '__prefix__' not in k})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(review.assignments.all()), [kept])

    def test_post_to_missing_review_changes_nothing(self):
        """Zapis pod nieistniejący numer (blokada rekordu pomija brakujący obiekt) wraca na stronę główną panelu."""
        response = self.client.post(reverse('admin:texts_review_change', args=[999999]), {'title': 'Nowa'})
        self.assertRedirects(response, reverse('admin:index'))
        self.assertFalse(Review.objects.exists())


class ReviewersAdminListTests(ReviewAdminData):
    def test_list_query_count_does_not_grow_with_rows(self):
        """Lista „oceny recenzentów” pokazuje tytuły zgłoszeń bez zapytania na wiersz: 2 i 6 wierszy – tyle samo zapytań."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from texts.models import Reviewers
        url = reverse('admin:texts_reviewers_changelist')
        counts = []
        for total in (2, 6):
            for number in range(Reviewers.objects.count(), total):
                Reviewers.objects.create(review=self.make_review(title=f'Zgłoszenie {number}'))
            self.client.get(url)  # rozgrzewka: sesja i pamięć podręczna
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1])
        self.assertContains(response, 'Oceny: Zgłoszenie 5')


class ReviewAdminDeletionTests(ReviewAdminData):
    """Usuwanie zgłoszeń w adminie: tylko zgłoszenia bez recenzentów i bez utworzonego tekstu (dostęp superusera:
    test_admin_tools.SuperuserOnlyAdminMixinTests)."""

    def request(self, user=None):
        request = RequestFactory().post('/')
        request.user = user or self.admin
        return request

    def reviews_by_case(self):
        text = Text.objects.create(title='Utworzony', anthology=self.book, length=100)
        reviews = {name: self.make_review(title=name) for name in
                   ('bez recenzentów', 'czytany', 'z opinią', 'konto usunięte', 'z tekstem', 'archiwalne puste')}
        Review.objects.filter(pk=reviews['archiwalne puste'].pk).update(old_reviews=True)
        ReviewAssignment.objects.create(review=reviews['czytany'], user=self.reviewer, position=1)
        ReviewAssignment.objects.create(review=reviews['z opinią'], user=self.reviewer, position=1, opinion=YES)
        ReviewAssignment.objects.create(review=reviews['konto usunięte'], user=None, position=1, opinion=YES)
        Review.objects.filter(pk=reviews['z tekstem'].pk).update(copied_text=text)
        return {name: Review.objects.get(pk=review.pk) for name, review in reviews.items()}

    def test_delete_permission_per_review(self):
        """Zgłoszenie da się usunąć, dopóki nikt się nie przypisał (nawet bez opinii) i nie powstał z niego tekst."""
        model_admin = ReviewAdmin(Review, site)
        deletable = {'bez recenzentów', 'archiwalne puste'}
        for name, review in self.reviews_by_case().items():
            with self.subTest(name):
                self.assertEqual(model_admin.has_delete_permission(self.request(), review), name in deletable)
        self.assertTrue(model_admin.has_delete_permission(self.request()))

    def test_delete_view_and_change_form_for_blocked_and_free_review(self):
        """Zablokowane: brak linku „Usuń” i komunikat zamiast potwierdzenia; wolne: usunięcie po potwierdzeniu."""
        reviews = self.reviews_by_case()
        cases = [('czytany', 'przypisał się już recenzent'), ('z tekstem', 'utworzono tekst')]
        for name, reason in cases:
            with self.subTest(name):
                review = reviews[name]
                delete_url = reverse('admin:texts_review_delete', args=[review.pk])
                self.assertNotContains(self.client.get(self.url(review)), delete_url)
                for method in ('get', 'post'):
                    response = getattr(self.client, method)(delete_url, {'post': 'yes'}, follow=True)
                    self.assertRedirects(response, self.url(review))
                    self.assertContains(response, reason)
                self.assertTrue(Review.objects.filter(pk=review.pk).exists())
        free = reviews['bez recenzentów']
        delete_url = reverse('admin:texts_review_delete', args=[free.pk])
        self.assertContains(self.client.get(self.url(free)), delete_url)
        token = self.client.get(delete_url).context['request'].edit_version_token
        self.assertEqual(self.client.post(delete_url, {'post': 'yes', '_edit_version': token}).status_code, 302)
        self.assertFalse(Review.objects.filter(pk=free.pk).exists())

    def test_bulk_action_deletes_only_reviews_without_reviewers(self):
        """Akcja zbiorcza: strona potwierdzenia tylko z wolnymi zgłoszeniami, ostrzeżenie o pominiętych, zapis po potwierdzeniu."""
        reviews = self.reviews_by_case()
        url = reverse('admin:texts_review_changelist')
        data = {'action': 'delete_selected', '_selected_action': [review.pk for review in reviews.values()]}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({review.pk for review in response.context['queryset']},
                         {reviews['bez recenzentów'].pk, reviews['archiwalne puste'].pk})
        warning = [str(message) for message in response.context['messages']]
        self.assertEqual(len(warning), 1)
        for name in ('czytany', 'z opinią', 'konto usunięte', 'z tekstem'):
            self.assertIn(f'„{name}”', warning[0])
        response = self.client.post(url, {**data, '_selected_action': [review.pk for review in response.context['queryset']],
                                          'post': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(Review.objects.values_list('title', flat=True)),
                         {'czytany', 'z opinią', 'konto usunięte', 'z tekstem'})
        response = self.client.post(url, {'action': 'delete_selected', '_selected_action': [reviews['czytany'].pk]},
                                    follow=True)
        self.assertContains(response, 'Pominięto zgłoszenia z recenzentem')
        self.assertTrue(Review.objects.filter(pk=reviews['czytany'].pk).exists())

    def test_deletion_rechecks_reviewers_and_drops_unsent_discord_notice(self):
        """Usunięcie sprawdza przypisania ponownie (wyścig z przydzieleniem) i kasuje tylko niewysłane ogłoszenia."""
        from core.models import DiscordNotification
        model_admin = ReviewAdmin(Review, site)
        review = self.make_review()
        notices = {status: DiscordNotification.objects.create(trigger='review_new', channel='kanał', content='x',
                                                              review=review, status=status)
                   for status in ('pending', 'sent')}
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1)
        for delete in (lambda: model_admin.delete_model(self.request(), review),
                       lambda: model_admin.delete_queryset(self.request(), Review.objects.filter(pk=review.pk))):
            with self.subTest(delete=delete), self.assertRaisesMessage(PermissionDenied, 'przypisał się już recenzent'):
                delete()
        self.assertTrue(Review.objects.filter(pk=review.pk).exists())
        ReviewAssignment.objects.filter(review=review).delete()
        model_admin.delete_model(self.request(), review)
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())
        self.assertEqual(list(DiscordNotification.objects.values_list('pk', 'review')), [(notices['sent'].pk, None)])


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
