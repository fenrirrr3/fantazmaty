"""Publiczna lista ilustracji do wzięcia: projekcja bez danych prywatnych, status liczony z osób,
zakres, filtry i sortowanie oraz udostępniany link do folderu GDrive."""
from django.templatetags.static import static
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import html

from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustration, Illustrator, PublicIllustrationSettings
from illustrations.public import link_version
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.models import WorkflowStage


class ExternalIllustrationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Migracja 0012 tworzy ustawienia z wytycznymi – testy zaczynają od stanu bez ustawień.
        PublicIllustrationSettings.objects.all().delete()
        cls.admin = create_superuser('external-admin', 'admin@example.test')
        # Rola o ogólnej nazwie „Koordynator” (nie przez create_member, które ją zamienia).
        cls.coordinator = get_user_model().objects.create_user('external-coordinator')
        cls.artist = get_user_model().objects.create_user('external-artist')
        for user, role in ((cls.coordinator, 'Koordynator'), (cls.artist, 'Ilustrator')):
            profile = Person.objects.create(user=user, first_name='Osoba', last_name=role)
            profile.roles.add(Role.objects.get_or_create(name=role)[0])
        cls.book = Anthology.objects.create(title='Antologia publiczna', has_illustrations=True)
        cls.text = Text.objects.create(title='Opowiadanie publiczne', anthology=cls.book, length=100, tags='kosmos, przyjaźń', genre='science fiction', file_url='https://example.test/private-folder')
        cls.illustration, _ = Illustration.objects.get_or_create(text=cls.text)
        Illustration.objects.filter(pk=cls.illustration.pk).update(story_url='https://example.test/private-story', illustrated_excerpt='TAJNY FRAGMENT', coordinator_notes='TAJNE UWAGI')
        cls.url = reverse('illustrations:external_illustrations')
        cls.internal = reverse('illustrations:illustration_list')

    def test_anonymous_projection_is_read_only_without_private_data(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'themes.')
        for value in ('Antologia publiczna', 'Opowiadanie publiczne', 'kosmos', 'science fiction', 'Dostępne'):
            self.assertContains(response, value)
        for value in ('TAJNY FRAGMENT', 'TAJNE UWAGI', 'private-story', 'private-folder', 'admin@example.test', 'site-sidebar'):
            self.assertNotContains(response, value)
        self.assertFalse(PublicIllustrationSettings.objects.exists())
        self.assertEqual(self.client.post(self.url, {'status':'assigned'}).status_code,405)
        for url in (self.internal, reverse('illustrations:illustration_detail', args=[self.illustration.pk]), reverse('core:recruitment_list'), reverse('core:home')):
            self.assertEqual(self.client.get(url).status_code,302)
        self.client.force_login(self.admin)
        # Even a logged-in admin receives the same public column projection.
        self.assertNotContains(self.client.get(self.url),'TAJNE UWAGI')
        self.assertNotContains(self.client.get(reverse('core:home')),self.url)

    def test_contact_notice_above_the_list_links_to_illustrations_mailbox(self):
        """Nad listą jest informacja, jak zgłosić chęć wzięcia ilustracji, z adresem jako linkiem mailto."""
        doc = html.fromstring(self.client.get(self.url).content)
        notice = doc.xpath('//p[contains(@class, "message") and contains(@class, "info")]')[0]
        self.assertEqual(' '.join(notice.text_content().split()),
                         'Chcesz wziąć ilustrację? Napisz na ilustracje@fantazmaty.pl i poinformuj, która cię interesuje.')
        self.assertEqual(notice.xpath('./a/@href'), ['mailto:ilustracje@fantazmaty.pl'])
        self.assertTrue(notice.xpath('following::table'))

    def test_status_uses_people_not_stored_status_and_does_not_reveal_names(self):
        artist = Illustrator.objects.create(first_name='PRYWATNY ILUSTRATOR', email='private-artist@example.test')
        self.illustration.illustrators.add(artist)
        response = self.client.get(self.url, {'status':'Przypisane'})
        self.assertContains(response, self.text.title)
        self.assertNotContains(response, 'PRYWATNY ILUSTRATOR')
        self.assertNotContains(response, 'private-artist@example.test')
        # Zapisany status bez przypisanych osób nie czyni tekstu zajętym.
        self.illustration.illustrators.clear()
        Illustration.objects.filter(pk=self.illustration.pk).update(status='assigned')
        self.assertNotContains(self.client.get(self.url, {'status': 'Przypisane'}), self.text.title)
        self.assertContains(self.client.get(self.url, {'status': 'Dostępne'}), self.text.title)

    def test_scope_filters_sorting_pagination_and_escaping(self):
        second = Text.objects.create(title='<script>nie wykonuj</script>', anthology=self.book, length=1, tags='smoki', genre='fantasy')
        Illustration.objects.get_or_create(text=second)
        result = self.client.get(self.url, {'q':'smoki','sort':'-title'})
        self.assertContains(result,'&lt;script&gt;')
        self.assertNotContains(result,'<script>nie wykonuj')
        self.assertNotContains(result,self.text.title)
        for sort in ('anthology','title','tags','genre','status','-status','coordinator_notes'):
            response = self.client.get(self.url, {'sort':sort,'page_size':'500'})
            self.assertEqual(response.status_code,200)
            self.assertNotContains(response,'TAJNE UWAGI')
        # Historical released catalogue fixture; transition rules are tested elsewhere.
        Anthology.objects.filter(pk=self.book.pk).update(status=Anthology.Status.READY)
        self.assertNotContains(self.client.get(self.url),self.text.title)
        for params in ({}, {'hide_published': '0'}, {'hide_published': '0', 'anthology': str(self.book.pk)}):
            page = self.client.get(self.url, params)
            self.assertNotContains(page, self.text.title)
            self.assertNotContains(page, self.book.title)
            self.assertEqual(page.context['rows'].paginator.count, 0)
            self.assertFalse(list(page.context['anthologies']))
            self.assertNotContains(page, 'name="hide_published"')
        # Test the other exclusions independently of publication status.
        Anthology.objects.filter(pk=self.book.pk).update(status=Anthology.Status.IN_PREPARATION)
        for flag in ('is_translated','is_novel'):
            Anthology.objects.filter(pk=self.book.pk).update(**{flag:True})
            self.assertNotContains(self.client.get(self.url, {'hide_published':'0'}),self.text.title)
            Anthology.objects.filter(pk=self.book.pk).update(**{flag:False})
        WorkflowStage.objects.filter(text=self.text).update(is_current=False)
        WorkflowStage.objects.create(text=self.text, stage_type=WorkflowStage.StageType.WITHDRAWN, is_current=True, is_released=True)
        self.assertNotContains(self.client.get(self.url, {'hide_published':'0'}),self.text.title)

    def test_shared_link_validation_permissions_conflict_and_layout(self):
        self.assertEqual(self.client.post(self.internal, {'drive_url':'https://drive.google.com/x'}).status_code,302)
        self.client.force_login(self.artist)
        self.assertEqual(self.client.post(self.internal, {'drive_url':'https://drive.google.com/x'}).status_code,403)
        self.assertNotContains(self.client.get(self.internal),'data-dialog-open="public-illustration-link"')
        self.client.force_login(self.coordinator)
        page = self.client.get(self.internal)
        doc = html.fromstring(page.content)
        self.assertTrue(doc.xpath('//div[contains(@class, "illustration-public-actions")]/a'))
        public_link = doc.xpath('//a[@href="%s"]' % self.url)[0]
        self.assertEqual(public_link.get("target"), "_blank")
        self.assertEqual(set(public_link.get("rel").split()), {"noopener", "noreferrer"})
        self.assertTrue(doc.xpath('//div[@class="illustration-filter-bottom"]/label/input[@name="hide_published"]'))
        token = link_version(self.coordinator,'')
        for url in ('javascript:alert(1)','https://drive.google.com.evil.test/x','http://drive.google.com/x','https://drive.google.com:wrong/x'):
            self.assertEqual(self.client.post(self.internal, {'drive_url':url,'version':token}).status_code,400)
        self.assertFalse(PublicIllustrationSettings.objects.exists())
        link = 'https://drive.google.com/drive/folders/example?usp=sharing'
        self.assertEqual(self.client.post(self.internal, {'drive_url':link,'version':token}).status_code,302)
        public_page = self.client.get(self.url)
        self.assertContains(public_page, link)
        public_doc = html.fromstring(public_page.content)
        buttons = public_doc.xpath('//div[contains(@class, "filter-actions")]/a')
        self.assertEqual([a.text_content() for a in buttons], ['Wyczyść filtry', 'Otwórz GDrive'])
        self.assertEqual(buttons[-1].get('href'), link)
        self.assertEqual(buttons[-1].get('target'), '_blank')
        self.assertEqual(len(public_doc.xpath('//thead/tr/th')), 6)
        self.assertEqual(len(public_doc.xpath('//tbody/tr[1]/td')), 6)
        self.assertFalse(public_doc.xpath('//table//a[@href="%s"]' % link))
        empty_doc = html.fromstring(self.client.get(self.url, {'q': 'brak-wyników'}).content)
        self.assertEqual(empty_doc.xpath('//tbody/tr/td/@colspan'), ['6'])
        self.assertEqual(len(empty_doc.xpath('//a[@href="%s"]' % link)), 1)
        self.assertEqual(self.client.post(self.internal, {'drive_url':'','version':token}).status_code,409)
        self.assertEqual(PublicIllustrationSettings.objects.get().drive_url,link)
        self.assertEqual(self.client.post(self.internal, {'drive_url':'','version':link_version(self.coordinator,link)}).status_code,302)
        self.assertNotContains(self.client.get(self.url),'Otwórz GDrive')
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.coordinator)
        self.assertEqual(
            csrf.post(self.internal, {"drive_url": link, "version": token}).status_code, 403
        )

    def test_content_warnings_column_shows_text_warnings_only(self):
        """Kolumna „Ostrzeżenia dotyczące treści” pokazuje ostrzeżenia tekstu (puste – „–”).
        Strona ładuje layout.js, który na telefonie zamienia wiersze w karty z etykietami kolumn."""
        self.assertContains(self.client.get(self.url), static('core/layout.js'))
        cases = (
            ('przemoc, śmierć zwierzęcia', 'przemoc, śmierć zwierzęcia'),
            ('przemoc\nkrew', 'przemoc\nkrew'),
            ('', '–'),
        )
        for warnings, expected in cases:
            with self.subTest(warnings=warnings):
                Text.objects.filter(pk=self.text.pk).update(content_warnings=warnings)
                response = self.client.get(self.url)
                doc = html.fromstring(response.content)
                headers = [th.text_content().strip() for th in doc.xpath('//thead/tr/th')]
                self.assertEqual(headers, ['Antologia', 'Tytuł opowiadania', 'Tagi', 'Gatunek', 'Ostrzeżenia dotyczące treści', 'Status'])
                cells = doc.xpath('//tbody/tr[1]/td')
                self.assertEqual(cells[headers.index('Ostrzeżenia dotyczące treści')].text_content(), expected)
                self.assertEqual(set(response.context['rows'][0]), {
                    'text__anthology__title', 'text__title', 'text__tags', 'text__genre', 'text__content_warnings', 'public_status'})

    def test_query_count_does_not_grow_with_rows(self):
        """Liczba zapytań strony publicznej jest taka sama dla 1 i 5 ilustracji (brak N+1)."""
        def count_queries():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(self.url).status_code, 200)
            return len(queries)
        single = count_queries()
        for number in range(4):
            Text.objects.create(title=f'Kolejne opowiadanie {number}', anthology=self.book, length=10, content_warnings=f'ostrzeżenie {number}')
        self.assertEqual(Illustration.objects.filter(text__anthology=self.book).count(), 5)
        self.assertEqual(count_queries(), single)


class SharedLinkVersionTests(TestCase):
    """Zapis wspólnego linku GDrive z podrobioną wersją formularza – 409 bez zapisu."""

    @classmethod
    def setUpTestData(cls):
        cls.coordinator = create_member('koord-link', 'Koordynator redakcji')

    def test_shared_link_with_forged_version_is_refused(self):
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse('illustrations:illustration_list'), {
            'drive_url': 'https://drive.google.com/drive/folders/x', 'version': 'podrobiony-token'})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(PublicIllustrationSettings.objects.exclude(drive_url='').exists())
