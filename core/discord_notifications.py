"""Powiadomienia Discord z reguł w panelu admina: tekst do wzięcia, nowe zgłoszenie do recenzji, rozpoczęcie
i zakończenie etapu, produkcja audiobooka, nowy wpis w kalendarzu Social Media.

Wiadomości trafiają do kolejki (DiscordNotification) w tej samej transakcji co zmiana w CMS-ie,
a wysyłka następuje dopiero po zatwierdzeniu transakcji. Błąd wysyłki nigdy nie cofa zmiany.
Ustawienie antologii „Powiadomienia Discord” ogranicza wiadomości o jej tekstach (queue()).
"""
import logging
import random
import re

from django.conf import settings
from django.db import connections, transaction
from django.urls import reverse
from django.utils import timezone

from core.account_identity import person_name

logger = logging.getLogger(__name__)
DISCORD_LIMIT = 2000
REVIEWS_PER_COMMIT = 5
# Etapy ogłaszane po trafieniu do puli „do wzięcia” – nazwa reguły to rodzaj etapu.
STAGE_TRIGGERS = ('ready_for_editing', 'first_proofreading', 'third_proofreading', 'first_verification',
                  'second_verification', 'third_verification')
# Bez wiadomości „Rozpoczęto” ani „Zakończono”: przejęcie tekstu do redakcji (zakończenie „Do redakcji”), Gotowe,
# wycofanie oraz praca autora (przekazanie pliku autorowi i jego powrót pomija osobno workflow_changes).
NOT_A_COMPLETION = {'ready_for_editing', 'ready', 'withdrawn', 'author_editing'}
STAGE_EVENTS = 'stage_completed'
EVENT_STARTED, EVENT_COMPLETED = 'Rozpoczęto', 'Zakończono'
AUDIOBOOK = 'audiobook_production'
SOCIAL_CALENDAR = 'social_calendar'

# Zgłoszenia do recenzji są anonimowe dla recenzentów (dane autora widzi tylko superuser).
ANONYMOUS_AUTHOR = '(anonimowo)'
LINK_LABEL = 'O, tutaj'
WAITING = 'stage_waiting'
REMINDER_DAYS = (7, 14, 21)
REVIEW = 'review_new'
# Ogłoszenia, po których przypominamy, gdy nikt się nie zgłosi (tekst do recenzji i etapy do wzięcia).
REMINDABLE = (REVIEW, *STAGE_TRIGGERS)
# Po tylu dniach od ogłoszenia przestajemy szukać zaległych przypomnień (np. po dłuższej przerwie w zadaniu cyklicznym).
REMINDER_WINDOW_DAYS = REMINDER_DAYS[-1] + 7


def discord_roles():
    """{nazwa roli małymi literami: ID roli} z listy „Role Discorda”."""
    from core.models import DiscordRole
    return {r.name.lower(): r.role_id for r in DiscordRole.objects.all()}


def parse_mention(raw, roles=None):
    """'@redaktor' → (['123…'], False); '@here' → ([], True); '' → ([], False). Błąd → ValueError."""
    raw = (raw or '').strip()
    if not raw:
        return [], False
    if raw.lower() == '@here':
        return [], True
    if not raw.startswith('@') or raw.lower() == '@everyone':
        raise ValueError('Wpisz nazwę roli poprzedzoną @, np. @redaktor, albo @here.')
    name = raw[1:].strip()
    lookup = roles if roles is not None else discord_roles()
    role_id = lookup.get(name.lower())
    if not role_id:
        raise ValueError(f'Nie ma roli @{name} na liście „Role Discorda”. Dodaj ją tam z ID roli.')
    return [role_id], False


DISABLED_NOTE = 'WYŁĄCZONE – test z panelu wysyła mimo to, ale zwykłe zdarzenia nie'


def configured_channel_names():
    """Nazwy kanałów z DISCORD_WEBHOOKS (bez adresów, w kolejności konfiguracji); None – konfiguracja jest błędna."""
    from core.discord_webhook import channels, ConfigurationError
    try:
        return list(channels())
    except ConfigurationError:
        return None


