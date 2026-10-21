"""Szukajka WWW: katalog opowiadań dla wyszukiwarki na fantazmaty.pl/czytaj/szukaj/.

Ten sam słownik dla endpointu i podglądu w zakładce, dopasowanie wpisów katalogu do antologii i tekstów CMS-u
oraz synchronizacja z CMS-em (plan różnic bez zapisu → zastosowanie w jednej transakcji). Do JSON-a trafiają
wyłącznie tytuły, podpisy autorów, gatunki i tagi – nigdy e-maile ani dane wewnętrzne.

Z CMS-u importowane są tylko antologie Gotowe i niebędące Ekstraktami. Powieść jest jedną pozycją w całości:
wpis katalogu powieści ma dokładnie jedno „opowiadanie” (tytuł powieści, autorzy, gatunki i tagi z ``NovelProfile``),
bez powiązania ``text`` – rozdziały są pomijane.
"""
import hashlib
import json
import unicodedata

from django.db import transaction
from django.utils import timezone

from core.discord_notifications import text_authors
from core.odkurzacz_forms import plural
from core.site_models import SiteCatalogAnthology, SiteCatalogStory, SiteTeamSettings, term_lines
from texts.models import Anthology, Text
from texts.production import active_production_texts
from texts.vocabulary import tokens

QUOTES = str.maketrans({'’': "'", '‘': "'", 'ʼ': "'", '`': "'", '„': '"', '”': '"', '“': '"', '«': '"', '»': '"'})
ROMAN = {'i': '1', 'ii': '2', 'iii': '3', 'iv': '4'}
PLAN_SALT = 'site-catalog-plan'


def fold(value):
    """Klucz porównania: ujednolicone cudzysłowy, bez wielkości liter i diakrytyków, pojedyncze odstępy."""
    value = unicodedata.normalize('NFKD', str(value or '').translate(QUOTES)).casefold().replace('ł', 'l')
    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())


def anthology_key(title):
    """Klucz antologii: jak ``fold``, ale rzymskie I–IV liczą się jak 1–4 („Fantazmaty I” = „Fantazmaty 1”)."""
    return ' '.join(ROMAN.get(word, word) for word in fold(title).split())


def split_genres(value):
    """Gatunki z pola tekstu CMS-u: rozdzielone przecinkiem, średnikiem albo kreską pionową."""
    return tokens((value or '').replace('|', ',').replace(';', ','))


def split_tags(value):
    return tokens(value)


def _fold_set(items):
    return {fold(item) for item in items}


def _missing(new_items, existing):
    """Elementy ``new_items`` nieobecne w ``existing`` (porównanie po ``fold``), pisownia z CMS-u, bez powtórzeń."""
    present, result = _fold_set(existing), []
    for item in new_items:
        if fold(item) not in present:
            present.add(fold(item))
            result.append(item)
    return result


# --- dane dla strony ---------------------------------------------------------------------------------------------

def catalog_anthologies(include_disabled=False):
    queryset = SiteCatalogAnthology.objects.select_related('anthology').prefetch_related('stories')
    return queryset if include_disabled else queryset.filter(is_enabled=True)


def story_entry(story):
    return {'tytul': story.title, 'autor': story.author, 'gatunki': story.genre_list, 'tagi': story.tag_list}


def catalog_counts(anthologies):
    """Liczby jak w szukajce: antologie, opowiadania, unikalne gatunki i tagi (bez wielkości liter i diakrytyków).

    Opowiadanie bez gatunku dostaje na stronie gatunek „do ustalenia”, który liczy się jako osobny.
    """
    stories = [story for item in anthologies for story in item['opowiadania']]
    genres = {fold(genre) for story in stories for genre in story['gatunki']}
    if any(not story['gatunki'] for story in stories):
        genres.add('do ustalenia')
    tags = {fold(tag) for story in stories for tag in story['tagi']}
    return {'antologie': len(anthologies), 'opowiadania': len(stories), 'gatunki': len(genres), 'tagi': len(tags)}


def catalog_payload():
    """Słownik wysyłany stronie: dokładnie format bloku ``#fzs-data`` plus ``wersja`` i ``liczby``."""
    anthologies = [{'nazwa': item.title, 'url': item.url,
                    'opowiadania': [story_entry(story) for story in item.stories.all()]}
                   for item in catalog_anthologies()]
    return {'wersja': timezone.now().isoformat(timespec='seconds'), 'liczby': catalog_counts(anthologies),
            'antologie': anthologies}


def record_catalog_fetch(version):
    SiteTeamSettings.load()
    SiteTeamSettings.objects.filter(pk=1).update(catalog_fetched_at=timezone.now(),
                                                  catalog_fetch_version=version[:40])


def count_text(count, forms):
    return f'{count} {plural(count, *forms)}'


