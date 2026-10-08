from email.message import EmailMessage
from importlib import import_module
from types import SimpleNamespace
from zipfile import ZipFile

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from lxml import html

from core.models import Recruitment
from core.recruitment_message import name_in_subject
from core.services.recruitment_samples import attachment_archive
from texts.models import Anthology, AnthologyTask


class RecruitmentArchiveTests(SimpleTestCase):
    def test_names_and_flat_safe_files_without_overwrites(self):
        self.assertEqual(name_in_subject('Nowe zgłoszenie do Fantazmatów – Jan Nowak-Kowalski – Redakcja, Korekta'), 'Jan Nowak-Kowalski')
        self.assertEqual(name_in_subject('Nowe zgłoszenie do Fantazmatów – Redakcja'), '')
        self.assertEqual(name_in_subject('Nowe zgłoszenie'), '')
        msg = EmailMessage(); msg['Subject'] = 'Rekrutacja – Jan Nowak – Ilustracje'; msg.set_content('Treść')
        for value in (b'one', b'two'):
            msg.add_attachment(value, maintype='application', subtype='octet-stream', filename='../../próbka.docx')
        with attachment_archive([{'uid': 1, 'raw': msg.as_bytes()}, {'uid': 2, 'raw': msg.as_bytes()}]) as stream, ZipFile(stream) as archive:
            names = archive.namelist()
            self.assertEqual(names[:2], ['Jan Nowak/próbka.docx', 'Jan Nowak/próbka (2).docx'])
            self.assertEqual(len(set(names)), 4)
            self.assertEqual(len({name.split('/')[0] for name in names}), 2)
            self.assertTrue(all(len(name.split('/')) == 2 for name in names))
            self.assertEqual([archive.read(name) for name in names], [b'one', b'two', b'one', b'two'])


class RecruitmentAndTypographyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('v47-admin', 'admin@example.test', 'test')
        self.client.force_login(self.user)

    def test_subject_name_and_notes_below_reason_persist(self):
        record = Recruitment.objects.create(mail_subject='Nowe zgłoszenie do Fantazmatów – Jan Nowak – Redakcja, Korekta')
        response = self.client.get(reverse('core:recruitment_list'), {'sort': 'subject'})
        doc = html.fromstring(response.content)
        self.assertEqual(doc.xpath('string(//td[@class="recruitment-subject"])'), 'Jan Nowak')
        headers = [th.text_content().strip() for th in doc.xpath('//table/thead/tr/th')]
        self.assertEqual(headers, ['E-mail', 'Kto', 'Rola', 'Data wiadomości', 'Przyjęty/Odrzucony', 'Powiadomiony'])
        self.assertEqual(len(doc.xpath('//table/tbody/tr[1]/td')), 6)
        self.assertEqual(doc.xpath('//td[@class="recruitment-subject"]/a/@href'), [reverse('core:recruitment_detail', args=[record.pk])])
        empty = html.fromstring(self.client.get(reverse('core:recruitment_list'), {'q': 'nieistniejący-kandydat'}).content)
        self.assertEqual(empty.xpath('//table/tbody/tr/td/@colspan'), ['6'])
        url = reverse('core:recruitment_detail', args=[record.pk])
        page = self.client.get(url)
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//main//textarea/@name'), ['other-decision_reason', 'other-unofficial_notes'])
        self.assertEqual(self.client.post(url, {'role': 'other', 'version': page.context['sections'][0]['version'], 'other-status': 'accepted',
            'other-decision_reason': 'Tak', 'other-unofficial_notes': 'Prywatna uwaga'}).status_code, 302)
        record.refresh_from_db(); self.assertEqual(record.role_decisions.get(role='other').unofficial_notes, 'Prywatna uwaga')

    def test_new_and_existing_anthologies_get_typography_once_and_edit_form_includes_it(self):
        book = Anthology.objects.create(title='Typografia test')
        task = book.production_tasks.get(task_type='cover_typography')
        self.assertEqual(task.status, 'not_commissioned')
        task.delete()
        migration = import_module('texts.migrations.0031_cover_typography_task')
        editor = SimpleNamespace(connection=SimpleNamespace(alias='default'))
        migration.add_typography_tasks(apps, editor); migration.add_typography_tasks(apps, editor)
        self.assertEqual(book.production_tasks.filter(task_type='cover_typography').count(), 1)
        self.assertContains(self.client.get(reverse('core:anthology_detail', args=[book.pk])), 'cover_typography-status')
        response = self.client.get(reverse('core:task_list'), {'task': 'cover_typography'})
        self.assertEqual([row['name'] for row in response.context['tasks']], ['Typografia okładki'])
        response = self.client.get(reverse('admin:texts_anthologytask_change', args=[book.production_tasks.get(task_type='cover_typography').pk]))
        self.assertContains(response, 'Typografia okładki')
