"""Constant-size edit tokens. Domain services retain their transaction/row locks."""
from django.apps import apps
from django.db.models import F
from django.db.models.signals import pre_save, post_save, post_delete, pre_delete, m2m_changed

# Only editable domain records; no audit/outbox/session traffic.
TRACKED = {
    'texts.text', 'texts.review', 'texts.anthology', 'texts.anthologytask', 'texts.textnote',
    'illustrations.illustration', 'illustrations.coverproposal',
    'texts.reviewassignment', 'texts.reviewers', 'texts.extract',
    'workflow.workflowstage', 'workflow.workflowroleassignment',
    'workflow.workflowrepetition', 'people.person', 'people.vacation',
    'people.role', 'people.rekrutacja', 'authors.author', 'authors.authornote', 'authors.blacklistedauthor',
    'core.anthologycorrection', 'core.recruitment', 'auth.user', 'auth.group',
}


def version_of(obj):
    from core.models import EditRevision
    return EditRevision.objects.using(obj._state.db).filter(
        model_label=obj._meta.concrete_model._meta.label_lower, object_id=obj.pk
    ).values_list('version', flat=True).first() or 0


def bump(label, pk, using):
    if not pk:
        return
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
    if label in ('texts.textnote', 'workflow.workflowstage', 'workflow.workflowroleassignment', 'workflow.workflowrepetition'):
        bump('texts.text', instance.text_id, using)
    elif label in ('texts.reviewassignment', 'texts.reviewers'):
        bump('texts.review', instance.review_id, using)
    elif label == 'authors.authornote':
        bump('authors.author', instance.author_id, using)
        old_author = getattr(instance, '_previous_note_author', None)
        if old_author and old_author != instance.author_id:
            bump('authors.author', old_author, using)
        instance._previous_note_author = None


def remember_note_author(sender, instance, using, raw=False, **kwargs):
    if raw or sender._meta.apps is not apps or sender._meta.label_lower != 'authors.authornote':
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
    if reverse and model._meta.label_lower in TRACKED:
        for pk in pk_set or getattr(instance, '_edit_reverse_clear', set()):
            bump(model._meta.label_lower, pk, using)


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
        text_ids = Text.objects.using(using).filter(authors=instance).values_list('pk', flat=True)
        review_ids = Review.objects.using(using).filter(Q(author=instance) | Q(coauthors=instance)).values_list('pk', flat=True).distinct()
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