def rule_problems(rule, configured, roles=None):
    """Powody, dla których reguła nie wyśle wiadomości przy zwykłym zdarzeniu; pusta lista – reguła działa.

    configured – wynik configured_channel_names(), roles – {nazwa roli małymi literami: ID} (None – z bazy).
    """
    problems = [] if rule.enabled else [DISABLED_NOTE]
    if configured is None:
        problems.append('błędna konfiguracja kanałów na serwerze (DISCORD_WEBHOOKS)')
    if not rule.channel:
        if rule.trigger != WAITING:
            problems.append('brak kanału')
    elif configured is not None and rule.channel not in configured:
        problems.append(f'kanału „{rule.channel}” nie ma w konfiguracji serwera – wysyłka skończy się stanem '
                        '„Brak skonfigurowanego kanału”')
    if not len(rule.templates.all()):
        problems.append('brak treści wiadomości')
    try:
        parse_mention(rule.mention, roles)
    except ValueError as exc:
        problems.append(f'oznaczenie {rule.mention} zostanie pominięte: {exc}')
    return problems


def mention_text(role_ids, here):
    return '@here' if here else ' '.join(f'<@&{role_id}>' for role_id in role_ids)


def _length(value):
    return f'{value:,}'.replace(',', ' ') if isinstance(value, int) else ''


def text_context(text):
    anthology = getattr(text, 'anthology', None)
    return {'tytuł': text.title or '', 'autor': text.source_authors_display, 'antologia': getattr(anthology, 'title', '') or '',
            'gatunek': getattr(text, 'genre', '') or '', 'długość': _length(getattr(text, 'length', None))}


def review_context(review):
    """Zgłoszenie do recenzji: bez danych autora – kanał czytają recenzenci, dla których zgłoszenia są anonimowe."""
    anthology = getattr(review, 'anthology', None)
    return {'tytuł': review.title or '', 'autor': ANONYMOUS_AUTHOR, 'antologia': getattr(anthology, 'title', '') or '',
            'gatunek': review.genre or '', 'długość': _length(review.length), 'etap': 'Recenzja'}


def base_url():
    return settings.CMS_BASE_URL.rstrip('/')


def preview_link(*, text_id=None, review_id=None, audiobook_id=None):
    """Odnośnik Markdown „O, tutaj” do podglądu tekstu, zgłoszenia do recenzji albo strony audiobooka w CMS-ie."""
    if audiobook_id is not None:
        path = reverse('core:audiobook_detail', args=[audiobook_id])
    elif review_id is not None:
        path = reverse('core:assigned_review_detail', args=[review_id])
    elif text_id is not None:
        path = reverse('core:assigned_text_detail', args=[text_id])
    else:
        return ''
    return f'[{LINK_LABEL}](<{base_url()}{path}>)'


EVENT_WORD_RE = re.compile(r'\b(?:zakończono|zakończenie|zakończony|rozpoczęto|rozpoczęcie|rozpoczęty)\b', re.IGNORECASE)


def with_event_tag(template):
    """Treść reguły „Rozpoczęcie i zakończenie etapu” zawsze rozróżnia zdarzenie.

    Treść bez [zdarzenie] (np. dawna „Zakończono etap…” wpisana w panelu) dostaje tag w miejscu pierwszego słowa
    „zakończono/rozpoczęto” (także „zakończenie”, „zakończony”…), a bez takiego słowa – na początku. Dzięki temu
    rozpoczęcie etapu nigdy nie jest ogłaszane jako zakończenie.
    """
    if '[zdarzenie]' in template.lower():
        return template
    replaced, count = EVENT_WORD_RE.subn('[zdarzenie]', template, count=1)
    return replaced if count else f'[zdarzenie]: {template}'


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
        content = with_link(content.replace(link, '').strip() if 'link' in used else content, link)
    return content[:DISCORD_LIMIT]


def with_link(content, link):
    """Odnośnik na końcu musi zostać cały – przy zbyt długiej treści skracamy tekst, nie link."""
    return f'{content[:DISCORD_LIMIT - len(link) - 1].rstrip()} {link}'


def anthology_allows(trigger, text, using='default'):
    """Ustawienie „Powiadomienia Discord” antologii tekstu: „Wyłączone” – nic, „Tylko rozpoczęcia i zakończenia
    etapów” – tylko reguła STAGE_EVENTS. Tekst bez antologii – jak „Włączone”."""
    from texts.models import Anthology
    anthology_id = getattr(text, 'anthology_id', None)
    if not anthology_id:
        return True
    setting = Anthology.objects.using(using).filter(pk=anthology_id).values_list(
        'discord_notifications', flat=True).first()
    if setting == Anthology.DiscordNotifications.OFF:
        return False
    return setting != Anthology.DiscordNotifications.STAGES or trigger == STAGE_EVENTS


