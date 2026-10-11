"""Wspólne dane i pomocniki testów antologii i powieści."""
from django.urls import reverse

from authors.models import Author
from core.anthology_tasks import TASK_CHOICES
from fantazmaty.testing import create_member, create_superuser, edit_token
from texts.models import Anthology
from texts.novels import new_chapter


class AnthologyPageData:
    """Koordynator, redaktor i superuser oraz pusta antologia ze stroną zadań i okładki."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('anthology-admin')
        cls.manager = create_member('anthology-manager', 'Koordynator redakcji')
        cls.member = create_member('anthology-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Antologia testowa')

    def setUp(self):
        super().setUp()
        self.url = reverse('core:anthology_detail', args=[self.book.pk])
        self.client.force_login(self.manager)

    def token(self, user=None):
        return edit_token(user or self.manager, self.book)

    def new_person(self, kind='blurb', **values):
        """Formularz „Dodaj osobę do zadania” (nowy kontakt zewnętrzny albo ilustrator okładki)."""
        return self.client.post(self.url, {
            'action': 'new_person', '_edit_version': self.token(),
            'new-person-first_name': 'Nowa', 'new-person-last_name': kind,
            'new-person-task_type': kind, **values,
        })

    def remove(self, kind, **extra):
        return self.client.post(self.url, {
            'action': 'tasks_and_cover', 'remove_task': kind, '_edit_version': self.token(), **extra,
        })

    def all_tasks_data(self):
        return {
            'action': 'tasks_and_cover', '_edit_version': self.token(),
            'cover-cover_status': 'not_started',
            **{f'{kind}-status': 'not_commissioned' for kind, _ in TASK_CHOICES if kind != 'cover'},
        }

    def post_cover(self, user=None, **values):
        user = user or self.manager
        self.client.force_login(user)
        return self.client.post(self.url, {
            'action': 'cover', '_edit_version': self.token(user), 'cover-cover_status': 'in_progress',
            **{f'cover-{k}': v for k, v in values.items()},
        })


class NovelData:
    """Powieść z autorem i pierwszym rozdziałem oraz zespół: superuser, koordynator i dwóch redaktorów."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('book-admin')
        cls.coordinator = create_member('book-coordinator', 'Koordynator redakcji')
        cls.editor = create_member('book-editor', 'Redaktor')
        cls.other = create_member('book-editor-other', 'Redaktor')
        cls.book = Anthology.objects.create(title='Książka z rozdziałami', is_novel=True)
        cls.author = Author.objects.create(first_name='Jan', last_name='Kowalski', pseudonym='Pióro', email=None)
        cls.book.novel.authors.add(cls.author)
        cls.chapter = new_chapter(cls.book, cls.book.novel, chapter_number=1)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse('core:novel_detail', args=[self.book.pk])
