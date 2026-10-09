import json
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from illustrations.directory import edit_token
from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


class IllustratorPseudonymTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('pseudo-admin', 'admin@example.test', 'test')
        self.client.force_login(self.admin)
        self.artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', pseudonym='Graphos', email='artist@example.test')
        self.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        self.story = Text.objects.create(title='Tekst', anthology=self.book, length=10)
        self.illustration = Illustration.objects.get(text=self.story)
        self.illustration.set_artists([self.artist], status='delivered')

    def test_inactive_email_absent_from_list_and_copy_but_available_in_edit(self):
        url = reverse('illustrations:illustrator_list')
        self.assertContains(self.client.get(url), self.artist.email)
        Illustrator.objects.filter(pk=self.artist.pk).update(is_active=False)
        for headers in ({}, {'HTTP_X_CMS_EMAIL_COPY': '1'}):
            page = self.client.get(url, {'show_inactive': '1', 'q': 'Graphos'}, **headers)
            self.assertContains(page, 'Graphos')
            self.assertNotContains(page, self.artist.email)
            self.assertContains(page, 'Przemek Świszcz (Graphos)')
            self.assertContains(page, 'Ukryty (nieaktywny)')
        self.assertContains(self.client.get(reverse('illustrations:illustrator_edit', args=[self.artist.pk])), self.artist.email)

    def test_pseudonym_in_credits_lists_picker_search_and_order(self):
        self.assertEqual(str(self.artist), 'Przemek Świszcz')
        self.assertEqual(self.illustration.illustrator_display, 'Graphos')
        other = Illustrator.objects.create(first_name='Zyta', last_name='AAA', pseudonym='Zet')
        url = reverse('illustrations:illustrator_list')
        for sort, expected in [('person', [self.artist.pk, other.pk]), ('-person', [other.pk, self.artist.pk])]:
            page = self.client.get(url, {'sort': sort})
            self.assertEqual([p.pk for p in page.context['page_obj']], expected)
        self.assertEqual([p.pk for p in self.client.get(url, {'q': 'graphos'}).context['page_obj']], [self.artist.pk])
        for url in (reverse('illustrations:illustration_list'), reverse('illustrations:illustration_detail', args=[self.illustration.pk]), reverse('core:assigned_text_detail', args=[self.story.pk])):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'Graphos')
            self.assertNotContains(page, 'Przemek Świszcz')
        admin = self.client.get(reverse('admin:illustrations_illustrator_changelist'))
        self.assertContains(admin, 'Przemek Świszcz')
        self.assertContains(admin, 'Graphos')

    def test_edit_pseudonym_and_clear_to_restore_name(self):
        url = reverse('illustrations:illustrator_edit', args=[self.artist.pk])
        for pseudonym, expected in [('  Graphos  II ', 'Graphos II'), ('', 'Przemek Świszcz')]:
            token = edit_token(self.admin, self.artist)
            result = self.client.post(url, {'first_name': 'Przemek', 'last_name': 'Świszcz', 'pseudonym': pseudonym, 'email': self.artist.email, 'is_active': 'on', 'version': token})
            self.assertEqual(result.status_code, 302)
            self.artist.refresh_from_db()
            self.assertEqual(self.artist.display_name, expected)
        doc = html.fromstring(self.client.get(reverse('admin:illustrations_illustrator_change', args=[self.artist.pk])).content)
        self.assertTrue(doc.xpath('//input[@name="pseudonym"]'))

    def test_both_imports_reuse_pseudonym_without_new_contact(self):
        row = {'anthology': self.book.title, 'title': self.story.title, 'author': 'Autor', 'illustrators': ['Graphos'], 'status': 'delivered', 'assigned_at': None, 'illustrated_excerpt': ''}
        with TemporaryDirectory() as tmp:
            for command in ('import_illustration_credits', 'import_illustration_tables'):
                path = Path(tmp) / 'data.json'
                path.write_text(json.dumps({'schema_version': 1, 'kind': 'illustration_tables', 'texts': [row]}), encoding='utf-8')
                try:
                    call_command(command, str(path), apply=True, report=str(Path(tmp) / 'report.json'), stdout=StringIO())
                except CommandError:
                    self.fail((Path(tmp) / 'report.json').read_text(encoding='utf-8'))
                self.assertEqual(Illustrator.objects.count(), 1)
                self.assertEqual(list(self.illustration.illustrators.all()), [self.artist])


class MergeIllustratorTests(TestCase):
    def setUp(self):
        self.target = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', preferences='Fantasy', is_active=False)
        self.source = Illustrator.objects.create(first_name='Graphos', email='graphos@example.test', portfolio='https://example.test/portfolio', preferences='Smoki', covers=True)
        self.other = Illustrator.objects.create(first_name='Inna osoba')
        book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        self.rows = []
        for number, artists in enumerate(([self.source, self.other], [self.target, self.source])):
            text = Text.objects.create(title=f'Tekst {number}', anthology=book, length=10)
            row = Illustration.objects.get(text=text)
            row.assigned_at = date(2020, 2, 3)
            row.set_artists(artists, status='delivered', preserve_assignment_date=True)
            self.rows.append(row)

    def merge(self, apply=False):
        out = StringIO()
        call_command('merge_illustrator', first_name='Przemek', last_name='Świszcz', pseudonym='Graphos', apply=apply, stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_merge_shared_assignments_and_idempotence(self):
        before = list(Illustration.objects.order_by('pk').values())
        revisions = [version_of(row) for row in self.rows]
        self.merge()
        self.target.refresh_from_db(); self.source.refresh_from_db()
        self.assertEqual(self.target.pseudonym, '')
        self.assertIsNone(self.target.email)
        self.assertEqual(self.source.email, 'graphos@example.test')
        self.assertEqual([version_of(row) for row in self.rows], revisions)
        report = self.merge(apply=True)
        self.target.refresh_from_db()
        self.assertFalse(Illustrator.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.target.display_name, 'Graphos')
        self.assertEqual(self.target.email, 'graphos@example.test')
        self.assertEqual(self.target.preferences, 'Fantasy\n\nSmoki')
        self.assertTrue(self.target.covers)
        self.assertFalse(self.target.is_active)
        self.assertEqual(set(self.rows[0].illustrators.values_list('pk', flat=True)), {self.target.pk, self.other.pk})
        self.assertEqual(list(self.rows[1].illustrators.values_list('pk', flat=True)), [self.target.pk])
        self.assertEqual(list(Illustration.objects.order_by('pk').values()), before)
        self.assertTrue(all(version_of(row) > old for row, old in zip(self.rows, revisions)))
        self.assertEqual(report['merged_id'], self.source.pk)
        repeat = self.merge(apply=True)
        self.assertIsNone(repeat['merged_id'])
        self.assertEqual(repeat['before'][0], repeat['after'])

    def test_conflicting_contact_and_ambiguous_name_abort_without_changes(self):
        self.target.email = 'different@example.test'; self.target.save()
        with self.assertRaisesMessage(CommandError, 'email'):
            self.merge(apply=True)
        self.target.refresh_from_db()
        self.assertEqual(self.target.pseudonym, '')
        self.assertTrue(self.rows[0].illustrators.filter(pk=self.source.pk).exists())
        self.target.email = None; self.target.save()
        Illustrator.objects.create(first_name='Graphos')
        with self.assertRaisesMessage(CommandError, 'kilka rekordów'):
            self.merge(apply=True)
        self.assertTrue(Illustrator.objects.filter(pk=self.source.pk).exists())
