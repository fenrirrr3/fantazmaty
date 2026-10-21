"""Słuchaj WWW – dane strony: format bloku ``#fza-audio-data`` (antologie z ustawień, nagrania z opublikowanych
audiobooków CMS-u), twórcy (Lektor / Korekta / Montaż), części, opcjonalne czasy, ostrzeżenia, seed z migracji core
0066 (``core/site_listen_seed.json``) i dopasowanie seedu do audiobooków po linkach i tytułach."""
import datetime
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from authors.models import Author
from core.models import Audiobook, AudiobookStage, AudioContributor
from core.site_listen import (audiobook_parts, link_listen_to_cms, listen_data, listen_payload, media_key,
                              proofreading_names, unique_slug)
from core.site_models import SiteListenAnthology, SiteListenRecording, SiteTeamSettings
from people.models import Person
from texts.models import Anthology, Text

SEED = Path(__file__).resolve().parents[2] / 'site_listen_seed.json'
COVER = 'https://fantazmaty.pl/wp-content/uploads/okladka.jpg'


def contributor(name):
    return AudioContributor.objects.get_or_create(name=name, email='')[0]


def team_user(username, first_name, last_name):
    user = get_user_model().objects.create_user(username=username, email=f'{username}@example.test')
    Person.objects.create(first_name=first_name, last_name=last_name, user=user, email=f'{username}@example.test')
    return user


DEFAULT_PREMIERE = datetime.date(2024, 1, 1)


def published(anthology, title, *, author='Jan Autor', youtube='', hearthis='', extra=(), premiere=DEFAULT_PREMIERE,
              narrator=None, engineer=None, proofreader=None, status=Audiobook.Status.PUBLISHED):
    """Tekst z autorem i audiobookiem (domyślnie opublikowanym, z premierą) z podanymi linkami i twórcami."""
    text = Text.objects.create(title=title, anthology=anthology, length=1000)
    if author:
        first, _, last = author.partition(' ')
        text.authors.add(Author.objects.create(first_name=first, last_name=last))
    return Audiobook.objects.create(
        text=text, status=status, youtube_url=youtube, hearthis_url=hearthis, additional_links=list(extra),
        premiere_date=premiere, narrator_contact=contributor(narrator) if narrator else None,
        engineer_contact=contributor(engineer) if engineer else None, proofreader=proofreader)


def proofreading(audio, performer, position):
    return AudiobookStage.objects.create(text=audio.text, stage_type='proofreading', position=position,
                                         is_completed=True, start_unknown=True, performer=performer)


class ListenDataMixin:
    @classmethod
    def setUpTestData(cls):
        SiteListenAnthology.objects.all().delete()
        cls.book = Anthology.objects.create(title='Antologia słuchana', status=Anthology.Status.READY)
        cls.other = Anthology.objects.create(title='Druga antologia', status=Anthology.Status.READY)
        cls.entry = SiteListenAnthology.objects.create(anthology=cls.book, slug='antologia-sluchana',
                                                       title='Antologia słuchana', cover_url=COVER, position=2)
        cls.first_entry = SiteListenAnthology.objects.create(anthology=cls.other, slug='druga', title='Druga',
                                                             cover_url=COVER, position=1)
        cls.reader = team_user('korektor-a', 'Jan', 'Testowy')
        cls.reader2 = team_user('korektor-b', 'Ewa', 'Druga')
        cls.full = published(cls.book, 'Pełne', youtube='https://www.youtube.com/watch?v=pelne', premiere=datetime.date(2025, 1, 5),
                             hearthis='https://hearthis.at/fantazmaty/pelne/', narrator='Anna Przykładowa',
                             engineer='Ola Fikcyjna', proofreader=cls.reader2)
        proofreading(cls.full, cls.reader, 1)
        proofreading(cls.full, cls.reader2, 2)
        proofreading(cls.full, cls.reader, 3)
        SiteListenRecording.objects.create(listen_anthology=cls.entry, audiobook=cls.full, card_id='fza-123',
                                           youtube_duration='41:11', hearthis_duration='40:59')
        cls.parts = published(cls.book, 'Wieloczęściowe', youtube='https://youtu.be/cz1', premiere=datetime.date(2025, 3, 1),
                              extra=[{'service': 'hearthis', 'part': 2, 'url': 'https://hearthis.at/fantazmaty/cz2/'},
                                     {'service': 'youtube', 'part': 2, 'url': 'https://youtu.be/cz2'}],
                              narrator='Anna Przykładowa')
        SiteListenRecording.objects.create(listen_anthology=cls.entry, audiobook=cls.parts, part=2,
                                           youtube_duration='1:07:45', hearthis_duration='1:07:44')
        cls.bare = published(cls.book, 'Tylko HearThis', hearthis='https://hearthis.at/fantazmaty/bez/')
        cls.in_other = published(cls.other, 'W drugiej', youtube='https://youtu.be/druga', proofreader=cls.reader)