def queue(trigger, *, using='default', text=None, review=None, stage_id=None, context=None, reminder_of=None,
          reminder_days=None, link=None):
    """Tworzy wiadomość z włączonej reguły; zwraca obiekt kolejki albo None.

    Przypomnienie bez własnego kanału lub oznaczenia przejmuje je z pierwszego powiadomienia (reminder_of).
    Wiadomości o tekście podlegają ustawieniu jego antologii (anthology_allows).
    """
    from core.models import DiscordNotification, NotificationRule
    if text is not None and not anthology_allows(trigger, text, using):
        return None
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
    if link is None:
        link = preview_link(text_id=getattr(text, 'pk', None), review_id=getattr(review, 'pk', None))
    template = random.choice(templates)
    if trigger == STAGE_EVENTS and (context or {}).get('zdarzenie'):
        template = with_event_tag(template)
    content = render(template, context or {}, mention_text(role_ids, here), link)
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


def send_item(item):
    """Wysyła wiadomość z kolejki na jej kanał; zwraca (stan, ID wiadomości, błąd albo None)."""
    from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError
    try:
        configured = channels()
        if item.channel not in configured:
            return 'disabled', '', None
        role_ids = [r for r in item.role_ids.split(',') if r]
        return 'sent', send_message(configured[item.channel], item.content, role_ids=role_ids, here=item.mention_here,
                                    suppress_embeds=True), None
    except (ConfigurationError, DeliveryError) as exc:
        return ('unknown' if getattr(exc, 'uncertain', False) else 'failed'), '', exc


def deliver(pk, using='default'):
    from core.models import DiscordNotification
    rows = DiscordNotification.objects.using(using)
    if not rows.filter(pk=pk, status='pending').update(status='sending', sending_started_at=timezone.now()):
        return
    status, message_id, _ = send_item(rows.get(pk=pk))
    rows.filter(pk=pk, status='sending').update(status=status, message_id=message_id)


def audiobook_changed(text, *, actor, status, change, using='default'):
    """Każda zmiana w produkcji audiobooka (status, etapy, korekta, przypisanie korektora) – jedna reguła.
    Odnośnik prowadzi do strony audiobooka."""
    safe_queue(AUDIOBOOK, using=using, text=text, link=preview_link(audiobook_id=text.pk),
               context={**text_context(text), 'osoba': actor, 'status': status, 'zmiana': change})


def calendar_day_label(day):
    """Dzień wpisu w kalendarzu do tagu [data], np. „czwartek, 5.11.2026”."""
    from django.utils.dates import WEEKDAYS
    from django.utils.translation import override
    with override('pl'):
        return f'{str(WEEKDAYS[day.weekday()]).lower()}, {day.day}.{day.month:02d}.{day.year}'


def calendar_link(day):
    """Odnośnik Markdown „O, tutaj” do dnia w kalendarzu Social Media (miesiąc wpisu, przewinięty do dnia)."""
    path = reverse('core:social_calendar')
    return f'[{LINK_LABEL}](<{base_url()}{path}?month={day:%Y-%m}#social-day-{day:%Y-%m-%d}>)'


def calendar_context(*, day, description, platform, person):
    return {'data': calendar_day_label(day), 'opis': description, 'platforma': platform, 'osoba': person}


def social_calendar_added(entry, using='default'):
    """Nowy wpis w kalendarzu Social Media (nie edycja) – wiadomość z odnośnikiem do dnia w kalendarzu."""
    safe_queue(SOCIAL_CALENDAR, using=using, link=calendar_link(entry.day), context=calendar_context(
        day=entry.day, description=entry.description, platform=entry.get_platform_display(),
        person=entry.added_by_name or person_name(entry.added_by)))


def stage_available(stage):
    """Etap w puli „Teksty do wzięcia”: otwarty, bez wykonawcy i bez daty rozpoczęcia."""
    return bool(stage.get('released') and not stage.get('started') and not stage.get('completed') and not stage.get('assigned'))


