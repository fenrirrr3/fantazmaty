from core.public_authors import name_matches, review_name_matches
from core.translation_scope import frontend_scope, non_abandoned
from core.author_access import contact_authors
from core.permissions import is_coordinator
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from authors.models import Author
from core.forms import GlobalSearchForm
from core.permissions import can_view_author_data, team_member_required
from people.models import Person, Role
from texts.models import Anthology, Extract, Review, Text


RESULT_LIMIT = 50


class SearchResults(list):
    def __init__(self, items, page):
        super().__init__(items)
        self.page = page
        self.total = page.paginator.count


def _page(queryset, number):
    return Paginator(queryset, RESULT_LIMIT).get_page(number)


def _page_results(queryset, number, project):
    page = _page(queryset, number)
    return SearchResults([project(item) for item in page], page)


def _pagination_links(results, request, key):
    params = request.GET.copy()
    for attr, number in [('previous_url', results.page.previous_page_number() if results.page.has_previous() else None),
                         ('next_url', results.page.next_page_number() if results.page.has_next() else None)]:
        if number is None:
            setattr(results, attr, '')
        else:
            params[key + '_page'] = str(number)
            setattr(results, attr, '?' + params.urlencode() + '#search-' + key + '-heading')


class _NamedResult(dict):
    """Zachowuje obsługę {{ author }} i {{ person }} w szablonie."""

    def __str__(self):
        if self.get("display_name"):
            return self["display_name"]
        return " ".join(
            part
            for part in (
                self.get("first_name", ""),
                self.get("last_name", ""),
            )
            if part
        )


def _matching_terms(query, fields):
    """Każdy człon zapytania musi pasować do jednego z dozwolonych pól."""
    condition = Q()

    for term in query.split():
        term_condition = Q()

        for field in fields:
            term_condition |= Q(**{f"{field}__plcontains": term})

        condition &= term_condition

    return condition


def _anthology_data(anthology):
    if anthology is None:
        return None

    return {
        "pk": anthology.pk,
        "title": anthology.title,
        "status": anthology.status,
        "get_status_display": anthology.get_status_display(),
    }


def _author_data(author):
    # Oznaczenie czarnej listy pozostaje wyłącznie w panelu admina.
    return _NamedResult(
        pk=author.pk,
        display_name=author.display_name,
        first_name=author.first_name,
        last_name=author.last_name,
        pseudonym=author.pseudonym,
        email=author.email,
        contact=author.contact,
    )


def _search_texts(query, *, include_authors, page=1):
    fields = ["title", "anthology__title"]

    condition = Q()
    for term in query.split():
        match = _matching_terms(term, fields)
        if include_authors:
            match |= name_matches(term, 'authors__', email=True)
            match |= name_matches(term, 'translation__foreign_authors__')
            match |= name_matches(term, 'translation__translators__')
        condition &= match

    queryset = (
        non_abandoned(Text.objects).filter(condition)
        .select_related("anthology")
        .order_by("title", "pk")
    )

    queryset = queryset.distinct().select_related("translation").prefetch_related("authors", "translation__foreign_authors")

    page_obj = _page(queryset, page)
    results = []

    for text in page_obj:
        translated = bool(text.anthology_id and text.anthology.is_translated)
        record = getattr(text, 'translation', None) if translated else None
        authors = list(record.foreign_authors.all()) if record else list(text.authors.all())
        results.append(
            {
                "pk": text.pk,
                "title": text.title,
                "url": reverse("core:translation_detail" if translated else "core:assigned_text_detail", args=[text.pk]),
                "anthology": _anthology_data(text.anthology),
                "authors": {
                    "all": (
                        ([str(author.display_name) for author in authors] if translated else [_author_data(author) for author in authors])
                        if include_authors
                        else [author.display_name
                              for author in authors]
                    ),
                },
            }
        )

    return SearchResults(results, page_obj)


