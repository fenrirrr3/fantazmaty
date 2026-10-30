"""Czytelność panelu admina: polskie tytuły stron i okruszki bez odmiany z tłumaczenia Django, polskie podpisy pól
widocznych w panelu, pola wyboru przed podpisem we własnych formularzach (usunięcie tekstu, powtórzenie, test
powiadomień) i podpis nad polem z opisem pod spodem."""
from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.models import AnthologyCorrection
from fantazmaty.testing import create_superuser
from texts.models import Anthology, Text

# Słowa identyczne po polsku i angielsku albo celowo bez tłumaczenia.
SAME_IN_POLISH = {'status', 'port', 'folder', 'fragment', 'uid', 'id', 'portfolio'}


def tree(response):
    return html.fromstring(response.content.decode())


class AdminTitlesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('tytuly-admin')
        cls.book = Anthology.objects.create(title='Fantazmaty 7')
        cls.correction = AnthologyCorrection.objects.create(anthology=cls.book, problem='Literówka')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_page_titles_and_breadcrumbs(self):
        """Lista – nazwa w liczbie mnogiej, dodawanie i edycja – „Dodawanie: …”, „Edycja: …” (także w okruszkach);
        własny tytuł widoku (recenzje) zostaje."""
        for url, expected in {
            reverse('admin:core_anthologycorrection_changelist'): 'Uwagi do antologii',
            reverse('admin:core_anthologycorrection_add'): 'Dodawanie: uwaga do antologii',
            reverse('admin:core_anthologycorrection_change', args=[self.correction.pk]): 'Edycja: uwaga do antologii',
            reverse('admin:texts_review_changelist'): 'Recenzje',
            reverse('admin:texts_review_add'): 'Dodaj zgłoszenie do recenzji',
        }.items():
            with self.subTest(url):
                page = tree(self.client.get(url))
                self.assertEqual(page.xpath('string(//div[@id="content"]/h1)').strip(), expected)
                self.assertNotIn('do zmiany', page.xpath('string(//title)'))
        breadcrumbs = tree(self.client.get(reverse('admin:core_anthologycorrection_add'))).xpath(
            'string(//div[@class="breadcrumbs"])')
        self.assertIn('Dodawanie: uwaga do antologii', ' '.join(breadcrumbs.split()))

    def test_read_only_record_has_view_title(self):
        """Rekord tylko do odczytu (historia zmian rang Discorda) – „Podgląd: …”."""
        from core.notification_models import DiscordRoleChange
        change = DiscordRoleChange.objects.create(discord_user_id='100000000000000001', role_label='@r',
                                                  role_id='500000000000000001', action='grant', result='done',
                                                  reason='test', source='panel')
        page = tree(self.client.get(reverse('admin:core_discordrolechange_change', args=[change.pk])))
        self.assertTrue(page.xpath('string(//div[@id="content"]/h1)').strip().startswith('Podgląd: '))

    def test_admin_visible_fields_have_polish_labels(self):
        """Pola z list, filtrów i formularzy panelu (także formularzy wierszowych) nie mają automatycznych
        angielskich podpisów („Updated at”, „Assignment”)."""
        request = RequestFactory().get('/')
        request.user = self.admin
        missing = []
        for model, model_admin in admin.site._registry.items():
            names = {name for name in (*model_admin.list_display, *model_admin.list_filter,
                                       *model_admin.readonly_fields) if isinstance(name, str)}
            names |= set(model_admin.get_fields(request, None))
            pairs = [(model, name) for name in names]
            for inline in model_admin.inlines:
                pairs += [(inline.model, name) for name in (*(inline.fields or ()), *inline.readonly_fields)
                          if isinstance(name, str)]
            for owner, name in pairs:
                try:
                    field = owner._meta.get_field(name)
                except Exception:  # noqa: BLE001 – pole wyliczane albo metoda admina
                    continue
                if str(field.verbose_name) == field.name.replace('_', ' ') and field.name not in SAME_IN_POLISH:
                    missing.append(f'{owner._meta.label}.{field.name}')
        self.assertEqual(sorted(set(missing)), [])


class OwnFormsLayoutTests(TestCase):
    """Własne formularze panelu: pole wyboru w podpisie przed jego tekstem, pozostałe pola z podpisem nad polem."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('formularze-admin')
        cls.text = Text.objects.create(title='Smocza przystań', length=100,
                                       anthology=Anthology.objects.create(title='Fantazmaty 7'))
        cls.text.authors.add(Author.objects.create(first_name='Jan', last_name='Autor'))

    def setUp(self):
        self.client.force_login(self.admin)

    def check_boxes(self, page):
        boxes = page.xpath('//form//input[@type="checkbox"]')
        for box in boxes:
            label = box.getparent()
            self.assertEqual((label.tag, label.get('for')), ('label', box.get('id')))
            self.assertIn('cms-admin-check', label.getparent().get('class'))
            self.assertTrue(label.xpath('normalize-space(./span)'))
        return boxes

    def test_purge_repeat_and_notification_test_forms(self):
        from core.models import NotificationRule
        pages = {
            'usunięcie tekstu': (reverse('admin:texts_text_purge', args=[self.text.pk]), ['confirm']),
            'test powiadomień': (reverse('admin:core_notificationrule_test'), ['mark_test']),
        }
        for name, (url, expected) in pages.items():
            with self.subTest(name):
                page = tree(self.client.get(url))
                self.assertEqual([box.get('name') for box in self.check_boxes(page)], expected)
        page = tree(self.client.get(reverse('admin:core_notificationrule_test')))
        self.assertTrue(page.xpath('//table[@class="notification-test-data"]//input[@name="title"]'))
        self.assertTrue(page.xpath('//table[@class="notification-test-data"]//p[@class="help"]'))
        self.assertTrue(NotificationRule.objects.exists())
        from workflow.models import WorkflowStage
        WorkflowStage.objects.create(text=self.text, stage_type='editing')
        repeat = tree(self.client.get(reverse('admin:texts_text_repeat', args=[self.text.pk])))
        field = repeat.xpath('//div[@class="cms-admin-field"][.//input[@name="stages"]]')
        self.assertEqual(len(field), 1)
        self.assertEqual(field[0][0].tag, 'label')

    def test_field_with_help_has_label_above_and_help_below(self):
        """Korekta etapu: „Status po usunięciu bieżącego etapu” – podpis nad listą, opis osobno pod nią."""
        from workflow.models import WorkflowStage
        stage = WorkflowStage.objects.create(text=self.text, stage_type='editing')
        page = tree(self.client.get(reverse('admin:workflow_stage_correct', args=[stage.pk])))
        field = page.xpath('//div[@class="cms-admin-field"][.//select[@name="replacement"]]')[0]
        self.assertEqual([child.tag for child in field], ['label', 'select', 'p'])
        self.assertEqual(field[2].get('class'), 'help')
        purge_error = self.client.post(reverse('admin:texts_text_purge', args=[self.text.pk]), {})
        self.assertContains(purge_error, 'errorlist')
