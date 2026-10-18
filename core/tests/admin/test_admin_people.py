"""Ludzie w panelu admina: konta użytkowników i profil osoby, łączenie kontaktów audio, czarna lista i wybór osób."""
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.admin_navigation import grouped_app_list
from core.models import AudioContributor, Audiobook
from fantazmaty.testing import create_member, create_superuser, edit_token
from people.models import Person
from texts.models import Anthology, Text

User = get_user_model()


class AccountAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('accounts-admin')
        cls.member = create_member('accounts-member', 'Redaktor')
        cls.bare = User.objects.create_user('bare-account', 'bare@example.test')

    def setUp(self):
        self.client.force_login(self.admin)

    def add(self, **values):
        data = {'first_name': 'Nowa', 'last_name': 'Osoba', 'email': 'nowe@example.test',
                'password1': 'Bardzo-trudne-haslo-42', 'password2': 'Bardzo-trudne-haslo-42',
                'usable_password': 'true', '_save': 'Zapisz', **values}
        return self.client.post(reverse('admin:auth_user_add'), data)

    def test_new_account_requires_unique_clean_identity(self):
        """Nowe konto: e-mail wolny w kontach (także jako nazwa użytkownika) i u osób, imię i nazwisko wymagane;
        nazwa użytkownika powstaje z e-maila."""
        User.objects.create_user('zajeta@example.test', 'inny@example.test')
        Person.objects.create(first_name='Ewa', last_name='Bezkonta', email='Ewa@Example.test')
        cases = [({'email': 'BARE@example.test'}, 'email', 'Konto z tym adresem e-mail już istnieje.'),
                 ({'email': 'zajeta@example.test'}, 'email', 'Konto z tym adresem e-mail już istnieje.'),
                 ({'email': 'ewa@example.test'}, 'email', 'Ten adres e-mail ma już osoba „Ewa Bezkonta” – konto załóż na jej ekranie.'),
                 ({'first_name': ''}, 'first_name', ''),
                 ({'last_name': 'x' * 101}, 'last_name', '')]
        for values, field, message in cases:
            with self.subTest(field=field, values=values):
                page = self.add(**values)
                self.assertEqual(page.status_code, 200)
                self.assertIn(message, str(page.context['adminform'].form.errors[field]))
        self.assertEqual(self.add(first_name='  Nowa   Maria ', email=' Nowe@Example.test').status_code, 302)
        user = User.objects.get(email='nowe@example.test')
        self.assertEqual((user.username, user.first_name), ('nowe@example.test', 'Nowa Maria'))

    def test_account_screen_has_no_staff_permissions_or_groups(self):
        """Ekran konta nie pokazuje is_staff, uprawnień ani grup; nazwa użytkownika jest tylko do odczytu,
        a aktywność konta powiązanego z osobą ustawia się na ekranie osoby."""
        for user, active_editable in ((self.member, False), (self.bare, True)):
            page = self.client.get(reverse('admin:auth_user_change', args=[user.pk]))
            form = page.context['adminform'].form
            with self.subTest(user=user.username):
                for name in ('is_staff', 'user_permissions', 'groups', 'username'):
                    self.assertNotIn(name, form.fields)
                self.assertEqual('is_active' in form.fields, active_editable)
                self.assertIn('is_superuser', form.fields)
        changelist = self.client.get(reverse('admin:auth_user_changelist'))
        self.assertNotIn('is_staff', changelist.context['cl'].list_display)

    def test_account_names_flow_into_linked_profile_and_link_is_shown(self):
        url = reverse('admin:auth_user_change', args=[self.member.pk])
        person = self.member.person_profile
        page = self.client.get(url)
        self.assertContains(page, reverse('admin:people_person_change', args=[person.pk]))
        self.assertContains(self.client.get(reverse('admin:auth_user_change', args=[self.bare.pk])),
                            'Brak powiązanego profilu')
        data = {'username': self.member.username, 'first_name': 'Zmienione', 'last_name': 'Nazwisko',
                'email': person.email, 'is_active': 'on', 'date_joined_0': '2026-01-01',
                'date_joined_1': '10:00:00', '_edit_version': edit_token(self.admin, self.member, 'auth.user'),
                '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        person.refresh_from_db()
        self.assertEqual((person.first_name, person.last_name), ('Zmienione', 'Nazwisko'))


class AudioContactMergeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('merge-admin')
        book = Anthology.objects.create(title='Nagrania')
        cls.keep = AudioContributor.objects.create(name='Jan Lektor')
        cls.copy = AudioContributor.objects.create(name='Jan Lektor', email='jan@example.test')
        cls.third = AudioContributor.objects.create(name='J. Lektor')
        cls.audio = Audiobook.objects.create(text=Text.objects.create(title='Nagranie', anthology=book, length=1),
                                             narrator_contact=cls.copy, engineer_contact=cls.third)

    def merge(self, *contacts):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('admin:core_audiocontributor_changelist'), {
            'action': 'merge_contacts', '_selected_action': [c.pk for c in contacts], 'index': '0'})
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_merge_needs_two_profiles(self):
        self.assertEqual(self.merge(self.keep), ['Zaznacz co najmniej dwa profile.'])
        self.assertEqual(AudioContributor.objects.count(), 3)

    def test_merge_message_uses_the_polish_plural(self):
        """Komunikat o połączeniu odmienia liczbę profili (2 profile, 5 profili)."""
        for count, word in ((2, 'profile'), (5, 'profili')):
            with self.subTest(count=count), transaction.atomic():
                extra = [AudioContributor.objects.create(name=f'Kopia {n}') for n in range(count - 1)]
                self.assertEqual(self.merge(self.keep, *extra)[-1], f'Połączono {count} {word} w „Jan Lektor”.')
                transaction.set_rollback(True)

    def test_merge_creating_a_duplicate_pair_is_refused_without_changes(self):
        """Uzupełniony e-mail powieliłby niezaznaczony wpis „Jan Lektor” z tym e-mailem – komunikat zamiast błędu 500."""
        with_email = AudioContributor.objects.create(name='Ktoś Inny', email='jan@example.test')
        messages = self.merge(self.keep, with_email)
        self.assertEqual(len(messages), 1)
        self.assertIn('Nie połączono profili: „Jan Lektor” z uzupełnionym e-mailem powielałby inny wpis', messages[0])
        self.assertTrue(AudioContributor.objects.filter(pk=with_email.pk).exists())
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.email, '')

    def test_merge_keeps_oldest_profile_and_moves_recordings_and_contact_data(self):
        self.assertEqual(self.merge(self.third, self.copy, self.keep), ['Połączono 3 profile w „Jan Lektor”.'])
        self.assertEqual(list(AudioContributor.objects.all()), [self.keep])
        self.keep.refresh_from_db()
        self.audio.refresh_from_db()
        self.assertEqual(self.keep.email, 'jan@example.test')
        self.assertEqual((self.audio.narrator_contact, self.audio.engineer_contact), (self.keep, self.keep))
        self.assertEqual(self.audio.narrator_email, 'jan@example.test')
        page = self.client.get(reverse('admin:core_audiocontributor_change', args=[self.keep.pk]))
        self.assertContains(page, reverse('core:audio_contributor', args=[self.keep.pk]))
        self.assertContains(self.client.get(reverse('admin:core_audiocontributor_add')), 'Zapisz profil.')


class PeopleChoiceAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('choice-admin')
        Person.objects.create(user=cls.admin, first_name='Ada', last_name='Wybierana', email=cls.admin.email)
        cls.nameless = Person.objects.create(first_name=' ', last_name='', email='nameless@example.test')

    def setUp(self):
        self.client.force_login(self.admin)

    def autocomplete(self, model, field, term):
        response = self.client.get(reverse('admin:autocomplete'), {
            'app_label': 'people', 'model_name': model, 'field_name': field, 'term': term})
        self.assertEqual(response.status_code, 200)
        return [row['text'] for row in response.json()['results']]

    def test_person_pickers_show_profile_names_or_id_placeholder(self):
        self.assertEqual(self.autocomplete('vacation', 'person', 'Wybierana'), ['Ada Wybierana'])
        self.assertIn(f'Osoba bez imienia i nazwiska (ID {self.nameless.pk})',
                      self.autocomplete('vacation', 'person', ''))
        self.assertEqual(self.autocomplete('person', 'user', 'choice-admin'), ['Ada Wybierana'])
        page = self.client.get(reverse('admin:people_person_add'))
        self.assertContains(page, 'admin-autocomplete')

    def test_blacklist_hub_lists_both_kinds_of_entries(self):
        page = self.client.get(reverse('admin:blacklist_index'))
        self.assertEqual([entry['name'] for entry in page.context['entries']],
                         ['Autorzy z bazy', 'Wpisy bez profilu autora'])


class AdminGroupingTests(SimpleTestCase):
    def test_unlisted_models_land_in_other_records_under_history(self):
        other = {'object_name': 'Thing', 'name': 'Rzeczy', 'admin_url': '/admin/x/thing/'}
        groups = grouped_app_list([{'app_label': 'x', 'models': [other]}])
        self.assertEqual([g['app_label'] for g in groups], ['history'])
        self.assertEqual(groups[0]['subgroups'][0]['models'], [other])
        review = {'object_name': 'Review', 'name': 'Review', 'admin_url': '/admin/texts/review/'}
        groups = grouped_app_list([{'app_label': 'texts', 'models': [review]}, {'app_label': 'x', 'models': [other]}])
        self.assertEqual([g['app_label'] for g in groups], ['history', 'submissions'])
        self.assertEqual(groups[1]['models'][0]['name'], 'Recenzje')
