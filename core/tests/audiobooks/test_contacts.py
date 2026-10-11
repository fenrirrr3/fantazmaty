"""Kontakty lektorów i montażystów: jeden profil na osobę, zmiany nazwy tylko na stronie kontaktu i linki na stronie audiobooka."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.audiobook_models import resolve_contact
from core.models import AudioContributor, Audiobook
from fantazmaty.testing import create_member, create_superuser, edit_token
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
