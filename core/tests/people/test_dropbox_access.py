"""Udostępnianie folderów Dropbox członkom zespołu: klient API (zapytania HTTP podstawione), synchronizacja po
zapisie w adminie, zmiana adresu i poziomu, odebranie dostępu przy usunięciu z zespołu, komenda i brak konfiguracji."""
import json
import urllib.error
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_member, create_superuser, form_value
from people.dropbox_access import NOT_CONFIGURED_NOTICE
from people.dropbox_sharing import DropboxClient, DropboxError, Member, message_for
from people.models import DropboxAccess, DropboxFolder, Person

CONFIGURED = override_settings(DROPBOX_APP_KEY='app-key', DROPBOX_APP_SECRET='app-secret',
                               DROPBOX_REFRESH_TOKEN='refresh-token')
State = DropboxAccess.State


class Transport:
    """Podstawione _send: odpowiedzi kolejno dla trasy API (albo „token”), zapisane zapytania."""

    def __init__(self, **routes):
        self.routes = {name.replace('__', '/'): list(responses) for name, responses in routes.items()}
        self.requests = []

    def __call__(self, url, data, headers):
        route = 'token' if url.endswith('/oauth2/token') else url.split('/2/', 1)[1]
        body = data.decode()
        self.requests.append((route, json.loads(body) if route != 'token' else body, headers))
        response = self.routes[route].pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response if isinstance(response, tuple) else (200, response)
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode(), {}

    def calls(self, route):
        return [payload for name, payload, _ in self.requests if name == route]


TOKEN = (200, {'access_token': 'token-1', 'expires_in': 14400, 'token_type': 'bearer'})


