"""Powiadomienia Discord z reguł w panelu admina: tekst do wzięcia, nowe zgłoszenie do recenzji, zakończenie etapu.

Wiadomości trafiają do kolejki (DiscordNotification) w tej samej transakcji co zmiana w CMS-ie,
a wysyłka następuje dopiero po zatwierdzeniu transakcji. Błąd wysyłki nigdy nie cofa zmiany.
"""
import logging
import os
import random

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)
DISCORD_LIMIT = 2000
REVIEWS_PER_COMMIT = 5
STAGE_TRIGGERS = {
    'ready_for_editing': 'ready_for_editing',
    'first_proofreading': 'first_proofreading',
    'third_proofreading': 'third_proofreading',
    'first_verification': 'first_verification',
    'second_verification': 'second_verification',
    'third_verification': 'third_verification',
}
NOT_A_COMPLETION = {'ready_for_editing', 'ready', 'withdrawn'}
LINK_LABEL = 'O, tutaj'
WAITING = 'stage_waiting'
REMINDER_DAYS = (7, 14, 21)
REVIEW = 'review_new'
# Ogłoszenia, po których przypominamy, gdy nikt się nie zgłosi (tekst do recenzji i etapy do wzięcia).
REMINDABLE = (REVIEW, *STAGE_TRIGGERS.values())
# Po tylu dniach od ogłoszenia przestajemy szukać zaległych przypomnień (np. po dłuższej przerwie w zadaniu cyklicznym).
REMINDER_WINDOW_DAYS = REMINDER_DAYS[-1] + 7


def parse_mention(raw, roles=None):
    """'@redaktor' → (['123…'], False); '@here' → ([], True); '' → ([], False). Błąd → ValueError."""
    from core.models import DiscordRole
    raw = (raw or '').strip()
    if not raw:
        return [], False
    if raw.lower() == '@here':
        return [], True
    if not raw.startswith('@') or raw.lower() == '@everyone':
        raise ValueError('Wpisz nazwę roli poprzedzoną @, np. @redaktor, albo @here.')
    name = raw[1:].strip()
    lookup = roles if roles is not None else {r.name.lower(): r.role_id for r in DiscordRole.objects.all()}
    role_id = lookup.get(name.lower())
    if not role_id:
        raise ValueError(f'Nie ma roli @{name} na liście „Role Discorda”. Dodaj ją tam z ID roli.')
    return [role_id], False


def mention_text(role_ids, here):
    return '@here' if here else ' '.join(f'<@&{role_id}>' for role_id in role_ids)


def _length(value):
    return f'{value:,}'.replace(',', ' ') if isinstance(value, int) else ''


def text_context(text):
    anthology = getattr(text, 'anthology', None)
    return {'tytuł': text.title or '', 'antologia': getattr(anthology, 'title', '') or '',
            'gatunek': getattr(text, 'genre', '') or '', 'długość': _length(getattr(text, 'length', None))}


def base_url():
    raw = getattr(settings, 'CMS_BASE_URL', None) or os.environ.get('CMS_BASE_URL') or 'https://cms.fantazmaty.pl'
    return str(raw).rstrip('/')


def preview_link(*, text_id=None, review_id=None):
    """Odnośnik Markdown „O, tutaj” do podglądu tekstu (albo zgłoszenia do recenzji) w CMS-ie."""
    if review_id is not None:
        path = reverse('core:assigned_review_detail', args=[review_id])
    elif text_id is not None:
        path = reverse('core:assigned_text_detail', args=[text_id])
    else:
        return ''
    return f'[{LINK_LABEL}](<{base_url()}{path}>)'


def render(template, context, mention='', link=''):
    from core.notification_models import TAG_RE
    used = set()

    def replace(match):
        key = match.group(1).lower()
        if key == 'rola':
            used.add(key)
            return mention
        if key == 'link':
            used.add(key)
            return link
        return str(context.get(key, match.group(0))) or '–'

    content = TAG_RE.sub(replace, template).strip()
    if mention and 'rola' not in used:
        content = f'{mention} {content}'
    if link and ('link' not in used or len(content) > DISCORD_LIMIT):
        # Odnośnik musi zostać cały – przy zbyt długiej treści skracamy tekst, nie link.
        content = content.replace(link, '').strip() if 'link' in used else content
        content = f'{content[:DISCORD_LIMIT - len(link) - 1].rstrip()} {link}'
    return content[:DISCORD_LIMIT]


