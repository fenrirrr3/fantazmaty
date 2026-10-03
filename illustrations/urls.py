from django.urls import path

from . import views
from . import directory


app_name = "illustrations"


urlpatterns = [
    path("ilustratorzy/", directory.illustrator_list, name="illustrator_list"),
    path("ilustratorzy/dodaj/", directory.illustrator_edit, name="illustrator_add"),
    path("ilustratorzy/<int:illustrator_id>/", directory.illustrator_edit, name="illustrator_edit"),
    path("<int:illustration_id>/", views.illustration_detail, name="illustration_detail"),
    path(
        "",
        views.illustration_list,
        name="illustration_list",
    ),
    path(
        "propozycje-okladek/",
        views.cover_proposal_list,
        name="cover_proposal_list",
    ),
    path(
        "propozycje-okladek/<int:proposal_id>/status/",
        views.update_cover_proposal_status,
        name="update_cover_proposal_status",
    ),
]
