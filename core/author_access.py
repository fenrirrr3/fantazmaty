from django.db.models import Q
from authors.models import Author
from core.permissions import is_coordinator, is_team_member


def contact_authors(user):
    if not is_team_member(user):
        return Author.objects.none()
    if is_coordinator(user):
        return Author.objects.all()
    return Author.objects.filter(
        Q(texts__workflow_role_assignments__assigned_to=user)
    ).distinct()