# --- dopasowanie do CMS-u -----------------------------------------------------------------------------------------

def _unique_keys(pairs):
    """{klucz: pk} tylko dla kluczy jednoznacznych (dwie antologie o tym samym kluczu nie są dopasowywane)."""
    keys = {}
    for key, pk in pairs:
        keys.setdefault(key, []).append(pk)
    return {key: pks[0] for key, pks in keys.items() if len(pks) == 1}


def link_catalog_to_cms(apps=None):
    """Dopasowuje wpisy katalogu bez powiązania do antologii (po tytule, rzymskie I–IV = 1–4) i tekstów CMS-u
    (po antologii i tytule, z ujednoliconymi cudzysłowami). Niedopasowane zostają bez powiązania.
    Powieść dopasowuje się tylko po tytule antologii – jej jedyna pozycja nie ma powiązania ``text``.

    ``apps`` to rejestr modeli historycznych z migracji; bez niego używane są modele bieżące.
    Zwraca liczbę nowo powiązanych antologii i opowiadań.
    """
    if apps is None:
        anthology_model, text_model = Anthology, Text
        catalog_model, story_model = SiteCatalogAnthology, SiteCatalogStory
    else:
        anthology_model, text_model = apps.get_model('texts', 'Anthology'), apps.get_model('texts', 'Text')
        catalog_model = apps.get_model('core', 'SiteCatalogAnthology')
        story_model = apps.get_model('core', 'SiteCatalogStory')
    linked = {'antologie': 0, 'opowiadania': 0}
    taken = set(catalog_model.objects.exclude(anthology__isnull=True).values_list('anthology_id', flat=True))
    by_title = _unique_keys((anthology_key(title), pk) for pk, title in anthology_model.objects.values_list('pk', 'title'))
    for pk, title in catalog_model.objects.filter(anthology__isnull=True).values_list('pk', 'title'):
        match = by_title.get(anthology_key(title))
        if match and match not in taken:
            catalog_model.objects.filter(pk=pk).update(anthology_id=match)
            taken.add(match)
            linked['antologie'] += 1
    used = set(story_model.objects.exclude(text__isnull=True).values_list('text_id', flat=True))
    for catalog_pk, anthology_id in catalog_model.objects.exclude(anthology__isnull=True).values_list('pk', 'anthology_id'):
        stories = list(story_model.objects.filter(anthology_id=catalog_pk, text__isnull=True).values_list('pk', 'title'))
        if not stories:
            continue
        texts = _unique_keys((fold(title), pk) for pk, title in text_model.objects.filter(
            anthology_id=anthology_id, chapter_number__isnull=True).values_list('pk', 'title'))
        for pk, title in stories:
            match = texts.get(fold(title))
            if match and match not in used:
                story_model.objects.filter(pk=pk).update(text_id=match)
                used.add(match)
                linked['opowiadania'] += 1
    return linked


# --- synchronizacja z CMS-em ----------------------------------------------------------------------------------------

def is_importable(anthology):
    """Z CMS-u importujemy tylko antologie Gotowe, które nie są Ekstraktami (powieści – tak, w całości)."""
    return bool(anthology and anthology.status == Anthology.Status.READY and not anthology.is_extracts)


def update_blocked_reason(catalog_anthology):
    """Dlaczego wpisu katalogu nie da się aktualizować z CMS-u; pusty napis – da się."""
    anthology = catalog_anthology.anthology
    if anthology is None:
        return 'Antologia nie jest powiązana z CMS-em – powiąż ją w panelu administracyjnym.'
    if anthology.is_extracts:
        return 'Ekstrakty nie trafiają do katalogu szukajki.'
    if not is_importable(anthology):
        return 'Antologia niegotowa – aktualizacja po ukończeniu antologii w CMS-ie.'
    return ''


def require_importable(anthology):
    if anthology is None:
        raise ValueError('Wpis katalogu nie jest powiązany z antologią w CMS-ie.')
    if anthology.is_extracts:
        raise ValueError('Ekstrakty nie trafiają do katalogu szukajki.')
    if anthology.status != Anthology.Status.READY:
        raise ValueError('Antologia w CMS-ie nie ma statusu „Gotowa” – import gatunków i tagów jest możliwy dopiero po jej '
                         'ukończeniu.')


def cms_texts(anthology):
    """Teksty antologii, które trafiają do katalogu: bez wycofanych, rozdziałów powieści i Ekstraktów."""
    if not is_importable(anthology) or anthology.is_novel:
        return Text.objects.none()
    return active_production_texts(Text.objects.filter(anthology=anthology, chapter_number__isnull=True)).exclude(
        import_source='extract-volume-v2').select_related('anthology').prefetch_related(
        'authors', 'translation__foreign_authors').order_by('title', 'pk')


