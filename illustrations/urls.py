from django.urls import path

from . import views


app_name = "illustrations"


urlpatterns = [
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