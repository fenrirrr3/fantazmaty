"""Spis lektorów i montażystów (bez kont, jak spis ilustratorów): lista, dodawanie i edycja przez koordynatora,
linki na stronie audiobooka i podpowiedzi kontaktów w formularzach."""
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.models import AudioContributor, Audiobook
from fantazmaty.testing import create_member, create_superuser, form_value
from illustrations.models import Illustrator
from people.models import Person
from texts.models import Anthology, Text


class AudioContactTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('contacts-admin')
        cls.book = Anthology.objects.create(title='Kontakty', status=Anthology.Status.READY)
        cls.first = Text.objects.create(title='Pierwszy', anthology=cls.book, length=10)
        cls.second = Text.objects.create(title='Drugi', anthology=cls.book, length=10)

    def test_directory_list_add_and_rename_like_illustrators(self):
        coordinator = create_member('contacts-coord', 'Koordynator')
        person = AudioContributor.objects.create(name='Łucja Lektorka', email='lucja@example.test')
        AudioContributor.objects.create(name='Uśpiony Montaż', is_active=False)
        Audiobook.objects.create(text=self.first, narrator_contact=person)
        Audiobook.objects.create(text=self.second, engineer_contact=person)
        list_url = reverse('core:audio_contributor_list')
        self.client.force_login(coordinator)
        rows = list(self.client.get(list_url).context['page_obj'])
        self.assertEqual([(r.name, r.narrated, r.engineered) for r in rows], [('Łucja Lektorka', 1, 1)])
        self.assertEqual(len(self.client.get(list_url, {'show_inactive': '1'}).context['page_obj']), 2)
        self.assertEqual([r.name for r in self.client.get(list_url, {'q': 'lucja@', 'show_inactive': '1'})
                          .context['page_obj']], ['Łucja Lektorka'])
        response = self.client.post(reverse('core:audio_contributor_add'),
                                    {'name': ' Nowy   Lektor ', 'email': 'NOWY@example.test', 'is_active': 'on'})
        created = AudioContributor.objects.get(email='nowy@example.test')
        self.assertRedirects(response, reverse('core:audio_contributor', args=[created.pk]))
        self.assertEqual(created.name, 'Nowy Lektor')
        # Zmiana nazwiska obejmuje wszystkie audiobooki osoby; nieaktualny formularz jest odrzucany.
        url = reverse('core:audio_contributor', args=[person.pk])
        version = form_value(self.client.get(url), name='version')
        self.assertEqual(len(self.client.get(url).context['current_works']), 2)
        data = {'name': 'Łucja Nowak', 'email': 'lucja@example.test', 'is_active': 'on', 'version': version}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertEqual(Audiobook.objects.get(text=self.first).narrator_name, 'Łucja Nowak')

    def test_contact_page_editing_is_for_coordinator_and_rejects_duplicates(self):
        person = AudioContributor.objects.create(name='Łucja Lektorka', email='lucja@example.test')
        AudioContributor.objects.create(name='Zajęty Adres', email='busy@example.test')
        AudioContributor.objects.create(name='Bez Adresu')
        Audiobook.objects.create(text=self.first, narrator_contact=person, status='published')
        Audiobook.objects.create(text=self.second, engineer_contact=person, status='editing')
        url = reverse('core:audio_contributor', args=[person.pk])
        self.client.force_login(create_member('contacts-member', 'Lektor'))
        page = self.client.get(url)
        self.assertIsNone(page.context['form'])
        self.assertEqual(([a.text for a in page.context['works']], [a.text for a in page.context['current_works']]),
                         ([self.first], [self.second]))
        self.assertEqual(self.client.post(url, {'name': 'Inna', 'email': ''}).status_code, 403)
        self.client.force_login(create_member('contacts-coordinator', 'Koordynator'))
        for data, field in (({'name': 'Łucja Lektorka', 'email': 'BUSY@example.test'}, 'email'),
                            ({'name': 'bez  adresu', 'email': ''}, 'name')):
            response = self.client.post(url, {**data, 'version': form_value(self.client.get(url), name='version')})
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.context['form'].errors)
        person.refresh_from_db()
        self.assertEqual((person.name, person.email), ('Łucja Lektorka', 'lucja@example.test'))
        self.assertEqual(self.client.get(reverse('core:audio_contributor', args=[999999])).status_code, 404)

    def test_detail_contact_buttons_and_proofreader_link(self):
        reader = create_member('contacts-reader', 'Korektor audiobooków')
        narrator = AudioContributor.objects.create(name='Lektor Testowy')
        engineer = AudioContributor.objects.create(name='Montażysta Testowy')
        Audiobook.objects.create(text=self.first, narrator_contact=narrator, engineer_contact=engineer,
                                 narrator_name=narrator.name, engineer_name=engineer.name, proofreader=reader)
        self.client.force_login(create_member('contacts-manager', 'Koordynator korekty'))
        page = self.client.get(reverse('core:audiobook_detail', args=[self.first.pk]))
        doc = html.fromstring(page.content)
        for role, person in [('narrator', narrator), ('engineer', engineer)]:
            container = doc.xpath(f'//fieldset[.//select[@name="people-{role}_contact"]]')[0]
            self.assertEqual(container.xpath('.//a/@href'), [reverse('core:audio_contributor', args=[person.pk])])
        self.assertContains(page, reverse('core:person_detail', args=[reader.person_profile.pk]))
        self.assertContains(page, 'Nowy montażysta – e-mail')
        self.assertNotContains(page, 'Przypisz w Korekcie audiobooków')
        self.assertNotContains(page, '<legend>Dźwiękowiec</legend>')


