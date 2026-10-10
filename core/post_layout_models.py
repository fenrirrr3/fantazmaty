import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.edit_versions import VersionedQuerySet


class PostLayoutAssignment(models.Model):
    class Status(models.TextChoices):
        ASSIGNED = 'assigned', 'Przydzielony'
        IN_PROGRESS = 'in_progress', 'W trakcie'
        COMPLETED = 'completed', 'Zakończony'

    objects = VersionedQuerySet.as_manager()
    anthology = models.ForeignKey('texts.Anthology', on_delete=models.PROTECT, related_name='post_layout_assignments', verbose_name='antologia')
    proofreader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='post_layout_assignments', verbose_name='korektor poskładowy')
    page_from = models.PositiveIntegerField('strona od', null=True, blank=True)
    page_to = models.PositiveIntegerField('strona do', null=True, blank=True)
    status = models.CharField('status', max_length=16, choices=Status.choices, default=Status.ASSIGNED, db_index=True)
    assigned_start = models.DateField('przydzielony – rozpoczęcie', default=timezone.localdate, null=True, blank=True)
    assigned_end = models.DateField('przydzielony – zakończenie', null=True, blank=True)
    work_start = models.DateField('w trakcie – rozpoczęcie', null=True, blank=True)
    work_end = models.DateField('w trakcie – zakończenie', null=True, blank=True)
    completed_on = models.DateField('zakończony – data zakończenia', null=True, blank=True)
    created_at = models.DateTimeField('data przypisania', auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_post_layout_assignments', verbose_name='przypisał', null=True, blank=True)
    historical = models.BooleanField('historyczna korekta ze stopki', default=False, editable=False)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    version = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        verbose_name = 'przydział korekty poskładowej'
        verbose_name_plural = 'Korekta poskładowa – przydziały'
        ordering = ('-created_at', '-pk')
        constraints = [
            models.CheckConstraint(condition=(models.Q(page_from__isnull=False, page_to__isnull=False, page_from__gte=1, page_to__gte=models.F('page_from')) | models.Q(historical=True, page_from__isnull=True, page_to__isnull=True)), name='post_layout_page_range'),
            models.CheckConstraint(condition=((models.Q(historical=True, status='completed')
                & (models.Q(assigned_end__isnull=True, work_start__isnull=True) | models.Q(assigned_end__isnull=False, work_start__isnull=False, assigned_end=models.F('work_start')))
                & (models.Q(work_end__isnull=True, completed_on__isnull=True) | models.Q(work_end__isnull=False, completed_on__isnull=False, work_end=models.F('completed_on')))
                & (models.Q(assigned_start__isnull=True) | models.Q(work_start__isnull=True) | models.Q(work_start__gte=models.F('assigned_start')))
                & (models.Q(work_start__isnull=True) | models.Q(completed_on__isnull=True) | models.Q(completed_on__gte=models.F('work_start')))
                & (models.Q(assigned_start__isnull=True) | models.Q(completed_on__isnull=True) | models.Q(completed_on__gte=models.F('assigned_start')))) | (models.Q(assigned_start__isnull=False) & (
                models.Q(status='assigned', assigned_end__isnull=True, work_start__isnull=True, work_end__isnull=True, completed_on__isnull=True)
                | models.Q(status='in_progress', assigned_end=models.F('work_start'), assigned_end__isnull=False, work_start__isnull=False,
                    work_start__gte=models.F('assigned_start'), work_end__isnull=True, completed_on__isnull=True)
                | models.Q(status='completed', assigned_end=models.F('work_start'), assigned_end__isnull=False, work_start__isnull=False,
                    work_start__gte=models.F('assigned_start'), work_end=models.F('completed_on'),
                    work_end__isnull=False, completed_on__isnull=False, completed_on__gte=models.F('work_start'))
            ))), name='post_layout_status_dates'),
            models.CheckConstraint(condition=(models.Q(historical=True, status='completed') | models.Q(historical=False, created_by__isnull=False)), name='post_layout_history_state'),
        ]

    def __str__(self):
        return f'{self.anthology} – {self.pages_display}'

    @property
    def pages_display(self):
        return f'strony {self.page_from}–{self.page_to}' if self.page_from is not None else 'brak zakresu stron'

    def clean(self):
        super().clean()
        if self.page_from is not None and self.page_to is not None and (self.page_from < 1 or self.page_to < self.page_from):
            raise ValidationError({'page_to': 'Koniec zakresu nie może być mniejszy od początku; numeracja zaczyna się od 1.'})

    @property
    def next_status(self):
        return {'assigned': self.Status.IN_PROGRESS, 'in_progress': self.Status.COMPLETED}.get(self.status)

    @property
    def next_status_label(self):
        return dict(self.Status.choices).get(self.next_status, '')
