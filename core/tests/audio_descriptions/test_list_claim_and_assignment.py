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
        i osoby), a wyświetlenie obu list opcji nie dodaje kolejnych; „Kto pisze” to część listy konsultacji (bez osób
        spoza ekipy i nieaktywnych bez historii); zapisany kontroler jest wśród opcji obu pól."""
        from core.views.audio_descriptions import DescriptionForm
        book = Anthology.objects.create(title='AD z dwoma polami')
        controller = Person.objects.create(first_name='Była', last_name='Kontrolerka', is_active=False)
        stranger = Person.objects.create(first_name='Obca', last_name='Spozaekipy', is_external=True)
        book.audio_description.controllers.add(controller)
        task = book.production_tasks.get(task_type='audio_description')
        with self.assertNumQueries(2):
            form = DescriptionForm(instance=task, description=book.audio_description)
            single = [value for value, _ in form.fields['assigned_to'].choices]
            multiple = [value for value, _ in form.fields['controllers'].choices]
        self.assertEqual(single, ['', *[value for value in multiple if value != stranger.pk]])
        self.assertIn(stranger.pk, multiple)
        self.assertIn(controller.pk, single)
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


class ExternalConsultantTests(AudioDescriptionTestData, TestCase):
    """Konsultacja audiodeskrypcji z osobami spoza ekipy: wybór z wyszukiwarki, dodanie nowej osoby bez konta,
    brak duplikatów, oznaczenie na stronach i brak dostępu, list zespołu, Discorda i Dropboxa dla takiej osoby."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from authors.models import Author
        cls.team_person = Person.objects.create(first_name='Ewa', last_name='Kowalska')
        author = Author.objects.create(first_name='Adam', last_name='Pisarz', pseudonym='Nocny Kruk')
        cls.pen_person = Person.objects.create(first_name='Adam', last_name='Pisarz', author_profile=author,
                                               email='adam@example.com')
        cls.external = Person.objects.create(first_name='Olga', last_name='Spozaekipy', is_external=True)
        cls.former = Person.objects.create(first_name='Stary', last_name='Członek', is_active=False)

    def form(self, **data):
        from core.views.audio_descriptions import DescriptionForm
        return DescriptionForm(data, instance=self.task, description=self.description)

    def assign(self, user=None, **data):
        """Zapis sekcji „Przypisanie” jako koordynator (domyślnie)."""
        return self.post(user or self.manager, **{'action': 'assignment', 'assigned_to': '', **data})

    def new_person(self, first='Nowa', last='Konsultantka', email=''):
        return {'new_controller_first_name': first, 'new_controller_last_name': last, 'new_controller_email': email}

    def test_new_person_form_variants(self):
        """Formularz nowej osoby: puste pola – bez nowej osoby; imię i nazwisko po usunięciu nadmiarowych spacji;
        brak imienia, nazwiska albo sam e-mail – błędy pól; duplikat po imieniu i nazwisku (inna wielkość liter,
        spacje), po pseudonimie powiązanego autora, po e-mailu (bez wielkości liter) – błąd; zły e-mail – błąd."""
        cases = [
            ('puste', self.new_person('', '', ''), set(), None),
            ('spacje', self.new_person('  Anna ', ' Maria   Nowa '), set(), ('Anna', 'Maria Nowa', None)),
            ('z e-mailem', self.new_person('Iza', 'Nowa', ' Iza@Example.com '), set(), ('Iza', 'Nowa', 'iza@example.com')),
            ('bez nazwiska', self.new_person('Anna', ''), {'new_controller_last_name'}, None),
            ('bez imienia', self.new_person('', 'Nowa'), {'new_controller_first_name'}, None),
            ('sam e-mail', self.new_person('', '', 'x@example.com'),
             {'new_controller_first_name', 'new_controller_last_name'}, None),
            ('duplikat nazwiska', self.new_person('  EWA ', 'kowalska  '), {'new_controller_first_name'}, None),
            ('duplikat osoby spoza ekipy', self.new_person('olga', 'SPOZAEKIPY'), {'new_controller_first_name'}, None),
            ('duplikat pseudonimu', self.new_person('nocny', '  KRUK'), {'new_controller_first_name'}, None),
            ('duplikat e-maila', self.new_person('Inna', 'Osoba', 'ADAM@example.com'),
             {'new_controller_first_name'}, None),
            ('zły e-mail', self.new_person('Inna', 'Osoba', 'bez-malpy'), {'new_controller_email'}, None),
        ]
        for case, data, errors, created in cases:
            with self.subTest(case):
                form = self.form(**data)
                self.assertEqual(form.is_valid(), not errors, form.errors)
                self.assertEqual(set(form.errors), errors)
                person = form.new_controller
                self.assertEqual(person and (person.first_name, person.last_name, person.email), created)
        form = self.form(**self.new_person('Ewa', 'Kowalska'))
        form.is_valid()
        self.assertIn('Ewa Kowalska', str(form.errors['new_controller_first_name']))

    def test_coordinator_adds_new_external_person_next_to_selected_ones(self):
        """Wybrana osoba spoza ekipy i nowa osoba podane razem: obie trafiają do konsultacji; nowa osoba jest spoza
        ekipy, nieaktywna, bez konta; strona AD, lista AD i lista zadań pokazują je z oznaczeniem „spoza ekipy”."""
        before = Person.objects.count()
        response = self.assign(controllers=[self.external.pk, self.controller.person_profile.pk],
                               **self.new_person(' Nowa ', 'Konsultantka', 'Nowa@Example.com'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Person.objects.count(), before + 1)
        new = Person.objects.get(last_name='Konsultantka')
        self.assertEqual((new.first_name, new.email, new.user_id, new.is_external, new.is_active),
                         ('Nowa', 'nowa@example.com', None, True, False))
        self.assertEqual(set(self.description.controllers.all()), {self.external, new, self.controller.person_profile})
        self.client.force_login(self.admin)
        for url in (self.detail, self.list_url, reverse('core:task_list')):
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                self.assertEqual(content.count('(spoza ekipy)'), 2)
                self.assertIn('Nowa Konsultantka', content)
                self.assertNotIn(f'{self.controller.person_profile} (spoza ekipy)', content)
        tasks = self.client.get(reverse('core:task_list'), {'q': 'konsultantka'})
        self.assertEqual(tasks.context['page_obj'].paginator.count, 1)

    def test_search_offers_external_and_inactive_people_only_for_consultation(self):
        """Wyszukiwarka konsultacji ma też osoby spoza ekipy i nieaktywne bez historii (wyraźnie oznaczone); pole
        „Kto pisze” ich nie ma. Wybór osoby spoza ekipy się zapisuje."""
        form = self.form()
        consultants = {str(value): label for value, label in form.fields['controllers'].choices}
        writers = {str(value) for value, _ in form.fields['assigned_to'].choices}
        self.assertEqual(consultants[str(self.external.pk)], 'Olga Spozaekipy – spoza ekipy')
        self.assertEqual(consultants[str(self.former.pk)], 'Stary Członek – nieaktywny')
        self.assertEqual(consultants[str(self.team_person.pk)], 'Ewa Kowalska')
        self.assertNotIn(str(self.external.pk), writers)
        self.assertNotIn(str(self.former.pk), writers)
        self.assertIn(str(self.team_person.pk), writers)
        self.assertEqual(self.assign(controllers=[self.external.pk, self.former.pk]).status_code, 302)
        self.assertEqual(set(self.description.controllers.all()), {self.external, self.former})
        # Zapisani konsultanci zostają w wyborze po ponownym otwarciu formularza.
        self.assertEqual(set(self.form().fields['controllers'].initial), {self.external.pk, self.former.pk})

    def test_parallel_creation_reuses_sole_match_and_rejects_several(self):
        """Tę samą osobę ktoś dodał między sprawdzeniem formularza a zapisem: jedyne dopasowanie zostaje użyte bez
        drugiego wpisu; kilka dopasowań – błąd 400 i nic się nie zmienia."""
        from unittest.mock import patch
        from core.views.audio_descriptions import DescriptionForm
        original = DescriptionForm.is_valid

        def creating(count, first):
            def is_valid(form):
                result = original(form)
                for _ in range(count):
                    Person.objects.create(first_name=first, last_name='Osoba', is_external=True)
                return result
            return is_valid

        with patch.object(DescriptionForm, 'is_valid', autospec=True, side_effect=creating(1, 'Równoległa')):
            response = self.assign(**self.new_person('Równoległa', 'Osoba'))
        self.assertEqual(response.status_code, 302)
        people = Person.objects.filter(last_name='Osoba')
        self.assertEqual(people.count(), 1)
        self.assertEqual(list(self.description.controllers.all()), list(people))
        self.description.controllers.clear()
        with patch.object(DescriptionForm, 'is_valid', autospec=True, side_effect=creating(2, 'Inna')):
            response = self.assign(assigned_to=self.writer.person_profile.pk,
                                   **self.new_person('Inna', 'Osoba'))
        self.assertContains(response, 'Powstało kilka pasujących wpisów', status_code=400)
        self.assertFalse(self.description.controllers.exists())
        self.assertEqual(Person.objects.filter(first_name='Inna', last_name='Osoba').count(), 2)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to_id)

    def test_role_matrix_for_adding_external_consultant(self):
        """Nową osobę spoza ekipy dodaje tylko koordynator lub superuser; osoba pisząca, kontroler, zwykły członek
        i konto spoza zespołu – 403; gość – przekierowanie do logowania. Odrzucona próba niczego nie tworzy."""
        self.assign_writer_and_controller()
        outsider = get_user_model().objects.create_user('ad-consult-outsider')
        cases = [('superuser', self.admin, 302), ('koordynator', self.manager, 302),
                 ('osoba pisząca', self.writer, 403), ('kontroler', self.controller, 403),
                 ('zwykły członek', self.other, 403), ('konto spoza zespołu', outsider, 403)]
        for number, (case, user, status) in enumerate(cases):
            with self.subTest(case):
                last = f'Macierz{number}'
                data = {'assigned_to': self.writer.person_profile.pk, **self.new_person('Rola', last)}
                response = self.post(user, action='assignment', **data)
                self.assertEqual(response.status_code, status)
                self.assertEqual(Person.objects.filter(last_name=last).exists(), status == 302)
        self.client.logout()
        response = self.client.post(self.detail, {'action': 'assignment', **self.new_person('Gość', 'Macierz')})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)
        self.assertFalse(Person.objects.filter(last_name='Macierz').exists())

    def test_external_consultant_gets_no_access(self):
        """Osoba spoza ekipy w konsultacji nie ma konta; nawet z dawnym kontem nie zaloguje się, nie jest członkiem
        zespołu i nie zmieni etapu ani przypisania (uprawnienia tylko dla osób z aktywnym kontem w ekipie)."""
        from core.auth_backends import EmailBackend
        from core.permissions import is_team_member
        from core.views.audio_descriptions import may_edit
        self.assign(controllers=[], **self.new_person('Bez', 'Dostępu'))
        new = Person.objects.get(last_name='Dostępu')
        self.assertIsNone(new.user_id)
        user = get_user_model().objects.create_user('ad-external-consultant')
        self.external.user = user
        self.external.save()
        self.description.controllers.set([self.external, new])
        user.refresh_from_db()
        self.assertFalse(EmailBackend().user_can_authenticate(user))
        self.assertFalse(is_team_member(user))
        self.assertFalse(may_edit(user, self.book))
        for data in ({'action': 'stage', 'stage': 'consultation'}, {'action': 'content', 'content': 'x'},
                     {'action': 'assignment', 'controllers': [self.external.pk]}):
            with self.subTest(data=data):
                response = self.post(user, **data)
                self.assertIn(response.status_code, (302, 403))
                if response.status_code == 302:
                    self.assertIn(reverse('login'), response.url)
        self.description.refresh_from_db()
        self.assertEqual((self.description.stage, self.description.content), ('writing', ''))

    def test_external_consultant_absent_from_team_lists_exports_and_integrations(self):
        """Nowa osoba spoza ekipy z konsultacji nie trafia do: aktywnego zespołu, danych strony „Ekipa”
        (fantazmaty.pl), eksportu zespołu, wyboru wykonawców innych zadań, Dropboxa ani Discorda.
        Członek ekipy z konsultacji zostaje na liście wykonawców (regresja)."""
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        from core.anthology_tasks import task_people
        from core.notification_models import DiscordNotification
        from core.site_team import people_by_role, team_payload
        from people.models import DropboxAccess, Role
        notifications = DiscordNotification.objects.count()
        with patch('people.dropbox_access.schedule') as schedule:
            self.assign(controllers=[self.former.pk], **self.new_person('Tylko', 'Konsultująca'))
        new = Person.objects.get(last_name='Konsultująca')
        schedule.assert_not_called()
        self.assertFalse(DropboxAccess.objects.filter(person=new).exists())
        self.assertEqual(DiscordNotification.objects.count(), notifications)
        # Nawet z rolą osoba spoza ekipy nie trafia na stronę „Ekipa”.
        new.roles.add(Role.objects.get(name='Korektor'))
        names = {name for people in people_by_role().values() for _, name in people.values()}
        self.assertNotIn('Tylko Konsultująca', names)
        self.assertNotIn('Konsultująca', str(team_payload()))
        new.roles.clear()
        export = StringIO()
        call_command('export_team_members', stdout=export)
        checks = {
            'aktywny zespół': Person.objects.active(),
            'wykonawcy zadań': task_people(),
        }
        for case, people in checks.items():
            with self.subTest(case):
                self.assertNotIn(new, people)
                self.assertNotIn(self.external, people)
        self.assertNotIn('Konsultująca', export.getvalue())
        # Dawny członek ekipy z konsultacji zostaje na liście wykonawców.
        self.assertIn(self.former, task_people())

    def test_people_page_lists_external_people_as_before(self):
        """Strona „Zespół” (decyzja właściciela – bez zmian): pokazuje wszystkie osoby spoza ekipy, także
        konsultantów audiodeskrypcji bez konta i ról."""
        from people.models import Role
        with_role = Person.objects.create(first_name='Zewn', last_name='Zrolą', is_external=True)
        with_role.roles.add(Role.objects.get(name='Korektor'))
        with_account = Person.objects.create(first_name='Zewn', last_name='Zkontem', is_external=True,
                                             user=get_user_model().objects.create_user('ad-ext-account'))
        self.client.force_login(self.other)
        people = list(self.client.get(reverse('core:people_list')).context['people'])
        for person in (with_role, with_account, self.external):
            with self.subTest(person=str(person)):
                self.assertIn(person, people)