class ListenPayloadTests(ListenDataMixin, TestCase):
    def test_payload_has_exact_site_format(self):
        """Antologie wg kolejności ustawień; w antologii od najnowszej premiery; każda część to
        osobna karta „, cz. N”; puste pola pominięte; czasy i id karty z wpisu nagrania, bez wpisu – fza-a<pk>-<część>;
        korekta – wykonawcy etapów bez powtórzeń, montaż i lektor z audiobooka."""
        data = listen_payload()
        self.assertEqual(set(data), {'antologie', 'wersja', 'liczby'})
        self.assertEqual(data['liczby'], {'antologie': 2, 'nagrania': 5})
        self.assertEqual(data['antologie'], [
            {'id': 'druga', 'nazwa': 'Druga', 'okladka': COVER, 'nagrania': [
                {'id': f'fza-a{self.in_other.pk}-1', 'tytul': 'W drugiej', 'autor': 'Jan Autor',
                 'youtube': 'https://youtu.be/druga', 'korekta': 'Jan Testowy'}]},
            {'id': 'antologia-sluchana', 'nazwa': 'Antologia słuchana', 'okladka': COVER, 'nagrania': [
                {'id': f'fza-a{self.parts.pk}-1', 'tytul': 'Wieloczęściowe, cz. 1', 'autor': 'Jan Autor',
                 'youtube': 'https://youtu.be/cz1', 'lektor': 'Anna Przykładowa'},
                {'id': f'fza-a{self.parts.pk}-2', 'tytul': 'Wieloczęściowe, cz. 2', 'autor': 'Jan Autor',
                 'youtube': 'https://youtu.be/cz2', 'hearthis': 'https://hearthis.at/fantazmaty/cz2/',
                 'czasYoutube': '1:07:45', 'czasHearthis': '1:07:44', 'lektor': 'Anna Przykładowa'},
                {'id': 'fza-123', 'tytul': 'Pełne', 'autor': 'Jan Autor', 'youtube': 'https://www.youtube.com/watch?v=pelne',
                 'hearthis': 'https://hearthis.at/fantazmaty/pelne/', 'czasYoutube': '41:11', 'czasHearthis': '40:59',
                 'lektor': 'Anna Przykładowa', 'korekta': 'Jan Testowy, Ewa Druga', 'montaz': 'Ola Fikcyjna'},
                {'id': f'fza-a{self.bare.pk}-1', 'tytul': 'Tylko HearThis', 'autor': 'Jan Autor',
                 'hearthis': 'https://hearthis.at/fantazmaty/bez/'}]},
        ])

    def test_counts_and_warnings(self):
        """Liczniki zakładki i ostrzeżenia: audiobook bez linków, antologia bez wpisu, bez okładki, wyłączona,
        Ekstrakty (cały tom), bez autora, bez daty premiery, nagranie bez czasu, wpis seedu bez audiobooka, czasy części, której nie ma na stronie;
        nieopublikowane audiobooki nie trafiają na stronę."""
        published(self.book, 'Bez linków')
        published(self.book, 'Bez premiery', youtube='https://youtu.be/bez-premiery', premiere=None)
        published(self.book, 'Nagrywane', youtube='https://youtu.be/nagrywane', status=Audiobook.Status.EDITING)
        published(self.book, 'Bez autora', author='', youtube='https://youtu.be/anonim')
        published(Anthology.objects.create(title='Poza stroną'), 'X', youtube='https://youtu.be/x')
        extracts = Anthology.objects.create(title='Ekstrakty 3', is_extracts=True)
        volume = Text.objects.get(anthology=extracts)  # wspólny tekst tomu powstaje z antologią
        volume.authors.add(Author.objects.create(first_name='Ewa', last_name='Tomowa'))
        Audiobook.objects.create(text=volume, status=Audiobook.Status.PUBLISHED, youtube_url='https://youtu.be/e',
                                 premiere_date=DEFAULT_PREMIERE)
        no_cover = Anthology.objects.create(title='Bez okładki')
        SiteListenAnthology.objects.create(anthology=no_cover, slug='bez-okladki', title='Bez okładki', position=3)
        published(no_cover, 'B1', youtube='https://youtu.be/b1')
        published(no_cover, 'B2', youtube='https://youtu.be/b2')
        off = Anthology.objects.create(title='Wyłączona')
        SiteListenAnthology.objects.create(anthology=off, slug='wylaczona', title='Wyłączona', cover_url=COVER,
                                           position=4, is_enabled=False)
        published(off, 'W', youtube='https://youtu.be/w')
        SiteListenRecording.objects.create(listen_anthology=self.entry, card_id='fza-9', source_title='Stare',
                                           source_author='Ktoś')
        SiteListenRecording.objects.create(listen_anthology=self.entry, audiobook=self.full, part=5, card_id='fza-10')
        data = listen_data()
        self.assertEqual(data['liczniki'], {'antologie': 2, 'nagrania': 5, 'youtube': 4, 'hearthis': 3,
                                            'bez_czasu': 3, 'bez_tworcow': 1})
        self.assertEqual(data['ostrzezenia'], {
            'no_links': ['Antologia słuchana: Bez linków'], 'no_premiere': ['Antologia słuchana: Bez premiery'],
            'no_entry': ['Poza stroną (1 nagranie)'],
            'no_cover': ['Bez okładki (2 nagrania)'], 'disabled': ['Wyłączona (1 nagranie)'],
            'skipped_kind': [f'Ekstrakty 3: {volume.title}'], 'no_author': ['Antologia słuchana: Bez autora'],
            'no_time': ['Antologia słuchana: Tylko HearThis', 'Antologia słuchana: Wieloczęściowe, cz. 1',
                        'Druga: W drugiej'],
            'unmatched': ['Antologia słuchana: Stare – Ktoś (fza-9)'],
            'stale': ['Antologia słuchana: Pełne, cz. 5 (fza-10)'], 'duplicate_ids': []})
        content = json.dumps(listen_payload(data), ensure_ascii=False)
        for hidden in ('Nagrywane', 'Bez autora', 'Bez premiery', '@', 'korektor-a', 'Poza stroną', 'Wyłączona'):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, content)

    def test_requires_published_status_premiere_and_link(self):
        """Na stronę trafia tylko audiobook o statusie Opublikowane z datą premiery i co najmniej jednym linkiem;
        opublikowany bez daty premiery albo bez linków – ostrzeżenie (oba braki – oba ostrzeżenia)."""
        cases = (
            ('komplet', {}, True, [], []),
            ('tylko HearThis', {'youtube': '', 'hearthis': 'https://hearthis.at/f/x/'}, True, [], []),
            ('brak daty premiery', {'premiere': None}, False, [], ['X']),
            ('brak linków', {'youtube': ''}, False, ['X'], []),
            ('brak daty i linków', {'premiere': None, 'youtube': ''}, False, ['X'], ['X']),
            ('nieopublikowany', {'status': Audiobook.Status.EDITING}, False, [], []),
        )
        for label, fields, shown, no_links, no_premiere in cases:
            with self.subTest(label):
                audio = published(self.other, 'X', **{'youtube': 'https://youtu.be/x', **fields})
                data = listen_data()
                titles = [n['tytul'] for a in data['antologie'] for n in a['nagrania']]
                self.assertEqual('X' in titles, shown)
                self.assertEqual(data['liczniki']['nagrania'], 5 + shown)
                self.assertEqual([w.split(': ')[1] for w in data['ostrzezenia']['no_links']], no_links)
                self.assertEqual([w.split(': ')[1] for w in data['ostrzezenia']['no_premiere']], no_premiere)
                text = audio.text
                audio.delete()
                text.delete()

    def test_proofreading_names(self):
        """Korekta: wykonawcy etapów w kolejności, bez powtórzeń; bez wykonawców – bieżący korektor; bez profilu
        osoby – imię i nazwisko z konta, nigdy login; nikt – pusty napis."""
        account = get_user_model().objects.create_user(username='login@example.test', first_name='Kamil', last_name='Konto')
        anonymous = get_user_model().objects.create_user(username='tylko-login')
        cases = (
            ('etapy', Audiobook(proofreader=self.reader2), [self.reader, self.reader2, self.reader], 'Jan Testowy, Ewa Druga'),
            ('bieżący korektor', Audiobook(proofreader=self.reader2), [], 'Ewa Druga'),
            ('konto bez profilu', Audiobook(proofreader=account), [], 'Kamil Konto'),
            ('sam login', Audiobook(proofreader=anonymous), [], ''),
            ('nikt', Audiobook(), [], ''),
        )
        for label, audio, performers, expected in cases:
            with self.subTest(label):
                stages = [AudiobookStage(performer=user) for user in performers]
                self.assertEqual(proofreading_names(audio, stages), expected)


