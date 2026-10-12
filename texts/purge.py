"""Trwałe usunięcie tekstu razem z etapami i całą historią (tylko superużytkownik, z panelu admina).

Usuwane są wyłącznie rekordy samego tekstu: etapy, przydziały, powtórzenia, przekazania, notatki, dane tłumaczenia,
audiobook z etapami, wpisy historii zmian i wysłane o nim powiadomienia Discord oraz wpisy dziennika panelu admina.
Zostają osoby, autorzy, antologia, zgłoszenie, z którego powstał tekst (odłączone), i errata antologii.
"""
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction


def _records(text_id, using):
    from core.audiobook_models import Audiobook, AudiobookStage
    from core.models import DiscordNotification, WorkflowEvent
    from texts.models import Text, TextNote, TextTranslation
    from workflow.models import WorkflowHandoff, WorkflowRepetition, WorkflowRoleAssignment, WorkflowStage
    stages = WorkflowStage.objects.using(using).filter(text_id=text_id)
    assignments = WorkflowRoleAssignment.objects.using(using).filter(text_id=text_id)
    logs = LogEntry.objects.using(using).none()
    for model, ids in ((Text, [text_id]), (WorkflowStage, stages.values_list('pk', flat=True)),
                       (WorkflowRoleAssignment, assignments.values_list('pk', flat=True))):
        content_type = ContentType.objects.db_manager(using).get_for_model(model)
        logs = logs | LogEntry.objects.using(using).filter(content_type=content_type,
                                                           object_id__in=[str(pk) for pk in ids])
    # Kolejność ma znaczenie: przekazania chronią etapy i przydziały, etapy – przydziały i powtórzenia.
    return [
        ('przekazania etapów', WorkflowHandoff.objects.using(using).filter(text_id=text_id)),
        ('etapy', stages),
        ('przydziały osób', assignments),
        ('powtórzenia etapów', WorkflowRepetition.objects.using(using).filter(text_id=text_id)),
        ('notatki', TextNote.objects.using(using).filter(text_id=text_id)),
        ('dane tłumaczenia', TextTranslation.objects.using(using).filter(text_id=text_id)),
        ('etapy audiobooka', AudiobookStage.objects.using(using).filter(text_id=text_id)),
        ('audiobook', Audiobook.objects.using(using).filter(text_id=text_id)),
        ('historia zmian workflow', WorkflowEvent.objects.using(using).filter(text_id=text_id)),
        ('wysłane powiadomienia Discord', DiscordNotification.objects.using(using).filter(text_id=text_id)),
        ('wpisy dziennika panelu admina', logs),
    ]


def purge_summary(text, using='default'):
    """Lista (opis, liczba) rekordów, które zostaną usunięte razem z tekstem."""
    return [(label, rows.count()) for label, rows in _records(text.pk, using)]


def purge_blocker(text, using='default'):
    from illustrations.models import Illustration
    if Illustration.objects.using(using).filter(text_id=text.pk).exists():
        return 'Do tekstu są przypisane ilustracje – to osobne prace ilustratorów. Najpierw przepnij lub usuń ilustracje.'
    return None


def purge_text(text, using='default'):
    """Usuwa tekst ze wszystkimi jego etapami i historią; zwraca podsumowanie usuniętych rekordów."""
    from core.audiobook_models import Audiobook
    from core.workflow_events import _scope
    from texts.models import Review, Text
    reason = purge_blocker(text, using)
    if reason:
        raise ValidationError(reason)
    text_id = text.pk
    with transaction.atomic(using=using):
        text = Text.objects.using(using).select_for_update().get(pk=text_id)
        summary = purge_summary(text, using)
        Audiobook.objects.using(using).filter(text_id=text_id).update(active_stage=None)
        # Zgłoszenie zostaje, ale nie może wrócić do puli „do przeniesienia do tekstów”.
        Review.objects.using(using).filter(copied_text_id=text_id).update(publication_detached=True)
        for _label, rows in _records(text_id, using):
            rows.delete()
        text.delete()
        # Usunięcie etapów i tekstu nie może zostawić nowego wpisu „Usunięty tekst” w historii ani na Discordzie.
        scope = _scope.get()
        if scope is not None:
            scope['before'].pop((using, text_id), None)
    return summary
