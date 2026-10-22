"""Macierz dostępu do wszystkich tras (bez panelu admina).

Każda trasa ma zadeklarowany minimalny poziom dostępu. Testy sprawdzają, że:
* anonim (GET i POST) jest przekierowywany do logowania – poza jawnie wymienionymi trasami publicznymi;
* zalogowani użytkownicy o różnych rolach dostają 403 dokładnie tam, gdzie poziom trasy ich wyklucza;
* nowa trasa bez klasyfikacji (albo klasyfikacja nieistniejącej trasy) kończy się błędem z listą nazw.

Argumenty adresów to nieistniejące identyfikatory: dekoratory uprawnień działają przed pobraniem obiektu,
więc dopuszczona rola dostaje 404 (albo 200/302/400/405), a wykluczona – 403. Dzięki temu trasy obiektowe
(np. edycja urlopu, szczegóły przydzielonego tekstu) są sprawdzane na poziomie „bramki” trasy; uprawnienia do
konkretnego obiektu mają własne testy przy widokach (np. core/tests/people/test_vacations.py).
"""
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, tag
from django.urls import URLResolver, get_resolver, reverse

from fantazmaty.testing import create_member, create_superuser
from people.models import Person, Role


def all_routes():
    """{klucz: adres} dla wszystkich tras poza panelem admina; trasy bez nazwy mają klucz ze wzorca."""
    def walk(resolver, namespace=None, prefix=''):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, pattern.namespace or namespace, prefix + str(pattern.pattern))
            else:
                yield namespace, prefix + str(pattern.pattern), pattern.name

    routes = {}
    for namespace, path, name in walk(get_resolver()):
        if namespace == 'admin':
            continue
        key = name or f'/{path}'
        key = f'{namespace}:{key}' if namespace else key
        routes[key] = '/' + re.sub(r'<(?:(\w+):)?\w+>', lambda m: '999999' if m[1] == 'int' else 'x', path)
    return routes


# Trasy dostępne bez logowania (anonim nie jest przekierowywany do logowania).
PUBLIC = {
    'login': 'strona logowania',
    'logout': 'wylogowanie działa także bez sesji (POST przekierowuje na logowanie)',
    'password_reset': 'reset hasła z definicji dla niezalogowanych',
    'password_reset_done': 'potwierdzenie wysłania linku resetu',
    'password_reset_confirm': 'ustawienie nowego hasła z linku w e-mailu (token chroni konto)',
    'password_reset_complete': 'potwierdzenie ustawienia nowego hasła',
    'account_invitation': 'ustawienie hasła z linku w zaproszeniu (token chroni konto)',
    'illustrations:external_illustrations': 'publiczna lista ilustracji dla ilustratorów spoza zespołu',
    'core:external_audiobooks': 'publiczna lista audiobooków dla lektorów spoza zespołu',
    'core:audiobook_guidelines_txt': 'wytyczne do pobrania z publicznej listy audiobooków',
    'core:site_team_api': 'dane ekipy dla fantazmaty.pl – zamiast logowania nagłówek X-Site-Key (bez klucza 503, ze złym 403)',
    'core:site_catalog_api': 'katalog szukajki dla fantazmaty.pl – ten sam nagłówek X-Site-Key co dane ekipy',
    'core:site_listen_api': 'lista audiobooków strony „Słuchaj” dla fantazmaty.pl – ten sam nagłówek X-Site-Key',
}


# Aktorzy: 'outsider' – zalogowany bez profilu w zespole; 'former' – były członek (profil nieaktywny, rola została);
# 'member' – członek zespołu bez ról;
# 'specialist' – członek z rolami Recenzent, Ilustrator, Korektor poskładowy, Korektor audiobooków i Social Media
# (żadna z nich nie daje uprawnień koordynatora); 'coordinator' – Koordynator redakcji;
# 'recruiter' – Koordynator rekrutacji (też koordynator); 'superuser' – superuser bez profilu.
ACTORS = ('outsider', 'former', 'member', 'specialist', 'coordinator', 'recruiter', 'superuser')
SPECIALIST_ROLES = ('Recenzent', 'Ilustrator', 'Korektor poskładowy', 'Korektor audiobooków', 'Social Media')

