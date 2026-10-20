"""Dziennik zmian workflow (WorkflowEvent): stan tekstu przed operacją i po niej.

Powiadomienia Discord wysyłają wyłącznie reguły z panelu (core.discord_notifications.workflow_changes).
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from django.db import transaction
from django.db.models.signals import pre_save, pre_delete

_scope = ContextVar('workflow_event_scope', default=None)


def snapshot(text_id, using):
    from texts.models import Text
    from workflow.state import current_stage
    from core.discord_notifications import person_name
    text = Text.objects.using(using).filter(pk=text_id).first()
    if text is None:
        return {'cycle': None, 'status': 'Brak tekstu', 'stages': {}, 'detail': {}}
    stages = list(text.workflow_stages.using(using).filter(workflow_cycle=text.current_workflow_cycle, is_current=True).select_related('assignment__assigned_to__person_profile'))
    stage = current_stage(stages)
    return {'cycle': text.current_workflow_cycle, 'status': stage.get_stage_type_display() if stage else 'Brak otwartego etapu',
            'detail': {s.pk: {'type': s.stage_type, 'type_display': s.get_stage_type_display(), 'released': s.is_released,
                              'started': bool(s.started_at), 'completed': s.is_completed,
                              'assigned': bool(s.assignment_id and s.assignment.assigned_to_id),
                              'iteration': s.iteration,
                              'performer': person_name(s.assignment.assigned_to) if s.assignment_id else ''}
                       for s in stages},
            'stages': {s.pk: (f'{s.get_stage_type_display()} – wykonanie {s.execution_number}' + (f' – {s.assignment.assigned_to.get_full_name() or s.assignment.assigned_to.get_username()}' if s.assignment_id and s.assignment.assigned_to_id else ''), str(s.started_at or ''), str(s.ended_at or ''), s.is_completed, s.assignment.assigned_to_id if s.assignment_id else None) for s in stages}}


def remember(sender, instance, using, **kwargs):
    scope = _scope.get()
    if scope is None:
        return
    from texts.models import Text
    text_id = instance.pk if sender is Text else instance.text_id
    if text_id:
        key = (using, text_id)
        if key not in scope['before']:
            scope['before'][key] = snapshot(text_id, using)


def changes(before, after):
    lines = []
    if before['cycle'] != after['cycle']:
        lines.append(f"Przebieg: {before['cycle'] or 'brak'} → {after['cycle'] or 'brak'}")
    for pk, stage in after['stages'].items():
        old = before['stages'].get(pk)
        if stage[3] and (not old or not old[3]):
            lines.append(f'Zakończono etap: {stage[0]}')
        elif stage[1] and (not old or old[1] != stage[1]):
            lines.append(f'Rozpoczęcie etapu: {stage[0]} ({stage[1]})')
        elif old is None:
            lines.append(f'Nowy etap: {stage[0]}')
        elif old != stage:
            lines.append(f'Korekta etapu: {stage[0]}')
    if before['cycle'] == after['cycle']:
        for pk, stage in before['stages'].items():
            if pk not in after['stages']:
                lines.append(f'Etap przeniesiono do historii lub usunięto: {stage[0]}')
    return lines


def personal_work_changes(before, after, actor_id):
    result = []
    for pk, stage in after['stages'].items():
        old = before['stages'].get(pk)
        if stage[4] != actor_id:
            continue
        if stage[3] and old and not old[3]:
            result.append('Zakończenie: ' + stage[0])
        elif not stage[3] and (old is None or old[4] != actor_id):
            result.append('Przejęcie: ' + stage[0])
        elif not stage[3] and stage[1] and old and not old[1]:
            result.append('Rozpoczęcie: ' + stage[0])
    return result


def history_correction():
    """Bieżąca operacja uzupełnia lub poprawia historię w adminie (brakujący etap, korekta dat, wykonawcy,
    cofnięcie zakończenia): zmiana trafia do dziennika, ale bez powiadomień Discord – to nie jest zdarzenie."""
    scope = _scope.get()
    if scope is not None:
        scope['correction'] = True


@contextmanager
def event_scope(user):
    if _scope.get() is not None or not user or not user.is_authenticated:
        yield
        return
    state = {'actor': user, 'before': {}}
    token = _scope.set(state)
    try:
        yield
        from core.models import WorkflowEvent
        from texts.models import Text
        for (using, text_id), before in state['before'].items():
            after = snapshot(text_id, using)
            if before == after:
                continue
            text = Text.objects.using(using).filter(pk=text_id).first()
            # Deleted texts have no destination detail; keep a readable audit record.
            title = text.title if text else f'Usunięty tekst #{text_id}'
            authors = ', '.join(a.display_name for a in text.authors.all()) if text else 'brak'
            person = getattr(user, 'person_profile', None)
            actor = str(person) if person else user.get_full_name() or user.get_username()
            own_work = personal_work_changes(before, after, user.pk)
            WorkflowEvent.objects.using(using).create(personal_work=bool(own_work), personal_work_description="; ".join(own_work)[:255], text=text, title=title, authors=authors,
                actor=user, actor_name=actor, previous_status=before['status'], next_status=after['status'],
                details='\n'.join(changes(before, after)))
            if not state.get('correction'):
                from core.discord_notifications import workflow_changes
                workflow_changes(using, text, before.get('detail', {}), after.get('detail', {}), actor=actor)
    finally:
        _scope.reset(token)


def track_workflow(function):
    @wraps(function)
    def tracked(*args, **kwargs):
        user = signature(function).bind(*args, **kwargs).arguments.get('user')
        obj = args[0] if args else None
        using = getattr(getattr(obj, '_state', None), 'db', None) or 'default'
        with transaction.atomic(using=using), event_scope(user):
            scope = _scope.get()
            bound = signature(function).bind(*args, **kwargs).arguments
            candidate = bound.get('text') or bound.get('stage')
            text_id = getattr(candidate, 'text_id', None) or getattr(candidate, 'pk', None) or bound.get('text_id')
            if text_id is None and bound.get('stage_id'):
                from workflow.models import WorkflowStage
                text_id = WorkflowStage.objects.using(using).filter(pk=bound['stage_id']).values_list('text_id', flat=True).first()
            if text_id and scope is not None and (using, text_id) not in scope['before']:
                scope['before'][(using, text_id)] = snapshot(text_id, using)
            return function(*args, **kwargs)
    return tracked


def run_admin_request(request, get_response):
    from core.request_match import resolve_request
    match = resolve_request(request)
    if match is None or request.method != 'POST' or match.namespace != 'admin' or not request.user.is_authenticated:
        return get_response(request)
    with transaction.atomic(), event_scope(request.user):
        response = get_response(request)
        if response.status_code >= 400:
            transaction.set_rollback(True)
            # Do not inspect a transaction marked for rollback.
            scope = _scope.get()
            if scope:
                scope['before'].clear()
        return response


def install():
    from texts.models import Text
    from workflow.models import WorkflowStage
    for sender in (Text, WorkflowStage):
        for signal in (pre_save, pre_delete):
            signal.connect(remember, sender=sender, weak=False, dispatch_uid=f'workflow-event-{sender._meta.label}-{id(signal)}')
