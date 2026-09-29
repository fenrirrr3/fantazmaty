from html.parser import HTMLParser
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from texts.models import Anthology, Review
from authors.models import Author


class FormControls(HTMLParser):
    def __init__(self, target):
        super().__init__();self.fields={};self.forms=[];self.buttons=[];self.target=target;self.in_target=False
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':
            self.forms.append(attrs)
            self.in_target = attrs.get('action') == self.target
        if self.in_target and tag in ('input','select','textarea') and attrs.get('name'):
            self.fields[attrs['name']]=attrs.get('value','')
        if self.in_target and tag=='button':self.buttons.append(attrs)
    def handle_endtag(self,tag):
        if tag=='form':self.in_target=False


class BulkFormRegressionTests(TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        override=self.settings(ACTIVITY_SPOOL_DIR=self.tmp.name);override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_superuser('admin','admin@example.com','unused-password')
        self.client.force_login(self.user)
        self.book=Anthology.objects.create(title='Nabór')
        self.url=reverse('core:review_bulk_submit')

    def parse(self,response):
        parser=FormControls(self.url);parser.feed(response.content.decode());return parser

    def test_initial_page_has_actual_bulk_controls(self):
        response=self.client.get(reverse('core:review_create'))
        self.assertContains(response,'<textarea name="records"')
        self.assertContains(response,'id="id_bulk_anthology"')
        self.assertContains(response,'id="id_bulk_records"')
        page=self.parse(response)
        self.assertIn(self.url,[f.get('action') for f in page.forms])
        self.assertTrue(any(b.get('value')=='preview' for b in page.buttons))
        self.assertTrue(any(b.get('value')=='import' and 'disabled' in b for b in page.buttons))

    def test_html_preview_token_can_be_submitted(self):
        data={'anthology':str(self.book.pk),'records':'Jan Autor;Test importu;fantasy;1000;;jan@example.com;','import_action':'preview'}
        response=self.client.post(self.url,data)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Test importu')
        page=self.parse(response)
        self.assertTrue(page.fields.get('preview_token'))
        self.assertTrue(any(b.get('value')=='import' and 'disabled' not in b for b in page.buttons))
        self.assertEqual(Review.objects.count(),0)
        data.update(preview_token=page.fields['preview_token'],import_action='import')
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        self.assertEqual(Review.objects.count(),1)

    def test_invalid_input_is_preserved_and_blocks_save(self):
        response=self.client.post(self.url,{'anthology':self.book.pk,'records':'Źle wklejony wiersz','import_action':'preview'})
        self.assertEqual(response.status_code,400)
        self.assertContains(response,'Źle wklejony wiersz',status_code=400)
        self.assertContains(response,'Import nie został wykonany',status_code=400)
        self.assertTrue(any(b.get('value')=='import' and 'disabled' in b for b in self.parse(response).buttons))
        self.assertFalse(Review.objects.exists())

    def test_blacklist_confirmation_is_available_in_rendered_form(self):
        Author.objects.create(first_name='Jan',last_name='Autor',email='jan@example.com',is_blacklisted=True)
        data={'anthology':self.book.pk,'records':'Jan Autor;Test ostrzeżenia;fantasy;1000;;jan@example.com;','import_action':'preview'}
        response=self.client.post(self.url,data)
        self.assertEqual(response.status_code,400)
        self.assertContains(response,'Sprawdź zgłoszenia przed importem',status_code=400)
        page=self.parse(response)
        self.assertTrue(page.fields.get('submission_warnings_token'))
        data.update(confirm_submission_warnings='on',submission_warnings_token=page.fields['submission_warnings_token'])
        response=self.client.post(self.url,data)
        self.assertEqual(response.status_code,200)
        data.update(preview_token=self.parse(response).fields['preview_token'],import_action='import')
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        review=Review.objects.get();self.assertTrue(review.is_hidden)
