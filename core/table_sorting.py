"""Allowlisted sorting before pagination, shared by table headers."""
from core.translation_scope import frontend_scope
from copy import copy
from datetime import date, datetime
from core.sort_keys import text_key, sql_text_key
from django.db.models import F, QuerySet, Case, When, Value, IntegerField, Count, Q

# Displayed column -> public URL key, database fields. No arbitrary ORM paths.
COMMON = {
 'Antologia': ('anthology', ('anthology__title',)),
 'Tytuł': ('title', ('title',)), 'Tekst': ('title', ('title',)),
 'Długość': ('length', ('length',)), 'Liczba znaków': ('length', ('length',)),
}
MODELS = {
 'core.recruitment': {'Nadawca': ('sender', ('mail_sender', 'last_name', 'first_name')), 'Kto': ('subject', ('mail_subject',)), 'Data wiadomości': ('received', ('mail_received_at',)), 'Rola': ('roles', ('mail_roles',)), 'Przyjęty/Odrzucony': ('decision', ('status',)), 'Powiadomiony': ('notified', ('notified',)),'Data nadesłania': ('created', ('submitted_at',)), 'E-mail': ('email', ('email',)), 'Imię i nazwisko': ('person', ('last_name','first_name'))},
 'core.anthologycorrection': {'Antologia': ('anthology', ('anthology__title',)), 'Tytuł opowiadania': ('title', ('story_title',)), 'Status zmiany': ('status', ('status',)), 'Zgłaszający': ('person', ('submitted_by__last_name','submitted_by__first_name'))},
 'texts.text': {**COMMON, 'Etap': ('status', ('current_stage_type',)), 'Etap tekstu': ('status', ('current_stage_type',)), 'Etap pracy': ('status', ('current_stage_type',))},
 'texts.review': {**COMMON, 'Status': ('status', ('status',)), 'Data nadesłania': ('created', ('created_at',)), 'Data decyzji': ('decision', ('decision_at',))},
 'workflow.workflowstage': {
  'Antologia': ('anthology', ('text__anthology__title',)), 'Tytuł': ('title', ('text__title',)),
  'Długość': ('length', ('text__length',)), 'Etap': ('stage', ('stage_type',)), 'Rozpoczęcie': ('started_at', ('started_at',)),
  'Zakończenie': ('ended_at', ('ended_at',)),
 },
 'texts.reviewassignment': {
  'Antologia': ('anthology', ('review__anthology__title',)), 'Tytuł': ('title', ('review__title',)),
  'Status zgłoszenia': ('status', ('review__status',)), 'Moja recenzja': ('opinion', ('opinion',)),
  'Data przydziału': ('assigned', ('assigned_at',)), 'Ostatnia zmiana recenzji': ('changed', ('opinion_changed_at',)),
 },
 'people.person': {'Osoba': ('person', ('last_name','first_name')), 'Imię i nazwisko': ('person', ('last_name','first_name')), 'E-mail': ('email', ('email',))},
 'core.newsletterconsent': {'Adres e-mail': ('email', ('email',)), 'Zgoda na newsletter o premierach': ('premieres', ('premieres',)), 'Zgoda na newsletter o naborach': ('recruitment', ('recruitment',))},
 'authors.author': {'Autor': ('person', ('last_name','first_name')), 'Imię i nazwisko': ('person', ('last_name','first_name')), 'Pseudonim': ('pseudonym', ('pseudonym',)), 'E-mail': ('email', ('email',))},
 'texts.anthology': {'Antologia': ('title', ('title',)), 'Tytuł': ('title', ('title',)), 'Status': ('status', ('status',))},
 'people.vacation': {'Urlop od': ('start', ('start_date',)), 'Urlop do': ('end', ('end_date',)), 'Osoba': ('person', ('person__last_name','person__first_name')), 'Rozpoczęcie': ('start', ('start_date',)), 'Zakończenie': ('end', ('end_date',)), 'Data rozpoczęcia': ('start', ('start_date',)), 'Data zakończenia': ('end', ('end_date',))},
 'core.useractivity': {'Czas': ('created', ('created_at',)), 'Użytkownik': ('person', ('actor',)), 'Data': ('created', ('created_at',)), 'Działanie': ('action', ('action',))},
}

# Every public key is declared here; URL parameters never become ORM paths.
def _columns(**fields):
    return {label: (field, (field,)) for label, field in fields.items()}

