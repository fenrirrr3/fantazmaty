"""Strona audiodeskrypcji: treść, uwagi, etapy pracy i ich zgodność ze statusem zadania AD."""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.edit_versions import version_of
from core.models import AudioDescriptionNote
from fantazmaty.testing import edit_token

from .support import AudioDescriptionTestData


class AudioDescriptionContentTests(AudioDescriptionTestData, TestCase):
    def setUp(self):
        super().setUp()
        self.assign_writer_and_controller()

    def test_every_team_member_saves_content_and_notes_but_not_the_stage(self):
        for user in (self.writer, self.controller, self.manager, self.admin, self.other):
            response = self.post(user, action='content', content=f'Treść {user.pk}\nDrugi akapit.')
            self.assertEqual(response.status_code, 302)
            self.description.refresh_from_db()
            self.assertEqual(self.description.content, f'Treść {user.pk}\nDrugi akapit.')
        self.assertEqual(self.post(self.other, action='note', note='Uwaga spoza zespołu AD').status_code, 302)
        self.assertTrue(AudioDescriptionNote.objects.filter(content='Uwaga spoza zespołu AD').exists())
        self.assertEqual(self.post(self.other, action='stage', stage='consultation').status_code, 403)
        self.client.force_login(self.other)
        response = self.client.get(self.detail)
        self.assertContains(response, 'Drugi akapit.')
        self.assertContains(response, 'name="content"')
        self.assertContains(response, 'name="note"')
        self.assertNotContains(response, 'name="stage"')

    def test_completion_is_final_on_page_and_reopening_keeps_stage_consistent(self):
        """Ukończonej AD nie da się cofnąć na stronie AD ani zdjąć z antologii; wznawia się ją w panelu admina."""
        self.client.force_login(self.writer)
        self.assertIsNone(self.client.get(self.detail).context['form'])
        self.assertEqual(self.post(self.writer, action='assignment', status='ready').status_code, 403)
        for stage in ('consultation', 'proofreading', 'completed'):
            self.assertEqual(self.post(self.controller, action='stage', stage=stage).status_code, 302)
            self.task.refresh_from_db()
            self.assertEqual(self.task.status, 'ready' if stage == 'completed' else 'commissioned')
        for stage in ('proofreading', 'writing'):
            self.assertEqual(self.post(self.writer, action='stage', stage=stage).status_code, 400)
        self.task.refresh_from_db()
        self.description.refresh_from_db()
        self.assertEqual((self.task.status, self.description.stage), ('ready', 'completed'))
        # Strona antologii blokuje status ukończonego zadania i nie zdejmuje przypisania.
        self.client.force_login(self.admin)
        anthology = reverse('core:anthology_detail', args=[self.book.pk])
        form = next(f for f in self.client.get(anthology).context['task_forms']
                    if f.instance.task_type == 'audio_description')
        self.assertTrue(form.fields['status'].disabled)
        self.client.post(anthology, {'remove_task': 'audio_description',
                                     '_edit_version': edit_token(self.admin, self.book)})
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.assigned_to), ('ready', self.writer.person_profile))
        # Wznowienie (ta sama ścieżka co panel admina) wraca do ostatniego etapu roboczego.
        self.task.status = 'commissioned'
        self.task.save()
        self.description.refresh_from_db()
        self.assertEqual(self.description.stage, 'writing')
        self.description.content = 'Szkic'
        self.description.save(update_fields=['content'])
        self.task.status = 'ready'
        self.task.save()
        self.description.refresh_from_db()
        self.assertEqual(self.description.stage, 'completed')
        self.task.status = 'commissioned'
        self.task.save()
        self.description.refresh_from_db()
        self.assertEqual(self.description.stage, 'proofreading')

    def test_finishing_unassigned_and_invalid_stage_do_not_save(self):
        self.task.assigned_to = None
        self.task.status = 'not_commissioned'
        self.task.save()
        for stage in ('completed', 'invalid'):
            self.assertEqual(self.post(self.manager, action='stage', stage=stage).status_code, 400)
        self.task.refresh_from_db()
        self.description.refresh_from_db()
        self.assertEqual(self.task.status, 'not_commissioned')
        self.assertEqual(self.description.stage, 'writing')

    def test_notes_stamp_real_author_and_time_escape_html_and_preserve_previous_entries(self):
        before = timezone.now()
        response = self.post(self.controller, action='note', note='<script>alert(1)</script> Uwaga',
                             author=self.admin.pk, author_name='Podszycie', created_at='2000-01-01')
        self.assertEqual(response.status_code, 302)
        note = self.description.notes.get()
        self.assertEqual(note.author, self.controller)
        self.assertEqual(note.author_name, str(self.controller.person_profile))
        self.assertGreaterEqual(note.created_at, before)
        self.assertEqual(self.post(self.writer, action='note', note='Druga uwaga').status_code, 302)
        self.assertEqual(self.description.notes.count(), 2)
        response = self.client.get(self.detail)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertEqual(self.post(self.writer, action='note', note='   ').status_code, 400)
        self.assertEqual(self.description.notes.count(), 2)

    def test_notes_invalidate_stale_content_and_content_save_does_not_reopen_completed_task(self):
        stale = edit_token(self.writer, self.book)
        self.post(self.controller, action='note', note='Uwzględnij zmianę')
        self.client.force_login(self.writer)
        response = self.client.post(self.detail, {'_edit_version': stale, 'action': 'content',
                                                  'content': 'Stara wersja'})
        self.assertEqual(response.status_code, 409)
        self.post(self.manager, action='stage', stage='completed')
        self.post(self.writer, action='content', content='Ostateczna treść')
        self.task.refresh_from_db()
        self.description.refresh_from_db()
        self.assertEqual(self.task.status, 'ready')
        self.assertEqual(self.description.stage, 'completed')

    def test_bulk_note_edit_invalidates_parent_form(self):
        self.post(self.controller, action='note', note='Pierwsza treść')
        previous = version_of(self.book)
        self.description.notes.update(content='Zmieniona uwaga')
        self.assertGreater(version_of(self.book), previous)

    def test_revoked_controller_cannot_save_and_unknown_action_is_bad_request(self):
        self.description.controllers.clear()
        self.assertEqual(self.post(self.controller, action='stage', stage='consultation').status_code, 403)
        self.assertEqual(self.post(self.writer, action='unknown').status_code, 400)
        self.assertFalse(AudioDescriptionNote.objects.exists())

    def test_list_labels_hide_completed_and_detail_has_independent_forms(self):
        self.post(self.writer, action='stage', stage='completed')
        response = self.client.get(self.list_url)
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertFalse(response.context['hide_completed'])
        self.assertContains(response, '<th>Kto pisze</th>')
        self.assertContains(response, '<th>Konsultacja</th>')
        self.assertEqual(self.client.get(self.list_url, {'hide_completed': '1'}).context['page_obj'].paginator.count, 0)
        self.client.force_login(self.manager)
        doc = html.fromstring(self.client.get(self.detail).content)
        self.assertEqual(len(doc.xpath('//div[contains(@class, "audiodesc-columns")]/section')), 2)
        # Ukończony etap jest zablokowany; przypisanie, treść i uwagi pozostają dostępne.
        self.assertEqual(doc.xpath('//form/input[@name="action"]/@value'), ['assignment', 'content', 'note'])
        self.assertTrue(doc.xpath('//select[@name="controllers"][@multiple][@data-person-multiple]'))