def story_warnings(entry):
    warnings = []
    if not entry['gatunki']:
        warnings.append('brak gatunku')
    if not entry['tagi']:
        warnings.append('brak tagów')
    if not entry['autor']:
        warnings.append('brak autora')
    return warnings


def cms_entry(text):
    """Opowiadanie z CMS-u w formacie katalogu (autor jak w stopce: pseudonim albo imię i nazwisko)."""
    entry = {'text_id': text.pk, 'tytul': text.title, 'autor': text_authors(text),
             'gatunki': split_genres(text.genre), 'tagi': split_tags(text.tags)}
    entry['ostrzezenia'] = story_warnings(entry)
    return entry


def novel_entry(anthology):
    """Powieść jako jedna pozycja: tytuł antologii, autorzy, gatunki i tagi z ``NovelProfile`` (rozdziały pomijane)."""
    profile = getattr(anthology, 'novel', None)
    names = []
    for author in (profile.authors.all() if profile else []):
        name = author.display_name.strip()
        if name and name not in names:
            names.append(name)
    entry = {'text_id': None, 'tytul': anthology.title, 'autor': ', '.join(names),
             'gatunki': split_genres(profile.genre if profile else ''), 'tagi': split_tags(profile.tags if profile else ''),
             'powiesc': True}
    entry['ostrzezenia'] = story_warnings(entry)
    return entry


def cms_entries(anthology):
    """Pozycje katalogu z CMS-u: opowiadania antologii albo jedna pozycja powieści."""
    if anthology.is_novel:
        return [novel_entry(anthology)]
    return [cms_entry(text) for text in cms_texts(anthology)]


def candidate_anthologies():
    """Gotowe antologie CMS-u (bez Ekstraktów; powieści w całości), których nie ma jeszcze w katalogu."""
    return Anthology.objects.filter(status=Anthology.Status.READY, is_extracts=False,
                                    site_catalog__isnull=True).order_by('title', 'pk')


def plan_add(anthology):
    """Nowa antologia z CMS-u: wszystkie jej teksty jak w ``cms_entry`` (powieść – jedna pozycja); bez zapisu."""
    if hasattr(anthology, 'site_catalog'):
        raise ValueError('Antologia jest już w katalogu.')
    require_importable(anthology)
    return {'anthology': anthology, 'stories': cms_entries(anthology)}


def _create_story(catalog_anthology, entry, position):
    return SiteCatalogStory.objects.create(
        anthology=catalog_anthology, text_id=entry['text_id'], title=entry['tytul'], author=entry['autor'],
        genres='\n'.join(entry['gatunki']), tags='\n'.join(entry['tagi']), position=position)


@transaction.atomic
def apply_add(anthology, url, plan=None):
    """Dodaje antologię na koniec katalogu (włączoną) z opowiadaniami z planu."""
    plan = plan or plan_add(anthology)
    last = SiteCatalogAnthology.objects.order_by('-position').values_list('position', flat=True).first() or 0
    catalog_anthology = SiteCatalogAnthology.objects.create(anthology=anthology, title=anthology.title, url=url,
                                                            position=last + 1)
    for position, entry in enumerate(plan['stories'], start=1):
        _create_story(catalog_anthology, entry, position)
    return catalog_anthology


def plan_update(catalog_anthology):
    """Różnice między katalogiem a CMS-em bez zapisu: nowe opowiadania, brakujące gatunki i tagi, rozbieżne
    tytuły i autorzy (tylko raport), wpisy bez tekstu w CMS-ie. Tylko antologie Gotowe spoza Ekstraktów
    (``ValueError``); powieść porównywana jako jedna pozycja z pierwszym wpisem."""
    anthology = catalog_anthology.anthology
    require_importable(anthology)
    stories = list(catalog_anthology.stories.all())
    by_text = {story.text_id: story for story in stories if story.text_id}
    plan = {'anthology': catalog_anthology, 'new_stories': [], 'updates': [], 'reports': [], 'unlinked': [],
            'missing': [], 'novel': anthology.is_novel}
    seen = set()
    for entry in cms_entries(anthology):
        if anthology.is_novel:
            story = stories[0] if stories else None
        else:
            seen.add(entry['text_id'])
            story = by_text.get(entry['text_id'])
        if story is None:
            plan['new_stories'].append(entry)
            continue
        new_genres = _missing(entry['gatunki'], story.genre_list)
        new_tags = _missing(entry['tagi'], story.tag_list)
        if new_genres or new_tags:
            plan['updates'].append({'story': story, 'new_genres': new_genres, 'new_tags': new_tags})
        new_title = entry['tytul'] if fold(entry['tytul']) != fold(story.title) else ''
        new_author = entry['autor'] if entry['autor'] and fold(entry['autor']) != fold(story.author) else ''
        if new_title or new_author:
            plan['reports'].append({'story': story, 'new_title': new_title, 'new_author': new_author})
    if not anthology.is_novel:
        plan['unlinked'] = [story for story in stories if not story.text_id]
        plan['missing'] = [story for story in stories if story.text_id and story.text_id not in seen]
    plan['has_changes'] = bool(plan['new_stories'] or plan['updates'])
    return plan


