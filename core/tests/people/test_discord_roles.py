"""Rangi Discorda według ról w CMS-ie: klient REST API (HTTP zawsze podstawione), podgląd i plan dla trybów,
zatwierdzenie pełnej synchronizacji, synchronizacja po zmianach osób (po commit), komenda okresowa, historia,
dopasowanie kont po imieniu i nazwisku, identyfikator w profilu osoby i uprawnienia stron panelu."""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse

from core.notification_models import DiscordRole, DiscordRoleChange
from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_member, create_superuser
from people import discord_api, discord_matching, discord_sync
from people.discord_api import DiscordClient, DiscordError
from people.models import Person, Role

User = get_user_model()
GUILD = '900000000000000000'
CONFIGURED = override_settings(DISCORD_BOT_TOKEN='bot-token', DISCORD_GUILD_ID=GUILD)
Mode = DiscordRole.SyncMode
Result = DiscordRoleChange.Result

EDITOR_RANK = '500000000000000001'
PROOF_RANK = '500000000000000002'
OTHER_RANK = '500000000000000099'  # ranga spoza mapowania – CMS jej nie dotyka


def uid(number):
    return str(100000000000000000 + number)


def member(number, *, nick=None, global_name=None, username=None, roles=(), bot=False):
    user = {'id': uid(number), 'username': username or f'user{number}', 'global_name': global_name}
    if bot:
        user['bot'] = True
    return {'user': user, 'nick': nick, 'roles': list(roles)}


class FakeDiscord:
    """Podstawione discord_api._send: serwer z członkami i rangami, zapis zapytań i wymuszone odpowiedzi."""

    def __init__(self, members=(), roles=None):
        self.members = {m['user']['id']: m for m in members}
        self.roles = roles if roles is not None else {
            EDITOR_RANK: 'Redakcja', PROOF_RANK: 'Korekta', OTHER_RANK: 'Inna'}
        self.calls = []
        self.forced = []  # (metoda, fragment ścieżki, status, treść) – zużywane raz

    def force(self, method, fragment, status, body=None):
        self.forced.append((method, fragment, status, body))

    def changes(self):
        return [(method, path.rsplit('/members/', 1)[1]) for method, path, _, _ in self.calls
                if method in ('PUT', 'DELETE')]

    def __call__(self, method, url, token, headers):
        parsed = urllib.parse.urlsplit(url)
        path, query = parsed.path.removeprefix('/api/v10'), dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((method, path, query, headers))
        for index, (forced_method, fragment, status, body) in enumerate(self.forced):
            if forced_method == method and fragment in path:
                del self.forced[index]
                return status, json.dumps(body).encode() if body is not None else b'', {}
        parts = path.strip('/').split('/')
        assert parts[:2] == ['guilds', GUILD], path
        if parts[2:] == ['roles']:
            return self.json([{'id': rid, 'name': name, 'position': 1} for rid, name in self.roles.items()])
        if parts[2:] == ['members']:
            after, limit = int(query['after']), int(query['limit'])
            page = [m for key, m in sorted(self.members.items(), key=lambda item: int(item[0])) if int(key) > after]
            return self.json(page[:limit])
        user_id = parts[3]
        found = self.members.get(user_id)
        if found is None:
            return 404, json.dumps({'message': 'Unknown Member', 'code': 10007}).encode(), {}
        if len(parts) == 4:
            return self.json(found)
        role_id = parts[5]
        if method == 'PUT' and role_id not in found['roles']:
            found['roles'].append(role_id)
        if method == 'DELETE' and role_id in found['roles']:
            found['roles'].remove(role_id)
        return 204, b'', {}

    @staticmethod
    def json(data):
        return 200, json.dumps(data).encode(), {}


