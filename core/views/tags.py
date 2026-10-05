"""Accepted texts with their own tags and genre."""
from django.contrib.auth.decorators import login_required
from django.db import connections
from django.db.models import F, OuterRef, Prefetch, Q, Subquery, Value, Exists, TextField
from django.db.models.functions import Concat, Collate, Lower, Replace, Trim
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from authors.models import Author
from texts.models import Anthology, Text
from core.pagination import paginate_items
from core.permissions import team_member_required
from core.sort_keys import REPLACEMENTS


def polish_key(field, using):
    # SQLite's LOWER handles ASCII only; normalize Polish capitals first.
    value = F(field)
    for letter, _ in REPLACEMENTS:
        value = Replace(value, Value(letter.upper()), Value(letter), output_field=TextField())
    value = Trim(Lower(value))
    for letter, replacement in REPLACEMENTS:
        value = Replace(value, Value(letter), Value(replacement), output_field=TextField())
    vendor = connections[using].vendor
    return Collate(value, 'utf8mb4_bin' if vendor == 'mysql' else 'BINARY' if vendor == 'sqlite' else 'C')


COLUMNS = {'Antologia': 'anthology', 'Tytuł': 'title', 'Imię i nazwisko autora': 'author',
           'Tagi': 'tags', 'Gatunek': 'genre'}


class TagsTable:
    def __init__(self, queryset):
        self.queryset = queryset

    def sort_table(self, request):
        selected = request.GET.get('sort', 'title')
        fields = {'anthology': ('anthology__title',), 'title': ('title',),
                  'author': ('tag_author_last', 'tag_author_first'), 'tags': ('tags',), 'genre': ('tag_genre',)}
        key = selected.lstrip('-')
        if key not in fields:
            key, selected = 'title', 'title'
        query = self.queryset
        ordering = []
        for i, field in enumerate(fields[key]):
            name = f'_tag_order_{i}'
            query = query.annotate(**{name: polish_key(field, query.db)})
            ordering.append(F(name).desc(nulls_last=True) if selected.startswith('-') else F(name).asc(nulls_last=True))
        return query.order_by(*ordering, 'pk'), COLUMNS


def positive_id(value):
    return int(value) if value.isascii() and value.isdecimal() and len(value) <= 18 and int(value) > 0 else None


@never_cache
@login_required
@require_GET
@team_member_required
def tag_list(request):
    authors = Author.objects.only('pk', 'first_name', 'last_name').order_by('last_name', 'first_name', 'pk')
    first_author = Author.objects.filter(texts=OuterRef('pk')).annotate(
        _last=polish_key('last_name', Text.objects.db), _first=polish_key('first_name', Text.objects.db),
    ).order_by('_last', '_first', 'pk')
    base = Text.objects.select_related('anthology').prefetch_related(Prefetch('authors', queryset=authors)).annotate(
        tag_genre=F('genre'),
        tag_author_last=Subquery(first_author.values('last_name')[:1]),
        tag_author_first=Subquery(first_author.values('first_name')[:1]),
    )
    query = base
    search = request.GET.get('q', '').strip()[:200]
    anthology = request.GET.get('anthology', '').strip()
    author = request.GET.get('author', '').strip()
    genre = request.GET.get('genre', '').strip()[:255]
    tag = request.GET.get('tag', '').strip()[:200]
    filled = request.GET.get('filled', '')
    if search:
        matching_authors = Author.objects.filter(texts=OuterRef('pk')).annotate(
            full_name=Concat('first_name', Value(' '), 'last_name'),
            reversed_name=Concat('last_name', Value(' '), 'first_name'),
        ).filter(Q(full_name__plcontains=search) | Q(reversed_name__plcontains=search))
        query = query.annotate(_author_matches=Exists(matching_authors)).filter(
            Q(title__plcontains=search) | Q(anthology__title__plcontains=search)
            | Q(_author_matches=True) | Q(tags__plcontains=search) | Q(tag_genre__plcontains=search))
    if anthology == 'none':
        query = query.filter(anthology__isnull=True)
    elif anthology:
        query = query.filter(anthology_id=positive_id(anthology)) if positive_id(anthology) else query.none()
    if author:
        query = query.filter(authors__pk=positive_id(author)) if positive_id(author) else query.none()
    if genre:
        query = query.filter(tag_genre='' if genre == '__missing__' else genre)
    if tag:
        query = query.filter(tags__plcontains=tag)
    if filled == 'yes':
        query = query.exclude(tags='')
    elif filled == 'no':
        query = query.filter(tags='')
    page = paginate_items(request, TagsTable(query.distinct()))
    return render(request, 'core/tag_list.html', {
        'page_obj': page, 'search': search, 'selected_anthology': anthology, 'selected_author': author,
        'selected_genre': genre, 'selected_tag': tag, 'selected_filled': filled,
        'selected_sort': request.GET.get('sort', 'title'),
        'anthologies': Anthology.objects.filter(texts__isnull=False).only('pk', 'title').distinct().order_by('title', 'pk'),
        'authors': authors.filter(texts__isnull=False).distinct(),
        'genres': base.exclude(tag_genre='').order_by('tag_genre').values_list('tag_genre', flat=True).distinct(),
    })
