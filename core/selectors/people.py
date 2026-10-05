from core.translation_scope import ordinary
from django.db.models import Count, F, Prefetch
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import STAGE_ROLES
from workflow.catalog import IMPORT_ONLY_ROLES, IMPORT_ONLY_STAGE_TYPES
from workflow.labels import assignment_label


from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from core.permissions import get_active_person_profile
from people.models import Vacation


UPCOMING_LEAVE_DAYS = 365


def user_leave_information(user):
    """
    Zwraca trwający urlop albo najbliższy zaplanowany urlop użytkownika.

    Użytkownik jest osobą, której dotyczy informacja. Uprawnienia
    odbiorcy sprawdza widok udostępniający te dane.

    Odczyt nie modyfikuje urlopów ani pól profilu osoby.
    """
    person = get_active_person_profile(user)

    if person is None:
        return None

    now = timezone.now()
    today = timezone.localdate(now)

    vacation = (
        Vacation.objects.filter(
            person_id=person.pk,
            person__is_active=True,
            start_date__lte=today + timedelta(days=UPCOMING_LEAVE_DAYS),
        )
        .filter(
            Q(until_revoked=True)
            | Q(end_date__gt=now)
        )
        .order_by("start_date", "pk")
        .first()
    )

    if vacation is None:
        return None

    # Trwające urlopy mają wcześniejszą datę rozpoczęcia niż przyszłe,
    # więc zaplanowanie kolejnego nie przesłania aktualnej nieobecności.
    is_active = vacation.start_date <= today
    is_upcoming = vacation.start_date > today

    # Jawna projekcja nie udostępnia szablonom relacji ORM prowadzących
    # z urlopu przez osobę i konto do innych danych aplikacji.
    vacation_data = {
        "pk": vacation.pk,
        "id": vacation.pk,
        "person_id": person.pk,
        "person": {
            "pk": person.pk,
            "first_name": person.first_name,
            "last_name": person.last_name,
        },
        "start_date": vacation.start_date,
        "end_date": vacation.end_date,
        "until_revoked": vacation.until_revoked,
        "created_at": vacation.created_at,
        "is_active": is_active,
        "is_upcoming": is_upcoming,
        "is_finished": False,
        "can_be_edited": True,
        "can_be_ended": is_active and vacation.until_revoked,
    }

    return {
        "vacation": vacation_data,
        "is_active": is_active,
        "is_upcoming": is_upcoming,
    }

def profile_assignments(person, *, include_authors):
    if person.user_id is None:
        return [], {"active": 0, "reserved": 0, "completed": 0}

    current_stages = (
        ordinary(WorkflowStage.objects).filter(
            workflow_cycle=F("text__current_workflow_cycle"),
        )
        .order_by("-iteration", "-pk")
    )

    queryset = (
        ordinary(WorkflowRoleAssignment.objects).filter(
            assigned_to_id=person.user_id,
            workflow_cycle=F("text__current_workflow_cycle"),
        )
        .exclude(role__in=IMPORT_ONLY_ROLES)
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

    queryset = queryset.prefetch_related("text__authors")

    today = timezone.localdate()
    from workflow.read_queries import work_text_ids
    from texts.models import Text
    own_texts = ordinary(Text.objects).filter(workflow_role_assignments__assigned_to_id=person.user_id, workflow_role_assignments__workflow_cycle=F("current_workflow_cycle")).distinct()
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


        authors = [
            {"pk": author.pk, "first_name": author.first_name,
             "last_name": author.last_name, "pseudonym": author.pseudonym}
            for author in text.authors.all()
        ]

        assignments.append(
            {
                "pk": assignment.pk,
                "role": assignment.role,
                "get_role_display": assignment_label(assignment, show_first=True),
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


def imported_work_summary(person):
    if person.user_id is None:
        return []
    counts = (ordinary(WorkflowStage.objects).filter(
        assignment__assigned_to_id=person.user_id,
        stage_type__in=IMPORT_ONLY_STAGE_TYPES,
        imported_completed=True, is_completed=True,
    ).order_by().values("assignment__role").annotate(
        texts=Count("text_id", distinct=True), executions=Count("pk"),
    ))
    labels = dict(WorkflowRoleAssignment.Role.choices)
    return sorted([
        {"role": row["assignment__role"], "label": labels[row["assignment__role"]],
         "texts": row["texts"], "executions": row["executions"]}
        for row in counts
    ], key=lambda row: row["label"])



def role_names_context(params):
    from people.models import Person, Role
    from core.sort_keys import text_key
    roles = Role.objects.order_by('name')
    selected = params.get('role', '')
    role = roles.filter(pk=int(selected)).first() if selected.isdecimal() and len(selected) < 19 else None
    names = []
    if role is not None:
        people = Person.objects.active().filter(
            Q(roles=role) | Q(user__groups__name__iexact=role.name)
        ).distinct()
        # Sort the profiles, before formatting as first name + surname.
        ordered_people = sorted(people, key=lambda person: (
            text_key(' '.join(person.last_name.split())),
            text_key(' '.join(person.first_name.split())), person.pk,
        ))
        names = [f'{person.first_name} {person.last_name}'.strip() for person in ordered_people]
    return {'roles': roles, 'selected_role': selected, 'names': names, 'chosen_role': role}