def _search_reviews(query, *, user, include_authors, page=1):
    """Current and decided submissions; withdrawn ones stay in the admin panel."""
    fields = ["title", "anthology__title"]

    condition = Q()
    for term in query.split():
        match = _matching_terms(term, fields)
        if include_authors:
            match |= review_name_matches(term)
        condition &= match

    # The same visibility as the submission page, so every result can be opened.
    queryset = (
        frontend_scope(Review.objects.accessible_to(user))
        .filter(condition)
        .select_related("anthology", "author").prefetch_related("coauthors")
        .order_by("title", "pk")
    )
    page_obj = _page(queryset, page)
    results = []

    for review in page_obj:
        result = {
            "pk": review.pk,
            "title": review.title,
            "anthology": _anthology_data(review.anthology),
            "status": review.status,
            "get_status_display": review.get_status_display(),
        }

        if include_authors:
            result.update(
                {
                    "author_display_name": review.author_display_name,
                    "author_first_name": review.author_first_name,
                    "author_last_name": review.author_last_name,
                    "email": review.email,
                }
            )

        results.append(result)

    return SearchResults(results, page_obj)


def _search_authors(query, user, page=1):
    from core.public_authors import public_name
    from core.sort_keys import sql_text_key
    authors = contact_authors(user).annotate(public_signature=public_name())
    for term in query.split():
        authors = authors.filter(name_matches(term, email=True))
    authors = authors.annotate(_public_order=sql_text_key('public_signature', authors.db)).order_by('_public_order', 'pk')

    return _page_results(authors, page, _author_data)


def _search_extracts(query, user, page=1):
    # The informational Extract register is restricted to coordinators.
    if not is_coordinator(user):
        return []
    from texts.extract_data import normalize_key, split_list

    queryset = Extract.objects.select_related('author')
    for term in query.split():
        queryset = queryset.filter(
            name_matches(term, 'author__', email=True)
            | _matching_terms(term, ('title', 'recruitment'))
        )

    def project(item):
        accepted = {normalize_key(title) for title in split_list(item.accepted_titles)}
        rejected = {normalize_key(title) for title in split_list(item.rejected_titles)}
        return {
            'pk': item.pk, 'author': item.author.display_name, 'recruitment': item.recruitment,
            'url': reverse('core:extract_edit', args=[item.pk]),
            'titles': [{'title': title, 'status': 'Przyjęty' if normalize_key(title) in accepted
                        else 'Odrzucony' if normalize_key(title) in rejected else 'Bez decyzji'}
                       for title in split_list(item.title)],
        }

    return _page_results(queryset.order_by('recruitment', 'pk'), page, project)


def _search_people(query, user, page=1):
    people = (
        Person.objects.all().select_related("user")
        .prefetch_related(
            Prefetch(
                "roles",
                queryset=Role.objects.order_by("name", "pk"),
            )
        )
        .distinct()
        .order_by("last_name", "first_name", "pk")
    )

    from core.search_people import rank_people
    coordinator = is_coordinator(user)
    page_obj = _page(rank_people(people, query, include_email=coordinator), page)
    people = list(page_obj)
    allowed_emails, protected_emails = set(), set()
    if not coordinator and people:
        # Bound contact checks to displayed matches, keeping case-insensitive matching.
        from django.db.models.functions import Lower
        emails = {(person.email or "").lower() for person in people}
        def matching_emails(queryset):
            return {email.casefold() for email in queryset.annotate(
                contact_email=Lower("email"),
            ).filter(contact_email__in=emails).values_list("email", flat=True) if email}
        protected_emails = matching_emails(Author.objects.all())
        allowed_emails = matching_emails(contact_authors(user))
    def state(person):
        if person.is_external:
            return "Zewnętrzny"
        return "Nieaktywny" if not person.is_active or (person.user_id and not person.user.is_active) else ""

    return SearchResults([
        _NamedResult(
            pk=person.pk,
            member_state=state(person),
            first_name=person.first_name,
            last_name=person.last_name,
            # Like the profile page: contact data of former members is not shown.
            email="" if state(person) else (person.email if coordinator or (person.email or "").casefold() not in protected_emails or (person.email or "").casefold() in allowed_emails else ""),
            roles={
                "all": [
                    {"pk": role.pk, "name": role.name}
                    for role in person.roles.all()
                ],
            },
        )
        for person in people
    ], page_obj)


def _search_anthologies(query, *, novels=False, page=1):
    anthologies = (
        (Anthology.objects.filter(is_novel=True).exclude(status='abandoned') if novels else non_abandoned(Anthology.objects).filter(is_novel=False)).filter(_matching_terms(query, ("title",)))
        .order_by("title", "pk")
    )

    return _page_results(anthologies, page, _anthology_data)