MODELS['core.recruitment'].update(_columns(**{
    'Imię':'first_name', 'Nazwisko':'last_name',
    'Czy powiadomiono':'notified', 'Uwagi':'notes', 'Uwagi nieoficjalne':'unofficial_notes'}))
MODELS['core.anthologycorrection'].update(_columns(**{
    'Fragment':'fragment', 'Co jest źle':'problem', 'Propozycja poprawki':'suggestion'}))
MODELS['texts.extract'] = _columns(**{
    'Imię i nazwisko':'full_name', 'Adres e-mail':'email', 'Numer telefonu':'phone_number',
    'Nadesłane tytuły':'title', 'Daty nadesłania':'submission_dates', 'Antologia':'recruitment',
    'Przyjęte':'accepted_titles', 'Odrzucone':'rejected_titles'})
MODELS['authors.author'].update(_columns(**{'Umowa':'has_contract', 'Kontakt':'contact'}))
MODELS['texts.anthology'].update(_columns(**{'Okładka':'cover_status', 'Druk':'print_status'}))
MODELS['people.vacation'].update(_columns(**{'Zgłoszono':'created_at'}))
MODELS['core.useractivity'].update(_columns(**{'Obiekt':'target', 'Rodzaj':'method', 'Wynik HTTP':'status_code'}))
MODELS['texts.review'].update({
    'Autor':('author',('author_last_name','author_first_name')),
    'E-mail':('email',('email',)), 'Decyzja':('status',('status',)), 'Termin':('decision',('decision_at',)),
    'Informacja':('information',('old_reviews',)),
    'Data zgłoszenia': ('created', ('created_at',)),
    'Data powiadomienia': ('notified_at', ('author_notified_at',)),
})
MODELS['illustrations.illustration'] = {
    'Antologia':('anthology',('text__anthology__title',)),
    'Tytuł tekstu':('title',('text__title',)),
    'Ilustratorzy':('illustrator',('artist_sort',)),
    **_columns(**{'Status':'status', 'Data przypisania':'assigned_at', 'Uwagi koordynatora':'coordinator_notes'})}
MODELS['illustrations.coverproposal'] = {
    **_columns(**{'Autor ilustracji':'illustration_author','Ilustracja':'illustration_url',
    'Data zgłoszenia':'submitted_at','Status':'status','Ostatnia zmiana statusu':'status_changed_at'}),
    'Zgłoszone przez':('person',('submitted_by__last_name','submitted_by__first_name'))}
MODELS['illustrations.illustrator'] = {
    'Imię i nazwisko': ('person', ('last_name', 'first_name')),
    **_columns(**{'Adres e-mail': 'email', 'Portfolio': 'portfolio',
                  'Preferencje': 'preferences', 'Okładki': 'covers'}),
}


def _get(row, path):
    for part in path.split('.'):
        if row is None:
            return None
        row = row.get(part) if isinstance(row, dict) else getattr(row, part, None)
        if callable(row):
            row = row()
    return row


def _value(value):
    if isinstance(value, (list, tuple)):
        return tuple(_value(item) for item in value)
    if isinstance(value, (int, float, date, datetime)):
        return value
    return text_key(str(value or ''))


def _sorted_rows(items, getter, reverse):
    present, missing = [], []
    for row in items:
        value = getter(row)
        if value is None or value == '' or value == []:
            missing.append(row)
        else:
            present.append((row, _value(value)))
    return [row for row, value in sorted(present, key=lambda pair: pair[1], reverse=reverse)] + missing


class DisplayTable:
    """Explicit columns for small computed tables; sort before pagination."""
    def __init__(self, items, columns):
        self.items, self.columns = items, columns

    def sort_table(self, request):
        selected = request.GET.get('sort', '')
        getter = next((getter for key, getter in self.columns.values() if key == selected.lstrip('-')), None)
        rows = _sorted_rows(self.items, getter, selected.startswith('-')) if getter else self.items
        return rows, {label: key for label, (key, _) in self.columns.items()}