class FakeDiscordMixin:
    """Bot skonfigurowany od setUp (dane z setUpTestData powstają bez bota) i podstawione HTTP."""

    def install(self, fake):
        self.fake = fake
        patcher = patch('people.discord_api._send', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch('people.discord_api.time.sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        return fake

    def setUp(self):
        super().setUp()
        self.enterContext(CONFIGURED)
        connection.__dict__.pop(discord_sync._PENDING_ATTR, None)


def make_person(first, last, *, roles=(), discord=None, active=True, external=False, user=False):
    account = User.objects.create_user(username=f'{first}.{last}@example.test'.lower()) if user else None
    person = Person.objects.create(first_name=first, last_name=last, discord_id=discord, is_active=active,
                                   is_external=external, user=account)
    person.roles.set(roles)
    return person


# Klient API

@CONFIGURED
@tag('external')
class DiscordClientTests(SimpleTestCase):
    def setUp(self):
        sleeper = patch('people.discord_api.time.sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def respond(self, *responses):
        """_send zwracający kolejne odpowiedzi (status, treść); zapytania w self.requests."""
        self.requests = []
        queue = list(responses)

        def send(method, url, token, headers):
            self.requests.append((method, url, token, headers))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            status, body = item
            return status, (json.dumps(body).encode() if body is not None else b''), {}
        patcher = patch('people.discord_api._send', send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_role_change_sends_bot_token_and_encoded_audit_reason(self):
        """PUT/DELETE rangi: ścieżka API v10, token bota, powód w X-Audit-Log-Reason zakodowany URL; 204 to sukces."""
        self.respond((204, None), (204, None))
        client = DiscordClient()
        client.add_role(uid(1), EDITOR_RANK, 'CMS Fantazmaty: zmiana ról osoby (Łucja Żak)')
        client.remove_role(uid(1), EDITOR_RANK, 'powód')
        (method, url, token, headers), (method2, url2, _, _) = self.requests
        self.assertEqual((method, method2), ('PUT', 'DELETE'))
        self.assertEqual(url, f'https://discord.com/api/v10/guilds/{GUILD}/members/{uid(1)}/roles/{EDITOR_RANK}')
        self.assertEqual(url2, url)
        self.assertEqual(token, 'bot-token')
        reason = headers['X-Audit-Log-Reason']
        self.assertNotIn(' ', reason)
        self.assertEqual(urllib.parse.unquote(reason), 'CMS Fantazmaty: zmiana ról osoby (Łucja Żak)')

    def test_send_uses_bot_authorization_and_user_agent(self):
        """Prawdziwe _send: nagłówki Authorization „Bot …” i User-Agent wymagany przez Discorda; błąd HTTP to odpowiedź."""
        captured = {}

        class Response:
            status = 204
            headers = {}

            def read(self):
                return b''

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def urlopen(request, timeout):
            captured.update(request.headers, method=request.get_method(), timeout=timeout)
            return Response()
        with patch('people.discord_api.urllib.request.urlopen', urlopen):
            status, body, _ = discord_api._send('PUT', 'https://discord.com/api/v10/x', 'tok', {})
        self.assertEqual((status, body, captured['method']), (204, b'', 'PUT'))
        self.assertEqual(captured['Authorization'], 'Bot tok')
        self.assertTrue(captured['User-agent'].startswith('DiscordBot ('))

    def test_network_errors_become_polish_messages(self):
        """Brak połączenia, przekroczony czas i błąd gniazda – DiscordError z prośbą o ponowienie."""
        for case, error in {'url': urllib.error.URLError('dns'), 'timeout': TimeoutError('t'),
                            'os': OSError('reset')}.items():
            with self.subTest(case), patch('people.discord_api.urllib.request.urlopen', side_effect=error):
                with self.assertRaisesMessage(DiscordError, 'Brak połączenia z Discordem'):
                    discord_api._send('GET', 'https://discord.com/api/v10/x', 'tok', {})

    def test_members_are_paged_by_last_id(self):
        """Lista członków: limit i „after” z ostatniego identyfikatora strony, koniec przy niepełnej stronie."""
        pages = [[member(1), member(2)], [member(3), member(4)], [member(5)]]
        self.respond(*[(200, page) for page in pages])
        with patch('people.discord_api.PAGE_SIZE', 2):
            members = DiscordClient().members()
        self.assertEqual([m['user']['id'] for m in members], [uid(n) for n in range(1, 6)])
        afters = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for _, url, _, _ in self.requests]
        self.assertEqual([(q['limit'], q['after']) for q in afters], [(['2'], ['0']), (['2'], [uid(2)]),
                                                                      (['2'], [uid(4)])])

    def test_error_messages_by_status_and_context(self):
        """Czytelne komunikaty: 403 przy randze (hierarchia), 403 przy liście członków (Server Members Intent),
        404 osoby, rangi i serwera, 401, 5xx i inne; członek spoza serwera przy odczycie to None."""
        cases = {
            'hierarchia': (lambda c: c.add_role(uid(1), EDITOR_RANK, 'r'), 403, {'code': 50013},
                           'ranga musi być na liście rang serwera wyżej'),
            'intent': (lambda c: c.members(), 403, {'code': 50001}, 'Server Members Intent'),
            'osoba poza serwerem': (lambda c: c.add_role(uid(1), EDITOR_RANK, 'r'), 404, {'code': 10007},
                                    'Tej osoby nie ma na serwerze'),
            'nieznana ranga': (lambda c: c.remove_role(uid(1), EDITOR_RANK, 'r'), 404, {'code': 10011},
                               'nie ma rangi o tym identyfikatorze'),
            'nieznany serwer': (lambda c: c.roles(), 404, {'code': 10004}, 'DISCORD_GUILD_ID'),
            'token': (lambda c: c.roles(), 401, {'code': 0, 'message': '401: Unauthorized'}, 'DISCORD_BOT_TOKEN'),
            'awaria': (lambda c: c.roles(), 502, None, 'chwilowo niedostępny (HTTP 502)'),
            'inne': (lambda c: c.roles(), 400, {'message': 'Invalid Form Body', 'code': 50035},
                     'odrzucił zapytanie (HTTP 400): Invalid Form Body'),
        }
        for case, (call, status, body, message) in cases.items():
            with self.subTest(case):
                self.respond((status, body))
                with self.assertRaisesMessage(DiscordError, message) as raised:
                    call(DiscordClient())
                self.assertEqual(raised.exception.status, status)
        self.respond((404, {'code': 10007, 'message': 'Unknown Member'}))
        self.assertIsNone(DiscordClient().member(uid(1)))

    def test_rate_limit_waits_retry_after_then_gives_up(self):
        """429: czekanie retry_after i ponowienie; zbyt długie czekanie albo ciągłe 429 – błąd bez dalszych prób."""
        self.respond((429, {'retry_after': 1.5}), (204, None))
        DiscordClient().add_role(uid(1), EDITOR_RANK, 'r')
        self.sleep.assert_called_once_with(1.5)
        self.assertEqual(len(self.requests), 2)
        self.sleep.reset_mock()
        self.respond((429, {'retry_after': 120}))
        with self.assertRaisesMessage(DiscordError, 'ogranicza liczbę zapytań – spróbuj ponownie za 120 s'):
            DiscordClient(max_wait=5).add_role(uid(1), EDITOR_RANK, 'r')
        self.sleep.assert_not_called()
        self.respond(*[(429, {'retry_after': 0.1})] * (discord_api.RATE_LIMIT_ATTEMPTS + 1))
        with self.assertRaises(DiscordError) as raised:
            DiscordClient().roles()
        self.assertEqual((raised.exception.status, len(self.requests)), (429, discord_api.RATE_LIMIT_ATTEMPTS + 1))

    def test_not_configured_and_reason_limit(self):
        """Bez tokenu lub ID serwera klient się nie tworzy; powód jest przycinany do limitu Discorda."""
        for case, values in {'bez tokenu': {'DISCORD_BOT_TOKEN': ''}, 'bez serwera': {'DISCORD_GUILD_ID': ''}}.items():
            with self.subTest(case), override_settings(**values):
                self.assertFalse(discord_api.is_configured())
                with self.assertRaisesMessage(discord_api.DiscordNotConfigured, 'DISCORD_BOT_TOKEN i DISCORD_GUILD_ID'):
                    DiscordClient()
        encoded = discord_api.encode_reason('Żółć ' * 300)
        self.assertLessEqual(len(encoded), discord_api.REASON_LIMIT)
        self.assertTrue(urllib.parse.unquote(encoded).startswith('Żółć Żółć'))


class DiscordSettingsEnvironmentTests(SimpleTestCase):
    def test_bot_variables_are_read_from_environment(self):
        """DISCORD_BOT_TOKEN i DISCORD_GUILD_ID trafiają do ustawień (bez spacji); domyślnie puste."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DJANGO_', 'DISCORD_'))}
        env.update(DJANGO_SKIP_DOTENV='1', DJANGO_SECRET_KEY='isolated-tests-only-' + 'x' * 40,
                   DJANGO_DB_NAME='unused', DJANGO_DB_USER='unused', DJANGO_DB_PASSWORD='unused')
        check = ('import json; from fantazmaty import settings as s; '
                 'print(json.dumps([s.DISCORD_BOT_TOKEN, s.DISCORD_GUILD_ID]))')
        for case, (values, expected) in {'domyślne': ({}, ['', '']),
                                         'ze środowiska': ({'DISCORD_BOT_TOKEN': ' abc ', 'DISCORD_GUILD_ID': '12 '},
                                                           ['abc', '12'])}.items():
            with self.subTest(case):
                result = subprocess.run([sys.executable, '-c', check], env={**env, **values}, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)


# Podgląd, plan i synchronizacja

class RankData:
    """Ekipa i serwer: Redaktor ma mieć rangę „Redakcja”; różne przypadki osób i członków serwera."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = Role.objects.get_or_create(name='Redaktor')[0]
        cls.proofreader = Role.objects.get_or_create(name='Korektor')[0]
        cls.rank = DiscordRole.objects.create(name='redakcja', role_id=EDITOR_RANK, sync_mode=Mode.GRANT)
        cls.rank.cms_roles.set([cls.editor])
        cls.anna = make_person('Anna', 'Nowak', roles=[cls.editor], discord=uid(1))  # dostanie
        cls.bartek = make_person('Bartek', 'Lis', roles=[cls.editor], discord=uid(2))  # już ma
        cls.celina = make_person('Celina', 'Bez', roles=[cls.editor])  # bez ID
        cls.daniel = make_person('Daniel', 'Obok', roles=[cls.editor], discord=uid(4))  # spoza serwera
        cls.ewa = make_person('Ewa', 'Kor', roles=[cls.proofreader], discord=uid(5))  # ma, nie powinna
        cls.former = make_person('Filip', 'Dawny', roles=[cls.editor], discord=uid(6), active=False)  # odszedł
        cls.blocked = make_person('Gosia', 'Blok', roles=[cls.editor], discord=uid(7), user=True)
        User.objects.filter(pk=cls.blocked.user_id).update(is_active=False)  # konto zablokowane

    def server(self):
        return FakeDiscord([
            member(1, nick='Anna Nowak', roles=[OTHER_RANK]), member(2, nick='Bartek Lis', roles=[EDITOR_RANK]),
            member(5, roles=[EDITOR_RANK, OTHER_RANK]), member(6, roles=[EDITOR_RANK]),
            member(7, roles=[EDITOR_RANK]), member(8, nick='Nieznany', roles=[EDITOR_RANK]),
            member(9, roles=[OTHER_RANK])])

    def approve(self, rank=None):
        rank = DiscordRole.objects.get(pk=(rank or self.rank).pk)
        DiscordRole.objects.filter(pk=rank.pk).update(sync_mode=Mode.FULL, full_sync_approved_for=rank.mapping_key())


@tag('external')
class PlanTests(RankData, FakeDiscordMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.install(self.server())

    def plan(self):
        plans, error = discord_sync.preview()
        self.assertEqual(error, '')
        return plans[0]

    def test_plan_lists_by_mode(self):
        """Podgląd: dostaną, mają, bez ID, spoza serwera, nierozpoznani; „stracą” to osoby z CMS-u z rangą bez prawa
        do niej (także spoza ekipy i z zablokowanym kontem) – w trybie nadawania tylko informacyjnie."""
        for mode, applies in ((Mode.GRANT, False), (Mode.FULL, True)):
            with self.subTest(mode):
                if mode == Mode.FULL:
                    self.approve()
                plan = self.plan()
                self.assertEqual((plan.mode, plan.applies_revoke, plan.has), (mode, applies, 1))
                self.assertEqual([e.person for e in plan.grant], [self.anna])
                self.assertEqual({e.person for e in plan.revoke}, {self.ewa, self.former, self.blocked})
                self.assertEqual([e.person for e in plan.no_id], [self.celina])
                self.assertEqual([e.person for e in plan.not_on_server], [self.daniel])
                self.assertEqual([(e.user_id, e.name) for e in plan.unrecognized], [(uid(8), 'Nieznany')])
                self.assertEqual(plan.discord_name, 'Redakcja')
        self.assertEqual(self.fake.changes(), [])
        self.assertFalse(DiscordRoleChange.objects.exists())

    def test_full_mode_without_valid_approval_only_grants(self):
        """Pełna synchronizacja bez zatwierdzenia albo po zmianie mapowania (ID rangi, role CMS) działa jak nadawanie."""
        DiscordRole.objects.filter(pk=self.rank.pk).update(sync_mode=Mode.FULL)
        self.assertEqual(self.plan().mode, Mode.GRANT)
        self.approve()
        self.assertEqual(self.plan().mode, Mode.FULL)
        self.rank.cms_roles.add(self.proofreader)
        plan = self.plan()
        self.assertEqual((plan.mode, plan.stale_approval), (Mode.GRANT, True))

    def test_missing_rank_on_server_and_disabled_ranks(self):
        """Ranga nieistniejąca na serwerze jest oznaczona; ranga wyłączona nie trafia do planu."""
        self.fake.roles.pop(EDITOR_RANK)
        self.assertTrue(self.plan().missing_on_server)
        DiscordRole.objects.update(sync_mode=Mode.OFF)
        self.assertEqual(discord_sync.preview(), ([], ''))

    def test_preview_reports_errors_and_missing_configuration(self):
        """Błąd Discorda i brak konfiguracji bota – komunikat zamiast planu."""
        self.fake.force('GET', '/members', 403, {'code': 50001})
        self.assertIn('Server Members Intent', discord_sync.preview()[1])
        with override_settings(DISCORD_BOT_TOKEN=''):
            self.assertEqual(discord_sync.preview(), ([], discord_api.NOT_CONFIGURED))

    def test_grant_mode_sync_only_adds_and_keeps_other_ranks(self):
        """Tylko nadawanie: dodaje brakującą rangę, nikomu nie zabiera; rangi spoza mapowania zostają; historia."""
        outcome = discord_sync.synchronize(source=DiscordRoleChange.Source.PANEL)
        self.assertEqual(self.fake.changes(), [('PUT', f'{uid(1)}/roles/{EDITOR_RANK}')])
        self.assertEqual(sorted(self.fake.members[uid(1)]['roles']), sorted([OTHER_RANK, EDITOR_RANK]))
        self.assertIn(EDITOR_RANK, self.fake.members[uid(5)]['roles'])
        change = outcome.changes[0]
        self.assertEqual((change.person, change.action, change.result, change.source, change.role_id),
                         (self.anna, 'grant', 'done', 'panel', EDITOR_RANK))
        self.assertEqual(change.person_label, 'Anna Nowak')

    def test_full_sync_revokes_only_recognized_people(self):
        """Pełna synchronizacja: odbiera rangę osobom z CMS-u bez prawa do niej; nierozpoznanych członków i innych
        rang nie rusza; drugi przebieg niczego nie zmienia."""
        self.approve()
        discord_sync.synchronize(source=DiscordRoleChange.Source.COMMAND)
        self.assertCountEqual(self.fake.changes(), [
            ('PUT', f'{uid(1)}/roles/{EDITOR_RANK}'), ('DELETE', f'{uid(5)}/roles/{EDITOR_RANK}'),
            ('DELETE', f'{uid(6)}/roles/{EDITOR_RANK}'), ('DELETE', f'{uid(7)}/roles/{EDITOR_RANK}')])
        self.assertEqual(self.fake.members[uid(5)]['roles'], [OTHER_RANK])
        self.assertEqual(self.fake.members[uid(8)]['roles'], [EDITOR_RANK])
        self.fake.calls.clear()
        self.assertEqual(discord_sync.synchronize(source=DiscordRoleChange.Source.COMMAND).changes, [])
        self.assertEqual(self.fake.changes(), [])

    def test_changed_state_since_preview_runs_nothing(self):
        """Skrót planu z podglądu musi się zgadzać z bieżącym – inaczej nic nie jest wykonywane."""
        plans, _ = discord_sync.preview()
        digest = discord_sync.plans_digest(plans)
        self.fake.members[uid(1)]['roles'].append(EDITOR_RANK)
        outcome = discord_sync.synchronize(source=DiscordRoleChange.Source.PANEL, expected=digest)
        self.assertTrue(outcome.stale)
        self.assertEqual(self.fake.changes(), [])

    def test_hierarchy_error_stops_rank_and_is_logged_for_retry(self):
        """403 przy randze: błąd w historii przy każdej operacji tej rangi, bez kolejnych prób w tym przebiegu;
        następny udany przebieg oznacza wcześniejsze błędy jako ponowione."""
        self.approve()
        self.fake.force('PUT', '/roles/', 403, {'code': 50013, 'message': 'Missing Permissions'})
        outcome = discord_sync.synchronize(source=DiscordRoleChange.Source.COMMAND)
        self.assertEqual(len(self.fake.changes()), 1)
        self.assertEqual(len(outcome.errors), 4)
        self.assertIn('Zarządzanie rolami', outcome.errors[0].response)
        self.assertIn('HTTP 403, kod 50013', outcome.errors[0].response)
        discord_sync.synchronize(source=DiscordRoleChange.Source.COMMAND)
        self.assertEqual(DiscordRoleChange.objects.filter(result=Result.ERROR).count(), 0)
        self.assertEqual(DiscordRoleChange.objects.filter(result=Result.RETRIED).count(), 4)

    def test_orphan_revoke_errors_are_retried_by_full_run(self):
        """Nieudane odebranie rangi kontu już bez osoby (usunięta osoba) ponawia pełny przebieg – tylko w pełnej
        synchronizacji; konto, które w międzyczasie straciło rangę, nie jest ruszane."""
        self.approve()
        DiscordRoleChange.objects.create(discord_user_id=uid(8), role_label='@redakcja', role_id=EDITOR_RANK,
                                         action='revoke', result=Result.ERROR, reason='usunięcie osoby z CMS-u',
                                         source='change', discord_role=self.rank)
        DiscordRoleChange.objects.create(discord_user_id=uid(9), role_label='@redakcja', role_id=EDITOR_RANK,
                                         action='revoke', result=Result.ERROR, reason='usunięcie osoby z CMS-u',
                                         source='change', discord_role=self.rank)
        discord_sync.synchronize(source=DiscordRoleChange.Source.COMMAND)
        self.assertIn(('DELETE', f'{uid(8)}/roles/{EDITOR_RANK}'), self.fake.changes())
        self.assertNotIn(('DELETE', f'{uid(9)}/roles/{EDITOR_RANK}'), self.fake.changes())
        self.assertFalse(DiscordRoleChange.objects.filter(result=Result.ERROR).exists())

    def test_approval_saves_and_runs_previewed_plan(self):
        """„Zatwierdź i włącz”: zapis zatwierdzenia dla mapowania i wykonanie podglądu tej rangi; inny stan albo brak
        ról CMS – bez zmian."""
        admin = create_superuser('discord-approver')
        plans, _ = discord_sync.approval_preview(self.rank)
        self.assertTrue(plans[0].applies_revoke)
        stale = discord_sync.approve_full_sync(self.rank, admin, expected='inny')
        self.assertTrue(stale.stale)
        self.assertEqual(DiscordRole.objects.get(pk=self.rank.pk).sync_mode, Mode.GRANT)
        outcome = discord_sync.approve_full_sync(self.rank, admin, expected=discord_sync.plans_digest(plans))
        self.assertEqual(len(outcome.changes), 4)
        rank = DiscordRole.objects.get(pk=self.rank.pk)
        self.assertEqual((rank.sync_mode, rank.effective_mode(), rank.full_sync_approved_by), (Mode.FULL, Mode.FULL, admin))
        self.assertIsNotNone(rank.full_sync_approved_at)
        self.assertEqual({c.source for c in outcome.changes}, {'approval'})
        empty = DiscordRole.objects.create(name='pusta', role_id=PROOF_RANK)
        refused = discord_sync.approve_full_sync(empty, admin, expected='x')
        self.assertIn('nie ma ról w CMS-ie', refused.error)
        self.assertEqual(DiscordRole.objects.get(pk=empty.pk).sync_mode, Mode.OFF)


@tag('external')
class ChangeSyncTests(RankData, FakeDiscordMixin, TestCase):
    """Synchronizacja po zmianach w CMS-ie: po zatwierdzeniu transakcji, wycofana transakcja – nic."""

    def setUp(self):
        super().setUp()
        self.install(self.server())

    def committed(self, change):
        with self.captureOnCommitCallbacks(execute=True):
            change()
        return self.fake.changes()

    def test_changes_trigger_sync_after_commit(self):
        """Zmiana ról, dezaktywacja, zablokowanie konta, usunięcie osoby i zmiana ID – właściwe operacje (pełna
        synchronizacja), każda z powodem w historii i w dzienniku serwera."""
        self.approve()
        newcomer = make_person('Nowy', 'Gość', discord=uid(9))
        self.fake.members[uid(10)] = member(10)
        cases = [
            ('rola dodana', lambda: newcomer.roles.add(self.editor), [('PUT', f'{uid(9)}/roles/{EDITOR_RANK}')],
             'zmiana ról osoby'),
            ('rola odebrana (od strony roli)', lambda: self.editor.people.remove(newcomer),
             [('DELETE', f'{uid(9)}/roles/{EDITOR_RANK}')], 'zmiana osób z rolą „Redaktor”'),
            ('dezaktywacja', lambda: self._set(self.bartek, is_active=False).save(),
             [('DELETE', f'{uid(2)}/roles/{EDITOR_RANK}')], 'odejście z ekipy (dezaktywacja)'),
            ('powrót', lambda: self._set(self.bartek, is_active=True).save(),
             [('PUT', f'{uid(2)}/roles/{EDITOR_RANK}')], 'powrót do ekipy'),
            ('zmiana ID', lambda: self._set(self.anna, discord_id=uid(10)).save(),
             [('PUT', f'{uid(10)}/roles/{EDITOR_RANK}')], 'zmiana identyfikatora Discorda'),
            ('usunięcie osoby', lambda: Person.objects.get(pk=self.anna.pk).delete(),
             [('DELETE', f'{uid(10)}/roles/{EDITOR_RANK}')], 'usunięcie osoby z CMS-u'),
        ]
        for case, change, expected, reason in cases:
            with self.subTest(case):
                self.fake.calls.clear()
                self.assertEqual(self.committed(change), expected)
                last = DiscordRoleChange.objects.first()
                self.assertIn(reason, last.reason)
                headers = [h for m, _, _, h in self.fake.calls if m in ('PUT', 'DELETE')][0]
                self.assertIn(urllib.parse.quote(reason, safe=''), headers['X-Audit-Log-Reason'])

    @staticmethod
    def _set(person, **values):
        person = Person.objects.get(pk=person.pk)
        for name, value in values.items():
            setattr(person, name, value)
        return person

    def test_changed_id_revokes_old_account_in_full_mode_only(self):
        """Zmiana ID: nowe konto dostaje rangę; stare traci ją tylko przy pełnej synchronizacji (przy nadawaniu –
        zostaje, bo staje się nierozpoznane)."""
        for mode, person, new_id in ((Mode.GRANT, self.bartek, 11), (Mode.FULL, self.blocked, 12)):
            with self.subTest(mode):
                self.fake.members[uid(new_id)] = member(new_id)
                if mode == Mode.FULL:
                    self.approve()
                    User.objects.filter(pk=self.blocked.user_id).update(is_active=True)
                old_id = person.discord_id
                self.fake.calls.clear()
                changes = self.committed(lambda: self._set(person, discord_id=uid(new_id)).save())
                self.assertIn(('PUT', f'{uid(new_id)}/roles/{EDITOR_RANK}'), changes)
                self.assertEqual(('DELETE', f'{old_id}/roles/{EDITOR_RANK}') in changes, mode == Mode.FULL)

    def test_account_block_and_grant_mode_never_revokes(self):
        """Zablokowanie konta przy pełnej synchronizacji odbiera rangę; tryb nadawania nikomu nie zabiera."""
        self.assertEqual(self.committed(lambda: self._set(self.bartek, is_active=False).save()), [])
        self.approve()
        account = User.objects.create_user(username='bartek@example.test')
        Person.objects.filter(pk=self.bartek.pk).update(user=account, is_active=True)
        self.fake.calls.clear()

        def block():
            account.is_active = False
            account.save()
        self.assertEqual(self.committed(block), [('DELETE', f'{uid(2)}/roles/{EDITOR_RANK}')])
        self.assertEqual(DiscordRoleChange.objects.first().reason, 'zmiana aktywności konta')

    def test_rolled_back_change_sends_nothing(self):
        """Wycofana transakcja (np. błąd zapisu) nie zmienia niczego na Discordzie i nie zapisuje historii."""
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.celina.roles.remove(self.editor)
                self._set(self.anna, discord_id=uid(9)).save()
                raise RuntimeError('błąd')
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(DiscordRoleChange.objects.exists())

    def test_discord_error_does_not_block_save(self):
        """Błąd Discorda: zmiana w CMS-ie zostaje, w historii – błąd do ponowienia; wyjątek nie wychodzi z zapisu."""
        self.fake.force('GET', f'/members/{uid(5)}', 502)
        self.committed(lambda: self.ewa.roles.add(self.editor))
        self.assertTrue(Person.objects.get(pk=self.ewa.pk).roles.filter(pk=self.editor.pk).exists())
        error = DiscordRoleChange.objects.get()
        self.assertEqual((error.result, error.action, error.person), (Result.ERROR, 'grant', self.ewa))
        with patch('people.discord_sync.sync_targets', side_effect=RuntimeError('nieoczekiwany')):
            self.committed(lambda: self.ewa.roles.remove(self.editor))

    def test_person_outside_server_and_unconfigured_bot(self):
        """Osoba spoza serwera – bez operacji i bez wpisu; bez konfiguracji bota – żadnych zapytań."""
        self.assertEqual(self.committed(lambda: self.daniel.roles.add(self.proofreader)), [])
        self.assertFalse(DiscordRoleChange.objects.exists())
        with override_settings(DISCORD_BOT_TOKEN=''):
            self.committed(lambda: self.anna.roles.remove(self.editor))
        self.assertFalse([c for c in self.fake.calls if c[0] != 'GET' or uid(1) in c[1]])

    def test_no_managed_ranks_means_no_requests(self):
        """Wszystkie rangi wyłączone – zmiana osób nie wysyła żadnych zapytań."""
        DiscordRole.objects.update(sync_mode=Mode.OFF)
        self.committed(lambda: self.anna.roles.remove(self.editor))
        self.assertEqual(self.fake.calls, [])


@tag('external')
class CommandTests(RankData, FakeDiscordMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.install(self.server())

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command('sync_discord_roles', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_dry_run_prints_plan_without_changes(self):
        """--dry-run: plan z nadaniami, „straciłby” i nierozpoznanymi, bez zmian i historii."""
        out, _ = self.run_command('--dry-run')
        for text in ('[próba] @redakcja', 'nadanie: Anna Nowak', 'straciłby przy pełnej synchronizacji: Ewa Kor',
                     'nierozpoznani z rangą: 1'):
            with self.subTest(text):
                self.assertIn(text, out)
        self.assertEqual(self.fake.changes(), [])
        self.assertFalse(DiscordRoleChange.objects.exists())

    def test_run_applies_and_summarizes(self):
        """Przebieg: zmiany wypisane i w historii (wywołanie: zadanie okresowe), podsumowanie; błędy na stderr."""
        self.approve()
        self.fake.force('DELETE', uid(6), 500)
        out, err = self.run_command()
        self.assertIn('nadano rangę @redakcja', out)
        self.assertIn('Rang: 1, zmian: 3, błędów: 1.', out)
        self.assertIn('Filip Dawny: nie udało się odebrać rangi', err)
        self.assertEqual(set(DiscordRoleChange.objects.values_list('source', flat=True)), {'command'})

    def test_missing_configuration_and_no_ranks(self):
        """Bez bota – błąd komendy; bez rang z mapowania – komunikat bez zapytań."""
        with override_settings(DISCORD_GUILD_ID=''), self.assertRaisesMessage(CommandError, 'DISCORD_GUILD_ID'):
            self.run_command()
        DiscordRole.objects.update(sync_mode=Mode.OFF)
        self.assertIn('Żadna ranga nie jest nadawana', self.run_command()[0])
        self.assertEqual(self.fake.calls, [])

    def test_fetch_error_is_command_error(self):
        """Błąd pobrania członków (brak Server Members Intent) kończy komendę błędem z podpowiedzią."""
        self.fake.force('GET', '/members', 403, {'code': 50001})
        with self.assertRaisesMessage(CommandError, 'Server Members Intent'):
            self.run_command()


# Panel admina

@tag('external')
class RankAdminTests(RankData, FakeDiscordMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('discord-admin')

    def setUp(self):
        super().setUp()
        self.install(self.server())
        self.client.force_login(self.admin)

    def post(self, url, data=None):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, data or {})
        return response, [str(m) for m in response.wsgi_request._messages]

    def token(self, page):
        from lxml import html
        return html.fromstring(page.content).xpath('//input[@name="token"]/@value')[0]

    def test_sync_page_shows_preview_and_runs_shown_plan(self):
        """Przegląd: podgląd z grupami i zasadą dla nierozpoznanych, nic nie wysyła; „Synchronizuj teraz” wykonuje
        pokazany plan; po zmianie stanu – ostrzeżenie bez zmian."""
        url = reverse('admin:core_discordrole_sync')
        page = self.client.get(url)
        for text in ('Dostaną rangę (1)', 'Anna Nowak', 'Straciliby rangę przy pełnej synchronizacji – teraz jej NIE '
                     'stracą (3)', 'Bez identyfikatora Discorda w CMS-ie (1)', 'Spoza serwera Discorda (1)',
                     'Nierozpoznani członkowie serwera z tą rangą (1)', 'CMS nie rusza', 'Synchronizuj teraz',
                     'Tryb: Tylko nadawanie'):
            with self.subTest(text):
                self.assertContains(page, text)
        self.assertEqual(self.fake.changes(), [])
        token = self.token(page)
        self.fake.members[uid(1)]['roles'].append(EDITOR_RANK)
        response, messages = self.post(url, {'token': token})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Stan na Discordzie albo w CMS-ie zmienił się od podglądu', messages[0])
        self.fake.members[uid(1)]['roles'].remove(EDITOR_RANK)
        response, messages = self.post(url, {'token': token})
        self.assertRedirects(response, url)
        self.assertEqual(messages, ['Discord – Anna Nowak: nadano rangę @redakcja.'])

    def test_sync_page_without_configuration_or_ranks(self):
        """Bez bota – komunikat o konfiguracji; bez rang z mapowania – podpowiedź, bez przycisku synchronizacji."""
        with override_settings(DISCORD_BOT_TOKEN=''):
            page = self.client.get(reverse('admin:core_discordrole_sync'))
        self.assertContains(page, 'Bot Discorda nie jest skonfigurowany')
        DiscordRole.objects.update(sync_mode=Mode.OFF)
        page = self.client.get(reverse('admin:core_discordrole_sync'))
        self.assertContains(page, 'Żadna ranga nie jest nadawana przez CMS')
        self.assertNotContains(page, 'Synchronizuj teraz')

    def test_approve_page_and_button(self):
        """Strona zatwierdzenia pokazuje, kto straci rangę; „Zatwierdź i włącz” włącza pełną synchronizację."""
        url = reverse('admin:core_discordrole_approve', args=[self.rank.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Stracą rangę (3)')
        self.assertContains(page, 'Zatwierdź i włącz pełną synchronizację')
        response, messages = self.post(url, {'token': self.token(page)})
        self.assertRedirects(response, reverse('admin:core_discordrole_sync'), fetch_redirect_response=False)
        self.assertIn('@redakcja: włączono pełną synchronizację.', messages)
        self.assertEqual(DiscordRole.objects.get(pk=self.rank.pk).effective_mode(), Mode.FULL)
        self.assertEqual(len(self.fake.changes()), 4)
        response, messages = self.post(url, {'token': 'zły'})
        self.assertEqual(response.status_code, 200)

    def test_form_offers_full_sync_only_after_approval_and_warns_on_mapping_change(self):
        """Formularz rangi: bez zatwierdzenia nie ma „pełnej synchronizacji”; zmiana ról CMS rangi zatwierdzonej –
        ostrzeżenie; ta sama ranga (ID) nie może być nadawana z dwóch wpisów."""
        url = reverse('admin:core_discordrole_change', args=[self.rank.pk])
        self.assertNotContains(self.client.get(url), 'value="full"')
        data = admin_form_data(self.client, url, 'discordrole_form', sync_mode='full')
        self.assertContains(self.client.post(url, data), 'Wybierz poprawną wartość')
        self.approve()
        self.assertContains(self.client.get(url), 'value="full"')
        data = admin_form_data(self.client, url, 'discordrole_form',
                               cms_roles=[str(self.editor.pk), str(self.proofreader.pk)])
        response, messages = self.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('do ponownego zatwierdzenia podglądu CMS rangę tylko nadaje', ' '.join(messages))
        self.assertEqual(DiscordRole.objects.get(pk=self.rank.pk).effective_mode(), Mode.GRANT)
        add = self.client.post(reverse('admin:core_discordrole_add'), {
            'name': 'kopia', 'role_id': EDITOR_RANK, 'sync_mode': 'grant'})
        self.assertContains(add, 'Tę rangę (ID 500000000000000001) nadaje już @redakcja')

    def test_history_is_read_only_with_filters(self):
        """Historia: lista z filtrami i wyszukiwaniem, szczegóły tylko do odczytu, bez dodawania i usuwania."""
        discord_sync.synchronize(source=DiscordRoleChange.Source.PANEL, actor=self.admin)
        change = DiscordRoleChange.objects.get()
        listing = self.client.get(reverse('admin:core_discordrolechange_changelist'), {'result__exact': 'done'})
        for text in ('Anna Nowak', '@redakcja', 'Nadanie', 'Wykonano', 'Synchronizacja z panelu', 'HTTP 204'):
            with self.subTest(text):
                self.assertContains(listing, text)
        self.assertContains(self.client.get(reverse('admin:core_discordrolechange_changelist'), {'q': uid(1)}),
                            'Anna Nowak')
        self.assertEqual(self.client.get(reverse('admin:core_discordrolechange_add')).status_code, 403)
        detail = self.client.get(reverse('admin:core_discordrolechange_change', args=[change.pk]))
        self.assertNotContains(detail, 'name="_save"')
        self.assertEqual(self.client.post(reverse('admin:core_discordrolechange_delete', args=[change.pk]),
                                          {'post': 'yes'}).status_code, 403)
        self.assertTrue(DiscordRoleChange.objects.filter(pk=change.pk).exists())

    def test_person_screen_saves_discord_id_and_reports_sync(self):
        """Ekran osoby: identyfikator zapisany, synchronizacja po zapisie z komunikatem w panelu."""
        url = reverse('admin:people_person_change', args=[self.celina.pk])
        data = admin_form_data(self.client, url, 'person_form', discord_id=uid(9))
        response, messages = self.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Person.objects.get(pk=self.celina.pk).discord_id, uid(9))
        self.assertIn('Discord – Celina Bez: nadano rangę @redakcja.', messages)


@tag('external')
class AdminAccessTests(RankData, FakeDiscordMixin, TestCase):
    """Strony rang Discorda, historia i ustawienia – tylko superuser (POST innych nic nie zmienia)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.actors = {
            'superuser': create_superuser('discord-access-admin'),
            'coordinator': create_member('discord-koordynator', 'Koordynator'),
            'member': create_member('discord-czlonek', 'Redaktor'),
            'outsider': User.objects.create_user(username='discord-obcy'),
        }

    def setUp(self):
        super().setUp()
        self.install(self.server())

    def test_only_superuser_sees_and_changes(self):
        urls = [reverse('admin:core_discordrole_sync'), reverse('admin:core_discordrole_match'),
                reverse('admin:core_discordrole_approve', args=[self.rank.pk]),
                reverse('admin:core_discordrole_changelist'), reverse('admin:core_discordrolechange_changelist'),
                reverse('admin:core_discordsettings_changelist')]
        for actor, user in [*self.actors.items(), ('anonymous', None)]:
            self.client.logout()
            if user:
                self.client.force_login(user)
            for url in urls:
                with self.subTest(actor=actor, url=url):
                    status = self.client.get(url).status_code
                    self.assertEqual(status, 200 if actor == 'superuser' else 302)
            if actor != 'superuser':
                with self.subTest(actor=actor, method='POST'):
                    for url in urls[:3]:
                        self.assertEqual(self.client.post(url, {'token': 'x'}).status_code, 302)
        self.assertEqual(self.fake.changes(), [])
        self.assertEqual(DiscordRole.objects.get(pk=self.rank.pk).sync_mode, Mode.GRANT)


# Identyfikator Discorda osoby

class DiscordIdTests(TestCase):
    def test_validation_and_uniqueness(self):
        """Same cyfry 17–20 znaków; pusty – brak (wiele osób bez ID); powtórzony – błąd unikalności."""
        from people.admin import PersonAdminForm
        Person.objects.create(first_name='Zajęty', last_name='Identyfikator', discord_id=uid(1))
        base = {'first_name': 'Jan', 'last_name': 'Test', 'is_active': 'on'}
        cases = {
            '17 cyfr': ('1' * 17, None), '20 cyfr': ('9' * 20, None), 'ze spacjami': (f' {uid(3)} ', None),
            'pusty': ('', None), '16 cyfr': ('1' * 16, '17–20 cyfr'), '21 cyfr': ('1' * 21, 'co najwyżej 20'),
            'litery': ('12345678901234567a', '17–20 cyfr'), 'z @': ('@' + uid(2), '17–20 cyfr'),
            'zajęty': (uid(1), 'Istnieje już'),
        }
        for case, (value, error) in cases.items():
            with self.subTest(case):
                form = PersonAdminForm({**base, 'discord_id': value})
                if error is None:
                    self.assertTrue(form.is_valid(), form.errors)
                    self.assertEqual(form.cleaned_data['discord_id'], value.strip() or None)
                else:
                    self.assertFalse(form.is_valid())
                    self.assertIn(error, ' '.join(form.errors['discord_id']))
        Person.objects.create(first_name='A', last_name='B')
        Person.objects.create(first_name='C', last_name='D')
        self.assertEqual(Person.objects.filter(discord_id=None).count(), 2)
        with self.assertRaises(ValidationError):
            Person(first_name='X', last_name='Y', discord_id='123').full_clean()


# Dopasowanie po imieniu i nazwisku

class MatchingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.anna = make_person('Anna', 'Nowak')
        cls.lukasz = make_person('Łukasz', 'Żak')
        cls.jan1 = make_person('Jan', 'Kowalski')
        cls.jan2 = make_person('Jan', 'Kowalski')
        cls.ola = make_person('Ola', 'Mała')
        cls.piotr = make_person('Piotr', 'Zapisany', discord=uid(20))
        cls.zofia = make_person('Zofia', 'Inna', discord=uid(21))
        cls.marek = make_person('Marek', 'Zajęty')
        cls.taken_owner = make_person('Właściciel', 'Konta', discord=uid(30), active=False)
        cls.tomasz = make_person('Tomasz', 'Dwa')
        cls.former = make_person('Ewa', 'Odeszła', active=False)

    def members(self):
        return [
            member(1, nick='  anna   NOWAK ', global_name='Ania'),  # pewne po pseudonimie (wielkość liter, spacje)
            member(2, nick='Lukasz Zak'),  # bez polskich znaków – nie pasuje do „Łukasz Żak”
            member(3, nick='Jan Kowalski'),  # dwie osoby o tym imieniu i nazwisku – niejednoznaczne
            member(4, global_name='ola mała'),  # pewne po nazwie globalnej
            member(20, nick='Piotr Zapisany'),  # już połączony
            member(22, nick='Zofia Inna'),  # osoba ma inny identyfikator – „zastąp”
            member(30, nick='Marek Zajęty'),  # konto połączone z inną osobą
            member(40, nick='Tomasz Dwa'), member(41, username='tomasz dwa'),  # pseudonim wygrywa z nazwą
            member(50, nick='Ewa Odeszła'),  # osoba spoza ekipy nie jest dopasowywana
            member(60, nick='Bot CMS', bot=True),
        ]

    def test_groups(self):
        """Pewne (jedno w obie strony; najpierw pseudonim, potem nazwa globalna lub użytkownika), niejednoznaczne,
        bez dopasowania, już połączone, konto zajęte przez inną osobę; polskie znaki rozróżniane, boty pomijane."""
        result = discord_matching.match(self.members())
        certain = {p.person: (p.status, p.candidate.user_id, p.candidate.level) for p in result.certain}
        self.assertEqual(certain, {
            self.anna: ('certain', uid(1), 'nick'), self.ola: ('certain', uid(4), 'name'),
            self.zofia: ('replace', uid(22), 'nick'), self.tomasz: ('certain', uid(40), 'nick')})
        self.assertEqual({p.person for p in result.ambiguous}, {self.jan1, self.jan2})
        self.assertEqual({p.person for p in result.unmatched}, {self.lukasz})
        self.assertEqual([(p.person, p.candidate.linked_to) for p in result.taken], [(self.marek, self.taken_owner)])
        self.assertEqual(result.linked, 1)
        left = {c.user_id for c in result.members_left}
        self.assertEqual(left, {uid(2), uid(41), uid(50)})

    def test_two_members_with_same_nick_are_ambiguous(self):
        """Dwa konta o tym samym pseudonimie – propozycja niejednoznaczna z oboma kontami."""
        result = discord_matching.match([member(1, nick='Anna Nowak'), member(2, nick='anna nowak')])
        self.assertEqual([(p.person, len(p.candidates)) for p in result.ambiguous], [(self.anna, 2)])

    def test_apply_saves_only_selected_and_never_overwrites_silently(self):
        """Zapis: tylko zaznaczone propozycje; inny istniejący ID – tylko z „zastąp”; spoza propozycji i jedno
        konto dla dwóch osób – błąd bez zapisu."""
        result = discord_matching.match(self.members())
        saved, errors = discord_matching.apply(result, {
            self.anna.pk: uid(1), self.zofia.pk: uid(22), self.ola.pk: uid(99), self.jan1.pk: uid(3),
            self.jan2.pk: uid(3)}, replace=set())
        self.assertEqual(saved, [self.anna])
        self.assertEqual(Person.objects.get(pk=self.zofia.pk).discord_id, uid(21))
        joined = ' '.join(errors)
        for text in ('zaznacz „zastąp”', 'nie ma już w propozycjach', 'to samo konto wybrano dla kilku osób'):
            with self.subTest(text):
                self.assertIn(text, joined)
        self.assertEqual(Person.objects.filter(discord_id=uid(3)).count(), 0)
        saved, errors = discord_matching.apply(result, {self.zofia.pk: uid(22)}, replace={self.zofia.pk})
        self.assertEqual((saved, errors), ([self.zofia], []))
        self.assertEqual(Person.objects.get(pk=self.zofia.pk).discord_id, uid(22))


@tag('external')
class MatchingAdminTests(FakeDiscordMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('discord-match-admin')
        cls.anna = make_person('Anna', 'Nowak')
        cls.zofia = make_person('Zofia', 'Inna', discord=uid(21))

    def setUp(self):
        super().setUp()
        self.install(FakeDiscord([member(1, nick='Anna Nowak'), member(22, nick='Zofia Inna')]))
        self.client.force_login(self.admin)

    def test_page_proposes_and_saves_checked(self):
        """Strona dopasowania: propozycje z zaznaczeniem tylko pewnych bez ID; zapis zaznaczonych z komunikatem."""
        url = reverse('admin:core_discordrole_match')
        page = self.client.get(url)
        self.assertContains(page, f'name="link_{self.anna.pk}" value="{uid(1)}" checked')
        self.assertContains(page, f'name="link_{self.zofia.pk}" value="{uid(22)}" aria-label')
        self.assertContains(page, 'zastąp obecny identyfikator')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {f'link_{self.anna.pk}': uid(1), f'link_{self.zofia.pk}': uid(22)})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(Person.objects.get(pk=self.anna.pk).discord_id, uid(1))
        self.assertEqual(Person.objects.get(pk=self.zofia.pk).discord_id, uid(21))
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn('Zapisano identyfikator Discorda: Anna Nowak.', messages)
        self.assertTrue(any('zaznacz „zastąp”' in m for m in messages))
        self.assertEqual(self.fake.changes(), [])

    def test_page_errors(self):
        """Błąd Discorda i brak konfiguracji – komunikat zamiast propozycji, POST niczego nie zapisuje."""
        self.fake.force('GET', '/members', 403, {'code': 50001})
        self.assertContains(self.client.get(reverse('admin:core_discordrole_match')), 'Server Members Intent')
        with override_settings(DISCORD_BOT_TOKEN=''):
            page = self.client.get(reverse('admin:core_discordrole_match'))
            self.client.post(reverse('admin:core_discordrole_match'), {f'link_{self.anna.pk}': uid(1)})
        self.assertContains(page, 'Bot Discorda nie jest skonfigurowany')
        self.assertIsNone(Person.objects.get(pk=self.anna.pk).discord_id)
