from django.urls import path
from .views import operations, intake

from .views import (
    anthologies,
    authors,
    dashboard,
    people,
    reports,
    reviews,
    search,
    texts,
    vacations,
    workflow,
)


app_name = "core"


urlpatterns = [
    path("moja-praca/recenzje/", reviews.my_reviews, name="my_reviews"),
    path('ekstrakty/', intake.extract_list, name='extract_list'),
    path('ekstrakty/dodaj/', intake.extract_edit, name='extract_add'),
    path('ekstrakty/<int:pk>/', intake.extract_edit, name='extract_edit'),
    path('rekrutacja/', intake.recruitment_list, name='recruitment_list'),
    path('rekrutacja/dodaj/', intake.recruitment_edit, name='recruitment_add'),
    path('rekrutacja/<int:pk>/', intake.recruitment_edit, name='recruitment_edit'),
    path('recenzje/dodaj/', intake.review_create, name='review_create'),
    path("organizacja/redaktorzy/", reports.editor_activity, name="editor_activity"),
    path("publikacje/uwagi-do-antologii/opowiadania/", operations.correction_texts, name="correction_texts"),
    path("publikacje/uwagi-do-antologii/", operations.corrections, name="anthology_corrections"),
    path("publikacje/uwagi-do-antologii/status/", operations.correction_status, name="correction_status"),
    path("organizacja/do-powiadomienia/", operations.notification_queue, name="notification_queue"),
    path("organizacja/zaplanowane-odrzucenia/", operations.notification_queue, {"scheduled": True}, name="scheduled_rejections"),
    path("recenzje/<int:review_id>/przywroc/", operations.release_hidden_review, name="release_hidden_review"),
    # Pulpit i wyszukiwanie.
    path("", dashboard.home, name="home"),
    path(
        "wyszukiwanie/",
        search.global_search,
        name="global_search",
    ),
    path(
        "audiobooki/",
        dashboard.audiobooks,
        name="audiobooks",
    ),

    # Teksty.
    path("teksty/", texts.text_list, name="text_list"),
    path(
        "teksty/operacja-zbiorcza/",
        texts.bulk_text_action,
        name="bulk_text_action",
    ),
    path(
        "teksty/<int:text_id>/autorzy/",
        texts.set_text_authors,
        name="set_text_authors",
    ),

    # Recenzje.
    path("recenzje/", reviews.review_list, name="review_list"),
    path(
        "recenzje/operacja-zbiorcza/",
        reviews.bulk_review_action,
        name="bulk_review_action",
    ),
    path(
        "recenzje/import/",
        reviews.review_bulk_import,
        name="review_bulk_import",
    ),
    path(
        "recenzje/<int:review_id>/przypisz/",
        reviews.assign_reviewer,
        name="assign_reviewer",
    ),
    path(
        "recenzje/<int:review_id>/zrezygnuj/",
        reviews.unassign_reviewer,
        name="unassign_reviewer",
    ),
    path(
        "recenzje/<int:review_id>/status/",
        reviews.update_review_status,
        name="update_review_status",
    ),
    path(
        "recenzje/<int:review_id>/ostrzezenia/",
        reviews.update_review_content_warnings,
        name="update_review_content_warnings",
    ),
    path(
        "recenzje/<int:review_id>/powiadomienie-autora/",
        reviews.update_author_notification,
        name="update_author_notification",
    ),
    path(
        "recenzje/<int:review_id>/przenies-do-tekstow/",
        reviews.copy_review_to_text,
        name="copy_review_to_text",
    ),
    path(
        "recenzje/<int:review_id>/",
        reviews.assigned_review_detail,
        name="assigned_review_detail",
    ),

    # Teksty przypisane do użytkownika.
    path("moje-teksty/", texts.my_texts, name="my_texts"),
    path(
        "moje-teksty/<int:text_id>/notatki/dodaj/",
        texts.add_text_note,
        name="add_text_note",
    ),
    path(
        "moje-teksty/<int:text_id>/notatka-koordynatora/",
        texts.update_coordinator_note,
        name="update_coordinator_note",
    ),
    path(
        "moje-teksty/<int:text_id>/ostrzezenia/",
        texts.update_text_content_warnings,
        name="update_text_content_warnings",
    ),
    path(
        "moje-teksty/<int:text_id>/",
        texts.assigned_text_detail,
        name="assigned_text_detail",
    ),

    # Rozpoczynanie i kończenie etapów.
    path(
        "moje-teksty/etapy/<int:stage_id>/rozpocznij/",
        workflow.start_assigned_workflow_stage,
        name="start_assigned_workflow_stage",
    ),
    path(
        "moje-teksty/etapy/<int:stage_id>/zakoncz/",
        workflow.complete_workflow_stage,
        name="complete_workflow_stage",
    ),

    # Przejścia procesu redakcji.
    path(
        "moje-teksty/<int:text_id>/redakcja/wznow/",
        workflow.resume_text_editing,
        name="resume_text_editing",
    ),
    path(
        "moje-teksty/<int:text_id>/redakcja/przekaz-autorowi/",
        workflow.send_text_to_author_stage,
        name="send_text_to_author_stage",
    ),
    path(
        "moje-teksty/<int:text_id>/redakcja/przekaz-do-pierwszej-weryfikacji/",
        workflow.send_to_first_verification_stage,
        name="send_to_first_verification_stage",
    ),
    path(
        "moje-teksty/<int:text_id>/redakcja/przekaz-do-drugiej-weryfikacji/",
        workflow.send_to_second_verification_stage,
        name="send_to_second_verification_stage",
    ),
    path(
        "moje-teksty/<int:text_id>/redakcja/zakoncz/",
        workflow.finish_text_editing,
        name="finish_text_editing",
    ),
    path(
        "moje-teksty/<int:text_id>/pierwsza-weryfikacja/rozpocznij/",
        workflow.start_first_verification_stage,
        name="start_first_verification_stage",
    ),

    # Nowy przebieg i wycofanie tekstu.
    path(
        "moje-teksty/<int:text_id>/cofnij-etap/",
        workflow.restart_text_workflow,
        name="restart_text_workflow",
    ),
    path(
        "moje-teksty/<int:text_id>/wycofaj/",
        workflow.withdraw_text,
        name="withdraw_text",
    ),

    # Etapy dostępne do przejęcia.
    path(
        "teksty-do-wziecia/",
        texts.available_texts,
        name="available_texts",
    ),
    path(
        "teksty-do-wziecia/<int:stage_id>/przejmij/",
        workflow.take_workflow_stage,
        name="take_workflow_stage",
    ),

    # Autorzy.
    path("autorzy/", authors.author_list, name="author_list"),
    path(
        "autorzy/<int:author_id>/notatki/dodaj/",
        authors.add_author_note,
        name="add_author_note",
    ),
    path(
        "autorzy/<int:author_id>/",
        authors.author_detail,
        name="author_detail",
    ),

    # Antologie i etapy prac.
    path(
        "antologie/",
        anthologies.anthology_list,
        name="anthology_list",
    ),
    path(
        "etapy-prac/",
        workflow.workflow_list,
        name="workflow_list",
    ),

    # Zespół.
    path("zespol/", people.people_list, name="people_list"),
    path(
        "zespol/<int:person_id>/",
        people.person_detail,
        name="person_detail",
    ),

    # Urlopy.
    path(
        "moje-urlopy/",
        vacations.my_vacations,
        name="my_vacations",
    ),
    path(
        "moje-urlopy/<int:vacation_id>/edytuj/",
        vacations.edit_vacation,
        name="edit_vacation",
    ),
    path(
        "moje-urlopy/<int:vacation_id>/zakoncz/",
        vacations.end_vacation,
        name="end_vacation",
    ),

    # Organizacja i raporty.
    path(
        "organizacja/urlopy/",
        vacations.active_vacations,
        name="active_vacations",
    ),
    path(
        "organizacja/korektorzy/",
        reports.proofreader_activity,
        name="proofreader_activity",
    ),
    path(
        "organizacja/weryfikatorzy/",
        reports.verifier_activity,
        name="verifier_activity",
    ),
    path(
        "organizacja/recenzenci/",
        reports.reviewer_activity,
        name="reviewer_activity",
    ),
    path(
        "organizacja/brak-aktywnosci/",
        reports.workflow_inactivity,
        name="workflow_inactivity",
    ),
]