# Pierwsza weryfikacja powstaje już przy przejęciu redakcji (rezerwacja). Do pracy trafia dopiero po redakcji.
RESERVATION_PHASE = {'editing', 'author_editing'}


def editing_open(stages):
    """Trwa redakcja (także praca autora) – pierwsza weryfikacja jest wtedy tylko rezerwacją."""
    return any(stage['type'] in RESERVATION_PHASE and not stage['completed'] for stage in stages.values())


def sent_to_author(before, after):
    """Ta zmiana otworzyła etap „Plik u autora” – zakończona przy tym redakcja to tylko przekazanie pliku."""
    def open_author_stage(stage):
        return bool(stage and stage['type'] == 'author_editing' and stage.get('released', True)
                    and not stage['completed'])
    return any(open_author_stage(stage) and not open_author_stage(before.get(pk)) for pk, stage in after.items())


# Etapy, do których redaktor przekazuje tekst (W1, W2, koordynator; w Ekstraktach także K1).
HANDOVER_TARGETS = ('first_verification', 'second_verification', 'editing_control', 'first_proofreading')


def handover_target(before, after, handed_over):
    """Etap, do którego ta zmiana przekazała tekst z redakcji: nowo otwarty albo – przy pierwszej weryfikacji –
    zarezerwowany wcześniej i teraz przekazany do pracy. Zwraca nazwę etapu albo ''."""
    for pk, stage in after.items():
        if stage['type'] in HANDOVER_TARGETS and not stage['completed'] and (
                pk not in before or (handed_over and stage['type'] == 'first_verification')):
            return stage['type_display']
    return ''


def returned_from_author(before, after):
    """Ta zmiana zakończyła etap „Plik u autora” – wznowiona przy tym redakcja to tylko powrót pliku."""
    return any(stage['type'] == 'author_editing' and stage['completed'] and not (before.get(pk) or {}).get('completed')
               for pk, stage in after.items())


