"""Autorzy w panelu admina: duplikaty e-maili i nazwisk, notatki z autorem wpisu, czarna lista i dostęp tylko dla superusera."""
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from authors.admin import AuthorAdmin, AuthorAdminForm, AuthorNoteAdmin
from authors.models import Author, AuthorNote, BlacklistEntry
from texts.models import Anthology, Review, Text
from core.normalization import person_name
from fantazmaty.testing import create_superuser

from .support import admin_form_data


def author_data(**values):
    return {'first_name': 'Nowy', 'last_name': 'Autor', 'pseudonym': '', 'email': 'nowy@example.test',
            'phone_number': '', 'contact': '', **values}


class AuthorAdminFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(first_name='Anna', last_name='Kowalska', pseudonym='Pióro',
                                           email='anna@example.test')

    def test_names_are_collapsed_and_email_must_be_unique_ignoring_case(self):
        form = AuthorAdminForm(author_data(first_name='  Jan   Maria ', last_name=' Nowak ', pseudonym=' Ja  N '))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data['first_name'], form.cleaned_data['last_name'],
                          form.cleaned_data['pseudonym']), ('Jan Maria', 'Nowak', 'Ja N'))
        form = AuthorAdminForm(author_data(email=' ANNA@example.test '))
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['email'].as_data()[0].code, 'duplicate_email')
        self.assertNotIn('confirm_possible_duplicate', form.errors)
        self.assertTrue(AuthorAdminForm(author_data(email='anna@example.test', first_name='Anna',
                                                    last_name='Kowalska', pseudonym='Pióro'),
                                        instance=self.author).is_valid())

    def test_person_name_fixes_case_only_for_all_lower_or_all_upper_input(self):
        """Nazwiska z mieszaną wielkością liter (McGregor, de la Cruz) zostają bez zmian,
        a wpisane samymi małymi lub wielkimi literami dostają wielkie pierwsze litery."""
        cases = [('kowalski', 'Kowalski'), ('KOWALSKI', 'Kowalski'), ('McGregor', 'McGregor'),
                 ('de la Cruz', 'de la Cruz'), ('  jan   kowalski ', 'Jan Kowalski'), ('', '')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(person_name(raw), expected)
                author = Author(first_name='Jan', last_name=raw)
                author.normalize_fields()
                self.assertEqual(author.last_name, expected)

    def test_similar_name_or_pseudonym_requires_confirmation(self):
        for values in ({'first_name': 'ANNA', 'last_name': 'kowalska'}, {'pseudonym': 'pióro'}):
            with self.subTest(values=values):
                form = AuthorAdminForm(author_data(**values))
                self.assertFalse(form.is_valid())
                error = form.errors['confirm_possible_duplicate'][0]
                self.assertIn(f'#{self.author.pk}: Anna Kowalska (Pióro) – anna@example.test', error)
                self.assertNotIn('dalsze pasujące', error)
                self.assertTrue(AuthorAdminForm(author_data(**values, confirm_possible_duplicate='on')).is_valid())

    def test_long_duplicate_list_is_shortened(self):
        Author.objects.bulk_create([Author(first_name='Jan', last_name='Kot', email=f'kot{i}@example.test')
                                    for i in range(11)])
        error = AuthorAdminForm(author_data(first_name='Jan', last_name='Kot')).errors['confirm_possible_duplicate'][0]
        self.assertEqual(error.count('#'), 10)
        self.assertIn('Istnieją również dalsze pasujące rekordy.', error)

    def test_changing_only_cooperation_fields_skips_similarity_check(self):
        twin = Author.objects.create(first_name='Anna', last_name='Kowalska', email='twin@example.test')
        data = author_data(first_name='Anna', last_name='Kowalska', email='twin@example.test', has_contract='on')
        self.assertTrue(AuthorAdminForm(data, instance=twin).is_valid())
        self.assertFalse(AuthorAdminForm({**data, 'pseudonym': 'Pióro'}, instance=twin).is_valid())


class AuthorAdminPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('author-admin', password='x')
        cls.admin.first_name, cls.admin.last_name = 'Ada', 'Admin'
        cls.admin.save()
        cls.author = Author.objects.create(first_name='Anna', last_name='Kowalska', email='anna@example.test')
        cls.listed = Author.objects.create(first_name='Zła', last_name='Osoba', email='bad@example.test',
                                           is_blacklisted=True)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_note_added_in_author_form_records_its_creator(self):
        url = reverse('admin:authors_author_change', args=[self.author.pk])
        data = admin_form_data(self.client, url, 'author_form', **{
            'notes-TOTAL_FORMS': '1', 'notes-0-content': 'Pisze wolno', 'notes-0-author': str(self.author.pk),
            'phone_number': '600 000 000', '_save': 'Zapisz'})
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        note = AuthorNote.objects.get()
        self.assertEqual((note.author, note.created_by, note.content), (self.author, self.admin, 'Pisze wolno'))
        self.assertRegex(str(note), r'^Anna Kowalska – Ada Admin – \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$')
        url = reverse('admin:authors_author_change', args=[self.author.pk])
        data = admin_form_data(self.client, url, 'author_form', **{'notes-0-DELETE': 'on', '_save': 'Zapisz'})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertFalse(AuthorNote.objects.exists())

    def test_note_admin_sets_creator_and_shortens_preview(self):
        response = self.client.post(reverse('admin:authors_authornote_add'), {
            'author': self.author.pk, 'content': 'słowo ' * 30, '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        note = AuthorNote.objects.get()
        self.assertEqual(note.created_by, self.admin)
        preview = AuthorNoteAdmin(AuthorNote, site).content_preview(note)
        self.assertEqual((len(preview), preview[-3:]), (100, '...'))
        self.assertEqual(AuthorNoteAdmin(AuthorNote, site).content_preview(AuthorNote(content=' krótko\n')), 'krótko')
        self.assertEqual(str(AuthorNote(author=self.author)), 'Anna Kowalska – nieznany autor notatki – bez daty')
        self.assertContains(self.client.get(reverse('admin:authors_authornote_changelist')), 'słowo słowo')

    def test_blacklist_shortcuts_and_lists(self):
        response = self.client.get(reverse('admin:authors_author_blacklist'))
        self.assertRedirects(response, reverse('admin:authors_author_changelist') + '?is_blacklisted__exact=1',
                             fetch_redirect_response=False)
        page = self.client.get(reverse('admin:authors_author_changelist'))
        self.assertContains(page, 'aria-label="Czarna lista: tak"')
        self.assertContains(page, 'Kowalska Anna')
        page = self.client.get(reverse('admin:authors_blacklistedauthor_changelist'))
        self.assertEqual([a.pk for a in page.context['cl'].result_list], [self.listed.pk])
        self.assertRedirects(self.client.get(reverse('admin:authors_blacklistedauthor_add')),
                             reverse('admin:authors_blacklistentry_add'), fetch_redirect_response=False)

    def test_blacklist_notes_are_edited_listed_short_and_searchable(self):
        """„Uwagi – za co wpisany”: edycja w formularzu autora i wpisu niezależnego, skrót na liście, wyszukiwanie."""
        reason = 'Plagiat opowiadania ' + 'i dalsze szczegóły ' * 10
        url = reverse('admin:authors_author_change', args=[self.listed.pk])
        self.assertContains(self.client.get(url), 'Uwagi – za co wpisany')
        data = admin_form_data(self.client, url, 'author_form', blacklist_notes=reason, is_blacklisted='on', _save='Zapisz')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.listed.refresh_from_db()
        self.assertEqual(self.listed.blacklist_notes, reason.strip())
        entry = BlacklistEntry.objects.create(email='spam@example.test', notes='Wysyła cudze teksty')
        pages = (('admin:authors_blacklistedauthor_changelist', 'Plagiat', self.listed.pk),
                 ('admin:authors_blacklistentry_changelist', 'cudze', entry.pk))
        for name, term, pk in pages:
            with self.subTest(name):
                page = self.client.get(reverse(name))
                self.assertContains(page, 'Uwagi – za co wpisany')
                found = self.client.get(reverse(name), {'q': term}).context['cl'].result_list
                self.assertEqual([item.pk for item in found], [pk])
        page = self.client.get(reverse('admin:authors_blacklistedauthor_changelist'))
        self.assertContains(page, 'Plagiat opowiadania i dalsze')
        self.assertNotContains(page, reason.strip())
        self.assertContains(self.client.get(reverse('admin:authors_blacklistentry_change', args=[entry.pk])),
                            'Uwagi – za co wpisany')

    def test_independent_blacklist_entry_email_is_normalized_and_unique(self):
        entry = BlacklistEntry(email='  Spam@Example.TEST ')
        entry.clean()
        entry.save()
        self.assertEqual(str(entry), 'spam@example.test')
        with self.assertRaises(ValidationError):
            BlacklistEntry(name='Inny', email='SPAM@example.test').clean()
        self.assertEqual(str(BlacklistEntry(name='Jan', email='a@b.pl')), 'Jan (a@b.pl)')

    def test_author_with_texts_or_submissions_cannot_be_deleted(self):
        """Autor z tekstem albo zgłoszeniem nie ma uprawnienia do usunięcia (strona usuwania → 403),
        a akcja zbiorcza pomija go z komunikatem wymieniającym nazwisko i liczbę tekstów."""
        book = Anthology.objects.create(title='Antologia')
        with_text = Author.objects.create(first_name='Jan', last_name='Pisarz', email='pisarz@example.test')
        Text.objects.create(title='Opowiadanie', length=10, anthology=book).authors.add(with_text)
        with_review = Author.objects.create(first_name='Ewa', last_name='Debiut', email='debiut@example.test')
        Review.objects.create(title='Zgłoszenie', anthology=book, author=with_review, length=10,
                              author_first_name='Ewa', author_last_name='Debiut')
        free = Author.objects.create(first_name='Wolny', last_name='Autor', email='wolny@example.test')
        model_admin = AuthorAdmin(Author, site)
        request = RequestFactory().get('/')
        request.user = self.admin
        for author, deletable in ((with_text, False), (with_review, False), (free, True)):
            with self.subTest(author=str(author)):
                self.assertIs(model_admin.has_delete_permission(request, author), deletable)
                response = self.client.get(reverse('admin:authors_author_delete', args=[author.pk]))
                self.assertEqual(response.status_code, 200 if deletable else 403)
        changelist = reverse('admin:authors_author_changelist')
        selected = [str(with_text.pk), str(with_review.pk), str(free.pk)]
        response = self.client.post(changelist, {'action': 'delete_selected', '_selected_action': selected})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nie można usunąć autora – ma teksty w bazie: Debiut Ewa (1 zgłoszenie); '
                                      'Pisarz Jan (1 tekst).')
        self.assertNotContains(response, 'Jan Pisarz')
        self.assertContains(response, 'Wolny Autor')
        response = self.client.post(changelist, {'action': 'delete_selected', '_selected_action': selected,
                                                 'post': 'yes'}, follow=True)
        self.assertContains(response, 'Nie można usunąć autora – ma teksty w bazie')
        self.assertEqual(set(Author.objects.filter(pk__in=[a.pk for a in (with_text, with_review, free)])),
                         {with_text, with_review})
        response = self.client.post(changelist, {'action': 'delete_selected', '_selected_action': [str(with_text.pk)]},
                                    follow=True)
        self.assertContains(response, 'Nie można usunąć autora – ma teksty w bazie: Pisarz Jan (1 tekst).')
        self.assertTrue(Author.objects.filter(pk=with_text.pk).exists())

    def test_author_data_is_hidden_from_non_superuser_even_if_called_directly(self):
        request = RequestFactory().get('/')
        request.user = get_user_model().objects.create_user('author-staff', is_staff=True)
        model_admin = AuthorAdmin(Author, site)
        self.assertFalse(model_admin.get_queryset(request).exists())
        with self.assertRaises(PermissionDenied):
            model_admin.history_view(request, str(self.author.pk))
        with self.assertRaises(PermissionDenied):
            model_admin.blacklist_view(request)
        with self.assertRaises(PermissionDenied):
            AuthorNoteAdmin(AuthorNote, site).save_model(request, AuthorNote(author=self.author), None, False)
        request.user = self.admin
        self.assertEqual(model_admin.history_view(request, str(self.author.pk)).status_code, 200)