class ContactSuggestionTests(TestCase):
    """Podpowiedzi przy dopisywaniu osób spoza spisu: tylko imiona i nazwiska, bez danych kontaktowych."""

    @classmethod
    def setUpTestData(cls):
        cls.url = reverse('core:contact_suggestions')
        cls.coordinator = create_member('suggest-coordinator', 'Koordynator')
        Person.objects.create(first_name='Anna', last_name='Zewnętrzna', email='zew@example.test', is_external=True)
        Person.objects.create(first_name='Anna', last_name='Dawna', is_active=False)
        Person.objects.create(first_name='Anna', last_name='Obecna')
        Illustrator.objects.create(first_name='Anna', last_name='Rysownik', email='rys@example.test')
        Illustrator.objects.create(first_name='Anna', pseudonym='Kreska', is_active=False)

    def setUp(self):
        self.client.force_login(self.coordinator)

    def results(self, kind, query, status=200):
        response = self.client.get(self.url, {'kind': kind, 'q': query})
        self.assertEqual(response.status_code, status)
        return response.json()['results']

    def test_person_hints_match_every_word_and_mark_external_or_inactive(self):
        self.assertEqual(self.results('person', 'anna'), [
            {'id': Person.objects.get(last_name='Dawna').pk, 'name': 'Anna Dawna', 'email': '', 'note': 'nieaktywny'},
            {'id': Person.objects.get(last_name='Obecna').pk, 'name': 'Anna Obecna', 'email': '', 'note': ''},
            {'id': Person.objects.get(last_name='Zewnętrzna').pk, 'name': 'Anna Zewnętrzna', 'email': '',
             'note': 'zewnętrzny'}])
        self.assertEqual([row['name'] for row in self.results('person', 'Anna zewnetrzna')], ['Anna Zewnętrzna'])

    def test_cover_hints_list_all_illustrators_without_email(self):
        rows = self.results('cover', 'Anna')
        self.assertEqual({(row['note'], row['email']) for row in rows}, {('', ''), ('nieaktywny', '')})
        self.assertEqual([row['note'] for row in self.results('cover', 'kreska')], ['nieaktywny'])

    def test_short_query_is_empty_and_unknown_kind_is_rejected(self):
        self.assertEqual(self.results('person', ' a '), [])
        self.assertEqual(self.results('unknown', 'Anna', status=400), [])
        self.assertEqual(self.results('', 'Anna', status=400), [])
        self.assertEqual(self.client.post(self.url, {'kind': 'person', 'q': 'Anna'}).status_code, 405)