def workflow_changes(using, text, before, after, actor=''):
    """Porównuje stany tekstu przed zmianą i po niej; kolejkuje wiadomości z reguł.

    Etap ogłaszamy, gdy trafia do puli „do wzięcia”. Pierwszą weryfikację ogłaszamy też po przekazaniu tekstu
    z pierwszego przebiegu redakcji, jeśli nikt jej nie zarezerwował – wcześniejsze ogłoszenie dotyczyło tylko
    rezerwacji. Powtórzone W1 (kolejne podejście) nie dostaje drugiego ogłoszenia przy przekazaniu.

    „Rozpoczęcie i zakończenie etapu” – jedna reguła dla wszystkich etapów, [zdarzenie] to „Rozpoczęto” albo
    „Zakończono”, a [osoba] – wykonawca etapu (bez przypisania – osoba, która wykonała operację: actor).
    Rozpoczęcie: także przejęcie tekstu do redakcji i wznowienie redakcji po weryfikacji; etap rozpoczęty
    i zakończony w jednej operacji – tylko zakończenie; sama rezerwacja W1 – nic. Każde przekazanie redakcji
    (do W1, W2, koordynatora; w Ekstraktach do W1 albo K1) jest ogłaszane, a [etap] wskazuje cel, np. „Redakcja →
    Pierwsza weryfikacja”. Przesłanie pliku autorowi i jego powrót nie są ani rozpoczęciem, ani zakończeniem etapu.
    """
    from workflow.models import WorkflowStage
    if text is None:
        return
    context = text_context(text)
    handed_over = editing_open(before) and not editing_open(after)
    to_author = sent_to_author(before, after)
    from_author = returned_from_author(before, after)
    target = handover_target(before, after, handed_over)

    def stage_event(event, type_display, performer=''):
        safe_queue(STAGE_EVENTS, using=using, text=text,
                   context={**context, 'zdarzenie': event, 'etap': type_display, 'osoba': performer or actor})

    for pk, stage in after.items():
        old = before.get(pk)
        first_handover = (handed_over and stage['type'] == 'first_verification'
                          and stage.get('iteration', 1) == 1)
        newly_available = not (old and stage_available(old)) or first_handover
        if stage['type'] in STAGE_TRIGGERS and stage_available(stage) and newly_available:
            safe_queue(stage['type'], using=using, text=text, stage_id=pk, context={**context, 'etap': stage['type_display']})
        if stage['type'] in NOT_A_COMPLETION:
            continue
        if stage['completed'] and not (old and old['completed']):
            if stage['type'] != 'editing':
                stage_event(EVENT_COMPLETED, stage['type_display'], stage.get('performer', ''))
            elif not to_author:
                label = f"{stage['type_display']} → {target}" if target else stage['type_display']
                stage_event(EVENT_COMPLETED, label, stage.get('performer', ''))
        elif (stage.get('started') and not stage['completed'] and not (old and old.get('started'))
              and not (stage['type'] == 'editing' and from_author)):
            stage_event(EVENT_STARTED, stage['type_display'], stage.get('performer', ''))
    # Etap zakończony i od razu przeniesiony do historii (np. decyzja koordynatora „dalsza redakcja”).
    gone = [pk for pk in before if pk not in after and not before[pk]['completed']]
    for stage in (WorkflowStage.objects.using(using).filter(pk__in=gone, is_completed=True)
                  .select_related('assignment__assigned_to__person_profile')):
        if stage.stage_type not in NOT_A_COMPLETION and not (stage.stage_type == 'editing' and to_author):
            stage_event(EVENT_COMPLETED, stage.get_stage_type_display(),
                        person_name(stage.assignment.assigned_to) if stage.assignment_id else '')


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
                     .filter(Q(trigger__in=STAGE_TRIGGERS, stage__isnull=False)
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
            kwargs = {'review': review, 'context': review_context(review)}
        kwargs['context']['dni'] = due[-1]
        if safe_queue(WAITING, using=using, reminder_of=announcement, reminder_days=due[-1], **kwargs):
            sent += 1
    return sent


# Partia zgłoszeń z jednej transakcji żyje na obiekcie połączenia (nie w słowniku modułu dzielonym między
# transakcjami i wątkami) i znika wraz z zatwierdzeniem albo wycofaniem transakcji.
_PENDING_ATTR = '_discord_pending_reviews'


class _ReviewBatch:
    """Lista ID zgłoszeń z jednej transakcji; ogłaszana jednym wywołaniem po zatwierdzeniu."""

    def __init__(self, using):
        self.using = using
        self.ids = []

    def is_registered(self, connection):
        # Django po wycofaniu transakcji (również samego punktu zapisu) usuwa jej wywołania on_commit –
        # partia bez wywołania pochodzi z poprzedniej, wycofanej transakcji i nie może być kontynuowana.
        return any(entry[1] == self.flush for entry in connection.run_on_commit)

    def flush(self):
        connection = connections[self.using]
        if getattr(connection, _PENDING_ATTR, None) is self:
            delattr(connection, _PENDING_ATTR)
        _announce_reviews(self.ids, self.using)


def review_created(review, using='default'):
    """Nowe zgłoszenie do recenzji; kilka zgłoszeń w jednej transakcji (import) – najwyżej 5 wiadomości."""
    if review.is_hidden or review.status != review.Status.NEW:
        return
    connection = connections[using]
    batch = getattr(connection, _PENDING_ATTR, None)
    if batch is None or not batch.is_registered(connection):
        batch = _ReviewBatch(using)
        setattr(connection, _PENDING_ATTR, batch)
        batch.ids.append(review.pk)
        # Poza transakcją on_commit wykonuje się od razu, więc pojedyncze zgłoszenie jest ogłaszane natychmiast.
        transaction.on_commit(batch.flush, using=using)
        return
    batch.ids.append(review.pk)


def _announce_reviews(ids, using):
    from texts.models import Review
    reviews = Review.objects.using(using).select_related('anthology').filter(pk__in=ids).order_by('pk')
    for review in reviews[:REVIEWS_PER_COMMIT]:
        safe_queue('review_new', using=using, review=review, context=review_context(review))
    if len(ids) > REVIEWS_PER_COMMIT:
        logger.info('Skipped %s review notifications in one batch', len(ids) - REVIEWS_PER_COMMIT)


def _on_review_saved(sender, instance, created, raw=False, using='default', **kwargs):
    if created and not raw:
        review_created(instance, using)


def install():
    from django.db.models.signals import post_save
    from texts.models import Review
    post_save.connect(_on_review_saved, sender=Review, weak=False, dispatch_uid='discord-review-created')