def _additional_results(query, user, pages=None):
    from core.models import AudioContributor, Recruitment
    from illustrations.models import Illustrator
    pages = pages or {}
    coordinator = is_coordinator(user)
    fields = ('name', 'email') if coordinator else ('name',)
    contacts = AudioContributor.objects.filter(_matching_terms(query, fields)).order_by('name', 'pk')
    groups = [{'key': 'audio', 'label': 'Lektorzy i montaż', 'empty': 'Brak pasujących lektorów i osób odpowiedzialnych za montaż.',
        'items': _page_results(contacts, pages.get('audio_page', 1), lambda person: {
            'label': person.name, 'url': reverse('core:audio_contributor', args=[person.pk])})}]
    if coordinator:
        artists = Illustrator.objects.all()
        for term in query.split():
            artists = artists.filter(_matching_terms(term, ('first_name', 'last_name', 'pseudonym', 'email')))
        groups.append({'key': 'illustrators', 'label': 'Ilustratorzy', 'empty': 'Brak pasujących ilustratorów.',
            'items': _page_results(artists.order_by('last_name', 'first_name', 'pk'), pages.get('illustrators_page', 1), lambda person: {
                'label': str(person) + (f' ({person.pseudonym})' if person.pseudonym else ''),
                'url': reverse('illustrations:illustrator_edit', args=[person.pk])})})
        applications = Recruitment.objects.filter(_matching_terms(query,
            ('first_name', 'last_name', 'applicant_name', 'email', 'mail_subject'))).order_by('-submitted_at', '-pk')
        groups.append({'key': 'recruitment', 'label': 'Rekrutacja', 'empty': 'Brak pasujących zgłoszeń rekrutacyjnych.',
            'items': _page_results(applications, pages.get('recruitment_page', 1), lambda application: {
                'label': application.subject_name or str(application), 'url': reverse('core:recruitment_detail', args=[application.pk])})})
    return groups


@never_cache
@login_required
@require_GET
@team_member_required
def global_search(request):
    data = request.GET.copy()

    if "query" not in data and "q" in data:
        data["query"] = data.get("q", "")

    form = GlobalSearchForm(
        data if "query" in data else None,
        user=request.user,
    )
    include_authors = can_view_author_data(request.user)

    context = {
        "form": form,
        "query": "",
        "texts": [],
        "extracts": [],
        "reviews": [],
        "authors": [],
        "people": [],
        "anthologies": [],
        "novels": [],
        "can_view_authors": include_authors,
        "can_search_authors": True,
        "can_view_author_data": include_authors,
        "result_limit": RESULT_LIMIT,
    }

    if form.is_bound and form.is_valid():
        query = form.cleaned_data["query"].strip()
        context["query"] = query

        # Puste zapytanie nie może zwracać całej bazy.
        if query:
            context.update(
                {
                    "texts": _search_texts(
                        query,
                        include_authors=include_authors, page=request.GET.get("texts_page", 1),
                    ),
                    "reviews": _search_reviews(
                        query, user=request.user,
                        include_authors=include_authors, page=request.GET.get("reviews_page", 1),
                    ),
                    "authors": (
                        _search_authors(query, request.user, page=request.GET.get("authors_page", 1))
                    ),
                    "people": _search_people(query, request.user, page=request.GET.get("people_page", 1)),
                    "extracts": _search_extracts(query, request.user, page=request.GET.get("extracts_page", 1)),
                    "additional_results": _additional_results(query, request.user, request.GET),
                    "anthologies": _search_anthologies(query, page=request.GET.get("anthologies_page", 1)),
                    "novels": _search_anthologies(query, novels=True, page=request.GET.get("novels_page", 1)),
                }
            )

    for key in ('texts', 'extracts', 'reviews', 'authors', 'people', 'anthologies', 'novels'):
        if isinstance(context[key], SearchResults):
            _pagination_links(context[key], request, key)
    for group in context.get('additional_results', []):
        _pagination_links(group['items'], request, group['key'])

    context['has_results'] = any(context[key] for key in
        ('texts', 'extracts', 'reviews', 'authors', 'people', 'anthologies', 'novels')) or any(
        group['items'] for group in context.get('additional_results', []))

    # Wyniki zawierają wyłącznie jawnie wybrane wartości. Szablon
    # nie otrzymuje obiektów ORM pozwalających przejść do danych
    # autora przez relacje recenzji, tekstu lub konta użytkownika.
    return render(
        request,
        "core/global_search.html",
        context,
        status=400 if form.is_bound and form.errors else 200,
    )
