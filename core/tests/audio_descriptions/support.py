"""Wspólne dane testów audiodeskrypcji: zespół, antologia z zadaniem AD i wysyłka formularzy strony."""
from django.urls import reverse

from fantazmaty.testing import create_member, create_superuser, edit_token
from texts.models import Anthology


class AudioDescriptionTestData:
    """Osoba pisząca (Redaktor), kontroler (Korektor), koordynator, inny redaktor i administrator."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.writer = create_member('ad-writer', 'Redaktor')
        cls.controller = create_member('ad-controller', 'Korektor')
        cls.manager = create_member('ad-manager', 'Koordynator redakcji')
        cls.other = create_member('ad-other', 'Redaktor')
        cls.admin = create_superuser('ad-admin')
        cls.book = Anthology.objects.create(title='Antologia do opisania')
        cls.list_url = reverse('core:audio_descriptions')
        cls.detail = reverse('core:audio_description_detail', args=[cls.book.pk])
        cls.claim = reverse('core:claim_audio_description', args=[cls.book.pk])

    def setUp(self):
        super().setUp()
        self.task = self.book.production_tasks.get(task_type='audio_description')
        self.description = self.book.audio_description

    def post(self, user, url=None, **data):
        """POST jako `user` z aktualnym tokenem wersji antologii."""
        self.client.force_login(user)
        return self.client.post(url or self.detail, {'_edit_version': edit_token(user, self.book), **data})

    def assign_writer_and_controller(self):
        self.task.assigned_to = self.writer.person_profile
        self.task.status = 'commissioned'
        self.task.save()
        self.description.controllers.add(self.controller.person_profile)
