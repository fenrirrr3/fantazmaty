"""Allowlisted sorting before pagination, shared by table headers."""
from copy import copy
from datetime import date, datetime
from core.sort_keys import text_key, sql_text_key
from django.db.models import F, QuerySet, Case, When, Value, IntegerField

# Displayed column -> public URL key, database fields. No arbitrary ORM paths.
COMMON = {
 'Antologia': ('anthology', ('anthology__title',)),
 'Tytuł': ('title', ('title',)), 'Tekst': ('title', ('title',)),
 'Długość': ('length', ('length',)), 'Liczba znaków': ('length', ('length',)),
}
MODELS = {
 'core.recruitment': {'Data nadesłania': ('created', ('submitted_at',)), 'E-mail': ('email', ('email',)), 'Imię i nazwisko': ('person', ('last_name','first_name'))},
 'core.anthologycorrection': {'Antologia': ('anthology', ('anthology__title',)), 'Tytuł opowiadania': ('title', ('story_title',)), 'Status zmiany': ('status', ('status',)), 'Zgłaszający': ('person', ('submitted_by__last_name','submitted_by__first_name'))},
 'texts.text': {**COMMON, 'Etap': ('status', ('current_stage_type',)), 'Etap tekstu': ('status', ('current_stage_type',)), 'Etap pracy': ('status', ('current_stage_type',))},
 'texts.review': {**COMMON, 'Status': ('status', ('status',)), 'Data nadesłania': ('created', ('created_at',)), 'Data decyzji': ('decision', ('decision_at',))},
 'workflow.workflowstage': {
  'Antologia': ('anthology', ('text__anthology__title',)), 'Tytuł': ('title', ('text__title',)),
  'Długość': ('length', ('text__length',)), 'Etap': ('stage', ('stage_type',)), 'Rozpoczęcie': ('started_at', ('started_at',)),
  'Zakończenie': ('ended_at', ('ended_at',)), 'Stan etapu': ('completed', ('is_completed',)),
 },
 'texts.reviewassignment': {
  'Antologia': ('anthology', ('review__anthology__title',)), 'Tytuł': ('title', ('review__title',)),
  'Status zgłoszenia': ('status', ('review__status',)), 'Moja recenzja': ('opinion', ('opinion',)),
  'Data przydziału': ('assigned', ('assigned_at',)), 'Ostatnia zmiana recenzji': ('changed', ('opinion_changed_at',)),
 },
 'people.person': {'Osoba': ('person', ('last_name','first_name')), 'Imię i nazwisko': ('person', ('last_name','first_name')), 'E-mail': ('email', ('email',))},
 'authors.author': {'Autor': ('person', ('last_name','first_name')), 'Imię i nazwisko': ('person', ('last_name','first_name')), 'Pseudonim': ('pseudonym', ('pseudonym',)), 'E-mail': ('email', ('email',))},
 'texts.anthology': {'Antologia': ('title', ('title',)), 'Tytuł': ('title', ('title',)), 'Status': ('status', ('status',))},
 'people.vacation': {'Urlop od': ('start', ('start_date',)), 'Urlop do': ('end', ('end_date',)), 'Osoba': ('person', ('person__last_name','person__first_name')), 'Rozpoczęcie': ('start', ('start_date',)), 'Zakończenie': ('end', ('end_date',)), 'Data rozpoczęcia': ('start', ('start_date',)), 'Data zakończenia': ('end', ('end_date',))},
 'core.useractivity': {'Czas': ('created', ('created_at',)), 'Użytkownik': ('person', ('actor',)), 'Data': ('created', ('created_at',)), 'Działanie': ('action', ('action',))},
}

