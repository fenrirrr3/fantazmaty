from django.urls import path
from .views import operations, intake, supervision, mailbox, newsletters
from core.user_preview import user_preview, user_preview_stop

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
from core.views.programs import programs
from core.views.program_jobs import program_job
from core.views.activity import user_activity
from core.views.recruitment_delete import recruitment_delete
from core.views.tags import tag_list
from core.views.translations import translation_list, translation_detail, set_translators, translation_person_detail, translation_person_suggestions
from core.views.production_tasks import task_list, audio_descriptions
from core.views.audiobooks import audiobook_list, update_text_audiobook
from core.views.external_audiobooks import external_audiobooks, audiobook_guidelines_txt
from core.views import novels, vocabulary
from core.views.recruitment_detail import recruitment_detail
from core.views.recruitment_notified import recruitment_notified
from core.views.recruitment_mailbox import recruitment_mailbox
from core.views.proofreading_sections import post_layout, audio_proofreading


app_name = "core"








urlpatterns = [
    path('audiobooki/zewnetrzne/', external_audiobooks, name='external_audiobooks'),
    path('audiobooki/zewnetrzne/wytyczne.txt', audiobook_guidelines_txt, name='audiobook_guidelines_txt'),
    path('korekta-poskladowa/', post_layout, name='post_layout'),
    path('korekta-audiobookow/', audio_proofreading, name='audio_proofreading'),
    path('rekrutacja/<int:pk>/usun/', recruitment_delete, name='recruitment_delete'),
    path('rekrutacja/<int:pk>/powiadomiony/', recruitment_notified, name='recruitment_notified'),
    path('rekrutacja/skrzynka/', recruitment_mailbox, name='recruitment_mailbox'),
    path('powiesci/', novels.novel_list, name='novel_list'),
    path('powiesci/dodaj/', novels.novel_add, name='novel_add'),
    path('powiesci/<int:novel_id>/', novels.novel_detail, name='novel_detail'),
    path('powiesci/<int:novel_id>/rozdzial/<int:chapter_id>/', novels.chapter_edit, name='chapter_edit'),
    path('slownik/', vocabulary.vocabulary_list, name='vocabulary_list'),
    path('slownik/podpowiedzi/', vocabulary.vocabulary_suggestions, name='vocabulary_suggestions'),
    path('slownik/<int:term_id>/scal/', vocabulary.vocabulary_merge, name='vocabulary_merge'),
    path("teksty/<int:text_id>/audiobook/", update_text_audiobook, name="update_text_audiobook"),
    path('tlumaczenia/osoby/<str:kind>/sugestie/', translation_person_suggestions, name='translation_person_suggestions'),
    path('zadania/', task_list, name='task_list'),
    path('audiodeskrypcje/', audio_descriptions, name='audio_descriptions'),
    path('tlumaczenia/osoby/<str:kind>/<int:person_id>/', translation_person_detail, name='translation_person_detail'),
    path('tlumaczenia/', translation_list, name='translation_list'),
    path('tlumaczenia/<int:text_id>/', translation_detail, name='translation_detail'),
    path('tlumaczenia/<int:text_id>/tlumacze/', set_translators, name='set_translators'),
    path("teksty/tagi/", tag_list, name="tag_list"),
    path("teksty/<int:text_id>/tagi/", texts.update_text_tags, name="update_text_tags"),
    path("podglad-uzytkownika/", user_preview, name="user_preview"),
    path("podglad-uzytkownika/zakoncz/", user_preview_stop, name="user_preview_stop"),
    path("newsletter/zgody/", newsletters.newsletter_list, name="newsletter_list"),
    path("zespol/lista-wedlug-roli/", supervision.role_names, name="role_names"),
    path("etapy/<int:stage_id>/pomin/", workflow.skip_workflow_stage, name="skip_workflow_stage"),
    path("recenzje/import-zbiorczy/", reviews.review_bulk_submit, name="review_bulk_submit"),
    path("organizacja/ostatnia-aktywnosc/", reports.last_activity, name="last_activity"),
    path("teksty/<int:text_id>/powiaz-recenzje/", texts.link_text_review, name="link_text_review"),
    path("teksty/<int:text_id>/powtorzenia/<int:repetition_id>/anuluj/", workflow.cancel_workflow_repetition, name="cancel_workflow_repetition"),
    path("etapy/<int:stage_id>/przekaz/", workflow.handoff_workflow_stage, name="handoff_workflow_stage"),
    path("etapy/<int:stage_id>/termin/", workflow.change_scheduled_workflow_stage, name="change_scheduled_workflow_stage"),
    path("spojnosc-danych/", supervision.data_integrity, name="data_integrity"),
    path("antologie/<int:anthology_id>/", supervision.anthology_detail, name="anthology_detail"),
    path("zespol/<int:person_id>/uprawnienia/", supervision.person_permissions, name="person_permissions"),
    path("teksty/<int:text_id>/plik/", texts.update_text_file, name="update_text_file"),
    path("aktywnosc-uzytkownikow/", user_activity, name="user_activity"),
    path("teksty/<int:text_id>/notatki/<int:note_id>/edytuj/", texts.edit_text_note, name="edit_text_note"),
    path("teksty/<int:text_id>/notatki/<int:note_id>/usun/", texts.delete_text_note, name="delete_text_note"),
    path("programy/", programs, name="programs"),
    path('programy/zadania/<str:token>/', program_job, name='program_job'),
    path("autorzy/sugestie/", intake.author_suggestions, name="author_suggestions"),
    path("moje-urlopy/<int:vacation_id>/odwolaj/", vacations.cancel_vacation, name="cancel_vacation"),
    path("publikacje/uwagi-do-antologii/<int:pk>/edytuj/", operations.correction_edit, name="correction_edit"),
    path("publikacje/uwagi-do-antologii/<int:pk>/usun/", operations.correction_delete, name="correction_delete"),
    path("moja-praca/recenzje/", reviews.my_reviews, name="my_reviews"),
    path('ekstrakty/', intake.extract_list, name='extract_list'),
    path('ekstrakty/dodaj/', intake.extract_edit, name='extract_add'),
    path('ekstrakty/<int:pk>/', intake.extract_edit, name='extract_edit'),
    path('rekrutacja/', intake.recruitment_list, name='recruitment_list'),
    path('rekrutacja/<int:pk>/szczegoly/', recruitment_detail, name='recruitment_detail'),
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
    path("pulpit/zadania/", dashboard.dashboard_tasks, name="dashboard_tasks"),
    path(
        "wyszukiwanie/",
        search.global_search,
        name="global_search",
    ),
    path(
        "audiobooki/",
        audiobook_list,
        name="audiobooks",
    ),

    # Teksty.
    path("teksty/", texts.text_list, name="text_list"),
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
        mailbox.mailbox_headers,
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
    path("recenzje/<int:review_id>/folder-dropbox/", reviews.update_review_file, name="update_review_file"),
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
        "tabelka-zbiorcza/",
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

# Dawne adresy pozostają zgodne także dla formularzy POST.
_legacy_routes = {'workflow/<int:stage_id>/pomin/': 'etapy/<int:stage_id>/pomin/', 'workflow/<int:stage_id>/przekaz/': 'etapy/<int:stage_id>/przekaz/', 'workflow/<int:stage_id>/schedule/': 'etapy/<int:stage_id>/termin/', 'dashboard/tasks/': 'pulpit/zadania/', 'etapy-prac/': 'tabelka-zbiorcza/'}
for _old, _new in _legacy_routes.items():
    _target = next(p for p in urlpatterns if str(p.pattern) == _new)
    urlpatterns.append(path(_old, _target.callback, _target.default_args))
