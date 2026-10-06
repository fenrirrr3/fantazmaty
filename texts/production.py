"""Read-only visibility of active production queues; assignments are retained."""
from django.db.models import Exists, OuterRef


def active_production_texts(queryset):
    from workflow.models import WorkflowStage
    withdrawn = WorkflowStage.objects.filter(
        text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'),
        is_current=True, is_released=True, stage_type=WorkflowStage.StageType.WITHDRAWN,
    )
    return queryset.exclude(anthology__is_novel=True).alias(
        production_withdrawn=Exists(withdrawn),
    ).filter(production_withdrawn=False)
