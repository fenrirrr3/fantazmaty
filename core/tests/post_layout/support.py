"""Wspólne dane i pomocniki testów korekty poskładowej."""
from datetime import date

from django.urls import reverse

from core.models import PostLayoutAssignment
from core.post_layout import AssignmentForm, create_assignment, selection_token
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology


class PostLayoutTestData:
    """Koordynator korekty, dwie osoby z rolą Korektor poskładowy, koordynator redakcji i antologia."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('layout-admin')
        cls.manager = create_member('layout-manager', 'Koordynator korekty')
        cls.reader = create_member('layout-reader', 'Korektor poskładowy')
        cls.other = create_member('layout-other', 'Korektor poskładowy')
        cls.editor = create_member('layout-editor', 'Koordynator redakcji')
        cls.book = Anthology.objects.create(title='Antologia poskładowa')
        cls.url = reverse('core:post_layout')

    def form_data(self, user=None, **values):
        """Dane formularza dodawania przydziału z aktualnym tokenem."""
        form = AssignmentForm(user=user or self.manager)
        return {'action': 'create', 'token': form.initial['token'], 'anthology': self.book.pk,
                'proofreader': self.reader.pk, **values}

    def create(self, user=None, **values):
        """Przydział utworzony jak z formularza; zakres stron ustawiany jak przy edycji wpisu."""
        pages = {'page_from': values.pop('page_from', 1), 'page_to': values.pop('page_to', 20)}
        user = user or self.manager
        form = AssignmentForm(self.form_data(user, **values), user=user)
        self.assertTrue(form.is_valid(), form.errors)
        return create_assignment(user=user, **form.cleaned_data, **pages)

    def item(self, **values):
        """Przydział zapisany bezpośrednio (strony 1–20, przydzielony 1.01.2026)."""
        return PostLayoutAssignment.objects.create(
            anthology=self.book, proofreader=self.reader, page_from=1, page_to=20,
            assigned_start=date(2026, 1, 1), created_by=self.manager, **values)

    def completed_item(self):
        return self.item(status='completed', assigned_end=date(2026, 1, 3), work_start=date(2026, 1, 3),
                         work_end=date(2026, 1, 5), completed_on=date(2026, 1, 5))

    def edit_data(self, item, **values):
        return {'proofreader': self.other.pk, 'page_from': 10, 'page_to': 30,
                'assigned_start': '2026-01-02', 'work_start': '2026-01-04', 'completed_on': '2026-01-06',
                'version': item.version, **values}

    def transition(self, item, status, user=None, version=None):
        self.client.force_login(user or self.reader)
        return self.client.post(self.url, {'action': 'status', 'pk': item.pk, 'status': status,
                                           'version': item.version if version is None else version})

    def bulk(self, items, operation, user=None, **values):
        user = user or self.manager
        self.client.force_login(user)
        return self.client.post(self.url, {'action': 'bulk', 'operation': operation,
                                           'selected': [selection_token(user, item) for item in items], **values})
