from django.urls import reverse

from core.supervision import text_credit_groups, anthology_credit_groups
from core.test_status_assignment_regression import StatusAssignmentFixtures
from people.models import Person, Role
from texts.models import Text
from workflow.handoffs import handoff_stage
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages


class HandoffCreditTests(StatusAssignmentFixtures):
    def setUp(self):
        super().setUp()
        for user in (self.member, self.other):
            user.person_profile.roles.add(Role.objects.get_or_create(name='Redaktor')[0])
        self.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage = S.objects.create(text=self.text, stage_type='editing', assignment=self.assignment,
                                      started_at=self.today)

    def names(self, text=None):
        return {row['label']: row['names'] for row in text_credit_groups(text or self.text)}['Redakcja']

    def handoff(self, person):
        self.stage.refresh_from_db()
        return handoff_stage(self.text, self.admin, stage_id=self.stage.pk,
                             assigned_to_id=person.pk, expected_assignment_id=self.stage.assignment_id,
                             reason='Zmiana wykonawcy')

    def test_recipient_and_previous_person_appear_before_recipient_starts(self):
        self.handoff(self.other)
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.started_at)
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.names(), ['Anna Test', 'Jan Test'])

    def test_handoff_chain_and_completed_stage_do_not_duplicate_people(self):
        self.handoff(self.other)
        self.handoff(self.member)
        S.objects.filter(pk=self.stage.pk).update(started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertEqual(self.names(), ['Anna Test', 'Jan Test'])

    def test_second_execution_recipient_appears_immediately(self):
        run = repeat_stages(self.text, ['editing'], self.admin, assignees={'editor': self.other})
        self.assertIsNone(run.stages.get().started_at)
        self.assertEqual(self.names(), ['Anna Test', 'Jan Test'])

    def test_other_text_handoffs_are_excluded(self):
        Person.objects.create(user=self.admin, email=self.admin.email, first_name='Maria', last_name='Inna')
        other_text = Text.objects.create(title='Inny tekst', anthology=self.book, length=100)
        assignment = A.objects.create(text=other_text, role='editor', assigned_to=self.member)
        stage = S.objects.create(text=other_text, stage_type='editing', assignment=assignment,
                                 started_at=self.today)
        handoff_stage(other_text, self.admin, stage_id=stage.pk, assigned_to_id=self.admin.pk,
                      expected_assignment_id=assignment.pk, reason='Inny tekst')
        self.assertEqual(self.names(), ['Jan Test'])
        self.assertIn('Maria Inna', self.names(other_text))

    def test_completed_publication_list_keeps_its_existing_scope(self):
        self.handoff(self.other)
        groups = {row['label']: row['names'] for row in anthology_credit_groups(self.book)}
        self.assertEqual(groups['Redakcja'], [])

    def test_text_page_credit_section_shows_both_people_once(self):
        self.handoff(self.other)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        section = response.content.decode().split('id="text-credits"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(section.count('Anna Test'), 1)
        self.assertEqual(section.count('Jan Test'), 1)
