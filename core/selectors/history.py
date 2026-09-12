from texts.models import HistoricalTextAssignment


def historical_assignments():
    return HistoricalTextAssignment.objects.filter(text__is_historical=True).select_related("person", "text", "text__anthology")


def historical_team_members(text):
    if not text.is_historical:
        return []
    return [dict(role=item.role, label=item.role_label, is_historical=True,
                 is_assigned=True, user={"get_full_name": item.display_name})
            for item in historical_assignments().filter(text=text)]


def historical_profile_assignments(person, *, include_authors):
    queryset = historical_assignments().filter(person=person)
    if include_authors:
        queryset = queryset.prefetch_related("text__authors")
    rows = []
    for item in queryset:
        text = item.text
        authors = [dict(pk=a.pk, first_name=a.first_name, last_name=a.last_name, pseudonym=a.pseudonym) for a in text.authors.all()] if include_authors else []
        rows.append(dict(
            pk=item.pk, role=item.role, get_role_display=item.role_label,
            is_historical=True, assigned_at=None, has_active_work=False,
            has_reserved_work=False, has_completed_work=item.is_completed,
            text=dict(pk=text.pk, title=text.title,
                      anthology=dict(pk=text.anthology_id, title=text.anthology.title) if text.anthology_id else None,
                      authors={"all": authors}),
            latest_stage=dict(get_stage_type_display=item.get_source_status_display() or "Dane historyczne", started_at=None,
                              ended_at=None, is_completed=item.is_completed),
        ))
    return rows


def merge_historical_team_members(text, members):
    """Replace only empty standard slots; never change actual active assignments."""
    if not text.is_historical:
        return members
    history = list(historical_assignments().filter(text=text))
    occupied = set()
    for item in history:
        if item.participant != 1:
            continue
        if item.role in ("editor", "editing_coordinator", "verification_coordinator") and item.position == 1:
            occupied.add(item.role)
        elif item.role in ("proofreader", "verifier"):
            occupied.add(f"{item.role}_{item.position}")
    base = [member for member in members if member["is_assigned"] or member["role"] not in occupied]
    return base + [dict(role=item.role, label=item.role_label, is_historical=True,
                        is_assigned=True, user={"get_full_name": item.display_name}) for item in history]
