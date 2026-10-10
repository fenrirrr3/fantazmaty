"""Constant-size edit tokens. Domain services retain their transaction/row locks.

Wersje rosną w sygnałach zapisu modeli. QuerySet.update(), bulk_create()
i bulk_update() sygnałów nie wysyłają, dlatego śledzone modele używają
VersionedQuerySet, który podbija wersje także przy operacjach masowych.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from django.apps import apps
from django.db import models
from django.db.models import F
from django.db.models.signals import pre_save, post_save, post_delete, pre_delete, m2m_changed

# Only editable domain records; no audit/outbox/session traffic.
TRACKED = {
    'core.postlayoutassignment',
    'core.audiocontributor',
    'core.audiobook',
    'core.audiobookstage',
    'core.publicaudiobooksettings',
    'texts.novelprofile', 'texts.vocabularyterm',
    'texts.texttranslation', 'texts.foreignauthor', 'texts.translator',
    'texts.text', 'texts.review', 'texts.anthology', 'texts.anthologytask', 'texts.textnote',
    'illustrations.publicillustrationsettings',
    'illustrations.illustration', 'illustrations.coverproposal', 'illustrations.illustrator',
    'texts.reviewassignment', 'texts.reviewers', 'texts.extract',
    'workflow.workflowstage', 'workflow.workflowroleassignment',
    'workflow.workflowrepetition', 'people.person', 'people.vacation',
    'people.role', 'authors.author', 'authors.authornote', 'authors.blacklistedauthor', 'authors.blacklistentry',
    'core.mailboxconnection', 'core.newsletterconsent',
    'core.anthologycorrection', 'core.recruitment', 'auth.user', 'auth.group',
}


# Child records whose change must also invalidate the parent edit form.
PARENTS = {
    'core.audiobook': (('text_id', 'texts.text'),),
    'core.audiobookstage': (('text_id', 'texts.text'),),
    'workflow.workflowstage': (('text_id', 'texts.text'),),
    'workflow.workflowroleassignment': (('text_id', 'texts.text'),),
    'workflow.workflowrepetition': (('text_id', 'texts.text'),),
    'texts.anthologytask': (('anthology_id', 'texts.anthology'),),
    'texts.reviewassignment': (('review_id', 'texts.review'),),
    'texts.reviewers': (('review_id', 'texts.review'),),
    'texts.texttranslation': (('text_id', 'texts.text'),),
    'texts.textnote': (('text_id', 'texts.text'),),
    'authors.authornote': (('author_id', 'authors.author'),),
}

_batch = ContextVar('edit_version_batch', default=None)


@contextmanager
def batched_bumps():
    """Collapse repeated bumps of one record into a single UPDATE.

    Use inside the caller's transaction around loops that save many related
    rows (e.g. adding hundreds of chapters). Nothing is written on error.
    """
    if _batch.get() is not None:
        yield
        return
    pending = {}
    token = _batch.set(pending)
    try:
        yield
    finally:
        _batch.reset(token)
    _flush(pending)


def _flush(pending):
    """Write collected bumps with a few queries per model instead of one per row."""
    from collections import defaultdict
    from django.db import IntegrityError, transaction
    from core.models import EditRevision
    groups = defaultdict(set)
    for label, pk, using in pending:
        groups[(label, using)].add(pk)
    for (label, using), pks in groups.items():
        rows = EditRevision.objects.using(using)
        existing = set(rows.filter(model_label=label, object_id__in=pks).values_list('object_id', flat=True))
        if existing:
            rows.filter(model_label=label, object_id__in=existing).update(version=F('version') + 1)
        missing = pks - existing
        if not missing:
            continue
        try:
            with transaction.atomic(using=using):
                rows.bulk_create([EditRevision(model_label=label, object_id=pk) for pk in missing])
        except IntegrityError:
            # Another transaction created some rows meanwhile; fall back to the safe path.
            for pk in missing:
                _bump_now(label, pk, using)


def version_of(obj):
    from core.models import EditRevision
    return EditRevision.objects.using(obj._state.db).filter(
        model_label=obj._meta.concrete_model._meta.label_lower, object_id=obj.pk
    ).values_list('version', flat=True).first() or 0


def bump(label, pk, using):
    if not pk:
        return
    pending = _batch.get()
    if pending is not None:
        pending[(label, pk, using)] = None
        return
    _bump_now(label, pk, using)


def _bump_now(label, pk, using):
    from core.models import EditRevision
    rows = EditRevision.objects.using(using)
    if not rows.filter(model_label=label, object_id=pk).update(version=F('version') + 1):
        record, created = rows.get_or_create(model_label=label, object_id=pk)
        if not created:
            rows.filter(pk=record.pk).update(version=F('version') + 1)


def changed(sender, instance, using, raw=False, **kwargs):
    if raw or sender._meta.apps is not apps or sender._meta.label_lower not in TRACKED:
        return
    label = sender._meta.concrete_model._meta.label_lower
    bump(label, instance.pk, using)
    for attname, parent in PARENTS.get(label, ()):
        bump(parent, getattr(instance, attname), using)
    if label in ('texts.foreignauthor', 'texts.translator'):
        for text_id in instance.translations.using(using).values_list('text_id', flat=True):
            bump('texts.text', text_id, using)
    elif label == 'texts.textnote':
        old_text = getattr(instance, '_previous_note_text', None)
        if old_text and old_text != instance.text_id:
            bump('texts.text', old_text, using)
        instance._previous_note_text = None
    elif label == 'authors.authornote':
        old_author = getattr(instance, '_previous_note_author', None)
        if old_author and old_author != instance.author_id:
            bump('authors.author', old_author, using)
        instance._previous_note_author = None


def remember_note_author(sender, instance, using, raw=False, **kwargs):
    if raw or sender._meta.apps is not apps:
        return
    if sender._meta.label_lower == 'texts.textnote':
        instance._previous_note_text = sender.objects.using(using).filter(pk=instance.pk).values_list('text_id', flat=True).first() if instance.pk else None
        return
    if sender._meta.label_lower != 'authors.authornote':
        return
    instance._previous_note_author = sender.objects.using(using).filter(pk=instance.pk).values_list('author_id', flat=True).first() if instance.pk else None


def relations_changed(sender, instance, action, reverse, model, pk_set, using, **kwargs):
    if instance._meta.apps is not apps:
        return
    # Reverse clear has no pk_set after deletion. Capture before clearing.
    if action == 'pre_clear' and reverse:
        ids = set()
        for field in sender._meta.fields:
            if getattr(field, 'related_model', None) is type(instance):
                for other in sender._meta.fields:
                    if getattr(other, 'related_model', None) is model:
                        ids.update(sender.objects.using(using).filter(**{field.attname: instance.pk}).values_list(other.attname, flat=True))
        instance._edit_reverse_clear = ids
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    if instance._meta.label_lower in TRACKED:
        bump(instance._meta.label_lower, instance.pk, using)
    if instance._meta.label_lower == 'texts.texttranslation':
        bump('texts.text', instance.text_id, using)
    if reverse and model._meta.label_lower in TRACKED:
        for pk in pk_set or getattr(instance, '_edit_reverse_clear', set()):
            bump(model._meta.label_lower, pk, using)
            if model._meta.label_lower == 'texts.texttranslation':
                text_id = model.objects.using(using).filter(pk=pk).values_list('text_id', flat=True).first()
                bump('texts.text', text_id, using)


def install():
    pre_save.connect(remember_note_author, dispatch_uid='cms-edit-version-note-parent')
    post_save.connect(changed, dispatch_uid='cms-edit-version-save')
    post_delete.connect(changed, dispatch_uid='cms-edit-version-delete')
    pre_delete.connect(deleting_identity, dispatch_uid='cms-edit-version-identity-delete')
    m2m_changed.connect(relations_changed, dispatch_uid='cms-edit-version-m2m')


def deleting_identity(sender, instance, using, **kwargs):
    """Django cascade/SET_NULL does not emit child save or m2m signals."""
    if sender._meta.apps is not apps:
        return
    from texts.models import Text, Review, TextNote
    from workflow.models import WorkflowRoleAssignment
    from django.db.models import Q
    label = sender._meta.concrete_model._meta.label_lower
    if label == 'authors.author':
        text_ids = Text.objects.using(using).filter(Q(authors=instance)).values_list('pk', flat=True).distinct()
        review_ids = Review.objects.using(using).filter(Q(author=instance) | Q(coauthors=instance)).values_list('pk', flat=True).distinct()
    elif label in ('texts.foreignauthor', 'texts.translator'):
        text_ids = instance.translations.using(using).values_list('text_id', flat=True)
        review_ids = []
    elif label == 'auth.user':
        text_ids = set(WorkflowRoleAssignment.objects.using(using).filter(assigned_to=instance).values_list('text_id', flat=True))
        text_ids.update(TextNote.objects.using(using).filter(author=instance).values_list('text_id', flat=True))
        review_ids = Review.objects.using(using).filter(assignments__user=instance).values_list('pk', flat=True).distinct()
    else:
        return
    for pk in text_ids:
        bump('texts.text', pk, using)
    for pk in review_ids:
        bump('texts.review', pk, using)


def _bump_rows(model, rows, using):
    """rows: tuples (pk, *parent ids) in PARENTS order."""
    label = model._meta.concrete_model._meta.label_lower
    parents = PARENTS.get(label, ())
    with batched_bumps():
        for row in rows:
            if label in TRACKED:
                bump(label, row[0], using)
            for (_, parent), value in zip(parents, row[1:], strict=True):
                bump(parent, value, using)


class VersionedQuerySet(models.QuerySet):
    """QuerySet whose bulk writes keep edit versions current.

    Every model listed in TRACKED or PARENTS uses it as the default manager;
    core.test_edit_versions checks this, so new models cannot silently skip it.
    """

    def _version_fields(self):
        label = self.model._meta.concrete_model._meta.label_lower
        return ['pk', *[attname for attname, _ in PARENTS.get(label, ())]]

    def update(self, **kwargs):
        rows = list(self.values_list(*self._version_fields()))
        count = super().update(**kwargs)
        if count:
            _bump_rows(self.model, rows, self.db)
        return count

    update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        created = super().bulk_create(objs, *args, **kwargs)
        rows = [(obj.pk, *[getattr(obj, f) for f in self._version_fields()[1:]]) for obj in created if obj.pk]
        _bump_rows(self.model, rows, self.db)
        return created

    bulk_create.alters_data = True

    def bulk_update(self, objs, fields, *args, **kwargs):
        # bulk_update() runs update() internally; one batch keeps a single bump per row.
        with batched_bumps():
            return super().bulk_update(objs, fields, *args, **kwargs)

    bulk_update.alters_data = True
