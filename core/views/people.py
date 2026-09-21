from people.role_ordering import ordered_team_roles
from django.contrib.auth.decorators import login_required
from django.db.models import F, Prefetch, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.forms import PeopleFilterForm
from core.pagination import paginate_items
from core.permissions import (
    can_view_author_data, is_coordinator, is_superuser, team_member_required,
)
from core.selectors.people import user_leave_information
from people.models import Person, Role
from texts.models import ReviewAssignment
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import STAGE_ROLES


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




def _profile_assignments(person, *, include_authors):
    if person.user_id is None:
        return [], {"active": 0, "reserved": 0, "completed": 0}

    current_stages = (
        WorkflowStage.objects.filter(
            workflow_cycle=F("text__current_workflow_cycle"),
        )
        .order_by("-iteration", "-pk")
    )

    queryset = (
        WorkflowRoleAssignment.objects.filter(
            assigned_to_id=person.user_id,
            workflow_cycle=F("text__current_workflow_cycle"),
        )
        .select_related("text", "text__anthology")
        .prefetch_related(
            Prefetch(
                "text__workflow_stages",
                queryset=current_stages,
                to_attr="profile_stages",
            )
        )
        .order_by(F("assigned_at").desc(nulls_last=True), "-pk")
    )

    if include_authors:
        queryset = queryset.prefetch_related("text__authors")

    today = timezone.localdate()
    from workflow.read_queries import work_text_ids
    from texts.models import Text
    own_texts = Text.objects.filter(workflow_role_assignments__assigned_to_id=person.user_id, workflow_role_assignments__workflow_cycle=F("current_workflow_cycle")).distinct()
    classified = work_text_ids(own_texts, person.user, today)
    completed_ids, active_ids, waiting_ids = (classified[key] for key in ('completed', 'active', 'waiting'))
    assignments = []

    from workflow.state import stage_is_open, stage_is_active
    stage_roles = {
        **STAGE_ROLES,
        WorkflowStage.StageType.EDITING: WorkflowRoleAssignment.Role.EDITOR,
        WorkflowStage.StageType.EDITOR_CONTROL: (
            WorkflowRoleAssignment.Role.EDITOR
        ),
    }

    for assignment in queryset:
        text = assignment.text
        stages = text.profile_stages

        matching_stages = [
            stage
            for stage in stages
            if stage.assignment_id == assignment.pk or (stage.assignment_id is None and assignment.is_current and stage_roles.get(stage.stage_type) == assignment.role)
        ]
        open_stages = [
            stage
            for stage in matching_stages
            if stage_is_open(stage)
        ]
        active_stages = [
            stage
            for stage in open_stages
            if stage_is_active(stage, today)
        ]

        # Aktywny etap danej roli ma pierwszeństwo przed rezerwacją
        # kolejnego etapu lub historią zakończonych prac.
        latest_stage = next(
            iter(active_stages or open_stages or matching_stages),
            None,
        )

        is_withdrawn = any(
            stage.stage_type == WorkflowStage.StageType.WITHDRAWN and stage.is_current
            and not stage.is_completed
            for stage in stages
        )
        is_ready = any(
            stage.stage_type == WorkflowStage.StageType.READY and stage.is_current
            for stage in stages
        )
        is_terminal = is_withdrawn or is_ready

        has_active_work = text.pk in active_ids and assignment.is_current and bool(active_stages)
        has_reserved_work = assignment.is_current and text.pk in waiting_ids
        has_completed_work = text.pk in completed_ids


        authors = []

        if include_authors:
            authors = [
                {
                    "pk": author.pk,
                    "first_name": author.first_name,
                    "last_name": author.last_name,
                    "pseudonym": author.pseudonym,
                }
                for author in text.authors.all()
            ]

        # Do szablonu nie przekazujemy obiektów ORM przydziału ani tekstu.
        # Zapobiega to odczytaniu autora przez relacje zagnieżdżone.
        assignments.append(
            {
                "pk": assignment.pk,
                "role": assignment.role,
                "get_role_display": assignment.get_role_display() + f" — wykonanie {assignment.execution_number}" + (" (wcześniejsze przypisanie)" if not assignment.is_current else ""),
                "assigned_at": assignment.assigned_at,
                "has_active_work": has_active_work,
                "has_reserved_work": has_reserved_work,
                "has_completed_work": has_completed_work,
                "is_withdrawn": is_withdrawn,
                "is_ready": is_ready,
                "text": {
                    "pk": text.pk,
                    "title": text.title,
                    "anthology": (
                        {
                            "pk": text.anthology.pk,
                            "title": text.anthology.title,
                        }
                        if text.anthology_id is not None
                        else None
                    ),
                    "authors": {"all": authors},
                },
                "latest_stage": (
                    {
                        "pk": latest_stage.pk,
                        "stage_type": latest_stage.stage_type,
                        "get_stage_type_display": (
                            latest_stage.get_stage_type_display()
                        ),
                        "iteration": latest_stage.iteration,
                        "imported_completed": latest_stage.imported_completed,
                        "started_at": latest_stage.started_at,
                        "ended_at": latest_stage.ended_at,
                        "is_completed": latest_stage.is_completed,
                        "is_active": has_active_work,
                        "is_scheduled": (
                            latest_stage.started_at is not None
                            and latest_stage.started_at > today
                            and not latest_stage.is_completed
                            and not is_terminal
                        ),
                    }
                    if latest_stage is not None
                    else None
                ),
            }
        )

    summary = {"active": len(active_ids), "reserved": len(waiting_ids), "completed": len(completed_ids)}
    return assignments, summary


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
        Person.objects.filter(is_active=True)
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
        Person.objects.filter(Q(is_active=True) | Q(user__workflow_role_assignments__isnull=False) | Q(historical_review_assignments__review__old_reviews=True) | Q(user__review_assignments__review__old_reviews=True)).distinct()
        .select_related("user")
        .prefetch_related("roles"),
        pk=person_id,
    )
    include_authors = can_view_author_data(request.user)

    assignments, person_summary = _profile_assignments(
        person,
        include_authors=include_authors,
    )

    return render(
        request,
        "core/person_detail.html",
        {
            "person": person,
            "assignments": assignments,
            "person_summary": person_summary,
            "archived_reviews": ReviewAssignment.objects.filter(review__old_reviews=True).filter(
                Q(historical_person=person) | (Q(user_id=person.user_id) if person.user_id else Q(pk__in=[]))
            ).select_related("review__anthology").order_by("review__anthology__title", "review__title", "position"),
            "can_view_authors": include_authors,
            "can_view_team_email": True,
            "can_view_team_dropbox_email": is_superuser(request.user),
            "leave_information": (
                user_leave_information(person.user)
                if person.user_id is not None
                else None
            ),
        },
    )