"""Read-only checks and publication credits; never repair data implicitly."""
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES
from core.sort_keys import text_key
from collections import defaultdict
from difflib import SequenceMatcher
import unicodedata
from django.contrib.auth import get_user_model
from django.db.models import F, Q, Prefetch
from django.urls import reverse
from django.utils import timezone
from core.models import AnthologyCorrection
from people.models import Person
from texts.models import Text, Review, ReviewAssignment, AnthologyTask
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.services import STAGE_ROLES


def folded(value):
    value = unicodedata.normalize('NFKD', value or '').casefold().replace('ł', 'l')
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value if not unicodedata.combining(c)).split())


def issue(label, detail, url):
    return {'label':label, 'detail':detail, 'url':url}


def integrity_issues():
    issues = []
    from django.db.models import Exists, OuterRef
    current_for_text = WorkflowStage.objects.filter(
        text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'), is_current=True,
    )
    missing = Text.objects.annotate(has_current_stage=Exists(current_for_text)).filter(has_current_stage=False)
    for text in missing.order_by('pk'):
        issues.append(issue('Tekst bez bieżącego etapu', text.title,
            reverse('core:assigned_text_detail', args=[text.pk])))
    current = WorkflowStage.objects.current_cycle().filter(workflow_cycle=F('text__current_workflow_cycle')).select_related('text', 'assignment')
    assignments = {(a.text_id,a.workflow_cycle,a.role):a for a in WorkflowRoleAssignment.objects.current_cycle().filter(workflow_cycle=F('text__current_workflow_cycle')).select_related('assigned_to__person_profile','text')}
    for stage in current.filter(is_completed=False, ended_at__isnull=True, started_at__lte=timezone.localdate()).exclude(stage_type__in=('ready','withdrawn')):
        role = STAGE_ROLES.get(stage.stage_type)
        a = stage.assignment or assignments.get((stage.text_id,stage.workflow_cycle,role))
        if role and (a is None or a.assigned_to_id is None):
            issues.append(issue('Rozpoczęty etap bez wykonawcy',f'{stage.text.title}: {stage.get_stage_type_display()}',reverse('core:assigned_text_detail',args=[stage.text_id])))
    from workflow.services import NEXT_STAGE_TYPES
    grouped = defaultdict(list)
    for stage in current:
        grouped[(stage.text_id, stage.workflow_cycle)].append(stage)
    for stages in grouped.values():
        text = stages[0].text
        url = reverse('core:assigned_text_detail', args=[text.pk])
        opened = defaultdict(list)
        for stage in stages:
            if (stage.ended_at is not None and not stage.is_completed) or (stage.is_completed and not stage.imported_completed and not stage.is_skipped and (stage.started_at is None or stage.ended_at is None)):
                issues.append(issue('Sprzeczny zapis zakończenia', f'{text.title}: {stage.get_stage_type_display()}', url))
            if stage.is_released and not stage.is_completed and stage.ended_at is None:
                opened[stage.stage_type].append(stage)
        for kind, repeated in opened.items():
            if len(repeated) > 1:
                issues.append(issue('Kilka otwartych etapów tego samego rodzaju', f'{text.title}: {repeated[0].get_stage_type_display()}', url))
        if not any(s.stage_type in ('ready', 'withdrawn') for s in stages) and not any(s.repetition_id for s in stages):
            for stage in stages:
                successor = NEXT_STAGE_TYPES.get(stage.stage_type)
                if stage.is_completed and not stage.imported_completed and successor and not any(s.stage_type == successor and (s.pk > stage.pk or (s.started_at and stage.ended_at and s.started_at >= stage.ended_at)) for s in stages):
                    issues.append(issue('Brak następnego etapu po zakończeniu', f'{text.title}: {stage.get_stage_type_display()}', url))
    for a in assignments.values():
        if not a.assigned_to_id:
            continue
        person = getattr(a.assigned_to,'person_profile',None)
        if not a.assigned_to.is_active or (not a.assigned_to.is_superuser and (person is None or not person.is_active)):
            # A completed role is historical work, not an active staffing problem.
            stages = grouped.get((a.text_id, a.workflow_cycle), [])
            terminal = any(s.stage_type in ('ready','withdrawn') for s in stages)
            matching = [s for s in stages if STAGE_ROLES.get(s.stage_type)==a.role]
            if not terminal and (not matching or any(not s.is_completed for s in matching)):
                issues.append(issue('Przydział do nieaktywnej osoby',f'{a.text.title}: {a.get_role_display()} – {a.assigned_to}',reverse('core:assigned_text_detail',args=[a.text_id])))
    from workflow.models import WorkflowRepetition
    closed_text_ids = set(WorkflowStage.objects.current_cycle().filter(stage_type__in=('ready','withdrawn')).values_list('text_id', flat=True))
    runs = WorkflowRepetition.objects.select_related('text').prefetch_related(Prefetch('stages', queryset=WorkflowStage.objects.order_by('queue_position','pk'), to_attr='ordered_steps'))
    for run in runs:
        steps = run.ordered_steps
        remaining=[s for s in steps if not s.is_completed]
        url=reverse('core:assigned_text_detail',args=[run.text_id])
        detail=f'{run.text.title}: kolejka #{run.pk}'
        closed=run.completed_at is not None or run.canceled_at is not None
        if run.completed_at and run.canceled_at:
            issues.append(issue('Kolejka jednocześnie zakończona i anulowana',detail,url))
        if run.completed_at and remaining:
            issues.append(issue('Zakończona kolejka zawiera niewykonane etapy',detail,url))
        if run.canceled_at and any(s.is_current and not s.is_completed for s in steps):
            issues.append(issue('Anulowana kolejka nadal udostępnia pracę',detail,url))
        if not closed:
            if not remaining or not any(s.is_current and s.is_released for s in remaining):
                issues.append(issue('Otwarta kolejka bez dostępnego etapu',detail,url))
            if remaining and any(s.is_current and s.is_released for s in remaining[1:]):
                issues.append(issue('Przedwcześnie udostępniony etap kolejki',detail,url))
            if run.text_id in closed_text_ids:
                issues.append(issue('Otwarta kolejka przy zamkniętym tekście',detail,url))
        positions=[s.queue_position for s in steps]
        if positions != list(range(len(positions))):
            issues.append(issue('Nieprawidłowa numeracja kolejki',detail,url))
    for user in get_user_model().objects.filter(is_active=True,person_profile__isnull=True):
        issues.append(issue('Konto bez profilu zespołu',str(user),reverse('admin:auth_user_change',args=[user.pk])))
    for person in Person.objects.select_related('user').exclude(user__isnull=True):
        if person.email and person.user.email and person.email.casefold()!=person.user.email.casefold():
            issues.append(issue('Różne e-maile powiązanej osoby i konta',str(person)+' – powiązanie po ID pozostaje zachowane',reverse('admin:people_person_change',args=[person.pk])))
    for text in Text.objects.filter(authors__isnull=True):
        issues.append(issue('Tekst bez autora',text.title,reverse('core:assigned_text_detail',args=[text.pk])))
    return issues


