from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from people.models import Person, Vacation
from texts.models import Anthology, Text


class DetailLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('layout57', 'admin57@example.test', 'test')
        cls.person = Person.objects.create(user=cls.admin, first_name='Jan', last_name='Test')
        cls.author = Author.objects.create(first_name='Anna', last_name='Test', email='author57@example.test')
        cls.book = Anthology.objects.create(title='Antologia')
        cls.story = Text.objects.create(title='Opowiadanie', anthology=cls.book, length=100)
        cls.story.authors.add(cls.author)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_vacations_show_entire_own_history_without_pagination(self):
        now = timezone.now() - timedelta(days=365)
        Vacation.objects.bulk_create([
            Vacation(person=self.person, start_date=(now + timedelta(days=i * 3)).date(),
                     end_date=now + timedelta(days=i * 3 + 1)) for i in range(60)
        ])
        other = Person.objects.create(first_name='Inna', last_name='Osoba')
        Vacation.objects.create(person=other, start_date=now.date(), end_date=now + timedelta(days=1))
        response = self.client.get(reverse('core:my_vacations'), {'page': 2, 'page_size': 25})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['vacations']), 60)
        self.assertTrue(all(v.person_id == self.person.pk for v in response.context['vacations']))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[@id="my-vacations-table"]/tbody/tr')), 60)
        self.assertEqual(doc.xpath('//*[@id="my-vacations-table"]/@data-pagination'), ['off'])
        self.assertFalse(doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," cms-pagination ")]'))

    def test_profile_tables_have_shared_inset_for_table_and_pagination(self):
        for route, pk, headings in [
            ('core:author_detail', self.author.pk, ['author-texts-heading', 'author-archive-heading']),
            ('core:person_detail', self.person.pk, ['person-assignments-heading']),
        ]:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[pk]))
                self.assertEqual(response.status_code, 200)
                doc = html.fromstring(response.content)
                for heading in headings:
                    section = doc.xpath(f'//section[@aria-labelledby="{heading}"]')[0]
                    self.assertTrue(section.xpath('./div[@class="profile-table-inset"]//table'))
                    self.assertFalse(section.xpath('./div[@class="table-container"]'))
                    self.assertFalse(section.xpath('./nav[contains(@class,"pagination")]'))

    def test_email_is_first_text_detail_and_both_note_actions_use_save(self):
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.story.pk]))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        email = doc.xpath('//a[@href="mailto:author57@example.test"]')[0]
        self.assertEqual(email.xpath('ancestor::dl[1]/div[1]/dt/text()'), ['E-mail autora'])
        for name in ['coordinator_note', 'content_warnings']:
            buttons = doc.xpath(f'//textarea[@name="{name}"]/ancestor::form[1]//button[@type="submit"]')
            self.assertTrue(buttons, name)
            self.assertEqual([' '.join(b.text_content().split()) for b in buttons], ['Zapisz'])
