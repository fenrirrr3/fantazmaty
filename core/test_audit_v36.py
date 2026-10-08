"""Regressions for the verified audit findings and shared table contracts."""
import json
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory
from django.urls import reverse

from authors.models import Author
from illustrations.models import Illustration
from people.models import Person, Role
from texts.models import Anthology, AnthologyTask, Text, NovelProfile


class AuditFixesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v36admin', 'admin@example.test', 'test')
        cls.member = get_user_model().objects.create_user('v36member', 'member@example.test', 'test')
        person = Person.objects.create(user=cls.member, first_name='Anna', last_name='Zespół', email=cls.member.email)
        person.roles.add(Role.objects.get_or_create(name='Redaktor')[0])
        cls.book = Anthology.objects.create(title='Antologia audytowa')
        cls.story = Text.objects.create(title='Opowiadanie', anthology=cls.book, length=100)
        cls.author = Author.objects.create(first_name='Jan', last_name='TajneNazwisko', pseudonym='Zeta Pseudonim', email='author@example.test')
        cls.story.authors.add(cls.author)

    def setUp(self):
        self.patch = patch('core.activity_spool.enqueue_activity')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client.force_login(self.admin)

    def test_public_author_search_and_sort_use_pseudonym(self):
        other = Author.objects.create(first_name='Zofia', last_name='Zetowska', pseudonym='Alfa Pseudonim', email='other@example.test')
        story = Text.objects.create(title='Drugie', anthology=self.book, length=100)
        story.authors.add(other)
        self.client.force_login(self.member)
        url = reverse('core:tag_list')
        self.assertEqual(list(self.client.get(url, {'q': 'Jan TajneNazwisko'}).context['page_obj']), [])
        self.assertEqual([x.pk for x in self.client.get(url, {'q': 'Zeta'}).context['page_obj']], [self.story.pk])
        for sort, expected in [('author', [story.pk, self.story.pk]), ('-author', [self.story.pk, story.pk])]:
            self.assertEqual([x.pk for x in self.client.get(url, {'sort': sort}).context['page_obj']], expected)
        from core.views.search import _search_texts, _search_authors
        self.assertEqual(_search_texts('TajneNazwisko', include_authors=True), [])
        self.assertEqual(_search_authors('TajneNazwisko', self.admin), [])

    def test_inactive_novel_profile_preserves_metadata_and_shows_recovery_link(self):
        novel = Anthology.objects.create(title='Zmiana typu', is_novel=True)
        profile = NovelProfile.objects.get(anthology=novel)
        profile.authors.add(self.author)
        profile.tags = 'zachowany tag'; profile.save()
        novel.is_novel = False; novel.save()
        from texts.novels import locked_book
        with self.assertRaises(ValidationError), locked_book(novel.pk, self.admin, 'old-token'):
            pass
        url = reverse('admin:texts_novelprofile_change', args=[profile.pk])
        response = self.client.get(url)
        self.assertContains(response, 'Zachowany profil')
        self.assertNotContains(response, reverse('core:novel_detail', args=[novel.pk]))
        from texts.catalog_admin import NovelAdminForm
        from texts.novels import edit_token
        form = NovelAdminForm({'title': novel.title, 'authors': [self.author.pk], 'novel_token': edit_token(novel, self.admin), 'tags': profile.tags}, instance=profile)
        form.novel_user = self.admin
        self.assertFalse(form.is_valid())
        self.assertIn('Publikacja nie jest oznaczona jako powieść', str(form.non_field_errors()))
        novel.is_novel = True; novel.save()
        profile.refresh_from_db()
        self.assertEqual(profile.tags, 'zachowany tag')
        self.assertEqual(list(profile.authors.all()), [self.author])

    def test_historical_person_can_be_edited_without_email_but_active_account_cannot(self):
        from people.admin import PersonAdminForm
        historical = Person.objects.create(first_name='Dawna', last_name='Osoba', email=None)
        data = {'first_name': 'Dawna', 'last_name': 'Nowe nazwisko', 'email': '', 'is_active': 'on'}
        form = PersonAdminForm(data, instance=historical)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().email)
        form = PersonAdminForm({**data, 'user': self.member.pk}, instance=self.member.person_profile)
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_fixture_restore_keeps_ids_and_does_not_generate_related_records(self):
        payload = json.dumps([
            {'model': 'texts.anthology', 'pk': 9999, 'fields': {'title': 'Fixture', 'status': 'in_preparation', 'has_illustrations': True}},
            {'model': 'texts.text', 'pk': 9999, 'fields': {'title': 'Fixture tekst', 'anthology': 9999, 'length': 100}},
            {'model': 'texts.anthologytask', 'pk': 20000, 'fields': {'anthology': 9999, 'task_type': 'blurb', 'status': 'not_commissioned', 'assigned_to': None, 'commissioned_at': None}},
        ])
        for obj in serializers.deserialize('json', payload):
            obj.save()
        self.assertEqual(list(AnthologyTask.objects.filter(anthology_id=9999).values_list('pk', flat=True)), [20000])
        self.assertFalse(Illustration.objects.filter(text_id=9999).exists())
        anthology = Anthology.objects.get(pk=9999)
        anthology.save()
        self.assertSetEqual(set(AnthologyTask.objects.filter(anthology=anthology).values_list('task_type', flat=True)), set(AnthologyTask.TaskType.values))
        self.assertTrue(AnthologyTask.objects.filter(pk=20000).exists())
        self.assertTrue(Illustration.objects.filter(text_id=9999).exists())

    def test_admin_and_model_reject_new_illustration_for_novel(self):
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(anthology=novel, chapter_number=1)
        request = RequestFactory().get('/'); request.user = self.admin
        form = admin.site.get_model_admin(Illustration).get_form(request)({'text': chapter.pk, 'status': 'unassigned'})
        self.assertFalse(form.is_valid())
        self.assertIn('text', form.errors)
        with self.assertRaises(ValidationError):
            Illustration.objects.create(text=chapter)

    def test_illustration_tags_badge_and_hidden_history(self):
        self.book.has_illustrations = True; self.book.save()
        self.story.genre = 'fantasy'; self.story.tags = 'smoki, magia'; self.story.save()
        illustration = Illustration.objects.get(text=self.story)
        response = self.client.get(reverse('illustrations:illustration_detail', args=[illustration.pk]))
        self.assertContains(response, 'smoki, magia')
        self.assertContains(response, 'illustration-status-unassigned')
        self.book.has_illustrations = False; self.book.save()
        illustration.coordinator_notes = 'Zachowane'; illustration.save()
        self.assertEqual(Illustration.objects.get(pk=illustration.pk).coordinator_notes, 'Zachowane')

    def test_tasks_require_coordinator_and_admin_fixed_tasks_cannot_be_deleted(self):
        url = reverse('core:task_list')
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        response = self.client.get(reverse('core:tag_list'))
        self.assertNotContains(response, 'href="' + url + '"')
        request = RequestFactory().get('/'); request.user = self.admin
        model_admin = admin.site.get_model_admin(AnthologyTask)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_repeat_workflow_has_admin_entry(self):
        url = reverse('admin:texts_text_repeat', args=[self.story.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('core:restart_text_workflow', args=[self.story.pk]))
        self.assertContains(response, '_edit_version')
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_additional_paginated_tables_have_sortable_data_headers(self):
        # Header parsing uses stdlib, keeping CI dependencies unchanged.
        from html.parser import HTMLParser
        class Headers(HTMLParser):
            def __init__(self):
                super().__init__(); self.inside = False; self.headers = []
            def handle_starttag(self, tag, attrs):
                if tag == 'th': self.inside = True; self.headers.append('')
            def handle_endtag(self, tag):
                if tag == 'th': self.inside = False
            def handle_data(self, data):
                if self.inside: self.headers[-1] += data
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        novel.novel.authors.add(self.author)
        from illustrations.models import Illustrator
        Illustrator.objects.create(first_name='Artysta')
        self.book.has_illustrations = True; self.book.save()
        for route in ('core:audiobooks', 'core:novel_list', 'core:task_list', 'core:tag_list', 'core:vocabulary_list', 'core:workflow_inactivity', 'illustrations:illustrator_list', 'illustrations:illustration_list'):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                columns = response.context['page_obj'].sort_columns
                parser = Headers(); parser.feed(response.content.decode())
                for label in parser.headers:
                    label = label.strip()
                    if label and label not in {'Akcje', 'Akcja', 'Szczegóły', 'Wybór', 'Porządkowanie'}:
                        self.assertIn(label, columns)
                for key in set(columns.values()):
                    self.assertEqual(self.client.get(reverse(route), {'sort': '-' + key}).status_code, 200)
