import csv
import importlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from lxml import html
from texts.models import Text, Review, Anthology
from illustrations.models import Illustration
from core.edit_versions import version_of
from texts.management.commands.import_tags_genres import COLUMNS
from workflow.tests import create_member


class GenreTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.user = create_member('genre-member', 'Recenzent')
        self.client.force_login(self.user)
        self.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        self.text = Text.objects.create(title='Dawny tekst', anthology=self.book, length=100, tags='magia')
        self.url = reverse('core:assigned_text_detail', args=[self.text.pk])
        self.save_url = reverse('core:update_text_tags', args=[self.text.pk])

    def token(self):
        doc = html.fromstring(self.client.get(self.url).content)
        return doc.xpath('//form[contains(@class,"text-tags-form")]/input[@name="_edit_version"]/@value')[0]

    def review(self, genre='Fantasy'):
        return Review.objects.create(title=self.text.title, anthology=self.book, copied_text=self.text, length=100,
                                     genre=genre, author_first_name='Jan', author_last_name='Testowy')

    def post(self, **kwargs):
        return self.client.post(self.save_url, dict(tags='magia, las', genre='groza', _edit_version=self.token(), **kwargs))

    def run_import(self, rows=None, apply=False, fails=False, name='report'):
        path = self.path / 'input.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle, delimiter=';'); writer.writerow(COLUMNS)
            writer.writerows(rows or [[1, '', self.book.title, self.text.title, 'magia, kosmos', 'science fiction']])
        report = self.path / (name+'.html')
        def run():
            call_command('import_tags_genres', str(path), apply=apply, report=str(report), stdout=io.StringIO())
        if fails:
            with self.assertRaises(CommandError): run()
        else: run()
        return json.loads(report.with_suffix('.json').read_text(encoding='utf-8'))

    def test_member_can_save_tags_and_genre_without_review(self):
        version=version_of(self.text)
        self.assertEqual(self.post().status_code,302)
        self.text.refresh_from_db()
        self.assertEqual((self.text.tags,self.text.genre),('magia, las','groza'))
        self.assertEqual(version_of(self.text),version+1)
        self.assertFalse(Review.objects.exists())
        self.assertContains(self.client.get(self.url),'groza')
        doc=html.fromstring(self.client.get(self.url).content)
        self.assertEqual(doc.xpath('//dt[text()="Gatunek"]/following-sibling::dd/text()'), ['groza'])

    def test_review_genre_is_not_overwritten(self):
        review=self.review()
        self.post(); review.refresh_from_db()
        self.assertEqual(review.genre,'Fantasy')

    def test_empty_genre_can_be_cleared_without_review_fallback(self):
        self.review(); self.text.genre='groza';self.text.save(update_fields=['genre'])
        response=self.client.post(self.save_url,{'tags':'magia','genre':'','_edit_version':self.token()})
        self.assertEqual(response.status_code,302)
        self.text.refresh_from_db();self.assertEqual(self.text.genre,'')
        self.assertEqual(Illustration.objects.get(text=self.text).genre_display,'')

    def test_old_tags_only_form_does_not_clear_genre(self):
        self.text.genre='groza';self.text.save(update_fields=['genre'])
        self.client.post(self.save_url,{'tags':'las','_edit_version':self.token()})
        self.text.refresh_from_db();self.assertEqual(self.text.genre,'groza')

    def test_oversized_genre_rolls_back_tags_and_displays_error(self):
        response=self.client.post(self.save_url,{'tags':'changed','genre':'g'*101,'_edit_version':self.token()})
        self.assertEqual(response.status_code,400)
        self.text.refresh_from_db();self.assertEqual((self.text.tags,self.text.genre),('magia',''))

    def test_stale_form_cannot_overwrite_genre(self):
        old=self.token();self.post()
        response=self.client.post(self.save_url,{'tags':'old','genre':'old','_edit_version':old})
        self.assertEqual(response.status_code,409)
        self.text.refresh_from_db();self.assertEqual(self.text.genre,'groza')

    def test_genre_normalization_and_escaped_rendering(self):
        self.client.post(self.save_url,{'tags':'magia','genre':'  <b>groza</b>  ','_edit_version':self.token()})
        self.text.refresh_from_db();self.assertEqual(self.text.genre,'<b>groza</b>')
        response=self.client.get(reverse('core:tag_list'))
        self.assertContains(response,'&lt;b&gt;groza&lt;/b&gt;')
        self.assertNotContains(response,'<b>groza</b>')

    def test_list_filters_search_and_sorts_text_genre(self):
        self.review('Inny gatunek'); self.text.genre='Groza';self.text.save(update_fields=['genre'])
        other=Text.objects.create(title='Inny',length=1,genre='Fantasy')
        for params in ({'genre':'Groza'},{'q':'Groza'}):
            result=self.client.get(reverse('core:tag_list'),params)
            self.assertEqual([x.pk for x in result.context['page_obj']],[self.text.pk])
        result=self.client.get(reverse('core:tag_list'),{'sort':'genre'})
        self.assertEqual([x.pk for x in result.context['page_obj']],[other.pk,self.text.pk])

    def test_layout_contains_stacked_fields_in_same_card(self):
        doc=html.fromstring(self.client.get(self.url).content)
        cards=doc.xpath('//div[@class="text-notes-tags"]/section')
        self.assertEqual(len(cards),2)
        self.assertEqual(cards[1].xpath('.//textarea/@name'),['tags','genre'])

    def test_migration_backfill_preserves_existing_values(self):
        review=self.review('Fantasy')
        migration=importlib.import_module('texts.migrations.0021_text_genre')
        migration.copy_existing_genres(apps,SimpleNamespace(connection=connection))
        self.text.refresh_from_db();self.assertEqual(self.text.genre,'Fantasy')
        self.text.genre='Groza';self.text.save(update_fields=['genre'])
        migration.copy_existing_genres(apps,SimpleNamespace(connection=connection))
        self.text.refresh_from_db();review.refresh_from_db()
        self.assertEqual(self.text.genre,'Groza');self.assertEqual(review.genre,'Fantasy')

    def test_admin_prefill_preserves_source_genre(self):
        from texts.admin import review_text_initial, review_source_signature
        review=self.review('Fantasy')
        self.assertEqual(review_text_initial(review)['genre'],'Fantasy')
        signature=review_source_signature(review)
        review.genre='Groza'
        self.assertNotEqual(signature,review_source_signature(review))

    def test_illustration_reads_canonical_text_genre(self):
        self.review('Fantasy'); self.text.genre='Groza';self.text.save(update_fields=['genre'])
        self.assertEqual(Illustration.objects.get(text=self.text).genre_display,'Groza')

    def test_import_dry_run_without_review(self):
        report=self.run_import()
        self.assertEqual(report['status'],'KONTROLA_OK')
        self.text.refresh_from_db();self.assertEqual(self.text.genre,'')

    def test_import_saves_both_fields_without_creating_review_and_is_idempotent(self):
        version=version_of(self.text)
        report=self.run_import(apply=True)
        self.assertEqual(report['status'],'ZAPISANO')
        self.text.refresh_from_db();self.assertEqual((self.text.tags,self.text.genre),('magia, kosmos','science fiction'))
        self.assertFalse(Review.objects.exists())
        self.assertEqual(version_of(self.text),version+1)
        again=self.run_import(apply=True,name='again')
        self.assertEqual(again['summary']['written'],0)

    def test_import_preserves_review_and_appends_existing_genre(self):
        review=self.review('Fantasy');self.text.genre='groza';self.text.save(update_fields=['genre'])
        self.run_import(apply=True)
        review.refresh_from_db();self.text.refresh_from_db()
        self.assertEqual(review.genre,'Fantasy');self.assertEqual(self.text.genre,'groza, science fiction')

    def test_import_mismatch_prevents_all_writes(self):
        self.run_import([[1,'','',self.text.title,'x','y'],[2,'','','Błędny tytuł','z','v']],apply=True,fails=True)
        self.text.refresh_from_db();self.assertEqual((self.text.tags,self.text.genre),('magia',''))

    def test_import_write_exception_rolls_back_all_records(self):
        other=Text.objects.create(title='Drugi',length=100)
        original=Text.save
        def fail(obj,*args,**kwargs):
            if obj.pk==other.pk: raise RuntimeError('test rollback')
            return original(obj,*args,**kwargs)
        with patch.object(Text,'save',fail):
            self.run_import([[1,'','',self.text.title,'x','y'],[2,'','',other.title,'z','v']],apply=True,fails=True)
        self.text.refresh_from_db();other.refresh_from_db()
        self.assertEqual((self.text.tags,self.text.genre,other.tags,other.genre),('magia','','',''))

    def test_import_merged_genre_limit_prevents_all_writes(self):
        self.text.genre='x'*95;self.text.save(update_fields=['genre'])
        self.run_import(apply=True,fails=True)
        self.text.refresh_from_db();self.assertEqual(self.text.tags,'magia')