def queue(trigger, *, using='default', text=None, review=None, stage_id=None, context=None, reminder_of=None,
          reminder_days=None):
    """Tworzy wiadomość z włączonej reguły; zwraca obiekt kolejki albo None.

    Przypomnienie bez własnego kanału lub oznaczenia przejmuje je z pierwszego powiadomienia (reminder_of).
    """
    from core.models import DiscordNotification, NotificationRule
    rule = NotificationRule.objects.using(using).filter(trigger=trigger, enabled=True).first()
    channel = (rule.channel or getattr(reminder_of, 'channel', '')) if rule else ''
    if not channel:
        return None
    templates = list(rule.templates.using(using).values_list('content', flat=True))
    if not templates:
        return None
    if not rule.mention and reminder_of is not None:
        role_ids, here = [r for r in reminder_of.role_ids.split(',') if r], reminder_of.mention_here
    else:
        try:
            role_ids, here = parse_mention(rule.mention)
        except ValueError:
            logger.warning('Discord notification rule %s has an unknown role mention', trigger)
            role_ids, here = [], False
    link = preview_link(text_id=getattr(text, 'pk', None), review_id=getattr(review, 'pk', None))
    content = render(random.choice(templates), context or {}, mention_text(role_ids, here), link)
    notification = DiscordNotification.objects.using(using).create(
        trigger=trigger, channel=channel, content=content, role_ids=','.join(role_ids),
        mention_here=here, text=text, review=review, stage_id=stage_id, reminder_of=reminder_of,
        reminder_days=reminder_days)
    transaction.on_commit(lambda pk=notification.pk: deliver_after_commit(pk, using), using=using)
    return notification


def safe_queue(trigger, **kwargs):
    """Kolejkowanie nie może zablokować zmiany w CMS-ie – błąd zostaje w logu."""
    using = kwargs.get('using', 'default')
    try:
        with transaction.atomic(using=using):
            return queue(trigger, **kwargs)
    except Exception:
        logger.exception('Could not queue Discord notification %s', trigger)
        return None


def deliver_after_commit(pk, using='default'):
    try:
        deliver(pk, using)
    except Exception:
        logger.error('Discord notification failed after commit; notification=%s', pk)
        try:
            from core.models import DiscordNotification
            DiscordNotification.objects.using(using).filter(pk=pk, status__in=('pending', 'sending')).update(status='unknown')
        except Exception:
            logger.error('Could not record notification outcome; notification=%s', pk)


def deliver(pk, using='default'):
    from core.models import DiscordNotification
    from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError
    rows = DiscordNotification.objects.using(using)
    if not rows.filter(pk=pk, status='pending').update(status='sending', sending_started_at=timezone.now()):
        return
    item = rows.get(pk=pk)
    status, message_id = 'failed', ''
    try:
        configured = channels()
        if item.channel not in configured:
            status = 'disabled'
        else:
            role_ids = [r for r in item.role_ids.split(',') if r]
            message_id = send_message(configured[item.channel], item.content, role_ids=role_ids, here=item.mention_here,
                                      suppress_embeds=True)
            status = 'sent'
    except ConfigurationError:
        status = 'failed'
    except DeliveryError as exc:
        status = 'unknown' if exc.uncertain else 'failed'
    rows.filter(pk=pk, status='sending').update(status=status, message_id=message_id)


def stage_available(stage):
    """Etap w puli „Teksty do wzięcia”: otwarty, bez wykonawcy i bez daty rozpoczęcia."""
    return bool(stage.get('released') and not stage.get('started') and not stage.get('completed') and not stage.get('assigned'))


def workflow_changes(using, text, before, after):
    """Porównuje stany tekstu przed zmianą i po niej; kolejkuje wiadomości z reguł."""
    from workflow.models import WorkflowStage
    if text is None:
        return
    context = text_context(text)
    for pk, stage in after.items():
        old = before.get(pk)
        trigger = STAGE_TRIGGERS.get(stage['type'])
        if trigger and stage_available(stage) and not (old and stage_available(old)):
            safe_queue(trigger, using=using, text=text, stage_id=pk, context={**context, 'etap': stage['type_display']})
        if stage['completed'] and not (old and old['completed']) and stage['type'] not in NOT_A_COMPLETION:
            safe_queue('stage_completed', using=using, text=text, context={**context, 'etap': stage['type_display']})
    # Etap zakończony i od razu przeniesiony do historii (np. decyzja koordynatora „dalsza redakcja”).
    gone = [pk for pk in before if pk not in after and not before[pk]['completed']]
    for stage in WorkflowStage.objects.using(using).filter(pk__in=gone, is_completed=True):
        if stage.stage_type not in NOT_A_COMPLETION:
            safe_queue('stage_completed', using=using, text=text,
                       context={**context, 'etap': stage.get_stage_type_display()})


