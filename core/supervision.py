"""Read-only checks and publication credits; never repair data implicitly."""
from collections import defaultdict
from difflib import SequenceMatcher
import unicodedata
from django.contrib.auth import get_user_model
from django.db.models import F, Q, Prefetch
from django.urls import reverse
from django.utils import timezone
from core.models import AnthologyCorrection
from people.models import Person
from texts.models import Text, Review, ReviewAssignment, AnthologyTask, HistoricalTextAssignment
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.services import STAGE_ROLES


def folded(value):
    value = unicodedata.normalize('NFKD', value or '').casefold().replace('ł', 'l')
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value if not unicodedata.combining(c)).split())


def issue(label, detail, url):
    return {'label':label, 'detail':detail, 'url':url}


def integrity_issues():
    issues = []
    current = WorkflowStage.objects.filter(workflow_cycle=F('text__current_workflow_cycle')).select_related('text')
    assignments = {(a.text_id,a.workflow_cycle,a.role):a for a in WorkflowRoleAssignment.objects.filter(workflow_cycle=F('text__current_workflow_cycle')).select_related('assigned_to__person_profile','text')}
    for stage in current.filter(is_completed=False, ended_at__isnull=True, started_at__lte=timezone.localdate()).exclude(stage_type__in=('ready','withdrawn')):
        role = STAGE_ROLES.get(stage.stage_type)
        a = assignments.get((stage.text_id,stage.workflow_cycle,role))
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
            if bool(stage.ended_at) != stage.is_completed:
                issues.append(issue('Sprzeczny zapis zakończenia', f'{text.title}: {stage.get_stage_type_display()}', url))
            if not stage.is_completed and stage.ended_at is None:
                opened[stage.stage_type].append(stage)
        for kind, repeated in opened.items():
            if len(repeated) > 1:
                issues.append(issue('Kilka otwartych etapów tego samego rodzaju', f'{text.title}: {repeated[0].get_stage_type_display()}', url))
        if not any(s.stage_type in ('ready', 'withdrawn') for s in stages):
            for stage in stages:
                successor = NEXT_STAGE_TYPES.get(stage.stage_type)
                if stage.is_completed and successor and not any(s.stage_type == successor and (s.pk > stage.pk or (s.started_at and stage.ended_at and s.started_at >= stage.ended_at)) for s in stages):
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


def all_duplicates():
    from itertools import combinations
    from authors.models import Author
    groups = defaultdict(dict)
    author_emails = {a.email.casefold(): a.pk for a in Author.objects.exclude(email__isnull=True) if a.email}
    for text in Text.objects.exclude(anthology__isnull=True).prefetch_related('authors'):
        item = {'id': ('text',text.pk), 'title':text.title, 'copy':None, 'url':reverse('core:assigned_text_detail',args=[text.pk])}
        for author in text.authors.all():groups[(text.anthology_id,('author',author.pk))][item['id']] = item
    for review in Review.objects.exclude(anthology__isnull=True).prefetch_related('coauthors'):
        identities = {('author', a.pk) for a in review.coauthors.all()}
        if review.author_id:identities.add(('author',review.author_id))
        if review.email:
            email = review.email.casefold()
            identities.add(('author',author_emails[email]) if email in author_emails else ('email',email))
        item = {'id': ('review',review.pk), 'title':review.title, 'copy':review.copied_text_id, 'url':reverse('core:assigned_review_detail',args=[review.pk])}
        for identity in identities:groups[(review.anthology_id,identity)][item['id']] = item
    rows=[];seen=set()
    for group in groups.values():
        for left,right in combinations(group.values(),2):
            pair=tuple(sorted((left['id'],right['id'])))
            if pair in seen:continue
            seen.add(pair)
            if left['copy'] and right['id']==('text',left['copy']) or right['copy'] and left['id']==('text',right['copy']):continue
            if folded(left['title']) and SequenceMatcher(None,folded(left['title']),folded(right['title'])).ratio()>=0.88:
                row=issue('Możliwy duplikat',f"{left['title']} ↔ {right['title']}",right['url'])
                row['source_url']=left['url'];rows.append(row)
    return rows


def anthology_checklist(anthology):
    rows=[]
    texts=list(Text.objects.filter(anthology=anthology).prefetch_related(
        'authors', Prefetch('workflow_stages', queryset=WorkflowStage.objects.filter(
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
    assignments=WorkflowRoleAssignment.objects.filter(text__anthology=anthology).select_related('assigned_to__person_profile','text')
    completed_roles = {
        (text_id, cycle, STAGE_ROLES.get(kind))
        for text_id, cycle, kind in WorkflowStage.objects.filter(
            text__anthology=anthology, is_completed=True
        ).order_by().values_list('text_id', 'workflow_cycle', 'stage_type')
    }
    for a in assignments:
        if a.assigned_to_id and (a.text_id, a.workflow_cycle, a.role) in completed_roles:
            person=getattr(a.assigned_to,'person_profile',None)
            add(('person',person.pk) if person else ('user',a.assigned_to_id),str(person) if person else a.assigned_to.get_full_name() or str(a.assigned_to),a.get_role_display(),a.text.title)
    for a in HistoricalTextAssignment.objects.filter(text__anthology=anthology,is_completed=True).select_related('person','text'):
        add(('person',a.person_id) if a.person_id else ('history',a.pk),a.display_name,a.role_label,a.text.title)
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
