"""Repair only the explicit historical import, preserving every record ID."""
import json
from collections import Counter
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from authors.models import Author
from people.models import Person
from texts.models import Text
from workflow.models import WorkflowStage as Stage, WorkflowRoleAssignment as Assignment
from workflow.catalog import all_stage_roles, IMPORT_ONLY_STAGE_TYPES, IMPORT_ONLY_ROLES
from workflow.completed_import import import_completed_workflow
from workflow.archive_executions import normalized_archive, SOURCE
from workflow.management.commands.import_completed_table import Command as Importer, normalized


def name_key(obj):
    return normalized(obj.first_name), normalized(obj.last_name)


class Command(BaseCommand):
    help = 'Podgląd/naprawa wykonań 29 tekstów archiwalnych i nazwiska Ilony. Zapis tylko z --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--before', default='archiwum_przed_korekta.json')
        parser.add_argument('--after', default='import_archiwum.json')
        parser.add_argument('--report', default='raport_korekty_archiwum')
        parser.add_argument('--apply', action='store_true')

    def ilona(self, report):
        people = list(Person.objects.select_for_update().select_related('user').all())
        old = [p for p in people if name_key(p) == ('ilona', 'skrzypczak')]
        new = [p for p in people if name_key(p) == ('ilona', 'żurawska')]
        if len(old) > 1 or len(new) > 1:
            raise ValueError('Niejednoznaczny profil Ilony Skrzypczak/Żurawskiej. Wymagane rozstrzygnięcie ID.')
        users = list(get_user_model().objects.select_for_update().all())
        old_users = [u for u in users if name_key(u) == ('ilona', 'skrzypczak')]
        new_users = [u for u in users if name_key(u) == ('ilona', 'żurawska')]
        if len(old_users) > 1 or len(new_users) > 1:
            raise ValueError('Niejednoznaczne konto Ilony Skrzypczak/Żurawskiej.')
        if old and not new and not new_users:
            p = old[0]
            if p.user_id and name_key(p.user) not in {('ilona', 'skrzypczak'), ('ilona', 'żurawska')}:
                raise ValueError('Profil Ilony jest powiązany z kontem o innych danych.')
            report['identity'].append({'action': 'zmiana nazwiska istniejącego profilu', 'person_id': p.pk,
                                       'user_id': p.user_id, 'before': 'Ilona Skrzypczak', 'after': 'Ilona Żurawska'})
            p.last_name = 'Żurawska'; p.save(update_fields=['last_name'])
            if p.user_id:
                p.user.last_name = 'Żurawska'; p.user.save(update_fields=['last_name'])
        elif not old and not new and old_users and not new_users:
            u = old_users[0]
            if Person.objects.filter(user=u).exists():
                raise ValueError('Konto Ilony Skrzypczak jest powiązane z profilem o innym nazwisku.')
            report['identity'].append({'action': 'zmiana nazwiska istniejącego konta', 'user_id': u.pk,
                                       'before': 'Ilona Skrzypczak', 'after': 'Ilona Żurawska'})
            u.last_name = 'Żurawska'; u.save(update_fields=['last_name'])
        if new or new_users:
            report['identity'].append({'action': 'użycie istniejącej Ilony Żurawskiej; poprzedniego profilu nie usunięto'})
        importer = Importer(); importer.events = []; importer.warnings = []
        user = importer.person({'first_name': 'Ilona', 'last_name': 'Żurawska'})
        report['identity'].extend(importer.events)
        return user

    def inspect(self, text, before, after, ilona):
        if (text.title != before['title'] or text.length != before['length']
                or not text.anthology_id or normalized(text.anthology.title) != normalized(before['anthology'])):
            raise ValueError(f'{text.title}: zmienione dane tekstu; nie nadpisano.')
        authors = list(text.authors.all())
        if len(authors) != len(before['authors']):
            raise ValueError(f'{text.title}: zmieniona lista autorów.')
        for expected in before['authors']:
            matches = [a for a in authors if (a.pk == expected['author_id'] if 'author_id' in expected else
                       name_key(a) == (normalized(expected['first_name']), normalized(expected['last_name'])))]
            if len(matches) != 1:
                raise ValueError(f'{text.title}: zmieniony autor.')
        stages = list(Stage.objects.select_for_update().filter(text=text).select_related('assignment__assigned_to').order_by('pk'))
        assignments = list(Assignment.objects.select_for_update().filter(text=text).order_by('pk'))
        work = [s for s in stages if s.imported_completed]
        markers = [s for s in stages if not s.imported_completed]
        if len(work) != len(before['stages']) or len(assignments) != len(work) or len(markers) != 1:
            raise ValueError(f'{text.title}: dodatkowy lub brakujący etap/przydział.')
        marker = markers[0]
        if (marker.stage_type != 'ready' or marker.assignment_id or marker.is_completed or not marker.is_current
                or not marker.is_released or marker.started_at or marker.ended_at or marker.queued_at or marker.repetition_id):
            raise ValueError(f'{text.title}: status nie jest niezmienionym Gotowe.')
        if {s.assignment_id for s in work} != {a.pk for a in assignments}:
            raise ValueError(f'{text.title}: zmienione powiązania etapów.')
        if Stage.objects.filter(assignment__in=assignments).exclude(text=text).exists():
            raise ValueError(f'{text.title}: przypisanie użyte przez inny tekst.')
        if any(s.workflow_cycle != text.current_workflow_cycle for s in stages):
            raise ValueError(f'{text.title}: workflow wznowiono po imporcie.')
        old_kinds = [s['stage_type'] for s in before['stages']]
        new_kinds = [s['stage_type'] for s in after['stages']]
        actual_kinds = [s.stage_type for s in work]
        if actual_kinds != old_kinds and actual_kinds != new_kinds:
            raise ValueError(f'{text.title}: etapy zmieniono poza tym importem.')
        numbers = Counter(); roles = all_stage_roles(); target_users = []
        for s, old, new in zip(work, before['stages'], after['stages']):
            a = s.assignment
            numbers[s.stage_type] += 1
            if (not s.is_completed or s.is_skipped or s.repetition_id or s.started_at or s.ended_at or s.queued_at
                    or s.waiting_reset_at or s.send_to_proofreading is not None or s.queue_position
                    or s.execution_number != numbers[s.stage_type] or s.iteration != numbers[s.stage_type]
                    or a.role != roles[s.stage_type] or a.execution_number != numbers[s.stage_type]
                    or a.workflow_cycle != text.current_workflow_cycle or a.repetition_id or a.assigned_at
                    or not a.assigned_to_id
                    or s.is_current != (s.stage_type not in IMPORT_ONLY_STAGE_TYPES)
                    or s.is_released != (s.stage_type not in IMPORT_ONLY_STAGE_TYPES)):
                raise ValueError(f'{text.title}: daty, numery lub stan pracy zmieniły się po imporcie.')
            expected = old['person']; user = a.assigned_to
            if (expected['first_name'], expected['last_name']) == ('Ilona', 'Skrzypczak'):
                if name_key(user) not in {('ilona', 'skrzypczak'), ('ilona', 'żurawska')}:
                    raise ValueError(f'{text.title}: zmieniono wykonawcę pracy Ilony.')
                target_users.append(ilona.pk)
            else:
                if 'person_id' in expected:
                    valid = Person.objects.filter(pk=expected['person_id'], user_id=user.pk).exists()
                else:
                    valid = name_key(user) == (normalized(expected['first_name']), normalized(expected['last_name']))
                if not valid:
                    raise ValueError(f'{text.title}: zmieniono wykonawcę {expected}.')
                target_users.append(user.pk)
        return work, assignments, target_users

    def fix(self, text, row, work, assignments, users):
        roles = all_stage_roles(); counters = Counter(); desired = []
        for stage, spec, user in zip(work, row['stages'], users):
            kind = spec['stage_type']; counters[kind] += 1
            desired.append((stage, kind, roles[kind], counters[kind], user))
        last = {role: stage.assignment_id for stage, kind, role, number, user in desired if role not in IMPORT_ONLY_ROLES}
        changed = any((s.stage_type, s.iteration, s.execution_number, s.assignment.role,
                       s.assignment.execution_number, s.assignment.assigned_to_id, s.assignment.is_current) !=
                      (kind, n, n, role, n, user, last.get(role) == s.assignment_id)
                      for s, kind, role, n, user in desired)
        if changed:
            # Vacate unique keys before moving multiple rows to a common role.
            Assignment.objects.filter(pk__in=[a.pk for a in assignments]).update(is_current=False)
            for i, (s, kind, role, n, user) in enumerate(desired):
                Stage.objects.filter(pk=s.pk).update(iteration=30000+i)
                Assignment.objects.filter(pk=s.assignment_id).update(execution_number=100000+i)
            for s, kind, role, n, user in desired:
                Assignment.objects.filter(pk=s.assignment_id).update(role=role, execution_number=n,
                    assigned_to_id=user, is_current=last.get(role) == s.assignment_id)
                Stage.objects.filter(pk=s.pk).update(stage_type=kind, iteration=n, execution_number=n,
                    is_current=kind not in IMPORT_ONLY_STAGE_TYPES, is_released=kind not in IMPORT_ONLY_STAGE_TYPES)
        # The normal importer must recognize the corrected data as a no-op.
        if import_completed_workflow(text_id=text.pk, stages=[{'stage_type':s['stage_type'], 'assigned_to_id':u}
            for s,u in zip(row['stages'],users)], next_stage='ready', preserve_executions=True) is not False:
            raise ValueError('Sprawdzenie idempotencji korekty nie powiodło się.')
        return changed

    def handle(self, *args, **options):
        report = {'mode': 'podgląd – nic nie zapisano', 'identity': [], 'texts': [], 'missing': [], 'conflicts': []}
        try:
            before = json.loads(Path(options['before']).read_text(encoding='utf-8-sig'))
            after = json.loads(Path(options['after']).read_text(encoding='utf-8-sig'))
            Importer().validate(before); Importer().validate(after)
            if (before['source'] != SOURCE or {r['source_row'] for r in before['texts']} != set(range(1,30))
                    or normalized_archive(before) != after):
                raise ValueError('Pliki nie odpowiadają uzgodnionej korekcie 29 tekstów.')
            with transaction.atomic():
                ilona = self.ilona(report)
                for old, new in zip(before['texts'], after['texts']):
                    texts = list(Text.objects.select_for_update().filter(import_source=SOURCE,
                                  import_source_row=old['source_row']).select_related('anthology'))
                    if not texts:
                        report['missing'].append(old['title']); continue
                    if len(texts) != 1:
                        raise ValueError(f'{old["title"]}: niejednoznaczny tekst.')
                    text = texts[0]
                    work, assignments, users = self.inspect(text, old, new, ilona)
                    record = {'text_id':text.pk, 'title':text.title,
                              'before_stages':list(Stage.objects.filter(text=text).order_by('pk').values()),
                              'before_assignments':list(Assignment.objects.filter(text=text).order_by('pk').values())}
                    changed = self.fix(text, new, work, assignments, users)
                    record['result'] = 'korekta' if changed else 'bez zmian'
                    report['texts'].append(record)
                if not options['apply']:
                    transaction.set_rollback(True)
                else:
                    report['mode'] = 'zapisano'
        except Exception as exc:
            report['mode'] = 'wycofano – nic nie zapisano'
            report['conflicts'].append(str(exc))
        report['counts'] = dict(Counter(row['result'] for row in report['texts']))
        path = Path(options['report']).with_suffix('.json'); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        self.stdout.write(json.dumps({k:v for k,v in report.items() if k != 'texts'}, ensure_ascii=False, indent=2))
        self.stdout.write(f'Pełny raport: {path}')
        if report['conflicts']:
            raise CommandError('Korekta przerwana; wszystkie zmiany wycofano.')
