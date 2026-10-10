from core.public_authors import name_matches, review_name_matches
from core.translation_scope import ordinary, non_abandoned
from core.author_access import contact_authors
from core.permissions import is_coordinator, can_view_review_archive
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.shortcuts import render
from django.urls import reverse
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


def _search_texts(query, *, include_authors):
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

    results = []

    for text in queryset[:RESULT_LIMIT]:
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

    return results


def _search_reviews(query, *, include_authors, include_archived=True):
    fields = ["title", "anthology__title"]

    condition = Q()
    for term in query.split():
        match = _matching_terms(term, fields)
        if include_authors:
            match |= review_name_matches(term)
        condition &= match

    queryset = (
        ordinary(Review.objects).filter(
            **({} if include_authors else {"is_hidden": False}),
        )
        .filter(condition)
        .select_related("anthology", "author").prefetch_related("coauthors")
        .order_by("title", "pk")
    )
    if not include_archived:
        queryset = queryset.filter(old_reviews=False)
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
                    "author_display_name": review.author_display_name,
                    "author_first_name": review.author_first_name,
                    "author_last_name": review.author_last_name,
                    "email": review.email,
                }
            )

        results.append(result)

    return results


def _search_authors(query, user):
    from core.public_authors import public_name
    from core.sort_keys import sql_text_key
    authors = contact_authors(user).annotate(public_signature=public_name())
    for term in query.split():
        authors = authors.filter(name_matches(term, email=True))
    authors = authors.annotate(_public_order=sql_text_key('public_signature', authors.db)).order_by('_public_order', 'pk')

    return [_author_data(author) for author in authors[:RESULT_LIMIT]]


def _search_people(query, user):
    people = (
        Person.objects.active()
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
    people = list(rank_people(people, query, include_email=coordinator)[:RESULT_LIMIT])
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
    return [
        _NamedResult(
            pk=person.pk,
            first_name=person.first_name,
            last_name=person.last_name,
            email=person.email if coordinator or (person.email or "").casefold() not in protected_emails or (person.email or "").casefold() in allowed_emails else "",
            roles={
                "all": [
                    {"pk": role.pk, "name": role.name}
                    for role in person.roles.all()
                ],
            },
        )
        for person in people[:RESULT_LIMIT]
    ]


def _search_anthologies(query, *, novels=False):
    anthologies = (
        (Anthology.objects.filter(is_novel=True).exclude(status='abandoned') if novels else non_abandoned(Anthology.objects).filter(is_novel=False)).filter(_matching_terms(query, ("title",)))
        .order_by("title", "pk")
    )

    return [
        _anthology_data(anthology)
        for anthology in anthologies[:RESULT_LIMIT]
    ]


def _additional_results(query, user):
    from core.models import AudioContributor, Recruitment
    from illustrations.models import Illustrator
    groups = []
    coordinator = is_coordinator(user)
    fields = ('name', 'email') if coordinator else ('name',)
    contacts = AudioContributor.objects.filter(_matching_terms(query, fields)).order_by('name', 'pk')
    groups.append({'label': 'Lektorzy i montaż', 'items': [
        {'label': person.name, 'url': reverse('core:audio_contributor', args=[person.pk])}
        for person in contacts[:RESULT_LIMIT]]})
    if coordinator:
        artists = Illustrator.objects.all()
        for term in query.split():
            artists = artists.filter(_matching_terms(term, ('first_name', 'last_name', 'pseudonym', 'email')))
        groups.append({'label': 'Ilustratorzy (także nieaktywni)', 'items': [
            {'label': str(person) + (f' ({person.pseudonym})' if person.pseudonym else ''), 'url': reverse('illustrations:illustrator_edit', args=[person.pk])}
            for person in artists.order_by('last_name', 'first_name', 'pk')[:RESULT_LIMIT]]})
        applications = Recruitment.objects.filter(_matching_terms(query,
            ('first_name', 'last_name', 'applicant_name', 'email', 'mail_subject'))).order_by('-submitted_at', '-pk')
        groups.append({'label': 'Rekrutacja', 'items': [
            {'label': application.subject_name or str(application),
             'url': reverse('core:recruitment_detail', args=[application.pk])}
            for application in applications[:RESULT_LIMIT]]})
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
                        include_authors=include_authors,
                    ),
                    "reviews": _search_reviews(
                        query,
                        include_authors=include_authors,
                        include_archived=can_view_review_archive(request.user),
                    ),
                    "authors": (
                        _search_authors(query, request.user)
                    ),
                    "people": _search_people(query, request.user),
                    "additional_results": _additional_results(query, request.user),
                    "anthologies": _search_anthologies(query),
                    "novels": _search_anthologies(query, novels=True),
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
