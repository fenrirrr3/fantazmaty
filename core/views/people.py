from people.role_ordering import ordered_team_roles
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.forms import PeopleFilterForm
from core.pagination import paginate_items
from core.permissions import (
    can_view_author_data, is_superuser, team_member_required,
)
from core.selectors.people import (user_leave_information, profile_assignments as _profile_assignments, imported_work_summary as _imported_work_summary)
from people.models import Person, Role
from texts.models import ReviewAssignment


def _filter_data(request):
    """Zachowuje obsługę wcześniejszych adresów z parametrami q i role."""
    data = request.GET.copy()

    if "query" not in data and "q" in data:
        data["query"] = data.get("q", "")

    if "roles" not in data:
        legacy_roles = [
            value
            for value in data.getlist("role")
            if value.strip()
        ]
        if legacy_roles:
            data.setlist("roles", legacy_roles)

    return data






@never_cache
@login_required
@require_GET
@team_member_required
def people_list(request):
    can_view_email = True
    can_view_dropbox_email = is_superuser(request.user)
    form = PeopleFilterForm(_filter_data(request))
    if not can_view_email:
        form.fields["query"].widget.attrs["placeholder"] = "Imię lub nazwisko"

    people = (
        Person.objects.filter(Q(pk__in=Person.objects.active().values('pk')) | Q(is_external=True))
        .prefetch_related("roles")
        .order_by("last_name", "first_name", "pk")
    )
    selected_role_ids = []
    query = ""

    if form.is_valid():
        query = form.cleaned_data.get("query", "").strip()
        selected_roles = form.cleaned_data.get("roles")
        selected_role_ids = (
            [role.pk for role in selected_roles]
            if selected_roles is not None
            else []
        )

        from core.search_people import rank_people
        people = rank_people(people, query)

        # Zachowaj również role bez przypisanych osób, w tym nowego Składacza.
        form.fields['roles'].queryset = ordered_team_roles()

        if selected_role_ids:
            # Wystarczy dowolna z zaznaczonych ról: np. redaktor
            # LUB korektor. Osoba z obiema rolami występuje tylko raz.
            people = people.filter(
                roles__pk__in=selected_role_ids,
            ).distinct()
    else:
        # Nieprawidłowy filtr nie powinien po cichu rozszerzać wyników.
        people = people.none()

    page_obj = paginate_items(request, people)

    return render(
        request,
        "core/people_list.html",
        {
            "people": page_obj,
            "page_obj": page_obj,
            "filter_form": form,
            "form": form,
            "roles": Role.objects.order_by("name", "pk"),
            "query": query,
            "selected_role_ids": selected_role_ids,
            "selected_role_id": (
                selected_role_ids[0]
                if len(selected_role_ids) == 1
                else None
            ),
            "can_view_authors": can_view_author_data(request.user),
            "can_view_team_email": can_view_email,
            "can_view_team_dropbox_email": can_view_dropbox_email,
            "people_column_count": 3,
        },
        status=400 if form.errors else 200,
    )


@never_cache
@login_required
@require_GET
@team_member_required
def person_detail(request, person_id):
    # Nieaktywne osoby z historią mają dostępny szczegół; lista aktywnego zespołu pozostaje bez zmian.
    person = get_object_or_404(
        Person.objects.filter(Q(is_active=True) | Q(is_external=True) | Q(user__audio_contacts__isnull=False)
            | Q(user__proofread_audiobooks__isnull=False) | Q(user__audiobook_stage_history__isnull=False)
            | Q(user__post_layout_assignments__isnull=False)
            | Q(user__workflow_role_assignments__isnull=False) | Q(historical_review_assignments__review__old_reviews=True) | Q(user__review_assignments__review__old_reviews=True)).distinct()
        .select_related("user")
        .prefetch_related("roles"),
        pk=person_id,
    )
    include_authors = can_view_author_data(request.user)

    assignments, person_summary = _profile_assignments(
        person,
        include_authors=include_authors,
    )
    hide_completed = request.GET.get('hide_completed') == '1'
    if hide_completed:
        assignments = [row for row in assignments if not (row.get('is_ready') or (
            row.get('has_completed_work') and not row.get('has_active_work') and not row.get('has_reserved_work')))]
    from core.post_layout import can_manage
    from core.permissions import can_view_post_layout
    for row in assignments:
        if row.get('kind_key') == 'post_layout':
            row['no_detail_link'] = not (can_view_post_layout(request.user) and (can_manage(request.user) or person.user_id == request.user.pk))

    return render(
        request,
        "core/person_detail.html",
        {
            "person": person,
            "audio_profiles": person.user.audio_contacts.all() if person.user_id else [],
            "assignments": assignments,
            "hide_completed": hide_completed,
            "person_summary": person_summary,
            "imported_work_summary": _imported_work_summary(person),
            "archived_reviews": ReviewAssignment.objects.submitted().filter(review__old_reviews=True).filter(
                Q(historical_person=person) | (Q(user_id=person.user_id) if person.user_id else Q(pk__in=[]))
            ).select_related("review__anthology").order_by("review__anthology__title", "review__title", "position"),
            "can_view_authors": include_authors,
            "can_view_team_email": person.can_show_team_contact,
            "can_view_team_dropbox_email": person.can_show_team_contact and is_superuser(request.user),
            "leave_information": (
                user_leave_information(person.user)
                if person.user_id is not None
                else None
            ),
        },
    )
