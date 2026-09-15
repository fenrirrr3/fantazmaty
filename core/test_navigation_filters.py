from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin
from texts.models import Text, Anthology
from workflow.models import WorkflowStage, WorkflowRoleAssignment

class NavigationFilterTests(CoreTestDataMixin, TestCase):
    def make_text(self, title, kind='ready_for_editing', assigned=False):
        text = Text.objects.create(title=title, length=12000, anthology=self.anthology)
        text.authors.add(self.author)
        WorkflowStage.objects.create(text=text, stage_type=kind, started_at=timezone.localdate() if assigned else None)
        if assigned:
            WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=self.editor)
        return text

    def test_ready_anthology_hides_panels_and_menu(self):
        text = self.make_text('Tekst', 'editing', True)
        self.client.force_login(self.superuser)
        url = reverse('core:assigned_text_detail', args=[text.pk])
        self.assertContains(self.client.get(url), 'dropbox-folder-heading')
        self.anthology.status = Anthology.Status.READY
        self.anthology.save()
        response = self.client.get(url)
        self.assertNotContains(response, 'workflow-folder-layout')
        self.assertNotContains(response, 'discord-test/')
        self.assertContains(response, 'data-menu-section="reports"')
        self.assertEqual(self.anthology.get_status_display(), 'Gotowa')

    def test_my_filters_are_scoped_and_preserve_view(self):
        own = self.make_text('Żółty smok', 'editing', True)
        self.make_text('Żółty cudzy')
        self.client.force_login(self.editor)
        response = self.client.get(reverse('core:my_texts'), {'view':'all', 'q':'zolty', 'sort':'-length'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t["pk"] for t in response.context['texts']], [own.pk])
        self.assertContains(response, 'name="view" value="all"')
        self.assertNotContains(response, 'id="text-author"')
        response = self.client.get(reverse('core:my_texts'), {'view':'all', 'q':'brak'})
        self.assertEqual(len(response.context['texts']), 0)

    def test_available_filters_and_sort(self):
        text = self.make_text('Żółty smok')
        self.make_text('Inny tekst')
        self.client.force_login(self.editor)
        for sort in ['title','-title','length','-length','status','-status']:
            response = self.client.get(reverse('core:available_texts'), {'q':'zolty','sort':sort,'status':'ready_for_editing','anthology':self.anthology.pk})
            self.assertEqual(response.status_code,200)
            self.assertEqual([s["text"]["pk"] for s in response.context['available_stages']], [text.pk])
        response = self.client.get(reverse('core:available_texts'), {'status':'styling'})
        self.assertEqual(len(response.context['available_stages']),2)
        for name in ('status', 'sort', 'hide_ready'):
            self.assertNotContains(response, 'id="text-' + name.replace('_', '-') + '"')
