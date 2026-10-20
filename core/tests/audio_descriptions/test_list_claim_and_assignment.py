"""Audiodeskrypcje: lista, przejmowanie zadania AD, przypisywanie osoby piszącej i kontrolerów oraz uprawnienia do zmian."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.models import AudioDescription
from fantazmaty.testing import create_member, create_superuser, edit_token, form_value
from illustrations.models import Illustration, Illustrator
from people.models import Person
from texts.models import Anthology, AnthologyTask, Text

from .support import AudioDescriptionTestData


class ClaimTests(AudioDescriptionTestData, TestCase):
    def test_claim_from_rendered_form_updates_existing_task(self):
        self.client.force_login(self.writer)
        before = AudioDescription.objects.count()
        page = self.client.get(self.list_url)
        form = html.fromstring(page.content).xpath(f'//form[@action="{self.claim}"]')[0]
        values = {i.get('name'): i.get('value') for i in form.xpath('.//input')}
        self.assertEqual(self.client.post(self.claim, values).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assigned_to_id, self.task.status), (self.writer.person_profile.pk, 'commissioned'))
        self.assertIsNotNone(self.task.commissioned_at)
        self.assertEqual(self.book.production_tasks.filter(task_type='audio_description').count(), 1)
        self.assertEqual(AudioDescription.objects.count(), before)
        # Status zadania zmienia się na stronie antologii; osoba pisząca przesuwa etap pracy.
        detail = self.client.get(self.detail)
        self.assertNotContains(detail, 'name="status"')
        self.assertContains(detail, 'name="stage"')

    def test_stale_second_claim_and_ready_task_never_overwrite(self):
        stale = edit_token(self.controller, self.book)
        self.post(self.writer, self.claim)
        self.client.force_login(self.controller)
        self.assertEqual(self.client.post(self.claim, {'_edit_version': stale}).status_code, 409)
        self.post(self.controller, self.claim)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.writer.person_profile.pk)
        self.post(self.writer, action='stage', stage='completed')
        self.post(self.controller, self.claim)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'ready')

    def test_inactive_external_or_no_profile_cannot_claim(self):
        person = self.writer.person_profile
        person.is_external = True
        person.save()
        response = self.post(self.writer, self.claim)
        self.assertIn(response.status_code, (302, 403))
        if response.status_code == 302:
            self.assertIn(reverse('login'), response.url)
        user = get_user_model().objects.create_user('accountonly')
        self.assertEqual(self.post(user, self.claim).status_code, 403)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to_id)

    def test_administrator_without_team_profile_cannot_claim(self):
        self.assertEqual(self.post(self.admin, self.claim).status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assigned_to_id, self.task.status), (None, 'not_commissioned'))

    def test_missing_version_rejected_and_status_without_assignee_not_saved(self):
        self.client.force_login(self.writer)
        self.assertEqual(self.client.post(self.claim, {}).status_code, 409)
        self.assertEqual(self.post(self.manager, status='ready', assigned_to='').status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'not_commissioned')


class AssignmentTests(AudioDescriptionTestData, TestCase):
    def test_coordinator_assigns_controller_and_only_designated_users_change_stage(self):
        self.assertEqual(self.post(self.controller, action='stage', stage='completed').status_code, 403)
        response = self.post(self.manager, assigned_to=self.writer.person_profile.pk,
                             controllers=[self.controller.person_profile.pk], status='ready')
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        # Status wysłany z tej strony jest ignorowany: przypisanie tylko zleca zadanie.
        self.assertEqual(self.task.status, 'commissioned')
        self.assertEqual(self.post(self.controller, action='assignment', assigned_to='').status_code, 403)
        self.assertEqual(self.post(self.controller, action='stage', stage='completed').status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'ready')
        self.description.controllers.clear()
        self.assertEqual(self.post(self.controller, action='stage', stage='consultation').status_code, 403)
        # Uwagi zapisuje każdy członek zespołu.
        self.assertEqual(self.post(self.controller, action='note', note='x').status_code, 302)

    def test_ready_task_keeps_its_assignee_on_this_page(self):
        self.task.assigned_to = self.writer.person_profile
        self.task.status = 'ready'
        self.task.save()
        response = self.post(self.manager, assigned_to=self.other.person_profile.pk)
        self.assertContains(response, 'Wykonawcę zmienisz tylko w panelu admina', status_code=400)
        # Bez zmiany osoby zapis kontrolerów działa, a zadanie pozostaje gotowe.
        response = self.post(self.manager, assigned_to=self.writer.person_profile.pk,
                             controllers=[self.controller.person_profile.pk])
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assigned_to, self.task.status), (self.writer.person_profile, 'ready'))
        self.assertTrue(self.description.controllers.filter(pk=self.controller.person_profile.pk).exists())

    def test_account_outside_the_team_cannot_change_anything(self):
        outsider = get_user_model().objects.create_user('ad-outsider')
        for data in ({'action': 'content', 'content': 'Obca treść'}, {'action': 'note', 'note': 'Obca uwaga'},
                     {'action': 'stage', 'stage': 'consultation'}, {'assigned_to': outsider.pk}):
            with self.subTest(data=data):
                self.assertEqual(self.post(outsider, **data).status_code, 403)
        self.description.refresh_from_db()
        self.assertEqual((self.description.content, self.description.stage), ('', 'writing'))
        self.assertFalse(self.description.notes.exists())

    def test_assignee_cannot_change_owner_or_grant_control(self):
        self.post(self.writer, self.claim)
        self.post(self.writer, status='ready', assigned_to=self.controller.person_profile.pk,
                  controllers=[self.controller.person_profile.pk])
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.writer.person_profile.pk)
        self.assertFalse(self.description.controllers.exists())

    def test_controller_without_assignee_gets_validation_error(self):
        self.description.controllers.add(self.controller.person_profile)
        response = self.post(self.controller, action='stage', stage='completed')
        self.assertContains(response, 'wymaga przypisania osoby piszącej', status_code=400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'not_commissioned')

    def test_removed_assignment_revokes_access_even_with_old_token(self):
        self.post(self.writer, self.claim)
        stale = edit_token(self.writer, self.book)
        self.task.assigned_to = self.controller.person_profile
        self.task.save()
        self.client.force_login(self.writer)
        self.assertEqual(self.client.post(self.detail, {'status': 'ready', '_edit_version': stale}).status_code, 403)


class ListTests(AudioDescriptionTestData, TestCase):
    def test_filters_sorting_and_no_writes_on_get(self):
        second = Anthology.objects.create(title='Zielona antologia')
        Anthology.objects.create(title='Porzucona', status='abandoned')
        Anthology.objects.create(title='Powieść', is_novel=True)
        self.client.force_login(self.writer)
        count = AudioDescription.objects.count()
        response = self.client.get(self.list_url, {'sort': '-anthology'})
        self.assertEqual(response.context['rows'][0]['book'], second)
        self.assertNotContains(response, 'Porzucona')
        self.assertNotContains(response, 'Powieść</a>')
        response = self.client.get(self.list_url, {'q': 'do opisania', 'status': 'not_commissioned'})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(AudioDescription.objects.count(), count)
        self.assertEqual(self.client.get(self.claim).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(self.list_url).status_code, 302)


class PeopleChoicesTests(TestCase):
    def test_people_lists_include_inactive_only_with_history(self):
        admin = create_superuser('ad-people-admin')
        book = Anthology.objects.create(title='AD z historią')
        former = Person.objects.create(first_name='Była', last_name='Osoba', is_active=False)
        stranger = Person.objects.create(first_name='Obca', last_name='Nieaktywna', is_active=False)
        task = Anthology.objects.create(title='Inna AD').production_tasks.get(task_type='blurb')
        task.assigned_to, task.status = former, 'commissioned'
        task.save()
        self.client.force_login(admin)
        form = self.client.get(reverse('core:audio_description_detail', args=[book.pk])).context['form']
        people = form.fields['assigned_to'].queryset
        self.assertTrue(people.filter(pk=former.pk).exists())
        self.assertFalse(people.filter(pk=stranger.pk).exists())
        self.assertIn('nieaktywny', form.fields['assigned_to'].label_from_instance(former))

    def test_form_fields_share_one_people_query(self):
        """Pola „Kto pisze” i „Konsultacja” korzystają z jednej listy osób: formularz robi 2 zapytania (kontrolerzy
        i osoby), a wyświetlenie obu list opcji nie dodaje kolejnych; zapisany kontroler jest wśród opcji obu pól."""
        from core.views.audio_descriptions import DescriptionForm
        book = Anthology.objects.create(title='AD z dwoma polami')
        controller = Person.objects.create(first_name='Była', last_name='Kontrolerka', is_active=False)
        book.audio_description.controllers.add(controller)
        task = book.production_tasks.get(task_type='audio_description')
        with self.assertNumQueries(2):
            form = DescriptionForm(instance=task, description=book.audio_description)
            single = [value for value, _ in form.fields['assigned_to'].choices]
            multiple = [value for value, _ in form.fields['controllers'].choices]
        self.assertEqual(single, ['', *multiple])
        self.assertIn(controller.pk, multiple)
        self.assertEqual(form.fields['controllers'].clean([str(controller.pk)])[0], controller)


class AnthologyTaskTableTests(TestCase):
    def test_task_table_and_audio_description_list_share_assignment(self):
        admin = create_superuser('ad-tasks-admin')
        artist = create_member('ad-artist', 'Ilustrator')
        book = Anthology.objects.create(title='Nowa antologia', has_illustrations=True)
        # Antologia z tekstem (i jego ilustracją) oraz kontakt ilustratora – pełna strona antologii i formularz zadań.
        text = Text.objects.create(title='Nowy tekst', anthology=book, length=20)
        self.assertTrue(Illustration.objects.filter(text=text).exists())
        Illustrator.objects.create(first_name='Artysta', last_name='Kontakt')
        ready = Anthology.objects.create(title='Gotowa pomijana', status='ready')
        self.assertEqual(book.production_tasks.filter(task_type='audio_description').count(), 1)
        self.client.force_login(admin)
        response = self.client.get(reverse('core:task_list'))
        self.assertEqual(response.status_code, 200)
        rows = list(response.context['tasks'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['cells']), 6)
        self.assertEqual({label for value, label in response.context['task_choices']},
                         {'Blurb', 'Banery', 'Skład', 'Okładka', 'Audiodeskrypcja', 'Typografia okładki'})
        self.assertNotContains(response, ready.title)
        anthology_url = reverse('core:anthology_detail', args=[book.pk])
        response = self.client.get(anthology_url)
        self.assertContains(response, 'audio_description-status')
        payload = {f'{kind}-status': 'not_commissioned' for kind in AnthologyTask.TaskType.values}
        payload.update({'audio_description-status': 'commissioned',
                        'audio_description-assigned_to': artist.person_profile.pk})
        response = self.client.post(anthology_url, {**payload, '_edit_version': form_value(response)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(book.production_tasks.get(task_type='audio_description').assigned_to_id,
                         artist.person_profile.pk)
        response = self.client.get(reverse('core:audio_descriptions'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h1 class="page-title">Audiodeskrypcje</h1>', html=True)
        self.assertTrue(html.fromstring(response.content).xpath('//main//table'))
        assigned = next(row for row in response.context['rows'] if row['book'].pk == book.pk)
        self.assertEqual(assigned['task'].assigned_to_id, artist.person_profile.pk)
        self.assertFalse(assigned['can_claim'])
        self.assertContains(response, ready.title)
