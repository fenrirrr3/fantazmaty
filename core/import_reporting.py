"""Shared per-record summary; no data changes outside the import transaction."""
import json
from collections import defaultdict
from pathlib import Path
from django.db.models.signals import pre_save, post_save, m2m_changed

class ImportReportMixin:
    def create_parser(self, *args, **kwargs):
        parser = super().create_parser(*args, **kwargs)
        parser.add_argument('--report', help='Zapisz podsumowanie JSON do wskazanego pliku.')
        return parser

    def execute(self, *args, **options):
        created = defaultdict(set); updated = defaultdict(set); before = {}; role_changes = []
        labels = {'auth.user': 'konta', 'people.person': 'osoby', 'people.role': 'role', 'authors.author': 'autorzy', 'texts.text': 'teksty', 'texts.review': 'zgloszenia', 'texts.extract': 'ekstrakty', 'texts.historicaltextassignment': 'przypisania_historyczne', 'texts.reviewassignment': 'recenzje', 'workflow.workflowstage': 'etapy', 'workflow.workflowroleassignment': 'przypisania'}
        def pre(sender, instance, **kw):
            if sender._meta.label_lower not in labels or not instance.pk:return
            fields = [f.attname for f in sender._meta.concrete_fields if f.name not in ('password','last_login','updated_at')]
            before[(sender, instance.pk)] = (fields, sender.objects.filter(pk=instance.pk).values(*fields).first())
        def post(sender, instance, created=False, **kw):
            label = labels.get(sender._meta.label_lower)
            if not label:return
            if created:new_rows[label].add(instance.pk)
            else:
                fields, old = before.get((sender,instance.pk), ([],None))
                if old and any(old[field] != getattr(instance,field) for field in fields):updated[label].add(instance.pk)
        def roles(sender, instance, action, pk_set, reverse, **kw):
            from people.models import Person, Role
            if sender is Person.roles.through and action == 'post_add' and pk_set:
                role_changes.append({'person_id': None if reverse else instance.pk, 'roles': [instance.name] if reverse else list(Role.objects.filter(pk__in=pk_set).values_list('name',flat=True))})
        new_rows=created
        pre_save.connect(pre, weak=False);post_save.connect(post,weak=False);m2m_changed.connect(roles,weak=False)
        error = None
        try:
            return super().execute(*args, **options)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            pre_save.disconnect(pre);post_save.disconnect(post);m2m_changed.disconnect(roles)
            report = {'mode':'wycofano' if error else 'zapis' if (options.get('commit') or options.get('apply')) else 'podglad', 'models': {label:{'added':len(created[label]), 'updated':len(updated[label]-created[label])} for label in sorted(set(labels.values()))}, 'added_roles':role_changes, 'conflicts':[error] if error else [], 'command_counts':dict(getattr(self,'counts',{})), 'note':'Przy błędzie zestawienie obejmuje sprawdzone rekordy do miejsca przerwania; zapis wycofano. Reguły pustych pól i __CLEAR__ opisano osobno dla poszczególnych pól w README-AKTUALIZACJA-8.md.'}
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            if options.get('report'):
                Path(options['report']).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