ANY_USER = frozenset(ACTORS)
TEAM = ANY_USER - {'outsider', 'former'}
COORDINATOR = frozenset({'coordinator', 'recruiter', 'superuser'})
ROLE_OR_COORDINATOR = COORDINATOR | {'specialist'}  # rola specjalisty albo koordynator/superuser
SUPERUSER = frozenset({'superuser'})
RECRUITMENT = frozenset({'recruiter', 'superuser'})  # Koordynator rekrutacji albo superuser
REVIEWER = frozenset({'specialist', 'superuser'})  # recenzent (lub osoba z przydziałem) i superuser – także bez profilu
SELF_ASSIGN = frozenset({'specialist', 'superuser'})  # has_role('Recenzent') – superuser ma każdą rolę
AUDIO_PROOFREADER = frozenset({'specialist'})  # przejęcie korekty audiobooka: tylko rola Korektor audiobooków

ROUTE_LEVELS = {
    'password_change': ANY_USER,
    'password_change_done': ANY_USER,
    'core:user_preview': SUPERUSER,
    'core:user_preview_stop': SUPERUSER,
    # Członkowie zespołu (uprawnienia do konkretnego obiektu sprawdza widok).
    **dict.fromkeys([
        'core:home', 'core:dashboard_tasks', 'core:/dashboard/tasks/', 'core:global_search',
        'core:novel_list', 'core:novel_detail', 'core:vocabulary_list',
        'core:vocabulary_suggestions', 'core:audio_descriptions', 'core:audio_description_detail',
        'core:claim_audio_description', 'core:translation_list', 'core:translation_detail', 'core:tag_list',
        'core:update_text_tags', 'core:change_scheduled_workflow_stage', 'core:/workflow/<int:stage_id>/schedule/',
        'core:anthology_detail', 'core:edit_text_note', 'core:delete_text_note', 'core:programs',
        'core:program_job', 'core:replacement_list', 'core:replacement_edit', 'core:correction_edit',
        'core:correction_delete', 'core:correction_texts', 'core:anthology_corrections', 'core:text_list',
        'core:unassign_reviewer', 'core:release_reviewer_slot', 'core:unblock_review_reading',
        'core:update_review_content_warnings',
        'core:assigned_review_detail', 'core:my_texts', 'core:add_text_note', 'core:update_text_content_warnings',
        'core:assigned_text_detail', 'core:start_assigned_workflow_stage', 'core:complete_workflow_stage',
        'core:resume_text_editing', 'core:send_text_to_author_stage', 'core:send_to_first_verification_stage',
        'core:send_to_second_verification_stage', 'core:finish_text_editing',
        'core:available_texts', 'core:take_workflow_stage',
        'core:anthology_list', 'core:workflow_list', 'core:/etapy-prac/', 'core:people_list', 'core:person_detail',
        'core:my_vacations', 'core:edit_vacation', 'core:end_vacation', 'core:cancel_vacation',
        'core:active_vacations', 'illustrations:cover_proposal_list', 'core:social_media',
    ], TEAM),
    'core:my_reviews': REVIEWER,
    'core:assign_reviewer': SELF_ASSIGN,
    'core:claim_audio_proofreading': AUDIO_PROOFREADER,
    'core:review_list': ROLE_OR_COORDINATOR,
    'core:audio_proofreading': ROLE_OR_COORDINATOR,
    # Audiobooki jak ilustracje: koordynatorzy i role audio (Lektor, Dźwiękowiec, Korektor audiobooków).
    'core:audiobooks': ROLE_OR_COORDINATOR,
    'core:audiobook_detail': ROLE_OR_COORDINATOR,
    'core:audio_contributor': ROLE_OR_COORDINATOR,
    'core:post_layout': ROLE_OR_COORDINATOR,
    # Social Media: koordynatorzy i rola „Social Media” (zapis wierszy to POST na ten sam adres).
    'illustrations:illustration_list': ROLE_OR_COORDINATOR,
    'illustrations:illustration_detail': ROLE_OR_COORDINATOR,
    'core:recruitment_mailbox': RECRUITMENT,
    **dict.fromkeys([
        'core:contact_suggestions', 'core:assign_audio_proofreader', 'core:post_layout_edit', 'core:recruitment_notified', 'core:novel_add', 'core:chapter_edit',
        'core:vocabulary_merge', 'core:task_list', 'core:last_activity', 'core:extract_list', 'core:extract_add',
        'core:extract_edit', 'core:recruitment_list', 'core:recruitment_detail', 'core:recruitment_add',
        'core:recruitment_edit', 'core:editor_activity', 'core:update_review_status', 'core:update_review_file',
        'core:update_coordinator_note', 'core:withdraw_text', 'core:author_list', 'core:author_detail',
        'core:update_text_audiobook',
        'core:proofreader_activity', 'core:verifier_activity', 'core:reviewer_activity', 'core:workflow_inactivity',
        'illustrations:illustrator_list', 'illustrations:illustrator_add', 'illustrations:illustrator_edit',
        'core:audio_contributor_list', 'core:audio_contributor_add',
        'illustrations:update_cover_proposal_status', 'illustrations:create_illustration',
    ], COORDINATOR),
    **dict.fromkeys([
        'core:unlinked_reviews', 'core:recruitment_delete', 'core:translation_person_suggestions',
        'core:translation_person_detail', 'core:set_translators', 'core:newsletter_list', 'core:role_names',
        'core:skip_workflow_stage', 'core:/workflow/<int:stage_id>/pomin/', 'core:review_bulk_submit',
        'core:link_text_review', 'core:cancel_workflow_repetition', 'core:handoff_workflow_stage',
        'core:/workflow/<int:stage_id>/przekaz/', 'core:data_integrity', 'core:person_permissions',
        'core:update_text_file', 'core:toggle_text_take_lock', 'core:author_suggestions', 'core:review_create',
        'core:correction_status', 'core:notification_queue', 'core:scheduled_rejections',
        'core:release_hidden_review', 'core:review_bulk_import', 'core:user_activity',
        'core:update_author_notification', 'core:copy_review_to_text', 'core:restart_text_workflow',
        'core:restore_withdrawn_text', 'core:add_author_note', 'core:split_mailing', 'core:site_www',
        'core:site_catalog', 'core:site_catalog_update', 'core:site_catalog_update_all', 'core:site_listen',
    ], SUPERUSER),
}