def plan_digest(plan):
    """Skrót zmian do zapisu (nowe opowiadania, gatunki, tagi): zastosowanie wymaga tego samego planu co podgląd."""
    data = {'new': [entry['text_id'] for entry in plan.get('new_stories', plan.get('stories', []))],
            'updates': [(c['story'].pk, c['new_genres'], c['new_tags']) for c in plan.get('updates', [])]}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


@transaction.atomic
def apply_update(plan):
    """Dopisuje nowe opowiadania i brakujące gatunki/tagi; nigdy nie usuwa ani nie nadpisuje istniejących."""
    catalog_anthology = plan['anthology']
    added = updated = 0
    position = catalog_anthology.stories.order_by('-position').values_list('position', flat=True).first() or 0
    for entry in plan['new_stories']:
        position += 1
        _create_story(catalog_anthology, entry, position)
        added += 1
    for change in plan['updates']:
        story = change['story']
        story.genres = '\n'.join([*story.genre_list, *change['new_genres']])
        story.tags = '\n'.join([*story.tag_list, *change['new_tags']])
        story.save(update_fields=['genres', 'tags'])
        updated += 1
    if added or updated:
        catalog_anthology.save(update_fields=['updated_at'])
    return {'added': added, 'updated': updated}


def plan_update_all():
    """``plan_update`` dla każdego wpisu katalogu, który da się aktualizować (bez zapisu): ``changed`` – plany ze
    zmianami, ``unchanged`` – bez zmian, ``skipped`` – pominięte wpisy z powodem (niepowiązane, niegotowe)."""
    overview = {'changed': [], 'unchanged': [], 'skipped': []}
    for item in catalog_anthologies(include_disabled=True):
        reason = update_blocked_reason(item)
        if reason:
            overview['skipped'].append({'anthology': item, 'reason': reason})
            continue
        plan = plan_update(item)
        overview['changed' if plan['has_changes'] else 'unchanged'].append(plan)
    return overview


def plans_digest(plans):
    """Skrót zestawu planów (wpis katalogu + ``plan_digest``): zbiorcze zastosowanie wymaga tego samego podglądu."""
    data = [[plan['anthology'].pk, plan_digest(plan)] for plan in plans]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:32]


@transaction.atomic
def apply_update_all(plans):
    """Stosuje wszystkie plany w jednej transakcji (``apply_update`` po kolei); zwraca liczniki zmian."""
    totals = {'anthologies': 0, 'added': 0, 'updated': 0, 'genres': 0, 'tags': 0}
    for plan in plans:
        result = apply_update(plan)
        totals['anthologies'] += 1
        totals['added'] += result['added']
        totals['updated'] += result['updated']
        totals['genres'] += sum(len(change['new_genres']) for change in plan['updates'])
        totals['tags'] += sum(len(change['new_tags']) for change in plan['updates'])
    return totals


# --- ostrzeżenia -----------------------------------------------------------------------------------------------------

def catalog_warnings():
    """Ostrzeżenia zakładki: antologia włączona bez adresu, opowiadania bez gatunku i bez tagów, gotowe antologie
    CMS-u poza katalogiem, wpisy bez powiązania z CMS-em (informacyjnie), powtórzone tytuły w antologii."""
    warnings = {'no_url': [], 'no_genre': [], 'no_tags': [], 'candidates': [], 'unlinked_anthologies': [],
                'unlinked_stories': [], 'duplicates': []}
    for item in catalog_anthologies(include_disabled=True):
        novel = bool(item.anthology_id and item.anthology.is_novel)
        if item.is_enabled and not item.url:
            warnings['no_url'].append(item.title)
        if not item.anthology_id:
            warnings['unlinked_anthologies'].append(item.title)
        titles = {}
        for story in item.stories.all():
            label = f'{item.title}: {story.title}'
            if not term_lines(story.genres):
                warnings['no_genre'].append(label)
            if not term_lines(story.tags):
                warnings['no_tags'].append(label)
            if not story.text_id and not novel:
                warnings['unlinked_stories'].append(label)
            titles.setdefault(fold(story.title), []).append(story.title)
        warnings['duplicates'].extend(f'{item.title}: {names[0]} ({len(names)}×)'
                                      for names in titles.values() if len(names) > 1)
    warnings['candidates'] = [anthology.title for anthology in candidate_anthologies()]
    return warnings
