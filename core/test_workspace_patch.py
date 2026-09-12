import re
from html import unescape
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin
from core.forms import VacationForm
from core.models import AnthologyCorrection
from core.services.vacations import create_vacation, finish_vacation
from people.models import Role
from texts.models import Review, Text


class WorkspacePatchTests(CoreTestDataMixin, TestCase):
    def test_dashboard_attention_for_each_role(self):
        for user in (self.superuser, self.coordinator, self.reviewer):
            self.client.force_login(user)
            response = self.client.get(reverse('core:home'))
            self.assertNotContains(response, 'Wymaga twojej uwagi')
            self.assertContains(response, 'Prace w toku')

    def review(self, **kwargs):
        return Review.objects.create(title="Próba powiadomienia", author_first_name="Jan", author_last_name="Próbny",
            email="proba@example.com", genre="fantasy", length=1234, anthology=self.anthology, **kwargs)

    def token(self, response):
        match = re.search(r'name="_edit_version" value="([^"]+)"', response.content.decode())
        self.assertIsNotNone(match)
        return unescape(match[1])

    def test_admin_form_also_rejects_an_outdated_version(self):
        self.client.force_login(self.superuser)
        url = reverse('admin:authors_author_change', args=[self.author.pk])
        token = self.token(self.client.get(url))
        self.author.pseudonym = 'Nowa wartość'
        self.author.save()
        self.assertEqual(self.client.post(url, {'_edit_version':token}).status_code, 409)

    def test_import_requires_preview_and_binds_confirmation_to_records(self):
        self.client.force_login(self.superuser)
        url = reverse('core:review_bulk_import')
        data = {'anthology': self.anthology.pk, 'records':'Jan Próbny;Nowy tytuł;fantasy;1234;przemoc;proba@example.com;123456789'}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Review.objects.exists())
        self.assertContains(response, 'Podgląd importu')
        token = response.context['preview_token']
        changed = dict(data, records=data['records'].replace('Nowy tytuł','Zmieniony'), preview_token=token, import_action='import')
        self.assertEqual(self.client.post(url, changed).status_code, 200)
        self.assertFalse(Review.objects.exists())
        self.assertEqual(self.client.post(url, dict(data, preview_token=token, import_action='import')).status_code, 302)
        self.assertEqual(Review.objects.count(), 1)

    def test_mixed_invalid_import_preserves_row_numbers_and_writes_nothing(self):
        self.client.force_login(self.superuser)
        response = self.client.post(reverse('core:review_bulk_import'), {'anthology':self.anthology.pk,
            'records':'Jan Próbny;Dobry;fantasy;1234;;proba@example.com;123456789\nZły wiersz'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual([row['line'] for row in response.context['preview_rows']], [1,2])
        self.assertTrue(response.context['preview_rows'][1]['errors'])
        self.assertFalse(Review.objects.exists())

    def test_notification_queues_are_private_and_due_dates_are_respected(self):
        due = self.review(status='rejected', is_hidden=True, decision_at=timezone.localdate())
        future = self.review(status='rejected', is_hidden=True, decision_at=timezone.localdate()+timedelta(days=20))
        for user in (self.reviewer, self.coordinator):
            self.client.force_login(user)
            for name in ('notification_queue','scheduled_rejections'):
                self.assertEqual(self.client.get(reverse('core:'+name)).status_code,403)
        self.client.force_login(self.superuser)
        self.assertEqual([r.pk for r in self.client.get(reverse('core:notification_queue')).context['reviews']], [due.pk])
        self.assertEqual([r.pk for r in self.client.get(reverse('core:scheduled_rejections')).context['reviews']], [future.pk])

    def test_hidden_review_can_be_released_only_with_confirmation_by_superuser(self):
        item = self.review(is_hidden=True, status='rejected', decision_at=timezone.localdate()+timedelta(days=20))
        url = reverse('core:release_hidden_review', args=[item.pk])
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(url, {'confirm':'yes'}).status_code,403)
        self.client.force_login(self.superuser)
        self.client.post(url, {})
        item.refresh_from_db(); self.assertTrue(item.is_hidden)
        self.client.post(url, {'confirm':'yes'})
        item.refresh_from_db(); self.assertFalse(item.is_hidden); self.assertEqual(item.status,'new'); self.assertIsNone(item.decision_at)

    def test_stale_text_form_is_rejected(self):
        item = Text.objects.create(title='Tekst', length=1234, anthology=self.anthology)
        self.client.force_login(self.coordinator)
        page = self.client.get(reverse('core:assigned_text_detail',args=[item.pk]))
        token = self.token(page)
        url = reverse('core:update_coordinator_note', args=[item.pk])
        first = self.client.post(url, {'coordinator_note':'Pierwsza zmiana','_edit_version':token})
        self.assertEqual(first.status_code,302)
        self.assertEqual(self.client.post(url, {'coordinator_note':'Stara karta','_edit_version':token}).status_code,409)
        item.refresh_from_db(); self.assertEqual(item.coordinator_note,'Pierwsza zmiana')
        self.client.force_login(self.reviewer)

    def test_corrections_create_bulk_update_and_stale_bulk_rejected(self):
        self.client.force_login(self.reviewer)
        url = reverse('core:anthology_corrections')
        text = Text.objects.create(title='Opowiadanie', length=1000, anthology=self.anthology)
        data = {'anthology':self.anthology.pk,'text':text.pk,'fragment':'zdanie','problem':'literówka','suggestion':'poprawka'}
        self.assertEqual(self.client.post(url,data).status_code,302)
        row=AnthologyCorrection.objects.get(); version=row.updated_at.isoformat()
        post={'selected':[row.pk],'status':'accepted',f'version_{row.pk}':version}
        bulk=reverse('core:correction_status')
        self.assertEqual(self.client.post(bulk,post).status_code,403)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.post(bulk,post).status_code,302)
        row.refresh_from_db(); self.assertEqual(row.status,'accepted')
        self.assertEqual(self.client.post(bulk,dict(post,status='rejected')).status_code,409)
        row.refresh_from_db(); self.assertEqual(row.status,'accepted')
        self.assertContains(self.client.get(url), 'Opowiadanie')

    def test_vacation_date_only_and_immediate_finish(self):
        today=timezone.localdate()
        self.assertEqual(VacationForm().initial['start_date'],today)
        form=VacationForm({'start_date':today.isoformat(),'end_date':today.isoformat()})
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(timezone.localtime(form.cleaned_data['end_date']).hour,23)
        vacation=create_vacation(user=self.reviewer, person_id=self.reviewer_person.pk,
            start_date=today,end_date=form.cleaned_data['end_date'])
        self.assertTrue(vacation.is_active)
        finish_vacation(user=self.reviewer,vacation_id=vacation.pk)
        vacation.refresh_from_db(); self.assertFalse(vacation.is_active)

    def test_illustrations_restricted_by_role_and_navigation(self):
        url=reverse('illustrations:illustration_list')
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(url).status_code,403)
        self.assertNotContains(self.client.get(reverse('core:home')), f'href="{url}"')
        role,_=Role.objects.get_or_create(name='Ilustrator'); self.reviewer_person.roles.add(role)
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertContains(self.client.get(reverse('core:home')), f'href="{url}"')

    def test_table_action_columns_removed_and_footer_follows_table(self):
        self.client.force_login(self.coordinator)
        for name in ('text_list','proofreader_activity','verifier_activity','reviewer_activity'):
            response=self.client.get(reverse('core:'+name)); content=response.content.decode()
            self.assertEqual(response.status_code,200)
            self.assertNotIn('>Szczegóły</th>',content)
            self.assertGreater(content.index('class="page-size-form"'),content.index('</table>'))
