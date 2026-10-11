"""Kontakty lektorów i montażystów: jeden profil na osobę, edycja profilu przez koordynatora, linki na stronie audiobooka
i podpowiedzi kontaktów w formularzach."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.audiobook_models import resolve_contact
from core.models import AudioContributor, Audiobook
from fantazmaty.testing import create_member, create_superuser, edit_token
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

    def test_one_profile_per_person_typos_and_renaming_only_on_contact_page(self):
        person = resolve_contact('Łucja Lektorka', 'lucja@example.test')
        self.assertEqual(resolve_contact('  łucja   lektorka ', ''), person)
        self.assertEqual(resolve_contact('Lucja Lektorka', 'LUCJA@example.test'), person)
        with self.assertRaises(ValidationError):
            resolve_contact('Ktoś Inny', 'lucja@example.test')
        first = Audiobook.objects.create(text=self.first, narrator_name='Łucja Lektorka')
        second = Audiobook.objects.create(text=self.second, engineer_name='lucja lektorka',
                                          engineer_email='lucja@example.test')
        self.assertEqual(first.narrator_contact, person)
        self.assertEqual(second.engineer_contact, person)
        self.assertEqual(first.narrator_email, 'lucja@example.test')
        self.assertEqual(AudioContributor.objects.count(), 1)
        self.client.force_login(self.admin)
        url = reverse('core:audio_contributor', args=[person.pk])
        self.assertEqual(len(self.client.get(url).context['current_works']), 2)
        response = self.client.post(url, {'name': 'Łucja Nowak', 'email': 'lucja@example.test'})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.narrator_name, 'Łucja Nowak')

    def test_resolve_contact_fills_missing_email_and_separates_namesakes(self):
        self.assertIsNone(resolve_contact('   ', 'nikt@example.test'))
        known = AudioContributor.objects.create(name='Jan Lektor')
        self.assertEqual(str(known), 'Jan Lektor')
        # Wybrany profil bez adresu dostaje adres, jeśli nikt inny go nie używa.
        self.assertEqual(resolve_contact('jan  lektor', '', contact=known), known)
        self.assertEqual(resolve_contact('Jan Lektor', ' JAN@example.test ', contact=known), known)
        known.refresh_from_db()
        self.assertEqual(known.email, 'jan@example.test')
        # Adres zajęty przez kogoś innego nie trafia do wybranego profilu.
        AudioContributor.objects.create(name='Zajęty Adres', email='busy@example.test')
        bare = AudioContributor.objects.create(name='Piotr Głos')
        with self.assertRaises(ValidationError):
            resolve_contact('Piotr Głos', 'busy@example.test', contact=bare)
        # Nowy kontakt z adresem konta zespołu wiąże się z tym kontem.
        self.assertEqual(resolve_contact('Konto Lektora', self.admin.email.upper()).user, self.admin)
        # Bez wybranego profilu: jedyny imiennik bez adresu dostaje adres.
        nameless = AudioContributor.objects.create(name='Ola Montaż')
        self.assertEqual(resolve_contact('Ola Montaz', 'ola@example.test'), nameless)
        nameless.refresh_from_db()
        self.assertEqual(nameless.email, 'ola@example.test')
        # Ta sama osoba z innym adresem to inny kontakt.
        other = resolve_contact('Jan Lektor', 'drugi-jan@example.test')
        self.assertNotIn(other.pk, (known.pk, nameless.pk))
        self.assertEqual(AudioContributor.objects.filter(name='Jan Lektor').count(), 2)
        # Kilku imienników – trzeba wybrać profil z podpowiedzi.
        with self.assertRaises(ValidationError):
            resolve_contact('JAN LEKTOR', '')

    def test_contact_page_editing_is_for_coordinator_and_rejects_duplicates(self):
        person = AudioContributor.objects.create(name='Łucja Lektorka', email='lucja@example.test')
        AudioContributor.objects.create(name='Zajęty Adres', email='busy@example.test')
        AudioContributor.objects.create(name='Bez Adresu')
        Audiobook.objects.create(text=self.first, narrator_contact=person, status='published')
        Audiobook.objects.create(text=self.second, engineer_contact=person, status='editing')
        url = reverse('core:audio_contributor', args=[person.pk])
        self.client.force_login(create_member('contacts-member', 'Redaktor'))
        page = self.client.get(url)
        self.assertIsNone(page.context['form'])
        self.assertEqual(([a.text for a in page.context['works']], [a.text for a in page.context['current_works']]),
                         ([self.first], [self.second]))
        self.assertEqual(self.client.post(url, {'name': 'Inna', 'email': ''}).status_code, 403)
        self.client.force_login(create_member('contacts-coordinator', 'Koordynator'))
        for data, field in (({'name': 'Łucja Lektorka', 'email': 'BUSY@example.test'}, 'email'),
                            ({'name': 'bez  adresu', 'email': ''}, 'name')):
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.context['form'].errors)
        person.refresh_from_db()
        self.assertEqual((person.name, person.email), ('Łucja Lektorka', 'lucja@example.test'))
        self.assertEqual(self.client.get(reverse('core:audio_contributor', args=[999999])).status_code, 404)

    def test_people_form_creates_new_profile_instead_of_renaming(self):
        person = resolve_contact('Jan Lektor', '')
        audio = Audiobook.objects.create(text=self.first, narrator_contact=person)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('core:audiobook_detail', args=[self.first.pk]), {
            'action': 'people', '_edit_version': edit_token(self.admin, self.first),
            'people-narrator_name': 'Piotr Lektor', 'people-narrator_contact': person.pk,
            'people-narrator_email': '', 'people-engineer_name': '', 'people-engineer_email': ''})
        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        audio.refresh_from_db()
        self.assertEqual(person.name, 'Jan Lektor')
        self.assertEqual(audio.narrator_name, 'Piotr Lektor')
        self.assertNotEqual(audio.narrator_contact_id, person.pk)

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
            container = doc.xpath(f'//div[contains(@class,"audio-name-profile")][.//input[@name="people-{role}_name"]]')[0]
            self.assertEqual(container.xpath('.//a/@href'), [reverse('core:audio_contributor', args=[person.pk])])
        self.assertContains(page, reverse('core:person_detail', args=[reader.person_profile.pk]))
        self.assertContains(page, 'E-mail dźwiękowca')
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
        # Pełne podpowiedzi lektorów i ilustratorów (z adresem); ilustratorzy – tylko aktywni.
        contact = AudioContributor.objects.create(name='Anna Lektorka', email='anna@example.test')
        self.assertEqual(self.results('audio', 'anna@'), [{'id': contact.pk, 'name': 'Anna Lektorka',
                                                           'email': 'anna@example.test'}])
        self.assertEqual([row['email'] for row in self.results('illustration', 'Anna')], ['rys@example.test'])

    def test_short_query_is_empty_and_unknown_kind_is_rejected(self):
        self.assertEqual(self.results('person', ' a '), [])
        self.assertEqual(self.results('unknown', 'Anna', status=400), [])
        self.assertEqual(self.results('', 'Anna', status=400), [])
        self.assertEqual(self.client.post(self.url, {'kind': 'person', 'q': 'Anna'}).status_code, 405)