class _SortedSubset:
    """Keep scalar IDs/keys; hydrate only the page requested by the paginator."""
    def __init__(self, queryset, ids, projector=None, batch_projector=None):
        self.queryset, self.ids, self.projector = queryset, ids, projector
        self.batch_projector = batch_projector

    def count(self): return len(self.ids)
    def __len__(self): return len(self.ids)
    def __iter__(self):
        for offset in range(0, len(self.ids), 200):
            yield from self[offset:offset + 200]

    def __getitem__(self, key):
        ids = self.ids[key] if isinstance(key, slice) else [self.ids[key]]
        if not ids:
            return []
        records = {_get(row, "pk"): row for row in self.queryset.filter(pk__in=ids)}
        if self.batch_projector:
            # Wiersze projektowane zbiorczo (np. lista zgłoszeń) – jak przy zwykłej stronie.
            result = self.batch_projector([records[pk] for pk in ids if pk in records])
            return result if isinstance(key, slice) else result[0]
        result = [
            self.projector(records[pk]) if self.projector else records[pk]
            for pk in ids
            if pk in records
        ]
        return result if isinstance(key, slice) else result[0]


def _sort_query_projection(items, queryset, getter, reverse):
    projector = getattr(items, "projector", None)
    keys, missing = [], []
    for record in queryset.iterator(chunk_size=200):
        row = projector(record) if projector else record
        pk = _get(record, "pk")
        value = getter(row)
        if value is None or value == "" or value == []:
            missing.append(pk)
        else:
            keys.append((pk, _value(value)))
    ids = [pk for pk, value in sorted(keys, key=lambda pair: pair[1], reverse=reverse)] + missing
    return _SortedSubset(queryset, ids, projector, getattr(items, "batch_projector", None))


def _joined(items):
    return ", ".join(sorted((str(item) for item in items), key=text_key))


def _extra_columns(items, queryset, request):
    """Computed columns use the existing projection, before pagination.

    Only selecting such a column materializes its filtered result. Normal SQL
    columns retain database pagination. Prefetched relations avoid per-row reads.
    """
    model = queryset.model._meta.label_lower
    projected = hasattr(items, "projector")
    columns = {}
    if model == "core.recruitment":
        columns["Kto"] = ("subject", lambda row: row.subject_name)
    if model == "texts.text" and projected:
        columns.update(
            {
                "Autorzy": ("authors", lambda r: _get(r, "authors_display")),
                "Rozpoczęcie etapu": (
                    "stage_start",
                    lambda r: _get(r, "current_status_started_at"),
                ),
            }
        )
        if request.resolver_match and request.resolver_match.url_name == "translation_list":
            columns["Tłumacz"] = ("translators", lambda r: _get(r, "translators_display"))
        if "work_active" in queryset.query.annotations:
            columns.update(
                {
                    "Twoje role": (
                        "roles",
                        lambda r: _joined(a["get_role_display"] for a in r["user_assignments"]),
                    ),
                    "Twoja praca": (
                        "work",
                        lambda r: (
                            "Wycofany"
                            if r["current_stage_type"] == "withdrawn"
                            else "W toku"
                            if r["has_active_work"]
                            else "Oczekiwanie na inną osobę"
                            if r["is_waiting_for_other_role"]
                            else "Zarezerwowana"
                            if r["has_reserved_work"]
                            else "Zakończona"
                            if r["has_completed_work"]
                            else "Tekst gotowy"
                            if r["current_stage_type"] == "ready"
                            else "Brak bieżącego zadania"
                        ),
                    ),
                }
            )
    if (
        model == "texts.text"
        and projected
        and request.resolver_match
        and request.resolver_match.url_name == "workflow_list"
    ):
        columns = {"Autorzy": ("authors", lambda r: _get(r, "text.authors_display"))}
        from workflow.catalog import workflow_role_choices

        for role, label in workflow_role_choices():
            label = "Redaktor" if role == "editor" else label
            columns[label] = (
                "role_" + role,
                lambda r, role=role: _joined(
                    _get(entry, "user.get_full_name") or ""
                    for cell in r.get("role_cells", [])
                    if cell["role"] == role
                    for entry in cell["entries"]
                ),
            )
    if model == "workflow.workflowstage" and projected:
        columns["Autorzy"] = ("authors", lambda r: _get(r, "text.authors_display"))
    if model == "core.useractivity":
        columns["Użytkownik"] = ("person", lambda r: str(_get(r, "user.person_profile") or r.actor))
    if model == "people.person":
        columns["Role"] = ("roles", lambda r: _joined(r.roles.all()))
    if model == "people.vacation":
        columns["Role"] = ("roles", lambda r: _joined(r.person.roles.all()))
        columns["Status"] = (
            "vacation_status",
            lambda r: (
                "Trwa"
                if r.is_active
                else "Zaplanowany"
                if r.is_upcoming
                else "Zakończony"
                if r.is_finished
                else "Nieaktywny"
            ),
        )
    if model == "texts.review":
        columns["Autor"] = ("author", lambda r: _get(r, "author_display_name"))
    if model == "texts.anthology":
        if request.resolver_match and request.resolver_match.url_name == "novel_list":
            columns["Autor"] = (
                "authors",
                lambda r: _joined(a.display_name for a in r.novel.authors.all()),
            )
            columns["Powieść"] = ("title", lambda r: r.title)
            columns["Rozdziały"] = ("chapters", lambda r: r.chapter_count)
            columns["Gatunek"] = ("genre", lambda r: r.novel.genre)
        columns["Skład"] = (
            "typesetting",
            lambda r: (
                r.typesetting_task.get_status_display() if r.typesetting_task else "Niezlecone"
            ),
        )
    if model == "illustrations.illustration":
        columns["Autorzy"] = (
            "authors",
            lambda r: _joined(a.display_name for a in r.text.authors.all()),
        )
    if model == "authors.author":
        columns["Autor"] = (
            "person",
            lambda r: (
                _get(r, "display_name")
                or _get(r, "pseudonym")
                or " ".join((_get(r, "first_name") or "", _get(r, "last_name") or ""))
            ),
        )
        columns["Imię i nazwisko"] = columns["Autor"]
        titles = {}
        if request.GET.get("sort", "").lstrip("-") == "anthologies":
            from texts.models import Text

            for author_id, title in (
                frontend_scope(Text.objects)
                .filter(authors__pk__in=queryset.values("pk"), anthology__isnull=False)
                .values_list("authors__pk", "anthology__title")
                .distinct()
            ):
                titles.setdefault(author_id, []).append(title)
        columns["Antologie"] = ("anthologies", lambda r: _joined(titles.get(_get(r, "pk"), [])))
    from core.permissions import can_view_author_data

    if not can_view_author_data(request.user):
        columns.pop("Autorzy", None)
        if model == "texts.review":
            columns.pop("Autor", None)
    return columns