def unlinked_review_candidates():
    """Possible legacy detachments, never inferred as confirmed historical facts."""
    from django.db.models import Exists, OuterRef
    texts = Text.objects.filter(anthology_id=OuterRef('anthology_id'), title__iexact=OuterRef('title'))
    return Review.objects.filter(status=Review.Status.ACCEPTED, copied_text__isnull=True,
        publication_detached=False).annotate(has_matching_text=Exists(texts)).filter(
        has_matching_text=True).select_related('anthology').order_by('anthology__title', 'title', 'pk')



def duplicate_candidates(title, anthology_id, author_ids, *, exclude_text_id=None, exclude_review_id=None, email=''):
    if not folded(title) or not anthology_id:
        return []
    text_q = Q(authors__pk__in=author_ids)
    review_q = Q(author_id__in=author_ids) | Q(coauthors__pk__in=author_ids)
    if email:
        text_q |= Q(authors__email__iexact=email)
        review_q |= Q(email__iexact=email)
    if exclude_review_id and not exclude_text_id:
        exclude_text_id = Review.objects.filter(pk=exclude_review_id).values_list('copied_text_id',flat=True).first()
    candidates=[]
    for model, match, excluded, route in ((Text,text_q,exclude_text_id,'assigned_text_detail'),(Review,review_q,exclude_review_id,'assigned_review_detail')):
        for obj in model.objects.filter(match,anthology_id=anthology_id).exclude(pk=excluded).distinct():
            if model is Review and exclude_text_id and obj.copied_text_id==exclude_text_id:
                continue
            if SequenceMatcher(None,folded(title),folded(obj.title)).ratio()>=0.88:
                candidates.append(issue('Możliwy duplikat',obj.title,reverse('core:'+route,args=[obj.pk])))
    return candidates


