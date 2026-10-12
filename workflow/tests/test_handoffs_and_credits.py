"""Przekazania etapów, oddawanie i odwoływanie przydziałów, wycofanie tekstu oraz zasługi (lista osób przy tekście)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.services.texts import change_scheduled_stage, restore_withdrawn_text, withdraw_text
from core.supervision import anthology_credit_groups, integrity_issues, text_credit_groups
from fantazmaty.testing import assign_artists, create_member, create_superuser, form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from illustrations.models import Illustration, Illustrator
from people.models import Person, Vacation
from texts.models import Anthology, Text
from workflow.handoffs import handoff_stage
from workflow.models import WorkflowHandoff, WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages

from .support import add_roles, importing


class HandoffCreditTests(StatusAssignmentFixtures):
    """Zasługi przy tekście: osoba przekazująca i przejmująca, bez duplikatów i adresów e-mail."""

    def setUp(self):
        super().setUp()
        for user in (self.member, self.other):
            add_roles(user, 'Redaktor')
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
        section = response.content.decode().split('id="text-credits"', 1)[1].split('</details>', 1)[0]
        self.assertEqual(section.count('Anna Test'), 1)
        self.assertEqual(section.count('Jan Test'), 1)

    def test_skip_is_not_corrupt_and_homonyms_not_merged(self):
        book = Anthology.objects.create(title="Test")
        t = Text.objects.create(title="Test", anthology=book, length=10)
        S.objects.create(
            text=t, stage_type="fourth_proofreading", is_completed=True, is_skipped=True
        )
        S.objects.create(text=t, stage_type="styling")
        self.assertFalse(
            any(x["label"] == "Sprzeczny zapis zakończenia" for x in integrity_issues())
        )
        with importing():
            for n, (first, last) in enumerate(
                [("Zofia", "Alfa"), ("Adam", "Zeta"), ("Zofia", "Alfa"), ("", "")], 1
            ):
                u = get_user_model().objects.create_user(
                    f"secret{n}@example.com",
                    email=f"secret{n}@example.com",
                    first_name=first,
                    last_name=last,
                )
                if first:
                    Person.objects.create(user=u, email=u.email, first_name=first, last_name=last)
                a = A.objects.create(
                    text=t, role="editor", assigned_to=u, execution_number=n, is_current=False
                )
                S.objects.create(
                    text=t,
                    stage_type="editing",
                    assignment=a,
                    iteration=n,
                    execution_number=n,
                    is_current=False,
                    is_completed=True,
                    imported_completed=True,
                )
        groups = {x["label"]: x["names"] for x in text_credit_groups(t)}
        names = groups["Redakcja"]
        self.assertEqual(names.count("Zofia Alfa"), 2)
        self.assertLess(names.index("Zofia Alfa"), names.index("Adam Zeta"))
        self.assertIn("Brak danych wykonawcy", names)
        self.assertNotIn("@", " ".join(names))



class HandoffRulesTests(StatusAssignmentFixtures):
    """Przekazanie pracy wymaga aktualnego przydziału, innej osoby i otwartego procesu; lista kandydatów to odzwierciedla."""

    def setUp(self):
        super().setUp()
        add_roles(self.member, 'Weryfikator')
        add_roles(self.other, 'Weryfikator')
        self.v1 = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        self.first = S.objects.create(text=self.text, stage_type='first_verification', assignment=self.v1,
                                      started_at=self.today)

    def handoff(self, person_id, expected=None):
        return handoff_stage(self.text, self.admin, stage_id=self.first.pk, assigned_to_id=person_id,
                             expected_assignment_id=expected or self.v1.pk)

    def test_refused_handoffs_keep_the_assignment(self):
        for person_id, expected, message in ((self.other.pk, self.v1.pk + 100, 'Przydział zmienił się'),
                                              (999999, None, 'już nie istnieje'),
                                              (self.member.pk, None, 'Wybierz inną osobę')):
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                self.handoff(person_id, expected)
        S.objects.create(text=self.text, stage_type='ready')
        with self.assertRaisesMessage(ValidationError, 'zamkniętej pracy'):
            self.handoff(self.other.pk)
        self.first.refresh_from_db()
        self.assertEqual(self.first.assignment_id, self.v1.pk)
        self.assertFalse(WorkflowHandoff.objects.exists())

    def test_candidates_exclude_second_verifier_and_closed_stage(self):
        from workflow.handoffs import eligible_handoff_users

        self.assertIn(self.other, eligible_handoff_users(self.first))
        A.objects.create(text=self.text, role='verifier_2', assigned_to=self.other)
        self.assertNotIn(self.other, eligible_handoff_users(self.first))
        with self.assertRaises(ValidationError):
            self.handoff(self.other.pk)
        S.objects.filter(pk=self.first.pk).update(ended_at=self.today, is_completed=True)
        self.first.refresh_from_db()
        self.assertFalse(eligible_handoff_users(self.first).exists())

class HandoffPageTests(TestCase):
    """Formularz przekazania etapu: nazwy osób w liście, opcjonalny powód, historia przekazania."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.old = create_member("v79-old", "Redaktor")
        cls.new = create_member("private-login@example.test", "Redaktor")
        profile = cls.new.person_profile
        profile.first_name, profile.last_name = "Anna", "Nowa"
        profile.save()
        cls.book = Anthology.objects.create(title="Fantazmaty 6")
        cls.text = Text.objects.create(
            title="Opowiadanie przeniesione", anthology=cls.book, length=100
        )
        cls.author = Author.objects.create(
            first_name="Jan", last_name="Autor", email="author@example.test"
        )
        cls.text.authors.add(cls.author)
        cls.assignment = A.objects.create(
            text=cls.text, role="editor", assigned_to=cls.old
        )
        cls.stage = S.objects.create(
            text=cls.text,
            stage_type="editing",
            assignment=cls.assignment,
            started_at=timezone.localdate(),
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_handoff_dropdown_names_and_optional_reason_preserve_history(self):
        url = reverse("core:handoff_workflow_stage", args=[self.stage.pk])
        response = self.client.get(url)
        doc = html.fromstring(response.content)
        self.assertEqual(
            doc.xpath(f'//select[@name="assigned_to"]/option[@value="{self.new.pk}"]/text()'),
            ["Anna Nowa"],
        )
        self.assertFalse(doc.xpath('//textarea[@name="reason"]/@required'))
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(
            url,
            {
                "assigned_to": self.new.pk,
                "expected_assignment_id": self.assignment.pk,
                "_edit_version": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        handoff = WorkflowHandoff.objects.get(text=self.text)
        self.assertEqual(handoff.reason, "")
        handoff.full_clean()
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.is_current)
        self.assertEqual(handoff.original_started_at, timezone.localdate())
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.assignment.assigned_to_id, self.new.pk)
        self.assertIsNone(self.stage.started_at)
        self.client.force_login(self.old)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_stale_form_shows_service_refusal(self):
        url = reverse("core:handoff_workflow_stage", args=[self.stage.pk])
        token = form_value(self.client.get(url))
        response = self.client.post(url, {"assigned_to": self.new.pk, "expected_assignment_id": self.assignment.pk + 1,
                                          "_edit_version": token})
        self.assertContains(response, "Przydział zmienił się")
        self.assertFalse(WorkflowHandoff.objects.exists())


class StageReleaseTests(StatusAssignmentFixtures):
    """Oddanie rozpoczętej redakcji, odwołanie rezerwacji weryfikacji i ostrzeżenia na stronie tekstu."""

    def setUp(self):
        super().setUp()
        for user in (self.member, self.other):
            add_roles(user, 'Redaktor')
        self.verifier = get_user_model().objects.create_user('ver', 'ver@example.com', 'test')
        Person.objects.create(user=self.verifier, first_name='Wera', last_name='Weryfikator', email=self.verifier.email)
        self.editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.editing = S.objects.create(text=self.text, stage_type='editing', assignment=self.editor,
                                        started_at=self.today)
        self.v1 = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.verifier)
        self.first = S.objects.create(text=self.text, stage_type='first_verification', assignment=self.v1)

    def detail(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk])).content.decode()

    def test_editor_gives_back_started_editing(self):
        self.assertIn('Cofnij przydział', self.detail(self.member))
        change_scheduled_stage(user=self.member, stage_id=self.editing.pk, cancel=True)
        self.editing.refresh_from_db()
        self.editor.refresh_from_db()
        self.assertEqual(self.editing.stage_type, 'ready_for_editing')
        self.assertIsNone(self.editing.started_at)
        self.assertIsNone(self.editor.assigned_to_id)

    def test_verifier_cancels_first_verification_reservation(self):
        self.assertIn('Odwołaj rezerwację', self.detail(self.verifier))
        change_scheduled_stage(user=self.verifier, stage_id=self.first.pk, cancel=True)
        self.v1.refresh_from_db()
        self.assertIsNone(self.v1.assigned_to_id)

    def test_editing_cannot_be_given_back_once_verification_started(self):
        S.objects.filter(pk=self.editing.pk).update(is_completed=True, ended_at=self.today)
        S.objects.filter(pk=self.first.pk).update(started_at=self.today)
        second = S.objects.create(text=self.text, stage_type='editing', iteration=2, assignment=self.editor,
                                  started_at=self.today)
        with self.assertRaises(ValidationError):
            change_scheduled_stage(user=self.member, stage_id=second.pk, cancel=True)

    def test_other_member_cannot_release_and_gets_a_message(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse('core:change_scheduled_workflow_stage', args=[self.editing.pk]),
                                    {'action': 'cancel'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.editing.refresh_from_db()
        self.assertIsNotNone(self.editing.started_at)

    def test_new_editor_can_start_after_handoff(self):
        handoff_stage(self.text, self.admin, stage_id=self.editing.pk, assigned_to_id=self.other.pk,
                      expected_assignment_id=self.editor.pk, reason='Zmiana')
        page = self.detail(self.other)
        self.assertIn(reverse('core:start_assigned_workflow_stage', args=[self.editing.pk]), page)

    def test_warning_when_reserved_verifier_is_on_leave(self):
        Vacation.objects.create(person=self.verifier.person_profile, start_date=self.today - timedelta(days=1),
                                end_date=timezone.now() + timedelta(days=5))
        self.assertIn('jest na urlopie', self.detail(self.member))

    def test_confirmations_and_finish_date_are_rendered(self):
        page = self.detail(self.member)
        self.assertIn('data-confirm="Przekazać tekst do pierwszej weryfikacji?', page)
        self.assertIn('pierwszej weryfikacji. Jeśli nie ma weryfikatora', page)


class WithdrawalTests(StatusAssignmentFixtures):
    """Wycofanie tekstu ukrywa go w kolejkach bez usuwania historii; przywrócić może tylko superuser."""

    def test_only_superuser_restores_withdrawn_text(self):
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today,
                         assignment=A.objects.create(text=self.text, role='editor', assigned_to=self.admin))
        withdraw_text(user=self.admin, text_id=self.text.pk)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk])).content.decode()
        self.assertIn('Przywróć do procesu', page)
        with self.assertRaises(PermissionDenied):
            restore_withdrawn_text(user=self.member, text_id=self.text.pk)
        self.assertTrue(S.objects.filter(text=self.text, stage_type='withdrawn').exists())
        restore_withdrawn_text(user=self.admin, text_id=self.text.pk)
        self.assertFalse(S.objects.filter(text=self.text, stage_type='withdrawn').exists())

    def test_withdrawal_hides_queues_and_reopening_restores_same_records(self):
        self.client.force_login(self.admin)
        book = Anthology.objects.create(title='Antologia ilustracji', has_illustrations=True)
        story = Text.objects.create(title='Znikający tekst', anthology=book, length=100)
        illustration = Illustration.objects.get(text=story)
        artist = Illustrator.objects.create(first_name='Osoba', last_name='Próbna')
        illustration.coordinator_notes = 'Nie usuwać historii'
        assign_artists(illustration, [artist], status=Illustration.Status.ASSIGNED)
        assignment = A.objects.create(text=story, role='editor', assigned_to=self.member)
        withdrawn = S.objects.create(text=story, stage_type='withdrawn', is_released=True)
        def queues():
            """Czy tekst jest w kolejce ilustracji i w kolejce audiobooków."""
            illustrations = self.client.get(reverse('illustrations:illustration_list')).context['page_obj']
            audiobooks = self.client.get(reverse('core:audiobooks')).context['page_obj']
            return (story.pk in {item.text_id for item in illustrations}, story.pk in {row['pk'] for row in audiobooks})
        self.assertEqual(queues(), (False, False))
        illustration.refresh_from_db()
        story.refresh_from_db()
        self.assertEqual(illustration.coordinator_notes, 'Nie usuwać historii')
        self.assertEqual(list(illustration.illustrators.all()), [artist])
        self.assertTrue(story.for_recording)
        self.assertTrue(A.objects.filter(pk=assignment.pk).exists())
        # Historic or unreleased withdrawal must not hide an active text.
        withdrawn.is_current = False
        withdrawn.save(update_fields=['is_current'])
        self.assertEqual(queues(), (True, True))
        self.assertEqual(Illustration.objects.get(text=story).pk, illustration.pk)
        withdrawn.is_current = True
        withdrawn.is_released = False
        withdrawn.save(update_fields=['is_current', 'is_released'])
        self.assertEqual(queues(), (True, True))
