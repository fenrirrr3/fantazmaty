"""Read-only checks and publication credits; never repair data implicitly."""
from workflow.catalog import IMPORT_ONLY_STAGE_TYPES
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
            if (stage.ended_at is not None and not stage.is_completed) or (stage.is_completed and not stage.imported_completed and (stage.started_at is None or stage.ended_at is None)):
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
                issues.append(issue('Przydział do nieaktywnej osoby',f'{a.text.title}: {a.get_role_display()} — {a.assigned_to}',reverse('core:assigned_text_detail',args=[a.text_id])))
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
            issues.append(issue('Różne e-maile powiązanej osoby i konta',str(person)+' — powiązanie po ID pozostaje zachowane',reverse('admin:people_person_change',args=[person.pk])))
    for text in Text.objects.filter(authors__isnull=True):
        issues.append(issue('Tekst bez autora',text.title,reverse('core:assigned_text_detail',args=[text.pk])))
    return issues


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


def all_duplicates(anthology_id):
    from itertools import combinations
    from authors.models import Author
    groups = defaultdict(dict)
    review_emails = list(Review.objects.filter(anthology_id=anthology_id).exclude(email='').values_list('email', flat=True))
    from django.db.models.functions import Lower
    author_emails = {a.email.casefold(): a.pk for a in Author.objects.annotate(_email=Lower('email')).filter(_email__in=[email.lower() for email in review_emails]) if a.email}
    for text in Text.objects.filter(anthology_id=anthology_id).prefetch_related('authors'):
        item = {'id': ('text',text.pk), 'title':text.title, 'folded':folded(text.title), 'copy':None, 'url':reverse('core:assigned_text_detail',args=[text.pk])}
        for author in text.authors.all():groups[(text.anthology_id,('author',author.pk))][item['id']] = item
    for review in Review.objects.filter(anthology_id=anthology_id).prefetch_related('coauthors'):
        identities = {('author', a.pk) for a in review.coauthors.all()}
        if review.author_id:identities.add(('author',review.author_id))
        if review.email:
            email = review.email.casefold()
            identities.add(('author',author_emails[email]) if email in author_emails else ('email',email))
        item = {'id': ('review',review.pk), 'title':review.title, 'folded':folded(review.title), 'copy':review.copied_text_id, 'url':reverse('core:assigned_review_detail',args=[review.pk])}
        for identity in identities:groups[(review.anthology_id,identity)][item['id']] = item
    rows=[];seen=set()
    for group in groups.values():
        for left,right in combinations(group.values(),2):
            pair=tuple(sorted((left['id'],right['id'])))
            if pair in seen:continue
            seen.add(pair)
            if left['copy'] and right['id']==('text',left['copy']) or right['copy'] and left['id']==('text',right['copy']):continue
            matcher = SequenceMatcher(None, left['folded'], right['folded'])
            if left['folded'] and matcher.real_quick_ratio() >= 0.88 and matcher.quick_ratio() >= 0.88 and matcher.ratio() >= 0.88:
                row=issue('Możliwy duplikat',f"{left['title']} ↔ {right['title']}",right['url'])
                row['source_url']=left['url'];rows.append(row)
    return rows


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
        for author in authors:
            if not author.has_contract:rows.append(issue('Brak potwierdzonej umowy',f'{author} — {text.title}',url))
    if not included:rows.append(issue('Brak tekstów do wydania','Antologia nie ma niewycofanych tekstów.',reverse('core:text_list')+f'?anthology={anthology.pk}&hide_ready=0'))
    for correction in AnthologyCorrection.objects.filter(anthology=anthology).exclude(status__in=('applied','rejected')):
        rows.append(issue('Uwaga wymaga obsługi',f'{correction.story_title} — {correction.get_status_display()}',reverse('core:anthology_corrections')+f'?anthology={anthology.pk}'))
    if anthology.cover_status!='ready':rows.append(issue('Okładka nie jest gotowa',anthology.title,reverse('admin:texts_anthology_change',args=[anthology.pk])))
    for task in anthology.production_tasks.exclude(status='ready'):
        rows.append(issue('Niezakończone zadanie produkcyjne',task.get_task_type_display(),reverse('admin:texts_anthology_change',args=[anthology.pk])))
    return rows


def anthology_credits(anthology):
    # Credits reflect recorded participation, not invented dates or assignments.
    credits=defaultdict(lambda:{'name':'','role':'','works':set()})
    def add(identity,name,role,title):
        item=credits[(identity,role)];item.update(name=name,role=role);item['works'].add(title)
    completed = WorkflowStage.objects.filter(text__anthology=anthology, is_completed=True, assignment__assigned_to__isnull=False).exclude(stage_type__in=IMPORT_ONLY_STAGE_TYPES).select_related('assignment__assigned_to__person_profile', 'text')
    for stage in completed:
        a=stage.assignment
        person=getattr(a.assigned_to,'person_profile',None)
        add(('person',person.pk) if person else ('user',a.assigned_to_id),str(person) if person else a.assigned_to.get_full_name() or str(a.assigned_to),a.get_role_display(),stage.text.title)
    for a in ReviewAssignment.objects.filter(Q(review__copied_text__anthology=anthology)|Q(review__anthology=anthology,review__status='accepted')).exclude(opinion__in=('','reading')).select_related('user__person_profile','historical_person','review'):
        person=a.historical_person or (getattr(a.user,'person_profile',None) if a.user_id else None)
        add(('person',person.pk) if person else ('user',a.user_id) if a.user_id else ('review',a.pk),str(person) if person else a.reviewer_display_name,'Recenzent',a.review.title)
    for a in AnthologyTask.objects.filter(anthology=anthology,status='ready').select_related('assigned_to'):
        if a.assigned_to_id:add(('person',a.assigned_to_id),str(a.assigned_to),a.get_task_type_display(),anthology.title)
    from illustrations.models import Illustration
    for illustration in Illustration.objects.filter(text__anthology=anthology,status='delivered').select_related('illustrator','text'):
        if illustration.illustrator_id:add(('person',illustration.illustrator_id),str(illustration.illustrator),'Ilustrator',illustration.text.title)
    if anthology.cover_author.strip():add(('cover',anthology.pk),anthology.cover_author,'Okładka',anthology.title)
    return [dict(item,works=sorted(item['works'])) for item in sorted(credits.values(),key=lambda item:(item['role'],item['name']))]
