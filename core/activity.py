"""Request audit. No request bodies, cookies, credentials or query strings."""
import logging
import time

logger = logging.getLogger(__name__)

LABELS = {
    'home': 'Pulpit', 'people_list': 'Zespół', 'person_detail': 'Profil osoby',
    'assigned_text_detail': 'Podgląd tekstu', 'assigned_review_detail': 'Podgląd recenzji',
    'my_reviews': 'Moje recenzje', 'my_texts': 'Moje teksty', 'available_texts': 'Teksty do wzięcia',
    'review_list': 'Recenzje', 'text_list': 'Wszystkie teksty', 'workflow_list': 'Etapy prac',
    'take_workflow_stage': 'Przejęcie etapu', 'complete_workflow_stage': 'Zakończenie etapu',
    'start_assigned_workflow_stage': 'Rozpoczęcie etapu', 'assign_reviewer': 'Przejęcie recenzji',
    'unassign_reviewer': 'Rezygnacja z recenzji', 'save_review': 'Zapis recenzji',
    'add_text_note': 'Dodanie notatki', 'edit_text_note': 'Edycja notatki', 'delete_text_note': 'Usunięcie notatki',
    'global_search': 'Wyszukiwanie', 'author_suggestions': 'Podpowiedzi autorów',
    'my_vacations': 'Moje urlopy', 'edit_vacation': 'Edycja urlopu', 'cancel_vacation': 'Odwołanie urlopu',
    'anthology_corrections': 'Uwagi do antologii', 'correction_edit': 'Edycja uwagi', 'correction_delete': 'Usunięcie uwagi',
    'review_create': 'Dodanie zgłoszenia', 'review_bulk_import': 'Import zgłoszeń',
    'user_activity': 'Aktywność użytkowników', 'login': 'Logowanie', 'logout': 'Wylogowanie',
}

LABELS.update({
    'discord_test': 'Test wysyłki Discord',
    'data_integrity':'Spójność danych', 'person_permissions':'Podgląd uprawnień osoby',
    'anthology_detail':'Podgląd antologii', 'anthology_credits_csv':'Eksport stopki antologii',
    'update_text_file':'Zmiana odnośnika do pliku tekstu',
    'programs':'Programy', 'extract_list':'Ekstrakty', 'extract_add':'Dodanie ekstraktu', 'extract_edit':'Edycja ekstraktu',
    'recruitment_list':'Rekrutacja', 'recruitment_add':'Dodanie rekrutacji', 'recruitment_edit':'Edycja rekrutacji',
    'editor_activity':'Aktywność redaktorów', 'correction_texts':'Wybór tekstu do uwagi', 'correction_status':'Zmiana statusu uwagi',
    'notification_queue':'Autorzy do powiadomienia', 'scheduled_rejections':'Zaplanowane odrzucenia', 'release_hidden_review':'Przywrócenie recenzji',
    'audiobooks':'Audiobooki', 'bulk_text_action':'Operacja zbiorcza na tekstach', 'set_text_authors':'Zmiana autorów tekstu',
    'bulk_review_action':'Operacja zbiorcza na recenzjach', 'update_review_status':'Zmiana statusu recenzji',
    'update_review_content_warnings':'Zmiana ostrzeżeń recenzji', 'update_author_notification':'Zapis powiadomienia autora',
    'copy_review_to_text':'Przeniesienie zgłoszenia do tekstów', 'update_coordinator_note':'Zapis notatki koordynatora',
    'update_text_content_warnings':'Zmiana ostrzeżeń tekstu', 'resume_text_editing':'Wznowienie redakcji',
    'send_text_to_author_stage':'Przekazanie tekstu autorowi', 'send_to_first_verification_stage':'Przekazanie do pierwszej weryfikacji',
    'send_to_second_verification_stage':'Przekazanie do drugiej weryfikacji', 'finish_text_editing':'Zakończenie redakcji',
    'start_first_verification_stage':'Rozpoczęcie pierwszej weryfikacji', 'restart_text_workflow':'Ponowne rozpoczęcie pracy nad tekstem',
    'withdraw_text':'Wycofanie tekstu', 'author_list':'Autorzy', 'author_detail':'Profil autora', 'add_author_note':'Dodanie notatki o autorze',
    'anthology_list':'Antologie', 'end_vacation':'Zakończenie urlopu', 'active_vacations':'Aktywne urlopy',
    'proofreader_activity':'Aktywność korektorów', 'verifier_activity':'Aktywność weryfikatorów', 'reviewer_activity':'Aktywność recenzentów',
    'workflow_inactivity':'Przestoje', 'last_activity':'Ostatnia aktywność',
})


def describe_request(match, method):
    name = match.url_name or ''
    label = LABELS.get(name, name.replace('_', ' '))
    if method == 'POST':
        label = {'assigned_review_detail':'Zapis oceny recenzenta', 'my_vacations':'Dodanie urlopu', 'anthology_corrections':'Zgłoszenie uwagi do antologii'}.get(name, label)
    target = ''
    for key in ('text_id', 'review_id', 'person_id', 'stage_id'):
        if key in match.kwargs:
            target = f"{key}: #{match.kwargs[key]}"
            break
    if match.namespace == 'admin':
        model_admin = getattr(match.func, 'model_admin', None)
        if model_admin:
            operation = {'changelist':'Lista', 'change':'Edycja', 'add':'Dodanie', 'delete':'Usunięcie', 'history':'Historia'}.get(name.rsplit('_', 1)[-1], 'Panel admina')
            label = f'{operation}: {model_admin.model._meta.verbose_name}'
        if match.kwargs.get('object_id'):
            target = f"#{match.kwargs['object_id']}"
    return label[:255], target[:255]


class UserActivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if match and (match.namespace in ('core', 'illustrations', 'admin') or match.url_name in ('login', 'logout')):
            if match.url_name in ('author_suggestions', 'global_search'):
                return
            try:
                request._cms_activity = describe_request(match, request.method)
            except Exception:
                logger.exception('Nie udało się opisać aktywności użytkownika.')

    def __call__(self, request):
        before = request.user if request.user.is_authenticated else None
        # Materialize before logout flushes the session/user.
        before_id = before.pk if before else None
        before_actor = (before.email or before.get_username()) if before else ''
        from core.workflow_events import run_admin_request
        response = run_admin_request(request, self.get_response)
        activity = getattr(request, '_cms_activity', None)
        after = request.user if request.user.is_authenticated else None
        user_id = before_id or (after.pk if after else None)
        actor = before_actor or ((after.email or after.get_username()) if after else '')
        record_activity = bool(activity and user_id)
        visit = request.method in ('GET', 'HEAD') and response.status_code < 400
        now = time.time()
        if record_activity and visit:
            try:
                previous = request.session.get('_activity_visit', {})
                record_activity = previous.get('user') != user_id or now - float(previous.get('at', 0)) >= 300
            except (TypeError, ValueError):
                record_activity = True
        if record_activity:
            from core.activity_spool import enqueue_activity
            try:
                action = activity[0]
                if request.method not in ('GET', 'HEAD'):
                    # A submitted form and HTTP 200/302 do not prove a domain write.
                    action = 'Próba / formularz: ' + action
                enqueue_activity(user_id=user_id, actor=actor[:254], method=request.method[:10],
                    action=action[:255], target=activity[1], path=request.path[:1000], status_code=response.status_code)
                if visit:
                    request.session['_activity_visit'] = {'user': user_id, 'at': now}
            except Exception:
                logger.exception('Nie udało się zapisać aktywności użytkownika w kolejce lokalnej.')
        return response