# Widoki z własnymi adnotacjami: etykieta -> (klucz w adresie, pole), dodatkowe sortowanie.
_AUDIO = {
    'Antologia': ('anthology', 'anthology__title'), 'Autor': ('author', 'audio_author'),
    'Tytuł': ('title', 'title'), 'Status': ('status', 'audio_status_rank'),
    'Lektor': ('narrator', 'audiobook__narrator_name'), 'Dźwiękowiec': ('engineer', 'audiobook__engineer_name'),
    'Montaż': ('engineer', 'audiobook__engineer_name'),
    'Korektor audiobooka': ('proofreader', 'audiobook__proofreader__person_profile__last_name'),
    'Historia korekty': ('proofreading', 'audio_proofreading_date'),
    'Data premiery': ('premiere', 'audiobook__premiere_date'),
    'YouTube': ('youtube', 'audiobook__youtube_url'), 'HearThis': ('hearthis', 'audiobook__hearthis_url'),
}
_AUDIOBOOKS = {label: spec for label, spec in _AUDIO.items() if label != 'Historia korekty'}
_AUDIOBOOKS['Montażysta'] = ('engineer', 'audiobook__engineer_name')
_AUDIOBOOKS.update({label: (kind, f'audio_{kind}_date') for label, kind in (
    ('Nagrywanie', 'recording'), ('Korekta', 'proofreading'), ('Poprawki', 'corrections'), ('Montaż', 'editing'))})