class RouteClassificationTests(TestCase):
    def test_every_route_has_exactly_one_access_level(self):
        routes = set(all_routes())
        classified = set(PUBLIC) | set(ROUTE_LEVELS)
        self.assertEqual(set(PUBLIC) & set(ROUTE_LEVELS), set())
        self.assertEqual(sorted(routes - classified), [], 'Nowe trasy bez poziomu dostępu (dopisz do ROUTE_LEVELS)')
        self.assertEqual(sorted(classified - routes), [], 'Klasyfikacja tras, których już nie ma')


class AnonymousAccessTests(TestCase):
    def assert_anonymous_access(self, method):
        login = reverse(settings.LOGIN_URL)
        wrong = []
        for key, url in sorted(all_routes().items()):
            if key in PUBLIC:
                continue
            response = getattr(self.client, method)(url)
            ok = response.status_code == 302 and response['Location'].startswith(f'{login}?next=')
            if not ok:
                wrong.append(f'{key} {method.upper()} {url} → {response.status_code}')
        self.assertEqual(wrong, [], 'Trasy dostępne bez logowania (dopisz zabezpieczenie albo wyjątek do PUBLIC)')

    def test_anonymous_get_redirects_to_login(self):
        self.assert_anonymous_access('get')

    def test_anonymous_post_redirects_to_login(self):
        self.assert_anonymous_access('post')