def stage_still_waiting(stage):
    """Etap nadal jest bieżący, otwarty i bez wykonawcy – tak jak w chwili pierwszego powiadomienia."""
    text = stage.text
    return bool(stage.is_current and stage.workflow_cycle == text.current_workflow_cycle and stage_available({
        'released': stage.is_released, 'started': stage.started_at, 'completed': stage.is_completed,
        'assigned': stage.assignment_id and stage.assignment.assigned_to_id}))


def review_still_waiting(review):
    """Zgłoszenie nadal czeka na pierwszego recenzenta (jak w chwili ogłoszenia)."""
    return bool(review.status == review.Status.NEW and not review.is_hidden and not review.assignments.exists())


def send_waiting_reminders(now=None, using='default'):
    """Przypomnienia po 7, 14 i 21 dniach od ogłoszenia, że tekst czeka na osobę (zadanie cykliczne raz dziennie).

    Dotyczy zgłoszeń do recenzji i etapów do wzięcia. Liczy się ostatnie ogłoszenie danego etapu – po cofnięciu
    przydziału odliczanie zaczyna się od nowa. Po przerwie w zadaniu wysyłane jest tylko najdalsze zaległe
    przypomnienie, bez serii nadrobionych. Zwraca liczbę zakolejkowanych przypomnień.
    """
    from datetime import timedelta
    from django.db.models import Q
    from core.models import DiscordNotification, NotificationRule
    if not NotificationRule.objects.using(using).filter(trigger=WAITING, enabled=True).exists():
        return 0
    now = now or timezone.now()
    announcements = (DiscordNotification.objects.using(using)
                     .filter(Q(trigger__in=set(STAGE_TRIGGERS.values()), stage__isnull=False)
                             | Q(trigger=REVIEW, review__isnull=False),
                             created_at__gte=now - timedelta(days=REMINDER_WINDOW_DAYS))
                     .select_related('stage__text__anthology', 'stage__assignment', 'review__anthology')
                     .order_by('-created_at', '-pk'))
    latest = {}
    for item in announcements:
        latest.setdefault(('stage', item.stage_id) if item.stage_id else ('review', item.review_id), item)
    sent = 0
    for announcement in latest.values():
        age = now - announcement.created_at
        due = [days for days in REMINDER_DAYS if age >= timedelta(days=days)]
        if not due or announcement.reminders.filter(reminder_days__gte=due[-1]).exists():
            continue
        stage, review = announcement.stage, announcement.review
        if stage is not None:
            if not stage_still_waiting(stage):
                continue
            kwargs = {'text': stage.text, 'stage_id': stage.pk,
                      'context': {**text_context(stage.text), 'etap': stage.get_stage_type_display()}}
        else:
            if not review_still_waiting(review):
                continue
            kwargs = {'review': review, 'context': {**text_context(review), 'etap': 'Recenzja'}}
        kwargs['context']['dni'] = due[-1]
        if safe_queue(WAITING, using=using, reminder_of=announcement, reminder_days=due[-1], **kwargs):
            sent += 1
    return sent


_pending_reviews = {}


def review_created(review, using='default'):
    """Nowe zgłoszenie do recenzji; kilka zgłoszeń w jednej transakcji (import) – najwyżej 5 wiadomości."""
    if review.is_hidden or review.status != review.Status.NEW:
        return
    _pending_reviews.setdefault(using, []).append(review.pk)
    # Każde zgłoszenie rejestruje opróżnienie kolejki; pierwsze wywołanie wysyła całą partię, kolejne nic nie robią.
    transaction.on_commit(lambda: _flush_reviews(using), using=using)


def _flush_reviews(using):
    from texts.models import Review
    ids = _pending_reviews.pop(using, [])
    reviews = Review.objects.using(using).select_related('anthology').filter(pk__in=ids).order_by('pk')
    for review in reviews[:REVIEWS_PER_COMMIT]:
        safe_queue('review_new', using=using, review=review, context={**text_context(review), 'etap': 'Recenzja'})
    if len(ids) > REVIEWS_PER_COMMIT:
        logger.info('Skipped %s review notifications in one batch', len(ids) - REVIEWS_PER_COMMIT)


def sample_context():
    return {'tytuł': 'Latarnik', 'antologia': 'Cienie miasta', 'gatunek': 'fantasy', 'długość': '25 000',
            'etap': 'Redakcja'}


def _on_review_saved(sender, instance, created, raw=False, using='default', **kwargs):
    if created and not raw:
        review_created(instance, using)


def install():
    from django.db.models.signals import post_save
    from texts.models import Review
    post_save.connect(_on_review_saved, sender=Review, weak=False, dispatch_uid='discord-review-created')
