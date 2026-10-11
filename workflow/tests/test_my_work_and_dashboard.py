"""Moje teksty, pulpit i zestawienie procesu: kiedy praca osoby jest w toku, oczekuje albo jest zakończona."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.template.loader import render_to_string
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.models import Audiobook, AudiobookStage
from core.selectors.texts import my_texts_context, workflow_list_context
from core.services.reviews import copy_review_to_text
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.read_queries import annotate_my_work, dashboard_querysets
from workflow.repetitions import repeat_stages
from workflow.services import (
    claim_stage, complete_stage, finish_editing_to_coordinator, resume_editing,
    send_to_first_verification, send_to_second_verification, start_first_verification,
)

from .support import AnthologyWorkflowMixin, EditingInProgressFixtures, add_roles, importing


class MyWorkVisibilityTests(AnthologyWorkflowMixin, TestCase):
    """Widoki „W toku” i „Wszystkie” w Moich tekstach oraz klasyfikacja pracy redaktora."""

    def my_ids(self, user=None, view='active'):
        return [r['pk'] for r in my_texts_context(user=user or self.editor, selected_view=view)['texts']]

    @tag('slow')
    def test_editor_finishes_on_approved_handoff_to_proofreading(self):
        self.begin_editing()
        send_to_first_verification(self.text, self.editor, self.today)
        self.assertIn(self.text.pk, self.my_ids())
        self.assertTrue(self.work().work_waiting)
        first = claim_stage(self.text, 'first_verification', self.verifier_1)
        self.assertIn(self.text.pk, self.my_ids())
        complete_stage(first, self.verifier_1, self.today)
        resume_editing(self.text, self.editor, self.today)
        self.assertTrue(self.work().work_active)
        send_to_second_verification(self.text, self.editor, self.today)
        second = claim_stage(self.text, 'second_verification', self.verifier_2)
        complete_stage(second, self.verifier_2, self.today)
        resume_editing(self.text, self.editor, self.today)
        finish_editing_to_coordinator(self.text, self.editor, self.today)
        control = claim_stage(self.text, 'editing_control', self.coordinator)
        self.assertIn(self.text.pk, self.my_ids())
        complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        self.assertNotIn(self.text.pk, self.my_ids())
        self.assertTrue(self.work().work_completed)
        proof = claim_stage(self.text, 'first_proofreading', self.proofreader,
                            started_at=self.today + timedelta(days=1))
        self.assertNotIn(self.text.pk, self.my_ids())
        S.objects.filter(pk=proof.pk).update(started_at=self.today)
        self.assertNotIn(self.text.pk, self.my_ids())
        self.assertIn(self.text.pk, self.my_ids(view='all'))
        self.assertTrue(self.work().work_completed)
        complete_stage(S.objects.get(pk=proof.pk), self.proofreader, self.today)
        self.assertTrue(self.work().work_completed)
        self.assertEqual(S.objects.filter(text=self.text, stage_type='editing').count(), 3)

    def test_imported_editorial_history_does_not_hide_current_editor_waiting_for_verification(self):
        editing = self.begin_editing()
        with importing():
            S.objects.create(text=self.text, stage_type='editing', iteration=2,
                assignment=editing.assignment, is_current=False, imported_completed=True, is_completed=True)
        send_to_first_verification(self.text, self.editor, self.today)
        self.assertTrue(self.work().work_waiting)
        self.assertIn(self.text.pk, self.my_ids())
        self.client.force_login(self.editor)
        page = self.client.get(reverse('core:my_texts'))
        self.assertContains(page, self.text.title)
        self.assertContains(page, 'Oczekujące')

    def test_pending_task_is_in_progress_and_old_tabs_resolve_to_remaining_tabs(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(started_at=self.today + timedelta(days=1))
        self.assertIn(self.text.pk, self.my_ids())
        self.client.force_login(self.editor)
        for view, selected in [('active', 'active'), ('waiting', 'active'), ('completed', 'all')]:
            with self.subTest(view=view):
                page = self.client.get(reverse('core:my_texts'), {'view': view})
                self.assertEqual(page.context['selected_view'], selected)
                nav = html.fromstring(page.content).xpath('//nav[@aria-label="Widok moich tekstów"]')[0]
                self.assertEqual([''.join(a.itertext()).strip() for a in nav.xpath('./a')], ['W toku', 'Wszystkie'])
                self.assertContains(page, 'Oczekujące')

    def test_ordinary_completed_task_only_remains_in_all(self):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.proofreader)
        S.objects.create(text=self.text, stage_type='first_proofreading', assignment=assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertNotIn(self.text.pk, self.my_ids(self.proofreader))
        self.assertIn(self.text.pk, self.my_ids(self.proofreader, 'all'))

    def test_proofreading_from_previous_cycle_does_not_finish_current_editorial_work(self):
        self.begin_editing()
        S.objects.create(text=self.text, stage_type='first_proofreading', workflow_cycle=2,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        send_to_first_verification(self.text, self.editor, self.today)
        self.assertTrue(self.work().work_waiting)

    def test_old_proofreading_does_not_approve_editorial_repetition(self):
        self.begin_editing()
        S.objects.create(text=self.text, stage_type='first_proofreading',
                         started_at=self.today, ended_at=self.today, is_completed=True)
        run = repeat_stages(self.text, ['editing'], self.superuser,
                            assignees={'editor': self.other_editor})
        self.assertIn(self.text.pk, self.my_ids(self.other_editor))
        row = annotate_my_work(Text.objects.all(), self.other_editor, self.today).get(pk=self.text.pk)
        self.assertTrue(row.work_waiting)
        self.assertFalse(row.work_completed)
        self.assertIsNone(run.stages.get().started_at)

    def test_first_proofreading_can_be_taken_from_detail_and_finishes_editorial_waiting(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(ended_at=self.today, is_completed=True)
        S.objects.filter(text=self.text, stage_type='first_verification').delete()
        S.objects.create(text=self.text, stage_type='editing_control', started_at=self.today, ended_at=self.today, is_completed=True, send_to_proofreading=True)
        proof = S.objects.create(text=self.text, stage_type='first_proofreading')
        self.assertNotIn(self.text.pk, self.my_ids())
        response = self.detail(self.proofreader)
        form = self.claim_form(response, proof)
        data = {node.get('name'): node.get('value', '') for node in form.xpath('.//input[@name]')}
        taken = self.client.post(form.get('action'), data)
        self.assertRedirects(taken, reverse('core:assigned_text_detail', args=[self.text.pk]), fetch_redirect_response=False)
        proof.refresh_from_db()
        self.assertEqual(proof.assignment.assigned_to, self.proofreader)
        self.assertEqual(proof.started_at, self.today)
        self.assertNotIn(self.text.pk, self.my_ids())
        self.assertTrue(self.work().work_completed)


class MyTextsAllOrderingTests(TestCase):
    """Widok „Wszystkie” w Moich tekstach: kolejność alfabetyczna niezależnie od dat zakończenia."""

    def setUp(self):
        self.today = timezone.localdate()
        self.member = create_member('member', 'Korektor')
        self.other = get_user_model().objects.create_user('other', 'other@example.com', 'test')
        self.book = Anthology.objects.create(title='Test')

    def work(self, title, ago=None, editor=False, control_ago=None):
        text = Text.objects.create(title=title, length=100, anthology=self.book)
        assignment = A.objects.create(text=text, role='editor' if editor else 'proofreader_1', assigned_to=self.member)
        ended = self.today - timedelta(days=ago) if ago is not None else None
        with importing():
            S.objects.create(text=text, stage_type='editing' if editor else 'first_proofreading', assignment=assignment,
                             started_at=ended, ended_at=ended, is_completed=True, imported_completed=ago is None)
        if editor:
            control_end = self.today - timedelta(days=control_ago) if control_ago is not None else self.today
            S.objects.create(text=text, stage_type='editing_control', started_at=control_end, ended_at=control_end, is_completed=True)
        return text

    def rows(self, params=''):
        return list(my_texts_context(user=self.member, selected_view='all', params=QueryDict(params))['texts'])

    def test_all_uses_title_order_including_unknown_completion_dates(self):
        oldest = self.work('A dawny', 20)
        newest = self.work('Z nowy', 1)
        unknown = self.work('B bez daty')
        middle = self.work('C średni', 10)
        self.assertEqual([r['pk'] for r in self.rows()], [oldest.pk, unknown.pk, middle.pk, newest.pk])

    def test_all_includes_editor_waiting_after_control_and_completed_proofreader(self):
        editor = self.work('A redakcja', 30, editor=True, control_ago=1)
        proof = self.work('Z korekta', 5)
        self.assertEqual([r['pk'] for r in self.rows()], [editor.pk, proof.pk])

    def test_other_performers_completion_does_not_change_title_order(self):
        old = self.work('A dawny', 20)
        recent = self.work('B nowy', 5)
        other_assignment = A.objects.create(text=old, role='proofreader_2', assigned_to=self.other)
        S.objects.create(text=old, stage_type='second_proofreading', assignment=other_assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertEqual([r['pk'] for r in self.rows()], [old.pk, recent.pk])

    def test_several_own_roles_keep_single_row_and_explicit_header_sort(self):
        both = self.work('A dwa zadania', 20)
        recent = self.work('Z nowe', 5)
        assignment = A.objects.create(text=both, role='proofreader_3', assigned_to=self.member)
        S.objects.create(text=both, stage_type='third_proofreading', assignment=assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertEqual([r['pk'] for r in self.rows()], [both.pk, recent.pk])
        self.assertEqual([r['pk'] for r in self.rows('sort=-title')], [recent.pk, both.pk])

    def test_default_order_is_preserved_through_pagination(self):
        texts = [self.work(f'{number:02}', 30 - number) for number in range(30)]
        self.client.force_login(self.member)
        url = reverse('core:my_texts')
        first = self.client.get(url, {'view': 'all', 'page_size': 25})
        second = self.client.get(url, {'view': 'all', 'page_size': 25, 'page': 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        actual = [row['pk'] for response in (first, second) for row in response.context['texts']]
        self.assertEqual(actual, [text.pk for text in texts])


class EditorDashboardTests(EditingInProgressFixtures):
    """Pulpit redaktora: praca oczekująca na weryfikację i kontrolę, zakończenie po przekazaniu do korekty."""

    def home(self, user=None):
        self.client.force_login(user or self.member)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_dashboard_keeps_editor_during_pending_and_started_first_verification(self):
        first = self.reserve()
        send_to_first_verification(self.text, self.member, self.today)
        page = self.home()
        self.assertEqual(page.context['active_stage_count'], 1)
        row = page.context['active_stages'][0]
        self.assertEqual(row['pk'], first.pk)
        self.assertTrue(row['editor_waiting'])
        self.assertContains(page, 'Oczekujące')
        self.assertContains(page, 'Pierwsza weryfikacja')
        started = start_first_verification(self.text, self.other, self.today)
        self.assertEqual(self.summary()['active_stages'][0]['pk'], started.pk)
        self.assertTrue(self.summary()['active_stages'][0]['editor_waiting'])

    def test_dashboard_keeps_editor_during_editing_coordinator_control(self):
        self.close_editing()
        control = S.objects.create(text=self.text, stage_type='editing_control')
        summary = self.summary()
        self.assertEqual(summary['active_stage_count'], 1)
        self.assertEqual(summary['active_stages'][0]['pk'], control.pk)
        self.assertTrue(summary['active_stages'][0]['editor_waiting'])
        self.assertEqual(summary['reserved_assignment_count'], 0)

    def test_dashboard_finishes_on_approved_handoff_even_with_future_proofreading(self):
        self.close_editing()
        S.objects.create(text=self.text, stage_type='editing_control', is_completed=True,
                         started_at=self.today, ended_at=self.today, send_to_proofreading=True)
        proof = S.objects.create(text=self.text, stage_type='first_proofreading',
                                 started_at=self.today + timedelta(days=1))
        self.assertEqual(self.summary()['active_stage_count'], 0)
        S.objects.filter(pk=proof.pk).update(started_at=self.today)
        self.assertEqual(self.summary()['active_stage_count'], 0)

    def test_dashboard_waits_are_paginated_with_correct_counts_and_labels(self):
        send_to_first_verification(self.text, self.member, self.today)
        for index in range(7):
            text = Text.objects.create(title=f'Oczekujący {index}', anthology=self.book, length=100)
            assignment = A.objects.create(text=text, role='editor', assigned_to=self.member)
            S.objects.create(text=text, stage_type='editing', assignment=assignment,
                             started_at=self.today, ended_at=self.today, is_completed=True)
            S.objects.create(text=text, stage_type='first_verification')
        page = self.home()
        self.assertEqual(page.context['active_stage_count'], 8)
        self.assertEqual(len(page.context['active_stages']), 6)
        response = self.client.get(reverse('core:dashboard_tasks'), {'kind': 'active', 'page': 2})
        self.assertEqual(response.status_code, 200)
        rows = html.fromstring(response.json()['html']).xpath('//li[@class="dashboard-list-item"]')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('Oczekujące' in ''.join(row.itertext()) for row in rows))
        self.assertIsNone(response.json()['next_url'])

    def test_dashboard_does_not_expose_waits_to_unassigned_people_or_closed_texts(self):
        send_to_first_verification(self.text, self.member, self.today)
        self.assertEqual(self.summary(self.other)['active_stage_count'], 0)
        S.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.summary()['active_stage_count'], 0)

    def test_author_editing_is_active_work_on_dashboard(self):
        """Regresja: praca z autorem nie pojawiała się na pulpicie redaktora."""
        self.close_editing()
        stage = S.objects.create(text=self.text, stage_type='author_editing', assignment=self.editor,
                                 started_at=self.today)
        active, reserved = dashboard_querysets(self.member, self.today)
        self.assertTrue(active.filter(pk=stage.pk).exists())
        self.assertFalse(reserved.filter(text=self.text).exists())


class WorkflowSummaryTests(TestCase):
    """Zestawienie procesu (kolumny ról, stan pracy redaktora, ukrywanie gotowych) i strona główna."""

    def setUp(self):
        self.admin = create_superuser()
        self.user = create_member('user', 'Redaktor')
        add_roles(self.user, 'Weryfikator')
        self.book = Anthology.objects.create(title='Nabór')
        self.text = Text.objects.create(title='Tytuł', length=100, anthology=self.book)
        self.today = timezone.localdate()
        self.client.force_login(self.admin)

    def stage(self, kind, role=None, **kw):
        a = A.objects.create(text=self.text, role=role, assigned_to=self.user) if role else None
        return S.objects.create(text=self.text, stage_type=kind, assignment=a, **kw)

    def test_summary_waits_for_editorial_approval(self):
        self.stage(
            "editing", "editor", is_completed=True, started_at=self.today, ended_at=self.today
        )
        self.stage("first_verification")

        def state():
            return list(workflow_list_context(user=self.admin, params={})["stages"])[0][
                "role_cells"
            ][0]["entries"][0]["state"]

        self.assertEqual(state(), "Oczekuje")
        self.stage("editing_control", is_completed=True, started_at=self.today, ended_at=self.today)
        self.assertEqual(state(), "Oczekuje")
        proof = self.stage("first_proofreading")
        self.assertEqual(state(), "Zakończone")
        proof.started_at = self.today
        proof.save()
        self.assertEqual(state(), "Zakończone")

    def test_summary_editors_only_and_anthology_link(self):
        self.stage("editing", "editor")
        self.stage("first_verification", "verifier_1")
        response = self.client.get(reverse("core:workflow_list"))
        self.assertContains(response, "Redaktor")
        self.assertNotContains(response, "Stan etapu")
        self.assertIn(("editor", "Redaktor"), response.context["role_columns"])
        self.assertGreater(len(response.context["role_columns"]), 1)
        book = Anthology.objects.create(title="Antologia")
        self.assertContains(
            self.client.get(reverse("core:anthology_detail", args=[book.pk])),
            reverse("core:text_list") + "?anthology=" + str(book.pk) + "&amp;hide_ready=0",
        )

    def test_closed_anthology_not_pending_publication(self):
        self.stage("ready", is_completed=True, started_at=self.today, ended_at=self.today)
        self.text.current_status = "ready"
        self.text.save()
        self.book.status = "ready"
        self.book.save()
        review = Review.objects.create(
            title="Zablokowane zgłoszenie", anthology=self.book, author_first_name="Jan",
            author_last_name="Autor", email="autor@example.com", length=100, genre="fantasy",
            status="accepted", author_notified_at=self.today)
        self.assertNotContains(self.client.get(reverse("core:home")), review.title)


class ArchivedReviewTransferTests(TestCase):
    """Archiwalna recenzja trafia do procesu raz i znika z pulpitu; zestawienie może ukryć gotowe teksty."""

    def setUp(self):
        self.user = create_superuser()
        self.book = Anthology.objects.create(title='Nabór')
        self.review = Review.objects.create(
            title='Archiwalny test', anthology=self.book, author_first_name='Jan', author_last_name='Test',
            email='jan@example.com', author_pseudonym='Pseudonim', length=100, old_reviews=True,
            status='accepted', author_notified_at=timezone.localdate())

    def test_transfer_archive_idempotent(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('core:assigned_review_detail', args=[self.review.pk])),
                            'Dodaj do procesu wydawniczego')
        text = copy_review_to_text(user=self.user, review_id=self.review.pk, contract_received=True)
        self.assertEqual(text.authors.get().pseudonym, 'Pseudonim')
        self.assertEqual(copy_review_to_text(user=self.user, review_id=self.review.pk).pk, text.pk)
        self.review.refresh_from_db()
        self.assertTrue(self.review.old_reviews)

    def test_dashboard(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("core:home")), self.review.title)
        copy_review_to_text(user=self.user, review_id=self.review.pk, contract_received=True)
        self.assertNotContains(self.client.get(reverse("core:home")), self.review.title)

    def test_hide_ready(self):
        text = Text.objects.create(title="Gotowy", length=100)
        S.objects.create(text=text, stage_type="ready")
        self.assertEqual(
            len(list(workflow_list_context(user=self.user, params={"hide_ready": "1"})["stages"])),
            0,
        )
        self.assertEqual(
            len(list(workflow_list_context(user=self.user, params={"hide_ready": "0"})["stages"])),
            1,
        )


class CompletionLabelsTests(TestCase):
    def test_profile_finished_labels_cover_media_and_preserve_active_work(self):
        member = create_member('v69work', 'Redaktor')
        book = Anthology.objects.create(title='Ukończone prace', status='ready')
        text = Text.objects.create(title='Gotowy tekst', anthology=book, length=1)
        A.objects.create(text=text, assigned_to=member, role='editor')
        S.objects.create(text=text, stage_type='ready', is_current=True)
        audio = Audiobook.objects.create(text=text, proofreader=member, status='published')
        AudiobookStage.objects.create(text=text, stage_type='proofreading', performer=member, is_completed=True)
        self.client.force_login(member)
        page = self.client.get(reverse('core:person_detail', args=[member.person_profile.pk]))
        self.assertEqual(page.status_code, 200)
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//*[@id="person-assignments-table"]//span[contains(@class,"completed")]/text()'), ['Zakończone', 'Zakończone'])
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'published')
        # Profile uses the same rows for ordinary stories, translations and chapters.
        for kind in ('text', 'audio', 'post_layout'):
            rows = [dict(pk=n, kind_key=kind, text={'pk': text.pk, 'title': str(n)},
                detail_url=reverse('core:audiobook_detail', args=[text.pk]), state_label='W toku',
                has_completed_work=finished, has_active_work=not finished,
                latest_stage={}, no_detail_link=True, role='editor', get_role_display='Redaktor')
                for n, finished in enumerate((True, False))]
            content = render_to_string('core/person_detail.html', {'person': member.person_profile,
                'assignments': rows, 'user': member})
            table = html.fromstring(content).get_element_by_id('person-assignments-table')
            self.assertEqual(table.xpath('.//span[contains(@class,"stage-status")]/text()'), ['Zakończone', 'W toku'])