def anthology_checklist(anthology):
    rows=[]
    texts=list(Text.objects.filter(anthology=anthology).prefetch_related(
        'authors', Prefetch('workflow_stages', queryset=WorkflowStage.objects.current_cycle().filter(
            workflow_cycle=F('text__current_workflow_cycle')), to_attr='checklist_stages')))
    included=[]
    for text in texts:
        stages=text.checklist_stages
        if any(s.stage_type=='withdrawn' for s in stages):continue
        included.append(text)
        url=reverse('core:assigned_text_detail',args=[text.pk])
        if not any(s.stage_type=='ready' for s in stages):rows.append(issue('Tekst nie jest gotowy',text.title,url))
        authors=list(text.authors.all())
        if not authors:rows.append(issue('Brak autora',text.title,url))
    if not included:rows.append(issue('Brak tekstów do wydania','Antologia nie ma niewycofanych tekstów.',reverse('core:text_list')+f'?anthology={anthology.pk}&hide_ready=0'))
    for correction in AnthologyCorrection.objects.filter(anthology=anthology).exclude(status__in=('applied','rejected')):
        rows.append(issue('Uwaga wymaga obsługi',f'{correction.story_title} – {correction.get_status_display()}',reverse('core:anthology_corrections')+f'?anthology={anthology.pk}'))
    if anthology.cover_status!='ready':rows.append(issue('Okładka nie jest gotowa',anthology.title,reverse('admin:texts_anthology_change',args=[anthology.pk])))
    tasks = {task.task_type: task for task in anthology.production_tasks.all()}
    for kind, label in AnthologyTask.TaskType.choices:
        task = tasks.get(kind)
        if task is None or task.status != AnthologyTask.Status.READY:
            rows.append(issue('Niezakończone zadanie produkcyjne', label, reverse('core:anthology_detail', args=[anthology.pk])))
    return rows


def _credit_person(person=None, user=None, fallback_id=None):
    identity = ('person', person.pk) if person is not None else ('user', user.pk) if user is not None else ('review', fallback_id)
    source = person if person is not None else user
    first = (getattr(source, 'first_name', '') or '').strip()
    last = (getattr(source, 'last_name', '') or '').strip()
    name = f'{first} {last}'.strip()
    # A username/email is never a public credit label.
    if not name or '@' in name:
        name, first, last = 'Brak danych wykonawcy', '', ''
    return identity, name, (text_key(last), text_key(first), str(identity))