@tag('slow')
class RoleAccessMatrixTests(TestCase):
    """Dla każdego aktora: 403 dokładnie na trasach, których poziom go nie obejmuje.

    Dostęp do tras zespołu (TEAM, ANY_USER) sprawdzają najsłabsi aktorzy (outsider, former, member); aktorzy z rolami
    są sprawdzani na trasach, których poziom ich rozróżnia – tak każdy test mieści się w szybkim zestawie.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.users = {
            'outsider': User.objects.create_user('matrix-outsider'),
            'former': create_member('matrix-former', 'Redaktor'),
            'member': User.objects.create_user('matrix-member'),
            'specialist': create_member('matrix-specialist', SPECIALIST_ROLES[0]),
            'coordinator': create_member('matrix-coordinator', 'Koordynator redakcji'),
            'recruiter': create_member('matrix-recruiter', 'Koordynator rekrutacji'),
            'superuser': create_superuser('matrix-superuser'),
        }
        Person.objects.create(user=cls.users['member'], first_name='Bez', last_name='Ról',
                              email='matrix-member@example.test')
        Person.objects.filter(user=cls.users['former']).update(is_active=False)
        cls.users['specialist'].person_profile.roles.add(
            *(Role.objects.get_or_create(name=name)[0] for name in SPECIALIST_ROLES[1:]))

    def assert_matrix(self, actor):
        self.client.force_login(self.users[actor])
        routes = all_routes()
        login = reverse(settings.LOGIN_URL)
        wrong = []
        for key, allowed in sorted(ROUTE_LEVELS.items()):
            if actor not in ('outsider', 'former', 'member') and allowed in (TEAM, ANY_USER):
                continue
            url = routes[key]
            response = self.client.get(url)
            if response.status_code == 405:
                response = self.client.post(url)
            expected = actor in allowed
            denied = response.status_code == 403 or (
                response.status_code == 302 and response['Location'].startswith(login))
            if denied == expected:
                wrong.append(f'{key} {url} → {response.status_code} (oczekiwano: {"dostęp" if expected else "403"})')
        self.assertEqual(wrong, [], f'Niezgodny dostęp dla: {actor}')

    def test_logged_in_user_outside_team(self):
        self.assert_matrix('outsider')

    def test_former_member_with_inactive_profile(self):
        self.assert_matrix('former')

    def test_team_member_without_roles(self):
        self.assert_matrix('member')

    def test_member_with_specialist_roles(self):
        self.assert_matrix('specialist')

    def test_coordinator(self):
        self.assert_matrix('coordinator')

    def test_recruitment_coordinator(self):
        self.assert_matrix('recruiter')

    def test_superuser(self):
        self.assert_matrix('superuser')

    def test_pages_for_logged_in_users_are_never_cached(self):
        """Każda strona wymagająca logowania (GET z odpowiedzią 200 dla superusera) ma nagłówek „no-store”,
        żeby przeglądarka nie pokazała jej po wylogowaniu ani innej osobie na tym samym komputerze."""
        self.client.force_login(self.users['superuser'])
        cached = []
        for key, url in sorted(all_routes().items()):
            if key in PUBLIC:
                continue
            response = self.client.get(url)
            if response.status_code == 200 and 'no-store' not in response.get('Cache-Control', ''):
                cached.append(f'{key} {url}')
        self.assertEqual(cached, [])


class EmailCopyAccessTests(TestCase):
    """Koperta w nagłówku (kopiowanie adresów e-mail z tabel strony) – superuser i koordynatorzy, nie pozostali.

    Koperta nie ma własnej trasy: to skrypt na stronach, które użytkownik i tak widzi. Pobieranie kolejnych stron listy
    z nagłówkiem X-CMS-Email-Copy nie zmienia zapamiętanego rozmiaru strony – tylko dla uprawnionych do koperty."""

    @classmethod
    def setUpTestData(cls):
        cls.users = {
            'superuser': create_superuser('copy-superuser'),
            'coordinator': create_member('copy-coordinator', 'Koordynator redakcji'),
            'recruiter': create_member('copy-recruiter', 'Koordynator rekrutacji'),
            'specialist': create_member('copy-specialist', 'Recenzent'),
            'member': create_member('copy-member', 'Redaktor'),
        }

    def test_envelope_by_actor(self):
        cases = [('superuser', True), ('coordinator', True), ('recruiter', True), ('specialist', False),
                 ('member', False), (None, False)]
        for actor, allowed in cases:
            with self.subTest(actor=actor or 'gość'):
                self.client.logout()
                if actor:
                    self.client.force_login(self.users[actor])
                page = self.client.get(reverse('core:home'), follow=True)
                self.assertEqual('data-email-copy-toggle' in page.content.decode(), allowed)
                self.assertEqual('core/email-copy.js' in page.content.decode(), allowed)
                if actor:
                    self.client.get(reverse('core:people_list'), {'page_size': 500}, HTTP_X_CMS_EMAIL_COPY='1')
                    remembered = self.client.session.get('cms_page_sizes', {})
                    self.assertEqual(bool(remembered), not allowed)
