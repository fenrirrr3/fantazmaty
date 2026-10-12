"""Powiadomienia Discord z reguł w panelu admina: tekst do wzięcia, nowe zgłoszenie do recenzji, zakończenie etapu.

Wiadomości trafiają do kolejki (DiscordNotification) w tej samej transakcji co zmiana w CMS-ie,
a wysyłka następuje dopiero po zatwierdzeniu transakcji. Błąd wysyłki nigdy nie cofa zmiany.
"""
import logging
import random

from django.db import transaction
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


def render(template, context, mention=''):
    from core.notification_models import TAG_RE
    used_mention = False

    def replace(match):
        nonlocal used_mention
        key = match.group(1).lower()
        if key == 'rola':
            used_mention = True
            return mention
        return str(context.get(key, match.group(0))) or '–'

    content = TAG_RE.sub(replace, template).strip()
    if mention and not used_mention:
        content = f'{mention} {content}'
    return content[:DISCORD_LIMIT]


def queue(trigger, *, using='default', text=None, review=None, context=None):
    """Tworzy wiadomość z włączonej reguły; zwraca obiekt kolejki albo None."""
    from core.models import DiscordNotification, NotificationRule
    rule = NotificationRule.objects.using(using).filter(trigger=trigger, enabled=True).exclude(channel='').first()
    if rule is None:
        return None
    templates = list(rule.templates.using(using).values_list('content', flat=True))
    if not templates:
        return None
    try:
        role_ids, here = parse_mention(rule.mention)
    except ValueError:
        logger.warning('Discord notification rule %s has an unknown role mention', trigger)
        role_ids, here = [], False
    content = render(random.choice(templates), context or {}, mention_text(role_ids, here))
    notification = DiscordNotification.objects.using(using).create(
        trigger=trigger, channel=rule.channel, content=content, role_ids=','.join(role_ids),
        mention_here=here, text=text, review=review)
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
            message_id = send_message(configured[item.channel], item.content, role_ids=role_ids, here=item.mention_here)
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
            safe_queue(trigger, using=using, text=text, context={**context, 'etap': stage['type_display']})
        if stage['completed'] and not (old and old['completed']) and stage['type'] not in NOT_A_COMPLETION:
            safe_queue('stage_completed', using=using, text=text, context={**context, 'etap': stage['type_display']})
    # Etap zakończony i od razu przeniesiony do historii (np. decyzja koordynatora „dalsza redakcja”).
    gone = [pk for pk in before if pk not in after and not before[pk]['completed']]
    for stage in WorkflowStage.objects.using(using).filter(pk__in=gone, is_completed=True):
        if stage.stage_type not in NOT_A_COMPLETION:
            safe_queue('stage_completed', using=using, text=text,
                       context={**context, 'etap': stage.get_stage_type_display()})


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
