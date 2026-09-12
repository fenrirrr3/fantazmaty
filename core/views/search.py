from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from authors.models import Author
from core.forms import GlobalSearchForm
from core.permissions import can_view_author_data, team_member_required
from people.models import Person, Role
from texts.models import Anthology, Review, Text


RESULT_LIMIT = 50


class _NamedResult(dict):
    """Zachowuje obsługę {{ author }} i {{ person }} w szablonie."""

    def __str__(self):
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
        first_name=author.first_name,
        last_name=author.last_name,
        pseudonym=author.pseudonym,
        email=author.email,
        contact=author.contact,
    )


def _search_texts(query, *, include_authors):
    fields = ["title", "anthology__title"]

    if include_authors:
        fields.extend(
            [
                "authors__first_name",
                "authors__last_name",
                "authors__pseudonym",
                "authors__email",
            ]
        )

    queryset = (
        Text.objects.filter(_matching_terms(query, fields))
        .select_related("anthology")
        .order_by("title", "pk")
    )

    if include_authors:
        queryset = queryset.distinct().prefetch_related("authors")

    results = []

    for text in queryset[:RESULT_LIMIT]:
        results.append(
            {
                "pk": text.pk,
                "title": text.title,
                "anthology": _anthology_data(text.anthology),
                "authors": {
                    "all": (
                        [_author_data(author) for author in text.authors.all()]
                        if include_authors
                        else []
                    ),
                },
            }
        )

    return results


def _search_reviews(query, *, include_authors):
    fields = ["title", "anthology__title"]

    if include_authors:
        fields.extend(
            [
                "author_first_name",
                "author_last_name",
                "email",
                "author__first_name",
                "author__last_name",
                "author__pseudonym",
                "author__email",
            ]
        )

    queryset = (
        Review.objects.filter(
            old_reviews=False,
            **({} if include_authors else {"is_hidden": False}),
        )
        .filter(_matching_terms(query, fields))
        .select_related("anthology")
        .order_by("title", "pk")
    )
    results = []

    for review in queryset[:RESULT_LIMIT]:
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
                    "author_first_name": review.author_first_name,
                    "author_last_name": review.author_last_name,
                    "email": review.email,
                }
            )

        results.append(result)

    return results


def _search_authors(query):
    authors = (
        Author.objects.filter(
            _matching_terms(
                query,
                (
                    "first_name",
                    "last_name",
                    "pseudonym",
                    "email",
                ),
            )
        )
        .order_by("last_name", "first_name", "pk")
    )

    return [_author_data(author) for author in authors[:RESULT_LIMIT]]


def _search_people(query):
    people = (
        Person.objects.filter(is_active=True)
        .filter(
            _matching_terms(
                query,
                (
                    "first_name",
                    "last_name",
                    "email",
                    "dropbox_email",
                    "roles__name",
                ),
            )
        )
        .prefetch_related(
            Prefetch(
                "roles",
                queryset=Role.objects.order_by("name", "pk"),
            )
        )
        .distinct()
        .order_by("last_name", "first_name", "pk")
    )

    return [
        _NamedResult(
            pk=person.pk,
            first_name=person.first_name,
            last_name=person.last_name,
            email=person.email,
            dropbox_email=person.dropbox_email,
            roles={
                "all": [
                    {"pk": role.pk, "name": role.name}
                    for role in person.roles.all()
                ],
            },
        )
        for person in people[:RESULT_LIMIT]
    ]


def _search_anthologies(query):
    anthologies = (
        Anthology.objects.filter(_matching_terms(query, ("title",)))
        .order_by("title", "pk")
    )

    return [
        _anthology_data(anthology)
        for anthology in anthologies[:RESULT_LIMIT]
    ]


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
        "reviews": [],
        "authors": [],
        "people": [],
        "anthologies": [],
        "can_view_authors": include_authors,
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
                        include_authors=include_authors,
                    ),
                    "reviews": _search_reviews(
                        query,
                        include_authors=include_authors,
                    ),
                    "authors": (
                        _search_authors(query) if include_authors else []
                    ),
                    "people": _search_people(query),
                    "anthologies": _search_anthologies(query),
                }
            )

    # Wyniki zawierają wyłącznie jawnie wybrane wartości. Szablon
    # nie otrzymuje obiektów ORM pozwalających przejść do danych
    # autora przez relacje recenzji, tekstu lub konta użytkownika.
    return render(
        request,
        "core/global_search.html",
        context,
        status=400 if form.is_bound and form.errors else 200,
    )
