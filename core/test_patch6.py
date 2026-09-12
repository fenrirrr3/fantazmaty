import uuid
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin
from core.forms import VacationForm
from core.models import AnthologyCorrection
from people.models import Vacation
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class Patch6Tests(CoreTestDataMixin, TestCase):
    def test_daily_vacation_calendar_and_limits(self):
        today = timezone.localdate()
        self.assertEqual(VacationForm().fields['end_date'].widget.attrs['step'], '1')
        for days in (0, 1, 2, 29, 30, 31, 365, 366, -1):
            form = VacationForm({'start_date': today.isoformat(), 'end_date': (today+timedelta(days=days)).isoformat()})
            self.assertEqual(form.is_valid(), 0 <= days <= 365, form.errors)
        form = VacationForm({'start_date':(today+timedelta(days=5)).isoformat(),'end_date':today.isoformat()})
        self.assertFalse(form.is_valid())

    def test_text_choices_and_continue_are_validated_and_idempotent(self):
        self.client.force_login(self.reviewer)
        text = Text.objects.create(title='Pierwszy tekst', length=1000, anthology=self.anthology)
        other = Anthology.objects.create(title='Inna antologia')
        wrong = Text.objects.create(title='Inny tekst', length=1000, anthology=other)
        options = self.client.get(reverse('core:correction_texts'), {'anthology':self.anthology.pk}).json()['texts']
        self.assertEqual(options, [{'id':text.pk,'title':text.title}])
        data = {'anthology':self.anthology.pk,'text':text.pk,'fragment':'Fragment','problem':'Błąd','suggestion':'Poprawka','submission_token':str(uuid.uuid4())}
        url = reverse('core:anthology_corrections')
        response = self.client.post(url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200)
        first = response.json()
        self.assertTrue(first['ok'])
        self.assertEqual(self.client.post(url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()['id'], first['id'])
        self.assertEqual(AnthologyCorrection.objects.count(),1)
        data.update(submission_token=first['next_token'], fragment='Drugi fragment')
        self.assertEqual(self.client.post(url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest').status_code,200)
        self.assertEqual(AnthologyCorrection.objects.count(),2)
        data.update(text=wrong.pk,submission_token=str(uuid.uuid4()))
        self.assertEqual(self.client.post(url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest').status_code,400)
        self.assertEqual(AnthologyCorrection.objects.count(),2)

    def test_active_leave_is_visible_at_current_stage(self):
        text = Text.objects.create(title='Na urlopie', length=1000, anthology=self.anthology)
        WorkflowStage.objects.create(text=text,stage_type='editing',started_at=timezone.localdate())
        WorkflowRoleAssignment.objects.create(text=text,role='editor',assigned_to=self.editor)
        vacation = Vacation.objects.create(person=self.editor_person,start_date=timezone.localdate(),until_revoked=True)
        self.client.force_login(self.coordinator)
        url = reverse('core:assigned_text_detail',args=[text.pk])
        self.assertContains(self.client.get(url),'Osoba przypisana jest na urlopie')
        vacation.until_revoked=False;vacation.end_date=timezone.now();vacation.save()
        self.assertNotContains(self.client.get(url),'Osoba przypisana jest na urlopie')

    def test_search_ignores_letter_case(self):
        self.editor_person.first_name='Jan';self.editor_person.save()
        self.client.force_login(self.reviewer)
        for query in ('jan','Jan','JAN'):
            response=self.client.get(reverse('core:global_search'),{'query':query})
            self.assertIn(self.editor_person.pk,[row['pk'] for row in response.context['people']])
            self.assertEqual(response.context['authors'],[])

    def test_admin_exposes_corrections_and_log_route_removed(self):
        self.client.force_login(self.superuser)
        response=self.client.get(reverse('admin:index'))
        self.assertContains(response,reverse('admin:core_anthologycorrection_changelist'))
        self.assertContains(self.client.get(reverse('admin:core_anthologycorrection_add')),'data-texts-url')
        self.assertEqual(self.client.get('/organizacja/dziennik-zmian/').status_code,404)

    def test_collapsed_stage_filter_and_menu_order(self):
        self.client.force_login(self.coordinator)
        response=self.client.get(reverse('core:workflow_inactivity'))
        self.assertContains(response,'class="checkbox-dropdown"')
        body=self.client.get(reverse('core:home')).content.decode()
        self.assertLess(body.index(reverse('core:audiobooks')),body.index('>Uwagi do antologii'))
        self.assertNotIn('Dziennik zmian',body)
        self.assertNotIn('data-set-density',body)