FIXED_SORTS = {
    'core:post_layout': ({
        'Antologia': ('anthology', 'anthology__title'), 'Korektor': ('proofreader', 'proofreader__person_profile__last_name'),
        'Strony': ('pages', 'page_from'), 'Status': ('status', 'status'),
        'Przydzielony od': ('assigned_start', 'assigned_start'), 'Przydzielony do': ('assigned_end', 'assigned_end'),
        'W trakcie od': ('work_start', 'work_start'), 'W trakcie do': ('work_end', 'work_end'),
        'Zakończony': ('completed', 'completed_on')}, ('page_from', 'pk')),
    'core:audiobooks': (_AUDIOBOOKS, ('title', 'pk')),
    'core:audio_proofreading': (_AUDIO, ('title', 'pk')),
    'core:assign_audio_proofreader': (_AUDIO, ('title', 'pk')),
    # Adnotacje social_author i social_status_rank dodaje widok (core/views/social_media.py).
    'core:social_media': ({
        'Antologia': ('anthology', 'anthology__title'), 'Tytuł': ('title', 'title'),
        'Autor': ('author', 'social_author'), 'Tagi': ('tags', 'tags'), 'Gatunek': ('genre', 'genre'),
        'Status': ('status', 'social_status_rank'), 'Data wrzucenia': ('posted_on', 'social_media__posted_on'),
        'Link': ('url', 'social_media__url')}, ('title', 'pk')),
    # Listy publiczne: nigdy nie udostępniają prywatnych pól tekstu ani ilustracji.
    'core:external_audiobooks': ({
        'Antologia': ('anthology', 'anthology__title'), 'Tytuł opowiadania': ('title', 'title'),
        'Tagi': ('tags', 'tags'), 'Gatunek': ('genre', 'genre'), 'Status': ('status', 'public_status')}, ('title', 'pk')),
    'illustrations:external_illustrations': ({
        'Antologia': ('anthology', 'text__anthology__title'), 'Tytuł opowiadania': ('title', 'text__title'),
        'Tagi': ('tags', 'text__tags'), 'Gatunek': ('genre', 'text__genre'), 'Status': ('status', 'public_status')}, ('pk',)),
}


