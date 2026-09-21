"""Best-effort Discord notifications sent only after a successful database commit."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import logging
import os
from django.conf import settings
from django.db import transaction
from django.db.models.signals import pre_save, pre_delete
from django.urls import resolve, Resolver404

_scope = ContextVar('workflow_event_scope', default=None)
logger = logging.getLogger(__name__)


def snapshot(text_id, using):
    from texts.models import Text
    from workflow.state import current_stage
    text = Text.objects.using(using).filter(pk=text_id).first()
    if text is None:
        return {'cycle': None, 'status': 'Brak tekstu', 'stages': {}}
    stages = list(text.workflow_stages.using(using).filter(workflow_cycle=text.current_workflow_cycle, is_current=True).select_related('assignment__assigned_to__person_profile'))
    stage = current_stage(stages)
    return {'cycle': text.current_workflow_cycle, 'status': stage.get_stage_type_display() if stage else 'Brak otwartego etapu',
            'stages': {s.pk: (f'{s.get_stage_type_display()} — wykonanie {s.execution_number}' + (f' — {s.assignment.assigned_to.get_full_name() or s.assignment.assigned_to.get_username()}' if s.assignment_id and s.assignment.assigned_to_id else ''), str(s.started_at or ''), str(s.ended_at or ''), s.is_completed) for s in stages}}


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
            authors = ', '.join(str(a) for a in text.authors.all()) if text else 'brak'
            person = getattr(user, 'person_profile', None)
            actor = str(person) if person else user.get_full_name() or user.get_username()
            event = WorkflowEvent.objects.using(using).create(text=text, title=title, authors=authors,
                actor=user, actor_name=actor, previous_status=before['status'], next_status=after['status'],
                details='\n'.join(changes(before, after)), channel=workflow_channel())
            transaction.on_commit(
                lambda pk=event.pk, db=using: notify_after_commit(pk, db), using=using
            )
    finally:
        _scope.reset(token)


def track_workflow(function):
    @wraps(function)
    def tracked(*args, **kwargs):
        user = signature(function).bind(*args, **kwargs).arguments.get('user')
        obj = args[0] if args else None
        using = getattr(getattr(obj, '_state', None), 'db', None) or 'default'
        with transaction.atomic(using=using), event_scope(user):
            return function(*args, **kwargs)
    return tracked


def run_admin_request(request, get_response):
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return get_response(request)
    if request.method != 'POST' or match.namespace != 'admin' or not request.user.is_authenticated:
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


def workflow_channel():
    return str(getattr(settings, 'DISCORD_WORKFLOW_CHANNEL', os.environ.get('DISCORD_WORKFLOW_CHANNEL', 'Testowy')))


def message(event):
    # No emails, phone numbers or mentions. Keep every identifying field even for long titles.
    status = event.next_status if event.previous_status == event.next_status else f'{event.previous_status} → {event.next_status}'
    return (f'Tekst: {event.title[:400]}\nAutor: {event.authors[:400]}\n'
            f'Status: {status}\n'
            f'Wykonał(a): {event.actor_name[:200]}\n{event.details[:700]}')[:2000]


def notify_after_commit(pk, using='default'):
    # Include database access in the guard: even a failure to record the delivery
    # result must not turn a committed workflow operation into an HTTP 500.
    try:
        deliver(pk, using)
    except Exception:
        logger.error('Workflow notification failed after commit; event=%s', pk)
        try:
            from core.models import WorkflowEvent
            WorkflowEvent.objects.using(using).filter(
                pk=pk, status__in=('pending', 'sending')
            ).update(status='unknown')
        except Exception:
            logger.error('Could not record notification outcome; event=%s', pk)


def deliver(pk, using='default'):
    from core.models import WorkflowEvent
    from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError
    events = WorkflowEvent.objects.using(using)
    from django.utils import timezone
    if not events.filter(pk=pk, status='pending').update(status='sending', sending_started_at=timezone.now()):
        return
    event = events.get(pk=pk)
    status, message_id = 'failed', ''
    try:
        configured = channels()
        if event.channel not in configured:
            status = 'disabled'
        else:
            message_id = send_message(configured[event.channel], message(event))
            status = 'sent'
    except ConfigurationError:
        status = 'failed'
    except DeliveryError as exc:
        status = 'unknown' if exc.uncertain else 'failed'
    except Exception:
        # Never log webhook URLs or exception bodies.
        logger.error('Unexpected workflow notification failure; event=%s', pk)
        status = 'unknown'
    events.filter(pk=pk, status='sending').update(status=status, message_id=message_id)


def install():
    from texts.models import Text
    from workflow.models import WorkflowStage
    for sender in (Text, WorkflowStage):
        for signal in (pre_save, pre_delete):
            signal.connect(remember, sender=sender, weak=False, dispatch_uid=f'workflow-event-{sender._meta.label}-{id(signal)}')