def prepare_table_sort(request, items):
    queryset = items if isinstance(items, QuerySet) else getattr(items, 'queryset', None)
    if queryset is None:
        if not isinstance(items, (list, tuple)) or not items or not isinstance(items[0], dict):
            return items, {}
        sample = items[0]
        candidates = {'Antologia':'anthology_title', 'Tytuł':'title', 'Autor':'author_name', 'Autorzy':'authors',
            'Recenzent':'reviewer_name', 'Redaktor':'person_name', 'Korektor':'person_name', 'Weryfikator':'person_name',
            'Osoba':'person_name', 'Rozpoczęcie':'started_at', 'Zakończenie':'ended_at',
            'Ocena':'opinion', 'Recenzja':'opinion', 'Opinia':'opinion', 'Przydział':'position', 'Stan etapu':'is_completed', 'Rola':'role', 'Data oceny':'opinion_at', 'Data decyzji':'decision_at', 'Status zgłoszenia':'review_status', 'Data recenzji':'opinion_at', 'Status':'review_status'}
        fields = {label:key for label,key in candidates.items() if key in sample}
        public = {'anthology_title':'anthology', 'author_name':'author', 'reviewer_name':'person', 'person_name':'person', 'is_completed':'completed', 'decision_at':'decision', 'review_status':'status'}
        columns = {label:public.get(field, field) for label,field in fields.items()}
        requested = request.GET.get('sort', '')
        key = requested.lstrip('-')
        field = next((field for label,field in fields.items() if columns[label] == key), None)
        if field:
            present = [row for row in items if row.get(field) is not None]
            missing = [row for row in items if row.get(field) is None]
            def value(row):
                v = row[field]
                return v if isinstance(v, (int, float, date, datetime)) else text_key(v)
            items = sorted(present, key=value, reverse=requested.startswith('-')) + missing
        return items, columns
    columns = MODELS.get(queryset.model._meta.label_lower, {}).copy()
    for field in queryset.model._meta.concrete_fields:
        if not field.is_relation and field.name in {'title', 'status', 'created_at', 'updated_at', 'submitted_at', 'pseudonym', 'has_contract', 'can_contact', 'decision_at', 'action', 'method', 'status_code', 'story_title', 'department', 'notified_at'}:
            label = str(field.verbose_name).capitalize()
            columns.setdefault(label, (field.name, (field.name,)))
    from core.permissions import can_view_author_data
    if queryset.model._meta.label_lower != "people.person" and not can_view_author_data(request.user):
        columns = {label:spec for label,spec in columns.items() if not any('email' in field or 'phone' in field for field in spec[1])}
    # Some lists do not annotate the derived current stage.
    if queryset.model._meta.label_lower == 'texts.text' and 'current_stage_type' not in queryset.query.annotations:
        columns = {k:v for k,v in columns.items() if v[0] != 'status'}
    for label, spec in list(columns.items()):
        if label == 'E-mail': columns['Adres e-mail'] = spec
        if label == 'Status': columns.setdefault('Status publikacji', spec)
    requested = request.GET.get('sort', '')
    key = requested.lstrip('-')
    fields = next((fields for _, (name,fields) in columns.items() if name == key), None)
    if fields:
        from workflow.state import ORDER
        ordered_fields = []
        for field in fields:
            if field in ('stage_type', 'current_stage_type'):
                queryset = queryset.annotate(_semantic_stage_order=Case(*[When(**{field:kind}, then=Value(index)) for kind,index in ORDER.items()], default=Value(999), output_field=IntegerField()))
                ordered_fields.append('_semantic_stage_order')
            elif field.split('__')[-1] in {'title','first_name','last_name','email','pseudonym','actor','story_title'}:
                name = '_alphabet_' + str(len(ordered_fields))
                queryset = queryset.annotate(**{name:sql_text_key(field, queryset.db)})
                ordered_fields.append(name)
            else:
                ordered_fields.append(field)
        fields = tuple(ordered_fields)
        order = [F(field).desc(nulls_last=True) if requested.startswith('-') else F(field).asc(nulls_last=True) for field in fields]
        queryset = queryset.order_by(*order, *(['execution_number', 'iteration'] if queryset.model._meta.label_lower == 'workflow.workflowstage' else []), 'pk')
        if isinstance(items, QuerySet):
            items = queryset
        else:
            items = copy(items)
            items.queryset = queryset
    return items, {label:key for label,(key,_) in columns.items()}