class ListenHelpersTests(TestCase):
    def test_media_key_normalizes_links(self):
        """Ten sam klucz dla różnych postaci adresu: youtu.be i watch?v=, www/m., http/https, końcowy „/”,
        dodatkowe parametry; różne nagrania – różne klucze."""
        groups = (
            ('https://www.youtube.com/watch?v=AbC_1', 'https://youtu.be/AbC_1', 'http://youtube.com/watch?v=AbC_1&t=5s',
             'https://m.youtube.com/watch?feature=share&v=AbC_1', 'youtu.be/AbC_1/'),
            ('https://hearthis.at/fantazmaty/tytul/', 'https://www.hearthis.at/fantazmaty/tytul',
             'http://hearthis.at/Fantazmaty/Tytul/?ref=x'),
        )
        for urls in groups:
            with self.subTest(urls[0]):
                self.assertEqual(len({media_key(url) for url in urls}), 1)
        self.assertNotEqual(media_key('https://youtu.be/AbC_1'), media_key('https://youtu.be/abc_1'))
        self.assertEqual(media_key(''), '')

    def test_audiobook_parts(self):
        """Główne linki to część 1, kolejne z additional_links rosnąco; przy powtórzeniu wygrywa główny link."""
        self.assertEqual(audiobook_parts('https://youtu.be/a', '', [
            {'service': 'hearthis', 'part': 3, 'url': 'https://hearthis.at/f/c3/'},
            {'service': 'youtube', 'part': 1, 'url': 'https://youtu.be/inny'},
            {'service': 'hearthis', 'part': 1, 'url': 'https://hearthis.at/f/a/'}]),
            [(1, {'youtube': 'https://youtu.be/a', 'hearthis': 'https://hearthis.at/f/a/'}),
             (3, {'hearthis': 'https://hearthis.at/f/c3/'})])
        self.assertEqual(audiobook_parts('', '', []), [])

    def test_unique_slug_and_recording_validation(self):
        """Identyfikator z nazwy (ł → l, bez diakrytyków, z dopiskiem przy zajętym); wpis nagrania: id karty
        nadawane z audiobooka, wymagany id albo audiobook, czas m:ss/g:mm:ss, audiobook z tej samej antologii."""
        self.assertTrue(SiteListenAnthology.objects.filter(slug='ja-legenda').exists())  # z seedu
        self.assertEqual(unique_slug('Ja, legenda'), 'ja-legenda-2')
        self.assertEqual(unique_slug('Łódź – żółte'), 'lodz-zolte')
        book = Anthology.objects.create(title='A')
        entry = SiteListenAnthology.objects.create(anthology=book, slug='a', title='A')
        audio = published(book, 'T', youtube='https://youtu.be/t')
        recording = SiteListenRecording.objects.create(listen_anthology=entry, audiobook=audio, part=2)
        self.assertEqual(recording.card_id, f'fza-a{audio.pk}-2')
        foreign = published(Anthology.objects.create(title='B'), 'U', youtube='https://youtu.be/u')
        cases = (('bez id i audiobooka', {}, 'card_id'), ('zły czas', {'card_id': 'x', 'youtube_duration': '61'}, 'youtube_duration'),
                 ('złe id', {'card_id': 'fza 1'}, 'card_id'), ('inna antologia', {'audiobook': foreign}, 'audiobook'))
        for label, fields, field in cases:
            with self.subTest(label), self.assertRaises(ValidationError) as caught:
                SiteListenRecording(listen_anthology=entry, **fields).full_clean()
            self.assertIn(field, caught.exception.message_dict)
        SiteListenRecording(listen_anthology=entry, card_id='fza-1', youtube_duration='1:05:24',
                            hearthis_duration='04:39').full_clean()
        with self.assertRaises(ValidationError):
            SiteListenAnthology(slug='x', title='X', cover_url='https://example.com/a.jpg').full_clean()