@CONFIGURED
class DropboxClientTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        patcher = patch('people.dropbox_sharing.time.sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_with(self, **routes):
        transport = Transport(**routes)
        patcher = patch('people.dropbox_sharing._send', transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        return DropboxClient(), transport

    def test_token_is_refreshed_once_and_renewed_after_expiry(self):
        """Refresh token wymieniany na token dostępu raz (pamięć podręczna); wygasły token – jedno odświeżenie i ponowienie."""
        client, transport = self.client_with(
            token=[TOKEN, (200, {'access_token': 'token-2', 'expires_in': 14400})],
            files__get_metadata=[{'.tag': 'folder', 'sharing_info': {'shared_folder_id': '11'}},
                                 (401, {'error': {'.tag': 'expired_access_token'}}),
                                 {'.tag': 'folder', 'shared_folder_id': '11'}])
        self.assertEqual(client.ensure_shared('/Fantazmaty/test CMS'), '11')
        self.assertEqual(client.ensure_shared('/Fantazmaty/test CMS'), '11')
        tokens = transport.calls('token')
        self.assertEqual(len(tokens), 2)
        self.assertIn('grant_type=refresh_token', tokens[0])
        self.assertIn('refresh_token=refresh-token', tokens[0])
        auth = [headers['Authorization'] for name, _, headers in transport.requests if name != 'token']
        self.assertEqual(auth, ['Bearer token-1', 'Bearer token-1', 'Bearer token-2'])

    def test_share_folder_waits_for_async_job(self):
        """Nieudostępniony folder: share_folder zwraca zadanie, a identyfikator przychodzi po jego zakończeniu."""
        client, transport = self.client_with(
            token=[TOKEN], files__get_metadata=[{'.tag': 'folder', 'sharing_info': None}],
            sharing__share_folder=[{'.tag': 'async_job_id', 'async_job_id': 'job-1'}],
            sharing__check_share_job_status=[{'.tag': 'in_progress'}, {'.tag': 'complete', 'shared_folder_id': '77'}])
        self.assertEqual(client.ensure_shared('/Fantazmaty/test CMS'), '77')
        self.assertEqual(transport.calls('sharing/share_folder'), [{'path': '/Fantazmaty/test CMS', 'force_async': False}])
        self.assertEqual(len(transport.calls('sharing/check_share_job_status')), 2)

    def test_member_operations_send_expected_payloads(self):
        """Dodanie (z powiadomieniem), zmiana poziomu, usunięcie bez kopii (zadanie asynchroniczne) i lista członków
        z zaproszeniami bez konta oraz kolejną stroną."""
        client, transport = self.client_with(
            token=[TOKEN], sharing__add_folder_member=[None], sharing__update_folder_member=[{}],
            sharing__remove_folder_member=[{'.tag': 'async_job_id', 'async_job_id': 'job-2'}],
            sharing__check_remove_member_job_status=[{'.tag': 'complete'}],
            sharing__list_folder_members=[{
                'users': [{'access_type': {'.tag': 'owner'}, 'user': {'email': 'Owner@Example.test'}, 'is_inherited': False}],
                'invitees': [{'access_type': {'.tag': 'viewer'}, 'invitee': {'.tag': 'email', 'email': 'nowy@example.test'}}],
                'cursor': 'next'}],
            sharing__list_folder_members__continue=[{
                'users': [{'access_type': {'.tag': 'editor'}, 'user': {'email': 'ola@example.test'}}], 'invitees': []}])
        client.add_member('11', 'ola@example.test', 'editor')
        client.update_member('11', 'ola@example.test', 'viewer')
        client.remove_member('11', 'ola@example.test')
        self.assertEqual(client.folder_members('11'), {
            'owner@example.test': Member('owner'), 'nowy@example.test': Member('viewer', invitee=True),
            'ola@example.test': Member('editor')})
        self.assertEqual(transport.calls('sharing/add_folder_member'), [{
            'shared_folder_id': '11', 'quiet': False,
            'members': [{'member': {'.tag': 'email', 'email': 'ola@example.test'}, 'access_level': {'.tag': 'editor'}}]}])
        self.assertEqual(transport.calls('sharing/update_folder_member')[0]['access_level'], {'.tag': 'viewer'})
        self.assertEqual(transport.calls('sharing/remove_folder_member')[0]['leave_a_copy'], False)
        self.assertEqual(transport.calls('sharing/check_remove_member_job_status'), [{'async_job_id': 'job-2'}])

    def test_removing_someone_already_gone_is_not_an_error(self):
        """Usunięcie osoby, której nie ma już w folderze, albo z folderu usuniętego w Dropboxie – bez błędu (dostępu
        i tak nie ma); inny błąd usuwania zostaje błędem."""
        job = [{'.tag': 'async_job_id', 'async_job_id': 'job-3'}]
        cases = [
            ('osoby nie ma w folderze', job, [{'.tag': 'failed', 'failed': {
                '.tag': 'member_error', 'member_error': {'.tag': 'not_a_member'}}}], None),
            ('folder usunięty', [(409, {'error_summary': 'access_error/invalid_id/..'})], [], None),
            ('folder odmontowany', [(409, {'error_summary': 'access_error/unmounted/.'})], [], None),
            ('brak uprawnień', [(409, {'error_summary': 'access_error/no_permission/..'})], [], 'nie ma uprawnień'),
        ]
        for label, remove, status, error in cases:
            with self.subTest(label):
                cache.clear()
                client, _ = self.client_with(token=[TOKEN], sharing__remove_folder_member=list(remove),
                                             sharing__check_remove_member_job_status=list(status))
                if error:
                    with self.assertRaisesMessage(DropboxError, error):
                        client.remove_member('11', 'gone@example.test')
                else:
                    client.remove_member('11', 'gone@example.test')

    def test_api_errors_become_polish_messages(self):
        """Błędy API, tokenu i połączenia → czytelny komunikat po polsku."""
        conflict = (409, {'error_summary': 'add_folder_member/bad_member/invalid_email/..'})
        cases = [
            ('niepoprawny e-mail', [TOKEN], conflict, 'Dropbox uznał adres e-mail za niepoprawny.'),
            ('limit członków', [TOKEN], (409, {'error_summary': 'add_folder_member/too_many_members/'}), 'limit członków'),
            ('folder nie należy do konta', [TOKEN], (409, {'error_summary': 'add_folder_member/no_permission/.'}),
             'folder nie należy do konta'),
            ('brak uprawnienia', [TOKEN], (401, {'error': {'.tag': 'missing_scope', 'required_scope': 'sharing.write'}}),
             'brakuje uprawnienia „sharing.write”'),
            ('limit zapytań', [TOKEN], (429, {}), 'ogranicza liczbę zapytań'),
            ('awaria Dropboxa', [TOKEN], (503, {}), 'chwilowo niedostępny (HTTP 503)'),
            ('odwołany refresh token', [(400, {'error': 'invalid_grant'})], None, 'Refresh token Dropboxa jest nieważny'),
            ('zły klucz aplikacji', [(400, {'error': 'invalid_client'})], None, 'Klucz lub sekret aplikacji Dropbox'),
        ]
        for label, token, response, message in cases:
            with self.subTest(label):
                cache.clear()
                client, _ = self.client_with(token=token, sharing__add_folder_member=[response])
                with self.assertRaisesMessage(DropboxError, message):
                    client.add_member('11', 'x@example.test', 'editor')

    def test_connection_failure_and_nested_folder(self):
        """Błąd sieci z urlopen → DropboxError; o folderze wewnątrz udostępnionego decyduje Dropbox (w folderze
        zespołu podfolder da się udostępnić, w zwykłym – błąd inside_shared_folder z podpowiedzią)."""
        with patch('people.dropbox_sharing.urllib.request.urlopen', side_effect=urllib.error.URLError('timeout')):
            with self.assertRaisesMessage(DropboxError, 'Brak połączenia z Dropboxem (timeout)'):
                DropboxClient().access_token()
        nested = {'.tag': 'folder', 'sharing_info': {'parent_shared_folder_id': '5'}}
        refused = (409, {'error_summary': 'bad_path/inside_shared_folder/..',
                         'error': {'.tag': 'bad_path', 'bad_path': {'.tag': 'inside_shared_folder'}}})
        cases = [('folder zespołu', {'.tag': 'complete', 'shared_folder_id': '77'}, '77'),
                 ('zwykły udostępniony folder', refused, None)]
        for label, answer, expected in cases:
            with self.subTest(label):
                cache.clear()
                client, transport = self.client_with(token=[TOKEN], files__get_metadata=[nested],
                                                     sharing__share_folder=[answer])
                if expected:
                    self.assertEqual(client.ensure_shared('/Fantazmaty/test CMS'), expected)
                else:
                    with self.assertRaisesMessage(DropboxError, 'cofnij udostępnienie folderu nadrzędnego'):
                        client.ensure_shared('/Fantazmaty/test CMS')
                self.assertEqual(len(transport.calls('sharing/share_folder')), 1)

    @override_settings(DROPBOX_REFRESH_TOKEN='')
    def test_missing_configuration(self):
        with self.assertRaisesMessage(DropboxError, 'Dropbox nie jest skonfigurowany'):
            DropboxClient()

    def test_token_cache_lifetime_follows_expires_in(self):
        """Token w pamięci podręcznej wygasa z zapasem przed terminem Dropboxa (najkrócej po 60 s)."""
        for expires_in, timeout in ((14400, 14100), (200, 60), (None, 14100)):
            with self.subTest(expires_in=expires_in), patch('people.dropbox_sharing.cache') as fake_cache:
                fake_cache.get.return_value = None
                client, _ = self.client_with(token=[(200, {'access_token': 'token-x', 'expires_in': expires_in})])
                self.assertEqual(client.access_token(), 'token-x')
                self.assertEqual(fake_cache.set.call_args.args, (client.cache_key, 'token-x', timeout))

    def test_share_folder_outcomes(self):
        """Udostępnianie: folder udostępniony w międzyczasie (metadane w błędzie 409, pola obok „.tag”, jak w API),
        zadanie zakończone błędem i zadanie, które nie kończy się w limicie prób."""
        already = (409, {'error_summary': 'bad_path/already_shared/..', 'error': {'.tag': 'bad_path', 'bad_path': {
            '.tag': 'already_shared', 'shared_folder_id': '88', 'path_lower': '/fantazmaty/test cms'}}})
        job = {'.tag': 'async_job_id', 'async_job_id': 'job-9'}
        failed = {'.tag': 'failed', 'failed': {'.tag': 'bad_path', 'bad_path': {'.tag': 'contains_shared_folder'}}}
        cases = [
            ('już udostępniony', [already], [], '88'),
            ('zadanie nieudane', [job], [failed], 'zawiera udostępnione podfoldery'),
            ('zadanie trwa', [job], [{'.tag': 'in_progress'}] * 15, 'wciąż przetwarza zmianę'),
        ]
        for label, share, status, expected in cases:
            with self.subTest(label):
                cache.clear()
                client, transport = self.client_with(
                    token=[TOKEN], files__get_metadata=[{'.tag': 'folder', 'sharing_info': None}],
                    sharing__share_folder=share, sharing__check_share_job_status=list(status))
                if label == 'już udostępniony':
                    self.assertEqual(client.ensure_shared('/Fantazmaty/test CMS'), expected)
                else:
                    with self.assertRaisesMessage(DropboxError, expected):
                        client.ensure_shared('/Fantazmaty/test CMS')
                self.assertEqual(len(transport.calls('sharing/check_share_job_status')), len(status))

    def test_unreadable_answers_and_rejected_token(self):
        """Nieczytelna odpowiedź 200, brak członka przy zmianie poziomu i token odrzucony także po odświeżeniu →
        DropboxError z polskim komunikatem (bez wyjątku innego typu i bez kolejnych ponowień)."""
        cases = [
            ('nieczytelny JSON', 'files__get_metadata', [(200, b'<html>proxy</html>')], [TOKEN],
             lambda client: client.folder_metadata('/Fantazmaty'), 'nieczytelną odpowiedź'),
            ('brak członka', 'sharing__update_folder_member',
             [(409, {'error_summary': 'member_error/not_a_member/..'})], [TOKEN],
             lambda client: client.update_member('11', 'x@example.test', 'viewer'), 'Tej osoby nie ma już w folderze'),
            ('token odrzucony dwa razy', 'files__get_metadata',
             [(401, {'error': {'.tag': 'invalid_access_token'}})] * 2, [TOKEN, TOKEN],
             lambda client: client.folder_metadata('/Fantazmaty'), 'Dropbox odrzucił token dostępu'),
        ]
        for label, route, responses, token, run, message in cases:
            with self.subTest(label):
                cache.clear()
                client, transport = self.client_with(token=token, **{route: list(responses)})
                with self.assertRaisesMessage(DropboxError, message):
                    run(client)
                self.assertEqual(len(transport.calls(route.replace('__', '/'))), len(responses))


class FakeDropbox:
    """Dropbox w pamięci dla synchronizacji: członkowie folderów, konta istniejące i wymuszone błędy."""

    def __init__(self, accounts=(), shared=None):
        self.accounts = {email.lower() for email in accounts}
        self.members = {}
        self.calls = []
        self.failures = {}
        self.shared = dict(shared or {})  # ścieżka → identyfikator folderu udostępnionego już w Dropboxie
        self.locations = {}  # identyfikator udostępnienia → obecna ścieżka folderu
        self.deleted = set()  # ścieżki i identyfikatory folderów usuniętych w Dropboxie

    def _gone(self, *keys):
        """Folder usunięty w Dropboxie – błędy jak z API (ścieżka: not_found, udostępnienie: invalid_id)."""
        for key in keys:
            if key in self.deleted:
                summary = 'path/not_found/..' if key.startswith('/') else 'access_error/invalid_id/..'
                raise DropboxError(message_for(summary), summary)

    def _fail(self, operation):
        if operation in self.failures:
            raise DropboxError(self.failures[operation])

    def folder_metadata(self, path):
        self._gone(path)
        shared = self.shared.get(path)
        return {'.tag': 'folder', 'sharing_info': {'shared_folder_id': shared} if shared else None}

    def ensure_shared(self, path):
        self._fail('share')
        self.calls.append(('share', path))
        return 'sf-' + path.rsplit('/', 1)[-1]

    def shared_folder_metadata(self, shared_id):
        self._fail('metadata')
        self._gone(shared_id)
        path = self.locations[shared_id]
        return {'shared_folder_id': shared_id, 'path_lower': path.lower(), 'path_display': path}

    def folder_members(self, shared_id):
        self._fail('members')
        self._gone(shared_id)
        return dict(self.members.setdefault(shared_id, {'owner@example.test': Member('owner')}))

    def add_member(self, shared_id, email, level):
        self._fail('add')
        self.calls.append(('add', shared_id, email, level))
        self.members.setdefault(shared_id, {})[email] = Member(level, invitee=email not in self.accounts)
        self._fail('add_after')  # Dropbox dodał osobę, ale odpowiedź nie dotarła (np. przekroczony czas).

    def update_member(self, shared_id, email, level):
        self.calls.append(('update', shared_id, email, level))
        self.members[shared_id][email] = Member(level, self.members[shared_id][email].invitee)

    def remove_member(self, shared_id, email):
        self._fail('remove')
        self.calls.append(('remove', shared_id, email))
        self.members[shared_id].pop(email, None)


class DropboxAccessAdminTests(TestCase):
    """Ekran osoby w adminie: poziom dostępu do folderów, synchronizacja po zapisie i odebranie przy usunięciu z zespołu."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('dropbox-admin')
        cls.folder = DropboxFolder.objects.create(name='Test CMS', path='/Fantazmaty/test CMS')
        cls.other = DropboxFolder.objects.create(name='Redakcja', path='/Fantazmaty/Redakcja', shared_folder_id='sf-Redakcja')
        cls.member = create_member('dropbox-member', 'Redaktor')
        cls.person = cls.member.person_profile
        Person.objects.filter(pk=cls.person.pk).update(dropbox_email='ola@example.test')

    def setUp(self):
        self.client.force_login(self.admin)
        self.fake = FakeDropbox(accounts=['ola@example.test', 'nowy@example.test'])
        patcher = patch('people.dropbox_access.DropboxClient', return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        activity = patch('core.activity_spool.enqueue_activity')
        self.activity = activity.start()
        self.addCleanup(activity.stop)

    def field(self, folder):
        return f'dropbox_folder_{folder.pk}'

    def save_person(self, person=None, **changes):
        """Zapis ekranu osoby jak z przeglądarki; zwraca (odpowiedź, komunikaty z synchronizacji po zatwierdzeniu)."""
        url = reverse('admin:people_person_change', args=[(person or self.person).pk])
        data = admin_form_data(self.client, url, 'person_form', **changes)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, data)
        return response, [str(message) for message in response.wsgi_request._messages]

    def access(self, folder=None, person=None):
        return DropboxAccess.objects.get(folder=folder or self.folder, person=person or self.person)

    def grant(self, level='editor', folder=None):
        with CONFIGURED:
            return self.save_person(**{self.field(folder or self.folder): level})

    def test_grant_after_save_with_message_and_activity(self):
        """Wybór poziomu: folder jest udostępniany, osoba dodana, stan „nadany”, komunikat i wpis w dzienniku."""
        response, messages = self.grant()
        self.assertEqual(response.status_code, 302)
        access = self.access()
        self.assertEqual((access.state, access.granted_email, access.level), (State.GRANTED, 'ola@example.test', 'editor'))
        self.assertEqual(self.fake.calls, [('share', '/Fantazmaty/test CMS'),
                                           ('add', 'sf-test CMS', 'ola@example.test', 'editor')])
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.shared_folder_id, 'sf-test CMS')
        self.assertIn('Dropbox – dropbox-member Testowy → Test CMS: nadanie dostępu dla ola@example.test (Edycja).', messages)
        entry = self.activity.call_args.kwargs
        self.assertEqual((entry['user_id'], entry['action']), (self.admin.pk, 'Dropbox: nadanie dostępu (Edycja)'))
        self.assertIn('/Fantazmaty/test CMS', entry['target'])

    def test_level_email_and_removal_changes(self):
        """Zmiana poziomu → update; zmiana adresu → usunięcie starego i dodanie nowego; „Brak dostępu” → usunięcie."""
        self.grant()
        cases = [
            ({self.field(self.folder): 'viewer'}, ('update', 'sf-test CMS', 'ola@example.test', 'viewer'),
             (State.GRANTED, 'ola@example.test')),
            ({'dropbox_email': 'Nowy@Example.test'}, ('add', 'sf-test CMS', 'nowy@example.test', 'viewer'),
             (State.GRANTED, 'nowy@example.test')),
            ({self.field(self.folder): ''}, ('remove', 'sf-test CMS', 'nowy@example.test'), (State.REMOVED, '')),
        ]
        for changes, last_call, state in cases:
            with self.subTest(changes=changes), CONFIGURED:
                self.save_person(**changes)
                access = self.access()
                self.assertEqual(self.fake.calls[-1], last_call)
                self.assertEqual((access.state, access.granted_email), state)
        self.assertIn(('remove', 'sf-test CMS', 'ola@example.test'), self.fake.calls)
        self.assertFalse(self.access().wanted)

    def test_invitee_without_account(self):
        Person.objects.filter(pk=self.person.pk).update(dropbox_email='bezkonta@example.test')
        self.grant()
        self.assertEqual(self.access().state, State.INVITED)

    def test_access_requires_dropbox_email_and_active_member(self):
        """Bez adresu Dropboxa albo dla osoby spoza zespołu poziomu nie da się ustawić."""
        cases = [({'dropbox_email': ''}, 'dropbox_email', 'Podaj adres e-mail Dropboxa'),
                 ({'is_active': None}, self.field(self.folder), 'tylko osoby „wciąż w ekipie”')]
        for changes, field, message in cases:
            with self.subTest(field=field):
                response, _ = self.save_person(**{self.field(self.folder): 'editor', **changes})
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, str(response.context['adminform'].form.errors[field]))
        self.assertFalse(DropboxAccess.objects.exists())

    def test_dropbox_error_is_saved_and_reported_then_retried(self):
        """Błąd Dropboxa nie cofa zapisu osoby: stan „błąd” z treścią, komunikat; „Synchronizuj ponownie” naprawia."""
        self.fake.failures['add'] = 'Folder osiągnął limit członków Dropboxa.'
        response, messages = self.grant()
        self.assertEqual(response.status_code, 302)
        access = self.access()
        self.assertEqual((access.state, access.last_error), (State.ERROR, 'Folder osiągnął limit członków Dropboxa.'))
        self.assertIn('Dropbox – dropbox-member Testowy → Test CMS: Folder osiągnął limit członków Dropboxa.', messages)
        self.assertContains(self.client.get(reverse('admin:people_dropboxaccess_changelist')), 'limit członków')
        del self.fake.failures['add']
        with CONFIGURED:
            self.client.post(reverse('admin:people_dropboxaccess_changelist'),
                             {'action': 'sync_again', '_selected_action': [access.pk], 'index': '0'})
        access.refresh_from_db()
        self.assertEqual((access.state, access.last_error), (State.GRANTED, ''))

    def test_leaving_team_revokes_every_folder(self):
        """Usunięcie z zespołu (ekran osoby, akcja zbiorcza, usunięcie osoby, usunięcie zbiorcze) odbiera dostęp do
        wszystkich folderów; osoby dodane w Dropboxie ręcznie zostają."""
        changelist = reverse('admin:people_person_changelist')

        def edit(person):
            self.save_person(person, is_active=None)

        def bulk(person):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(changelist, {'action': 'remove_from_team', '_selected_action': [person.pk], 'index': '0'})

        def delete(person):
            url = reverse('admin:people_person_delete', args=[person.pk])
            token = form_value(self.client.get(url))
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {'post': 'yes', '_edit_version': token})

        def bulk_delete(person):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(changelist, {'action': 'delete_selected', '_selected_action': [person.pk], 'post': 'yes'})

        for index, (label, leave) in enumerate((('ekran osoby', edit), ('akcja „Usuń z zespołu”', bulk),
                                                ('usunięcie osoby', delete), ('usunięcie zbiorcze', bulk_delete))):
            with self.subTest(label), CONFIGURED:
                email = f'osoba{index}@example.test'
                person = Person.objects.create(first_name='Osoba', last_name=str(index), email=f'p{index}@example.test',
                                               dropbox_email=email)
                self.fake.accounts.add(email)
                self.fake.members.setdefault('sf-Redakcja', {})['reczny@example.test'] = Member('editor')
                self.save_person(person, **{self.field(self.folder): 'editor', self.field(self.other): 'viewer'})
                ids = list(DropboxAccess.objects.filter(granted_email=email).values_list('pk', flat=True))
                self.assertEqual(len(ids), 2)
                leave(person)
                rows = DropboxAccess.objects.filter(pk__in=ids)
                self.assertEqual(set(rows.values_list('state', 'wanted', 'granted_email')), {(State.REMOVED, False, '')})
                self.assertEqual(Person.objects.filter(pk=person.pk, is_active=True).exists(), False)
                for shared_id in ('sf-test CMS', 'sf-Redakcja'):
                    self.assertIn(('remove', shared_id, email), self.fake.calls)
                self.assertIn('reczny@example.test', self.fake.members['sf-Redakcja'])
        self.assertNotIn('owner@example.test', [call[2] for call in self.fake.calls if call[0] == 'remove'])

    def test_reactivation_does_not_restore_access(self):
        self.grant()
        with CONFIGURED:
            self.save_person(is_active=None)
            self.save_person(is_active='on')
        access = self.access()
        self.assertEqual((access.wanted, access.state), (False, State.REMOVED))
        page = self.client.get(reverse('admin:people_person_change', args=[self.person.pk]))
        self.assertEqual(page.context['adminform'].form[self.field(self.folder)].initial, '')

    def test_without_configuration_access_waits(self):
        """Bez kluczy Dropboxa: informacja na ekranie osoby, zapis możliwy, stan „oczekuje”, bez połączenia."""
        page = self.client.get(reverse('admin:people_person_change', args=[self.person.pk]))
        self.assertContains(page, NOT_CONFIGURED_NOTICE)
        self.assertContains(page, 'Dropbox: Test CMS')
        response, messages = self.save_person(**{self.field(self.folder): 'viewer'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.access().state, self.access().level), (State.PENDING, 'viewer'))
        self.assertIn(NOT_CONFIGURED_NOTICE, messages)
        self.assertEqual(self.fake.calls, [])

    def test_external_flag_revokes_access(self):
        """Zaznaczenie „Zewnętrzny” (bez ręcznego odznaczania „wciąż w ekipie”) też odbiera dostęp do folderów."""
        self.grant()
        with CONFIGURED:
            response, messages = self.save_person(is_external='on')
        self.assertEqual(response.status_code, 302)
        access = self.access()
        self.assertEqual((access.wanted, access.state, access.granted_email), (False, State.REMOVED, ''))
        self.assertEqual(self.fake.calls[-1], ('remove', 'sf-test CMS', 'ola@example.test'))
        self.assertIn('Dropbox – dropbox-member Testowy → Test CMS: odebrano dostęp (ola@example.test).', messages)

    def test_inactive_folder_only_for_people_with_access(self):
        """Folder wyłączony („do wyboru przy osobach” odznaczone) znika z ekranu nowych osób, ale zostaje u osoby,
        która ma do niego dostęp – można go jej odebrać."""
        self.grant()
        DropboxFolder.objects.filter(pk=self.folder.pk).update(is_active=False)
        field = self.field(self.folder)
        pages = [('osoba z dostępem', reverse('admin:people_person_change', args=[self.person.pk]), True),
                 ('nowa osoba', reverse('admin:people_person_add'), False)]
        for label, url, shown in pages:
            with self.subTest(label):
                self.assertEqual(field in self.client.get(url).context['adminform'].form.fields, shown)
        with CONFIGURED:
            self.save_person(**{field: ''})
        self.assertEqual(self.access().state, State.REMOVED)

    def test_new_person_with_account_and_access(self):
        """Dodanie osoby z zakładaniem konta (zaproszeniem) od razu z dostępem do folderu."""
        url = reverse('admin:people_person_add')
        data = admin_form_data(self.client, url, 'person_form', first_name='Ewa', last_name='Nowa',
                               email='ewa@example.test', dropbox_email='ewa-box@example.test', create_account='on',
                               **{self.field(self.folder): 'editor'})
        with CONFIGURED, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url, data).status_code, 302)
        access = DropboxAccess.objects.get(person__email='ewa@example.test')
        self.assertEqual((access.state, access.granted_email), (State.INVITED, 'ewa-box@example.test'))

    def test_admin_screens_are_superuser_only(self):
        """Foldery, dostępy i ekran osoby są w panelu admina – tylko dla superusera (koordynator go nie widzi)."""
        coordinator = create_member('dropbox-coordinator', 'Koordynator redakcji')
        urls = [reverse('admin:people_person_change', args=[self.person.pk]),
                reverse('admin:people_dropboxfolder_changelist'), reverse('admin:people_dropboxfolder_fetch'),
                reverse('admin:people_dropboxaccess_changelist')]
        for user, allowed in ((self.admin, True), (coordinator, False)):
            self.client.force_login(user)
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code == 200, allowed)


class DropboxFolderAdminTests(TestCase):
    """Lista folderów: pobranie podfolderów /Fantazmaty z oznaczeniem udostępnienia i dodanie zaznaczonych."""

    def setUp(self):
        self.client.force_login(create_superuser('folders-admin'))
        DropboxFolder.objects.create(name='Stary', path='/Fantazmaty/Stary')

    def test_fetch_lists_and_adds_selected_folders(self):
        listing = [{'name': 'Stary', 'path': '/Fantazmaty/stary', 'shared_folder_id': ''},
                   {'name': 'test CMS', 'path': '/Fantazmaty/test CMS', 'shared_folder_id': ''},
                   {'name': 'Redakcja', 'path': '/Fantazmaty/Redakcja', 'shared_folder_id': '42'}]
        url = reverse('admin:people_dropboxfolder_fetch')
        with patch('people.dropbox_sharing.DropboxClient') as client:
            client.return_value.list_subfolders.return_value = listing
            page = self.client.get(url)
            self.assertEqual(client.return_value.list_subfolders.call_args.args, ('/Fantazmaty',))
            self.assertContains(page, 'już na liście', count=1)
            self.assertContains(page, '>nieudostępniony<', count=2)
            self.assertContains(page, '>udostępniony<', count=1)
            self.client.post(url, {'path': ['/Fantazmaty/Redakcja', '/Fantazmaty/stary', '/Inny']})
        self.assertEqual(sorted(DropboxFolder.objects.values_list('path', 'shared_folder_id')),
                         [('/Fantazmaty/Redakcja', '42'), ('/Fantazmaty/Stary', '')])

    def test_fetch_api_error_and_polish_names(self):
        """Błąd API przy pobieraniu: komunikat zamiast listy i nic nie jest dodawane; polskie znaki w nazwie
        i ścieżce trafiają na listę bez zmian."""
        url = reverse('admin:people_dropboxfolder_fetch')
        with patch('people.dropbox_sharing.DropboxClient') as client:
            client.return_value.list_subfolders.side_effect = DropboxError('Na koncie Dropbox aplikacji nie ma takiego folderu.')
            self.assertContains(self.client.get(url), 'nie ma takiego folderu')
            self.assertEqual(self.client.post(url, {'path': ['/Fantazmaty/Stary']}).status_code, 200)
            self.assertEqual(DropboxFolder.objects.count(), 1)
            client.return_value.list_subfolders.side_effect = None
            client.return_value.list_subfolders.return_value = [
                {'name': 'Świąteczna łódź', 'path': '/Fantazmaty/Świąteczna łódź', 'shared_folder_id': ''}]
            self.assertContains(self.client.get(url), 'value="/Fantazmaty/Świąteczna łódź"')
            self.client.post(url, {'path': ['/Fantazmaty/Świąteczna łódź']})
        self.assertTrue(DropboxFolder.objects.filter(name='Świąteczna łódź', path='/Fantazmaty/Świąteczna łódź').exists())

    def test_fetch_without_configuration_shows_error(self):
        page = self.client.get(reverse('admin:people_dropboxfolder_fetch'))
        self.assertContains(page, 'Dropbox nie jest skonfigurowany')

    def test_path_is_normalized_and_unique(self):
        folder = DropboxFolder(name='Duplikat', path='fantazmaty//STARY/ ')
        with self.assertRaisesMessage(Exception, 'Ten folder jest już na liście.'):
            folder.full_clean()
        self.assertEqual(folder.path, '/fantazmaty/STARY')


class SyncCommandTests(TestCase):
    """Komenda sync_dropbox_access: plan bez zmian (--dry-run), nadanie czekających dostępów, brak konfiguracji."""

    @classmethod
    def setUpTestData(cls):
        folder = DropboxFolder.objects.create(name='Test CMS', path='/Fantazmaty/test CMS')
        person = Person.objects.create(first_name='Ola', last_name='Czeka', email='ola@example.test',
                                       dropbox_email='ola-box@example.test')
        DropboxAccess.objects.filter(pk=DropboxAccess.objects.create(person=person, folder=folder).pk).update(
            state=State.PENDING)

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command('sync_dropbox_access', *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def test_dry_run_then_real_run(self):
        fake = FakeDropbox(accounts=['ola-box@example.test'])
        with CONFIGURED, patch('people.dropbox_access.DropboxClient', return_value=fake), \
                patch('core.activity_spool.enqueue_activity') as activity:
            output = self.run_command('--dry-run')
            self.assertIn('[próba] Dropbox – Ola Czeka → Test CMS: nadanie dostępu dla ola-box@example.test (Edycja).',
                          output)
            self.assertEqual((fake.calls, DropboxAccess.objects.get().state), ([], State.PENDING))
            output = self.run_command()
        self.assertIn('Wierszy: 1, zmian: 1, błędów: 0.', output)
        self.assertEqual(DropboxAccess.objects.get().state, State.GRANTED)
        self.assertEqual(activity.call_args.kwargs['actor'], 'CMS (automatycznie)')

    def test_revocation_without_configuration_waits_for_command(self):
        """Dezaktywacja bez konfiguracji Dropboxa: wiersz „do usunięcia” z zachowanym adresem; komenda po
        konfiguracji usuwa osobę z folderu."""
        access = DropboxAccess.objects.get()
        DropboxAccess.objects.filter(pk=access.pk).update(state=State.GRANTED, granted_email='ola-box@example.test')
        DropboxFolder.objects.update(shared_folder_id='sf-test CMS')
        person = access.person
        person.is_active = False
        with self.captureOnCommitCallbacks(execute=True):
            person.save()
        access.refresh_from_db()
        self.assertEqual((access.state, access.wanted, access.granted_email),
                         (State.TO_REMOVE, False, 'ola-box@example.test'))
        fake = FakeDropbox()
        fake.members['sf-test CMS'] = {'ola-box@example.test': Member('editor')}
        with CONFIGURED, patch('people.dropbox_access.DropboxClient', return_value=fake), \
                patch('core.activity_spool.enqueue_activity'):
            self.assertIn('odebrano dostęp (ola-box@example.test)', self.run_command())
        access.refresh_from_db()
        self.assertEqual((access.state, fake.calls), (State.REMOVED, [('remove', 'sf-test CMS', 'ola-box@example.test')]))

    def test_without_configuration(self):
        with self.assertRaisesMessage(CommandError, 'Dropbox nie jest skonfigurowany'):
            self.run_command()
        output = self.run_command('--dry-run')
        self.assertIn('plan bez sprawdzenia stanu folderów', output)
        self.assertEqual(DropboxAccess.objects.get().state, State.PENDING)


class SyncEdgeCaseTests(TestCase):
    """Synchronizacja wierszy (people.dropbox_access.sync) w sytuacjach brzegowych: osoba już w folderze, zmiany
    zrobione ręcznie w Dropboxie, błąd w połowie zmiany adresu, odpowiedź Dropboxa, która nie dotarła."""

    @classmethod
    def setUpTestData(cls):
        cls.folder = DropboxFolder.objects.create(name='Test CMS', path='/Fantazmaty/test CMS', shared_folder_id='sf-1')
        cls.person = Person.objects.create(first_name='Ola', last_name='Brzeg', email='ola@example.test',
                                           dropbox_email='Ola-Box@Example.test')

    def setUp(self):
        self.fake = FakeDropbox(accounts=['ola-box@example.test', 'nowy@example.test'])
        activity = patch('core.activity_spool.enqueue_activity')
        activity.start()
        self.addCleanup(activity.stop)

    def run_sync(self):
        from people.dropbox_access import sync
        return sync(DropboxAccess.objects.all(), client=self.fake)

    def make_access(self, level='editor', **fields):
        access = DropboxAccess.objects.create(person=self.person, folder=self.folder, level=level)
        DropboxAccess.objects.filter(pk=access.pk).update(**fields)
        return access

    def test_person_already_in_folder(self):
        """Osoba już w folderze (dodana ręcznie albo zaproszona): bez ponownego dodania; inny poziom – tylko zmiana."""
        cases = [
            ('konto, ten sam poziom', Member('editor'), [], State.GRANTED),
            ('zaproszenie, inny poziom', Member('viewer', invitee=True),
             [('update', 'sf-1', 'ola-box@example.test', 'editor')], State.INVITED),
        ]
        for label, member, calls, state in cases:
            with self.subTest(label):
                DropboxAccess.objects.all().delete()
                self.fake.calls = []
                self.fake.members['sf-1'] = {'ola-box@example.test': member}
                access = self.make_access()
                self.run_sync()
                access.refresh_from_db()
                self.assertEqual(self.fake.calls, calls)
                self.assertEqual((access.state, access.granted_email), (state, 'ola-box@example.test'))

    def test_revoking_someone_already_removed_in_dropbox(self):
        """Osoba usunięta z folderu ręcznie: odebranie dostępu kończy się stanem „usunięty” bez wywołania API."""
        self.fake.members['sf-1'] = {}
        access = self.make_access(wanted=False, granted_email='ola-box@example.test', state=State.TO_REMOVE)
        [result] = self.run_sync()
        access.refresh_from_db()
        self.assertEqual((self.fake.calls, result.error), ([], ''))
        self.assertEqual((access.state, access.granted_email), (State.REMOVED, ''))

    def test_email_change_failing_halfway_is_finished_by_retry(self):
        """Zmiana adresu: błąd usunięcia starego zostawia stary adres (nowy nie jest dodawany); błąd dodania nowego
        po usunięciu starego zostawia nowy adres do ponowienia. Ponowna synchronizacja kończy zmianę."""
        Person.objects.filter(pk=self.person.pk).update(dropbox_email='nowy@example.test')
        cases = [('remove', 'stary@example.test', [('remove', 'sf-1', 'stary@example.test')]),
                 ('add', 'nowy@example.test', [('remove', 'sf-1', 'stary@example.test'),
                                               ('add', 'sf-1', 'nowy@example.test', 'editor')])]
        for failing, kept, calls_after_failure in cases:
            with self.subTest(failing=failing):
                DropboxAccess.objects.all().delete()
                self.fake.calls = []
                self.fake.members['sf-1'] = {'stary@example.test': Member('editor')}
                self.fake.failures = {failing: 'Dropbox jest chwilowo niedostępny.'}
                access = self.make_access(granted_email='stary@example.test', state=State.GRANTED)
                self.run_sync()
                access.refresh_from_db()
                self.assertEqual((access.state, access.granted_email), (State.ERROR, kept))
                self.assertEqual(self.fake.calls, [call for call in calls_after_failure if call[0] != failing])
                self.fake.failures = {}
                self.run_sync()
                access.refresh_from_db()
                self.assertEqual((access.state, access.granted_email), (State.GRANTED, 'nowy@example.test'))
                self.assertEqual(set(self.fake.members['sf-1']), {'nowy@example.test'})

    def test_lost_answer_after_adding_still_allows_revoking(self):
        """Dropbox dodał osobę, ale odpowiedź nie dotarła: adres zostaje zapamiętany, więc odebranie dostępu
        (np. dezaktywacja) usuwa osobę z folderu."""
        self.fake.members['sf-1'] = {}
        self.fake.failures['add_after'] = 'Brak połączenia z Dropboxem (timed out). Spróbuj ponownie później.'
        access = self.make_access()
        self.run_sync()
        access.refresh_from_db()
        self.assertEqual((access.state, access.granted_email), (State.ERROR, 'ola-box@example.test'))
        self.fake.failures = {}
        DropboxAccess.objects.filter(pk=access.pk).update(wanted=False)
        self.run_sync()
        access.refresh_from_db()
        self.assertIn(('remove', 'sf-1', 'ola-box@example.test'), self.fake.calls)
        self.assertEqual((access.state, access.granted_email), (State.REMOVED, ''))
        self.assertEqual(self.fake.members['sf-1'], {})


class DropboxImportTests(TestCase):
    """Przejęcie obecnych członków folderu z Dropboxa (people.dropbox_import, admin folderów, komenda): podgląd bez
    zapisu, dopasowanie osób, zastosowanie bez zaproszeń i zmian w Dropboxie, skrót planu i brak konfiguracji."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('import-admin')
        cls.folder = DropboxFolder.objects.create(name='Redakcja', path='/Fantazmaty/Redakcja',
                                                  shared_folder_id='sf-Redakcja')

        def person(name, dropbox_email='', **fields):
            fields.setdefault('email', f'{name}@example.test')
            return Person.objects.create(first_name=name, last_name='Import', dropbox_email=dropbox_email, **fields)

        cls.ola = person('ola', 'Ola-Box@Example.test')
        cls.ewa = person('ewa', 'ewa-box@example.test')
        cls.nina = person('nina', 'nina-box@example.test')
        cls.jan = person('jan')
        cls.kasia = create_member('kasia', 'Redaktor').person_profile
        cls.kasia.user.email = 'kasia-konto@example.test'
        cls.kasia.user.save()
        cls.piotr = person('piotr', 'piotr-box@example.test')
        cls.marek = person('marek', 'marek-box@example.test')
        cls.adam = person('adam', 'adam-box@example.test', is_active=False)
        cls.wspolny = [person(f'wspolny{index}', 'wspolny@example.test') for index in (1, 2)]
        # bulk_create bez sygnałów: partia synchronizacji z setUpTestData nie przejmowałaby wierszy z testów.
        DropboxAccess.objects.bulk_create([
            DropboxAccess(person=who, folder=cls.folder, level=level, state=State.GRANTED, granted_email=who.dropbox_email)
            for who, level in ((cls.piotr, 'editor'), (cls.marek, 'viewer'))])

    def setUp(self):
        self.client.force_login(self.admin)
        self.fake = FakeDropbox()
        self.fake.members['sf-Redakcja'] = {
            'owner@example.test': Member('owner'), 'ola-box@example.test': Member('editor'),
            'ewa-box@example.test': Member('viewer', invitee=True), 'nina-box@example.test': Member('viewer_no_comment'),
            'jan@example.test': Member('viewer'), 'kasia-konto@example.test': Member('editor'),
            'piotr-box@example.test': Member('viewer'), 'marek-box@example.test': Member('viewer'),
            'adam-box@example.test': Member('editor'), 'wspolny@example.test': Member('editor'),
            'obcy@example.test': Member('editor')}
        for target in ('people.dropbox_import.DropboxClient', 'people.dropbox_access.DropboxClient'):
            patcher = patch(target, return_value=self.fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        activity = patch('core.activity_spool.enqueue_activity')
        self.activity = activity.start()
        self.addCleanup(activity.stop)
        self.url = reverse('admin:people_dropboxfolder_import') + f'?ids={self.folder.pk}'

    def preview(self):
        with CONFIGURED:
            return self.client.get(self.url)

    def apply(self, token=None, **choices):
        """„Zastosuj” z tokenem podglądu (domyślnie świeżego); zwraca (odpowiedź, komunikaty)."""
        token = token or self.preview().context['token']
        data = {'ids': str(self.folder.pk), 'token': token, **choices}
        with CONFIGURED, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:people_dropboxfolder_import'), data)
        return response, [str(message) for message in response.wsgi_request._messages]

    def key(self, email):
        return f'{self.folder.pk}:{email}'

    def rows(self):
        return {access.person.first_name: (access.state, access.level, access.granted_email, access.wanted)
                for access in DropboxAccess.objects.select_related('person')}

    def test_preview_groups_members_without_changes(self):
        """Podgląd: grupy według dopasowania (adres Dropbox bez wielkości liter, inny adres osoby – kontaktowy i konta,
        niejednoznaczny, spoza bazy, osoba spoza ekipy, różnica poziomu); właściciel pominięty; nic nie jest zapisywane."""
        before = self.rows()
        page = self.preview()
        [plan] = page.context['plans']
        groups = {entry.email: entry.group for entry in plan.entries}
        cases = [('ola-box@example.test', 'import'), ('nina-box@example.test', 'import'),
                 ('ewa-box@example.test', 'invited'), ('jan@example.test', 'other_email'),
                 ('kasia-konto@example.test', 'other_email'), ('piotr-box@example.test', 'level'),
                 ('marek-box@example.test', 'managed'), ('adam-box@example.test', 'inactive'),
                 ('wspolny@example.test', 'problems'), ('obcy@example.test', 'outside')]
        for email, group in cases:
            with self.subTest(email):
                self.assertEqual(groups.pop(email), group)
        self.assertEqual(groups, {})
        self.assertEqual(plan.owners, ['owner@example.test'])
        self.assertContains(page, 'W Dropboxie Odczyt, w CMS-ie Edycja.')
        self.assertContains(page, 'Adres pasuje do kilku osób: wspolny1 Import, wspolny2 Import.')
        self.assertContains(page, 'name="set_email"', count=2)
        self.assertContains(page, 'name="revoke"', count=1)
        self.assertNotContains(page, 'checked')
        self.assertEqual((self.rows(), self.fake.calls), (before, []))

    def test_apply_takes_over_without_calls_to_dropbox(self):
        """„Zastosuj” bez zaznaczeń: wiersze z poziomem i adresem z Dropboxa (zaproszenie – „zaproszenie wysłane”),
        poziom z Dropboxa przy różnicy; dopasowania po innym adresie, osoby spoza ekipy i spoza bazy bez zmian;
        żadnego wywołania add/update/remove – także przy późniejszej synchronizacji („Odczyt bez komentarzy” = Odczyt)."""
        response, messages = self.apply()
        self.assertRedirects(response, reverse('admin:people_dropboxfolder_changelist'))
        self.assertIn('Przejęto 4 dostępy w folderze Redakcja.', messages)
        self.assertEqual(self.rows(), {
            'ola': (State.GRANTED, 'editor', 'ola-box@example.test', True),
            'ewa': (State.INVITED, 'viewer', 'ewa-box@example.test', True),
            'nina': (State.GRANTED, 'viewer', 'nina-box@example.test', True),
            'piotr': (State.GRANTED, 'viewer', 'piotr-box@example.test', True),
            'marek': (State.GRANTED, 'viewer', 'marek-box@example.test', True)})
        self.jan.refresh_from_db()
        self.assertEqual(self.jan.dropbox_email, '')
        actions = sorted(call.kwargs['action'] for call in self.activity.call_args_list
                         if call.kwargs['action'].startswith('Dropbox:'))
        self.assertEqual(actions, ['Dropbox: przejęcie dostępu z Dropboxa (Edycja)']
                         + ['Dropbox: przejęcie dostępu z Dropboxa (Odczyt)'] * 3)
        from people.dropbox_access import sync
        with CONFIGURED:
            results = sync(DropboxAccess.objects.all())
        self.assertEqual([result.action for result in results if result.action or result.error], [])
        self.assertEqual(self.fake.calls, [])

    def test_checked_options_set_dropbox_email_or_revoke(self):
        """Zaznaczone „ustaw jako e-mail Dropbox” – adres trafia do osoby i dostęp jest przejęty (bez zaproszenia);
        zaznaczone „odbierz dostęp” – osoba spoza ekipy jest usuwana z folderu istniejącym mechanizmem."""
        cases = [
            ('set_email', 'jan@example.test', self.jan,
             lambda: self.assertEqual(Person.objects.get(pk=self.jan.pk).dropbox_email, 'jan@example.test'),
             (State.GRANTED, 'viewer', 'jan@example.test', True), []),
            ('revoke', 'adam-box@example.test', self.adam, lambda: None,
             (State.REMOVED, 'editor', '', False), [('remove', 'sf-Redakcja', 'adam-box@example.test')]),
        ]
        for field_name, email, who, check, row, calls in cases:
            with self.subTest(field_name):
                DropboxAccess.objects.filter(person__in=[self.ola, self.ewa, self.nina]).delete()
                self.fake.calls = []
                response, messages = self.apply(**{field_name: [self.key(email)]})
                self.assertEqual(response.status_code, 302)
                check()
                self.assertEqual(self.rows()[who.first_name], row)
                self.assertEqual(self.fake.calls, calls)
        self.assertIn('Dropbox – adam Import → Redakcja: odebrano dostęp (adam-box@example.test).', messages)
        self.assertNotIn('kasia', self.rows())

    def test_changed_state_requires_new_preview(self):
        """Zmiana w Dropboxie albo w CMS-ie między podglądem a „Zastosuj” (albo zły token): nic nie jest zapisywane,
        komunikat i nowy podgląd."""
        def new_member():
            self.fake.members['sf-Redakcja']['nowy@example.test'] = Member('editor')

        def person_changed():
            Person.objects.filter(pk=self.jan.pk).update(dropbox_email='jan@example.test')

        cases = [('nowy członek w Dropboxie', new_member, None), ('zmiana osoby w CMS-ie', person_changed, None),
                 ('zły token', lambda: None, 'zly-token')]
        for label, change, token in cases:
            with self.subTest(label):
                token = token or self.preview().context['token']
                change()
                before = self.rows()
                response, messages = self.apply(token)
                self.assertEqual(response.status_code, 200)
                self.assertIn('zmienił się od podglądu', ' '.join(messages))
                self.assertEqual(self.rows(), before)
                self.assertNotEqual(response.context['token'], token)

    def test_errors_and_unshared_folder(self):
        """Brak konfiguracji i błąd API – komunikat zamiast 500, bez zapisu; folder nieudostępniony w Dropboxie –
        „brak członków”, bez udostępniania."""
        unshared = DropboxFolder.objects.create(name='Nowy', path='/Fantazmaty/Nowy')
        response = self.client.get(self.url)
        self.assertContains(response, 'Dropbox nie jest skonfigurowany')
        self.assertNotContains(response, 'name="apply"')
        self.fake.failures['members'] = 'Dropbox jest chwilowo niedostępny.'
        self.assertContains(self.preview(), 'Dropbox jest chwilowo niedostępny.')
        response, messages = self.apply()
        self.assertIn('Folder Redakcja: Dropbox jest chwilowo niedostępny.', messages)
        self.assertEqual(DropboxAccess.objects.count(), 2)
        url = reverse('admin:people_dropboxfolder_import') + f'?ids={unshared.pk}'
        with CONFIGURED:
            self.assertContains(self.client.get(url), 'Folder nie jest udostępniony w Dropboxie – brak członków')
        self.assertEqual(self.fake.calls, [])
        self.fake.shared['/Fantazmaty/Nowy'] = 'sf-Nowy'
        del self.fake.failures['members']
        with CONFIGURED:
            self.client.post(reverse('admin:people_dropboxfolder_import'),
                             {'ids': str(unshared.pk), 'token': self.client.get(url).context['token']})
        unshared.refresh_from_db()
        self.assertEqual(unshared.shared_folder_id, 'sf-Nowy')

    def test_superuser_only(self):
        """Podgląd, zastosowanie, akcja i przycisk na stronie folderu – tylko superuser."""
        change_page = self.client.get(reverse('admin:people_dropboxfolder_change', args=[self.folder.pk]))
        self.assertContains(change_page, self.url)
        changelist = self.client.post(reverse('admin:people_dropboxfolder_changelist'),
                                      {'action': 'import_members', '_selected_action': [self.folder.pk], 'index': '0'})
        self.assertRedirects(changelist, self.url, fetch_redirect_response=False)
        staff = create_member('import-staff', 'Redaktor')
        staff.is_staff = True
        staff.save()
        from django.contrib.auth.models import Permission
        staff.user_permissions.add(*Permission.objects.filter(codename__endswith='dropboxfolder'))
        for user in (staff, create_member('import-coordinator', 'Koordynator redakcji')):
            self.client.force_login(user)
            for method in ('get', 'post'):
                with self.subTest(user=user.username, method=method), CONFIGURED:
                    response = getattr(self.client, method)(self.url, {'ids': str(self.folder.pk)} if method == 'post' else None)
                    self.assertIn(response.status_code, (302, 403))
                    self.assertNotEqual(response.get('Location', ''), reverse('admin:people_dropboxfolder_changelist'))
        self.assertEqual(DropboxAccess.objects.count(), 2)

    def test_command_imports_only_direct_matches(self):
        """sync_dropbox_access --import-members: --dry-run tylko wypisuje; bez niego przejmuje dopasowania po adresie
        Dropbox (bez dopasowań po innym adresie i osób spoza ekipy); bez konfiguracji – błąd."""
        with self.assertRaisesMessage(CommandError, 'Dropbox nie jest skonfigurowany'):
            call_command('sync_dropbox_access', '--import-members', stdout=StringIO())
        out = StringIO()
        with CONFIGURED:
            call_command('sync_dropbox_access', '--import-members', '--dry-run', stdout=out)
        self.assertIn('[próba] Do przejęcia: 4 dostępy w folderze Redakcja.', out.getvalue())
        self.assertIn('[próba] Redakcja: Osoby spoza ekipy z dostępem – adam-box@example.test (adam Import)',
                      out.getvalue())
        self.assertEqual(DropboxAccess.objects.count(), 2)
        out = StringIO()
        with CONFIGURED:
            call_command('sync_dropbox_access', '--import-members', stdout=out)
        self.assertIn('Przejęto 4 dostępy w folderze Redakcja.', out.getvalue())
        self.assertEqual(set(self.rows()), {'ola', 'ewa', 'nina', 'piotr', 'marek'})
        self.assertEqual(self.fake.calls, [])


class DropboxFolderRemovalTests(TestCase):
    """Usuwanie folderów z listy (people.dropbox_removal, admin folderów): stan w Dropboxie, wybór „przestań zarządzać”
    albo „odbierz dostęp”, skrót stanu, folder usunięty w Dropboxie przy synchronizacji i na liście „Pobierz foldery”."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('remove-admin')
        cls.ola = Person.objects.create(first_name='Ola', last_name='Usuwa', email='ola@example.test',
                                        dropbox_email='ola-box@example.test')

    def setUp(self):
        self.client.force_login(self.admin)
        self.fake = FakeDropbox()
        for target in ('people.dropbox_removal.DropboxClient', 'people.dropbox_access.DropboxClient'):
            patcher = patch(target, return_value=self.fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        activity = patch('core.activity_spool.enqueue_activity')
        self.activity = activity.start()
        self.addCleanup(activity.stop)

    def make_folder(self, kind, state=State.GRANTED, wanted=True):
        """Folder z dostępem Oli: gone – usunięty w Dropboxie, live – udostępniony (Ola i osoba dodana ręcznie),
        unshared – istnieje bez udostępnienia (wiersz czeka na nadanie)."""
        path = f'/Fantazmaty/{kind}'
        shared = '' if kind == 'unshared' else f'sf-{kind}'
        folder = DropboxFolder.objects.create(name=kind, path=path, shared_folder_id=shared)
        granted = 'ola-box@example.test' if shared else ''
        DropboxAccess.objects.bulk_create([DropboxAccess(person=self.ola, folder=folder, wanted=wanted,
                                                         state=state if shared else State.PENDING, granted_email=granted)])
        if kind == 'gone':
            self.fake.deleted |= {path, shared}
        elif shared:
            self.fake.locations[shared] = path
            self.fake.members[shared] = {'owner@example.test': Member('owner'), granted: Member('editor'),
                                         'reczny@example.test': Member('viewer')}
        return folder

    def url(self, *folders):
        return reverse('admin:people_dropboxfolder_remove') + '?ids=' + ','.join(str(f.pk) for f in folders)

    def check(self, *folders):
        with CONFIGURED:
            return self.client.get(self.url(*folders))

    def remove(self, folders, token=None, configured=True, modes=None):
        """Potwierdzenie usunięcia z tokenem świeżego sprawdzenia; modes: {folder: tryb}."""
        settings = CONFIGURED if configured else override_settings()
        with settings:
            token = token or self.client.get(self.url(*folders)).context['token']
            data = {'ids': ','.join(str(f.pk) for f in folders), 'token': token,
                    **{f'mode_{folder.pk}': mode for folder, mode in (modes or {}).items()}}
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse('admin:people_dropboxfolder_remove'), data)
        return response, [str(message) for message in response.wsgi_request._messages]

    def test_check_shows_state_without_changes(self):
        """Sprawdzenie: usunięty w Dropboxie, udostępniony, przeniesiony, nieudostępniony, błąd API i brak konfiguracji;
        nic nie jest zmieniane."""
        gone, live, unshared = self.make_folder('gone'), self.make_folder('live'), self.make_folder('unshared')
        moved = self.make_folder('moved')
        self.fake.locations['sf-moved'] = '/Archiwum/moved'
        failing = self.make_folder('failing')
        self.fake.locations.pop('sf-failing')
        page = self.check(gone, live, unshared, moved)
        states = {check.folder.name: (check.state, check.rows, check.granted) for check in page.context['checks']}
        self.assertEqual(states, {'gone': ('gone', 1, 1), 'live': ('exists', 1, 1), 'unshared': ('unshared', 1, 0),
                                  'moved': ('exists', 1, 1)})
        self.assertContains(page, 'Folder jest teraz w Dropboxie pod ścieżką /Archiwum/moved.')
        self.assertContains(page, 'name="mode_%d" value="revoke"' % live.pk)
        self.assertNotContains(page, 'name="mode_%d"' % gone.pk)
        cases = [('błąd API', CONFIGURED, 'Dropbox jest chwilowo niedostępny.'),
                 ('brak konfiguracji', override_settings(), 'Dropbox nie jest skonfigurowany')]
        self.fake.failures['metadata'] = 'Dropbox jest chwilowo niedostępny.'
        for label, settings, note in cases:
            with self.subTest(label), settings:
                page = self.client.get(self.url(failing))
                [check] = page.context['checks']
                self.assertEqual(check.state, 'unknown')
                self.assertIn(note, check.note)
                self.assertContains(page, 'type="checkbox" name="mode_%d" value="stop"' % failing.pk)
                self.assertNotContains(page, 'value="revoke"')
        self.assertEqual((DropboxFolder.objects.count(), DropboxAccess.objects.count(), self.fake.calls), (5, 5, []))

    def test_remove_by_state_and_choice(self):
        """Usunięcie według stanu: usunięty w Dropboxie i nieudostępniony – bez API; istniejący – „przestań zarządzać”
        (członkowie zostają) albo „odbierz dostęp” (folder znika dopiero po udanym odebraniu, błąd zostawia folder);
        bez wyboru i stan nieznany bez potwierdzenia – folder zostaje."""
        remove_call = ('remove', 'sf-live', 'ola-box@example.test')
        cases = [
            # (rodzaj, tryb, konfiguracja, awaria, usunięty, wywołania, komunikat)
            ('gone', None, True, None, True, [], 'Usunięto folder gone z listy (usunięty w dropboxie).'),
            ('unshared', None, True, None, True, [], 'Usunięto folder unshared z listy'),
            ('live', 'stop', True, None, True, [], 'Usunięto folder live z listy'),
            ('live', None, True, None, False, [], 'nie wybrano, co zrobić z dostępem'),
            ('live', 'revoke', True, None, True, [remove_call], 'Usunięto folder live z listy'),
            ('live', 'revoke', True, 'remove', False, [], 'nie udało się odebrać dostępu wszystkim osobom'),
            ('live', 'stop', False, None, True, [], 'Usunięto folder live z listy (stan nieznany).'),
            ('live', 'revoke', False, None, False, [], 'nie wybrano, co zrobić z dostępem'),
        ]
        for kind, mode, configured, failure, removed, calls, message in cases:
            with self.subTest(kind=kind, mode=mode, configured=configured, failure=failure):
                DropboxAccess.objects.all().delete()
                DropboxFolder.objects.all().delete()
                self.fake.calls, self.fake.failures = [], ({failure: 'Dropbox jest chwilowo niedostępny.'} if failure else {})
                self.activity.reset_mock()
                folder = self.make_folder(kind)
                response, messages = self.remove([folder], configured=configured, modes={folder: mode} if mode else None)
                self.assertRedirects(response, reverse('admin:people_dropboxfolder_changelist'))
                self.assertIn(message, ' '.join(messages))
                self.assertEqual(DropboxFolder.objects.filter(pk=folder.pk).exists(), not removed)
                self.assertEqual(DropboxAccess.objects.filter(folder_id=folder.pk).exists(), not removed)
                self.assertEqual(self.fake.calls, calls)
                logged = [call.kwargs['action'] for call in self.activity.call_args_list
                          if 'usunięcie folderu' in call.kwargs['action']]
                self.assertEqual(len(logged), int(removed))
                if kind == 'live' and mode == 'stop':
                    self.assertIn('ola-box@example.test', self.fake.members['sf-live'])
                if failure:
                    access = DropboxAccess.objects.get(folder=folder)
                    self.assertEqual((access.state, access.wanted, access.granted_email),
                                     (State.ERROR, False, 'ola-box@example.test'))
                    self.assertIn('Dropbox – Ola Usuwa → live: Dropbox jest chwilowo niedostępny.', messages)
        self.assertIn('reczny@example.test', self.fake.members['sf-live'])

    def test_changed_state_requires_new_check(self):
        """Folder usunięty w Dropboxie po sprawdzeniu (inny stan niż w tokenie) – nic nie jest usuwane, komunikat."""
        folder = self.make_folder('live')
        token = self.check(folder).context['token']
        self.fake.deleted.add('sf-live')
        response, messages = self.remove([folder], token=token, modes={folder: 'stop'})
        self.assertIn('Stan folderów zmienił się od sprawdzenia', ' '.join(messages))
        self.assertTrue(DropboxFolder.objects.filter(pk=folder.pk).exists())

    def test_superuser_only_and_builtin_delete_disabled(self):
        """Usuwanie tylko przez „Usuń z listy” (akcja i przycisk na stronie folderu) i tylko dla superusera;
        zwykłe usuwanie (strona usunięcia, akcja delete_selected) jest wyłączone dla każdego folderu."""
        folder, empty = self.make_folder('gone'), DropboxFolder.objects.create(name='Pusty', path='/Fantazmaty/Pusty')
        self.assertContains(self.client.get(reverse('admin:people_dropboxfolder_change', args=[folder.pk])), self.url(folder))
        for target in (folder, empty):
            with self.subTest(target.name):
                self.assertEqual(self.client.get(reverse('admin:people_dropboxfolder_delete', args=[target.pk])).status_code, 403)
        changelist = reverse('admin:people_dropboxfolder_changelist')
        actions = self.client.get(changelist).context['action_form'].fields['action'].choices
        self.assertNotIn('delete_selected', dict(actions))
        response = self.client.post(changelist, {'action': 'remove_from_list', '_selected_action': [folder.pk], 'index': '0'})
        self.assertRedirects(response, self.url(folder), fetch_redirect_response=False)
        staff = create_member('remove-staff', 'Redaktor')
        staff.is_staff = True
        staff.save()
        from django.contrib.auth.models import Permission
        staff.user_permissions.add(*Permission.objects.filter(codename__endswith='dropboxfolder'))
        self.client.force_login(staff)
        for method in ('get', 'post'):
            with self.subTest(method=method), CONFIGURED:
                response = getattr(self.client, method)(self.url(folder))
                self.assertIn(response.status_code, (302, 403))
                self.assertNotEqual(response.get('Location', ''), reverse('admin:people_dropboxfolder_changelist'))
        self.assertEqual(DropboxFolder.objects.count(), 2)

    def test_sync_with_folder_deleted_in_dropbox(self):
        """Folder usunięty w Dropboxie nie blokuje synchronizacji: odebranie (dezaktywacja) kończy się stanem
        „usunięty” bez wywołań, nadanie – czytelnym błędem, a drugi folder synchronizuje się normalnie."""
        from people.dropbox_access import FOLDER_GONE, sync
        from people.dropbox_sharing import is_gone
        gone = self.make_folder('gone')
        self.make_folder('live')
        other = Person.objects.create(first_name='Ewa', last_name='Nowa', email='ewa@example.test',
                                      dropbox_email='ewa-box@example.test')
        DropboxAccess.objects.bulk_create([DropboxAccess(person=other, folder=gone, state=State.PENDING)])
        self.ola.is_active = False
        with CONFIGURED, self.captureOnCommitCallbacks(execute=True):
            self.ola.save()
        rows = {(a.person.first_name, a.folder.name): (a.state, a.granted_email, a.last_error)
                for a in DropboxAccess.objects.select_related('person', 'folder')}
        self.assertEqual(rows[('Ola', 'gone')], (State.REMOVED, '', ''))
        self.assertEqual(rows[('Ola', 'live')], (State.REMOVED, '', ''))
        self.assertEqual(self.fake.calls, [('remove', 'sf-live', 'ola-box@example.test')])
        sync(DropboxAccess.objects.filter(person=other), client=self.fake)
        access = DropboxAccess.objects.get(person=other)
        self.assertEqual((access.state, access.last_error), (State.ERROR, FOLDER_GONE))
        for summary, expected in (('path/not_found/..', True), ('access_error/invalid_id/..', True),
                                  ('access_error/unmounted/.', True), ('member_error/not_a_member/..', False),
                                  ('add_folder_member/bad_member/invalid_email/..', False)):
            with self.subTest(summary):
                self.assertEqual(is_gone(DropboxError('x', summary)), expected)

    def test_fetch_marks_folders_missing_in_dropbox(self):
        """„Pobierz foldery z Dropboxa”: foldery z listy CMS-u, których nie ma w folderze głównym – „usunięty
        w Dropboxie” (albo nowa ścieżka po przeniesieniu) z odnośnikiem do usunięcia; foldery spoza folderu głównego
        nie są oznaczane."""
        gone = self.make_folder('gone')
        self.make_folder('live')
        DropboxFolder.objects.create(name='Poza', path='/Inny/Poza')
        listing = [{'name': 'nowa nazwa', 'path': '/Fantazmaty/nowa nazwa', 'shared_folder_id': 'sf-live'}]
        with patch('people.dropbox_sharing.DropboxClient') as client:
            client.return_value.list_subfolders.return_value = listing
            page = self.client.get(reverse('admin:people_dropboxfolder_fetch'))
        self.assertEqual([(folder.name, moved) for folder, moved in page.context['missing']],
                         [('gone', ''), ('live', '/Fantazmaty/nowa nazwa')])
        self.assertContains(page, 'usunięty w Dropboxie')
        self.assertContains(page, self.url(gone))