def prepare_table_sort(request, items):
    view = request.resolver_match.view_name if request.resolver_match else None
    if view in FIXED_SORTS and (view != 'core:social_media' or (
            isinstance(items, QuerySet) and items.model._meta.label_lower == 'texts.text')):
        fields, tiebreak = FIXED_SORTS[view]
        # Nieznany klucz sortuje po antologii, z zachowaniem kierunku („-” = malejąco).
        selected = request.GET.get('sort', 'anthology')
        field = next((field for key, field in fields.values() if key == selected.lstrip('-')), fields['Antologia'][1])
        return items.order_by(('-' if selected.startswith('-') else '') + field, *tiebreak), {
            label: key for label, (key, _) in fields.items()}
    if hasattr(items, "sort_table"):
        return items.sort_table(request)
    queryset = items if isinstance(items, QuerySet) else getattr(items, "queryset", None)
    if queryset is None:
        return items, {}
    model = queryset.model._meta.label_lower
    columns = MODELS.get(model, {}).copy()
    if model == "illustrations.illustrator" and "artist_name" in queryset.query.annotations:
        columns["Imię i nazwisko"] = ("person", ("artist_name",))
    if model == "core.recruitment" and "_candidate_sort" in queryset.query.annotations:
        columns["Nadawca"] = ("sender", ("_candidate_sort",))
    if model == "people.person" and "last_activity_at" in queryset.query.annotations:
        columns.update(
            {
                "Data logowania": ("last_activity", ("last_activity_at",)),
                "Data działania": ("last_activity", ("last_activity_at",)),
            }
        )
        if "last_action" in queryset.query.annotations:
            columns["Działanie"] = ("last_action", ("last_action",))
    if model == "texts.text" and "last_status_change" in queryset.query.annotations:
        columns["Ostatnia zmiana statusu"] = ("last_status_change", ("last_status_change",))
    if model == "texts.text" and "current_stage_type" in queryset.query.annotations:
        columns["Status"] = ("status", ("current_stage_type",))
    if model == "texts.review":
        if (
            request.GET.get("sort", "").lstrip("-") == "opinions"
            and "completed_count" not in queryset.query.annotations
        ):
            queryset = queryset.annotate(
                completed_count=Count(
                    "assignments",
                    filter=~Q(assignments__opinion__in=("", "reading")),
                    distinct=True,
                )
            )
        columns["Recenzenci i opinie"] = ("opinions", ("completed_count",))
    if model == "texts.review" and "source_information" in queryset.query.annotations:
        columns["Informacja"] = ("information", ("source_information",))
    for field in queryset.model._meta.concrete_fields:
        if not field.is_relation and field.name in {
            "title",
            "status",
            "created_at",
            "updated_at",
            "submitted_at",
            "pseudonym",
            "has_contract",
            "can_contact",
            "decision_at",
            "action",
            "method",
            "status_code",
            "story_title",
            "notified_at",
        }:
            label = str(field.verbose_name).capitalize()
            columns.setdefault(label, (field.name, (field.name,)))
    from core.permissions import can_view_author_data, is_coordinator

    if model in {"texts.review", "texts.text", "authors.author"} and not (
        can_view_author_data(request.user)
        or (model == "authors.author" and is_coordinator(request.user))
    ):
        columns = {
            label: spec
            for label, spec in columns.items()
            if not any(
                "email" in field or "phone" in field or field.startswith("author_")
                for field in spec[1]
            )
        }
    # Some lists do not annotate the derived current stage.
    if (
        queryset.model._meta.label_lower == "texts.text"
        and "current_stage_type" not in queryset.query.annotations
    ):
        columns = {k: v for k, v in columns.items() if v[0] != "status"}
    for label, spec in list(columns.items()):
        if label == "E-mail":
            columns["Adres e-mail"] = spec
        if label == "Status":
            columns.setdefault("Status publikacji", spec)
    extras = _extra_columns(items, queryset, request)
    requested = request.GET.get("sort", "")
    key = requested.lstrip("-")
    extra = next((getter for name, getter in extras.values() if name == key), None)
    if extra:
        if isinstance(items, QuerySet):
            if queryset.model._meta.label_lower == "people.person":
                items = items.prefetch_related("roles")
            elif queryset.model._meta.label_lower == "people.vacation":
                items = items.select_related("person").prefetch_related("person__roles")
        return _sort_query_projection(
            items,
            items if isinstance(items, QuerySet) else queryset,
            extra,
            requested.startswith("-"),
        ), {
            **{label: spec[0] for label, spec in columns.items()},
            **{label: spec[0] for label, spec in extras.items()},
        }
    fields = next((fields for _, (name, fields) in columns.items() if name == key), None)
    if fields:
        from workflow.state import stage_order

        ordered_fields = []
        for field in fields:
            if field in ("stage_type", "current_stage_type"):
                queryset = queryset.annotate(_semantic_stage_order=stage_order(default=999, field=field))
                ordered_fields.append("_semantic_stage_order")
            elif (
                "__" not in field
                and field in {f.name for f in queryset.model._meta.concrete_fields}
                and queryset.model._meta.get_field(field).choices
            ):
                choices = queryset.model._meta.get_field(field).flatchoices
                name = "_choice_" + str(len(ordered_fields))
                ranks = {
                    v: index
                    for index, (v, label) in enumerate(
                        sorted(choices, key=lambda pair: text_key(str(pair[1])))
                    )
                }
                queryset = queryset.annotate(
                    **{
                        name: Case(
                            *[When(**{field: v}, then=Value(rank)) for v, rank in ranks.items()],
                            default=Value(None),
                            output_field=IntegerField(),
                        )
                    }
                )
                ordered_fields.append(name)
            elif field.split("__")[-1] in {
                "title",
                "first_name",
                "last_name",
                "email",
                "pseudonym",
                "actor",
                "story_title",
                "full_name",
                "author_last_name",
                "author_first_name",
                "fragment",
                "problem",
                "suggestion",
                "notes",
                "unofficial_notes",
                "recruitment",
                "accepted_titles",
                "rejected_titles",
                "illustrated_excerpt",
                "illustration_author",
                "target",
                "action",
                "story_url",
                "illustration_url",
            }:
                name = "_alphabet_" + str(len(ordered_fields))
                queryset = queryset.annotate(**{name: sql_text_key(field, queryset.db)})
                ordered_fields.append(name)
            else:
                ordered_fields.append(field)
        fields = tuple(ordered_fields)
        order = [
            F(field).desc(nulls_last=True)
            if requested.startswith("-")
            else F(field).asc(nulls_last=True)
            for field in fields
        ]
        queryset = queryset.order_by(
            *order,
            *(
                ["execution_number", "iteration"]
                if queryset.model._meta.label_lower == "workflow.workflowstage"
                else []
            ),
            "pk",
        )
        if isinstance(items, QuerySet):
            items = queryset
        else:
            items = copy(items)
            items.queryset = queryset
    return items, {
        **{label: key for label, (key, _) in columns.items()},
        **{label: spec[0] for label, spec in extras.items()},
    }