class ListenSeedTests(TestCase):
    def test_seed_matches_page_json(self):
        """Seed: 11 antologii i 116 nagrań ze strony w kolejności pliku (id, nazwa, okładka; id kart, czasy, tytuły),
        w pustej bazie bez powiązań; adres strony w ustawieniach; wszystkie nagrania w ostrzeżeniu „niedopasowane”."""
        data = json.loads(SEED.read_text(encoding='utf-8'))
        entries = list(SiteListenAnthology.objects.prefetch_related('recordings'))
        self.assertEqual((len(entries), SiteListenRecording.objects.count()), (11, 116))
        self.assertEqual([(e.slug, e.title, e.cover_url, e.position) for e in entries],
                         [(g['id'], g['nazwa'], g['okladka'], n) for n, g in enumerate(data['antologie'], start=1)])
        for entry, group in zip(entries, data['antologie'], strict=True):
            with self.subTest(entry=entry.slug):
                self.assertIsNone(entry.anthology_id)
                self.assertEqual([(r.card_id, r.youtube_duration, r.hearthis_duration, r.source_title, r.source_author,
                                   r.source_youtube, r.source_hearthis, r.audiobook_id) for r in entry.recordings.all()],
                                 [(n['id'], n['czasYoutube'], n['czasHearthis'], n['tytul'], n['autor'], n['youtube'],
                                   n['hearthis'], None) for n in group['nagrania']])
        self.assertEqual(SiteTeamSettings.load().listen_page_url, 'https://fantazmaty.pl/sluchaj/')
        self.assertEqual(len(listen_data()['ostrzezenia']['unmatched']), 116)

    def test_link_matches_links_in_any_form_then_titles(self):
        """Dopasowanie seedu: antologia po nazwie (rzymskie I–IV jak 1–4); nagranie po linku YouTube albo HearThis
        w innej postaci (youtu.be, bez www, bez „/”), część z additional_links, a bez linku – po antologii i tytule
        („, cz. N” to część); niejednoznaczny tytuł zostaje; drugie wywołanie niczego nie zmienia; payload bierze
        id kart i czasy z seedu."""
        data = json.loads(SEED.read_text(encoding='utf-8'))
        group = next(g for g in data['antologie'] if g['nazwa'] == 'Fantazmaty II')
        records = {n['tytul']: n for n in group['nagrania']}
        book = Anthology.objects.create(title='Fantazmaty 2', status=Anthology.Status.READY)
        first, second = records['Incydent helenowski, cz. 1'], records['Incydent helenowski, cz. 2']
        by_link = published(book, 'Inny tytuł w CMS-ie', author='Tomasz Krzywik',
                            youtube=first['youtube'].replace('https://www.youtube.com/watch?v=', 'https://youtu.be/'),
                            extra=[{'service': 'hearthis', 'part': 2, 'url': second['hearthis'].rstrip('/').replace('https://', 'http://www.')}])
        dom = records['Dom']
        by_title = published(book, 'Dom', youtube='https://youtu.be/zmieniony-link')
        for _ in range(2):
            published(book, 'Iskra z Edry', youtube='https://youtu.be/dubel')
        self.assertEqual(link_listen_to_cms(), {'antologie': 1, 'nagrania': 3})
        entry = SiteListenAnthology.objects.get(slug='fantazmaty-2')
        self.assertEqual(entry.anthology, book)
        matched = {r.card_id: (r.audiobook_id, r.part) for r in entry.recordings.exclude(audiobook=None)}
        self.assertEqual(matched, {first['id']: (by_link.pk, 1), second['id']: (by_link.pk, 2), dom['id']: (by_title.pk, 1)})
        self.assertEqual(link_listen_to_cms(), {'antologie': 0, 'nagrania': 0})
        SiteListenAnthology.objects.exclude(pk=entry.pk).delete()
        cards = {n['id']: n for n in listen_payload()['antologie'][0]['nagrania']}
        self.assertEqual((cards[second['id']]['tytul'], cards[second['id']]['czasHearthis']),
                         ('Inny tytuł w CMS-ie, cz. 2', second['czasHearthis']))
        self.assertNotIn('czasYoutube', cards[second['id']])
        self.assertEqual(cards[dom['id']]['czasYoutube'], dom['czasYoutube'])