def anthology_credits(anthology, *, text=None, include_assignments=False):
    credits = {}
    def add(person, user, role, title, fallback_id=None):
        identity, name, sort_key = _credit_person(person, user, fallback_id)
        item = credits.setdefault((identity, role), {'identity': identity, 'name': name, 'sort_key': sort_key, 'role': role, 'works': set()})
        item['works'].add(title)
    completed = WorkflowStage.objects.filter(text__anthology=anthology, is_completed=True, assignment__assigned_to__isnull=False).select_related('assignment__assigned_to__person_profile', 'text')
    if text is not None:
        completed = completed.filter(text=text)
    for stage in completed:
        a = stage.assignment
        add(getattr(a.assigned_to, 'person_profile', None), a.assigned_to, a.get_role_display(), stage.text.title)
    if include_assignments and text is not None:
        # The text's working credit list includes the recipient immediately,
        # while publication-wide credits retain their completed-work scope.
        assigned = WorkflowRoleAssignment.objects.current_cycle().filter(
            text=text, assigned_to__isnull=False,
        ).exclude(repetition__canceled_at__isnull=False).select_related('assigned_to__person_profile')
        for assignment in assigned:
            add(getattr(assignment.assigned_to, 'person_profile', None), assignment.assigned_to,
                assignment.get_role_display(), text.title)
        from workflow.models import WorkflowHandoff
        handoffs = WorkflowHandoff.objects.filter(text=text).exclude(
            stage__repetition__canceled_at__isnull=False,
        ).select_related('previous_assignment__assigned_to__person_profile',
                         'new_assignment__assigned_to__person_profile')
        for handoff in handoffs:
            for assignment in (handoff.previous_assignment, handoff.new_assignment):
                if assignment.assigned_to_id:
                    add(getattr(assignment.assigned_to, 'person_profile', None), assignment.assigned_to,
                        assignment.get_role_display(), text.title)
    review_scope = Q(review__copied_text=text) if text is not None else (Q(review__copied_text__anthology=anthology)|Q(review__anthology=anthology,review__status='accepted'))
    for a in ReviewAssignment.objects.filter(review_scope).exclude(opinion__in=('', 'reading')).select_related('user__person_profile','historical_person','review'):
        person = a.historical_person or (getattr(a.user, 'person_profile', None) if a.user_id else None)
        add(person, a.user, 'Recenzent', a.review.title, a.pk)
    for task in (AnthologyTask.objects.filter(anthology=anthology,status='ready').select_related('assigned_to') if text is None else []):
        if task.assigned_to_id:
            add(task.assigned_to, None, task.get_task_type_display(), anthology.title)
    from illustrations.models import Illustration
    illustrations = Illustration.objects.filter(text__anthology=anthology,status='delivered').select_related('illustrator','text')
    if text is not None:
        illustrations = illustrations.filter(text=text)
    for illustration in illustrations:
        if illustration.illustrator_id:
            add(illustration.illustrator, None, 'Ilustrator', illustration.text.title)
    if text is None and anthology.cover_author.strip():
        name = anthology.cover_author.strip()
        if '@' in name:
            name = 'Brak danych wykonawcy'
        identity = ('cover', anthology.pk)
        credits[(identity, 'Okładka')] = {'identity': identity, 'name': name, 'sort_key': (text_key(name.split()[-1]), text_key(name), str(identity)), 'role': 'Okładka', 'works': {anthology.title}}
    return [dict(item, works=sorted(item['works'])) for item in sorted(credits.values(), key=lambda item: (item['role'], item['sort_key']))]


def anthology_credit_groups(anthology, *, text=None, include_assignments=False):
    groups = {label: {} for label in (
        'Redakcja', 'Kontrola redakcji', 'Korekta', 'Weryfikacja',
        'Kontrola weryfikacji', 'Kontrola przed składem', 'Recenzje', 'Ilustracja',
    )}
    mapping = {
        'Redaktor': 'Redakcja', 'K. redakcji': 'Kontrola redakcji',
        'Kontrola redakcji': 'Kontrola redakcji', 'K. weryfikacji': 'Kontrola weryfikacji',
        'Stylowanie': 'Kontrola przed składem', 'Recenzent': 'Recenzje',
        'Ilustrator': 'Ilustracja', 'Okładka': 'Ilustracja',
    }
    for row in anthology_credits(anthology, text=text, include_assignments=include_assignments):
        role = row['role']
        group = ('Korekta' if role.startswith('Korektor ') else
                 'Weryfikacja' if role.startswith('Weryfikator ') else mapping.get(role))
        if group and row['name'].strip():
            groups[group][row['identity']] = row
    return [{'label': label, 'names': [row['name'] for row in sorted(people.values(), key=lambda row: row['sort_key'])]} for label, people in groups.items()]


def text_credit_groups(text):
    return anthology_credit_groups(text.anthology, text=text, include_assignments=True)
