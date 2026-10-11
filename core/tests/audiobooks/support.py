"""Wspólne dane i pomocniki testów produkcji audiobooków (strona audiobooka, Korekta audiobooków)."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.models import Audiobook
from fantazmaty.testing import create_member, edit_token
from texts.models import Anthology, Text


class AudiobookProductionTestData:
    """Tekst z gotowej antologii, koordynator, dwie osoby z rolą Korektor audiobooków i redaktor bez uprawnień."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = get_user_model().objects.create_superuser('audio-manager', 'audio@example.test', 'test')
        cls.coordinator = create_member('audio-coordinator', 'Koordynator')
        cls.reader = create_member('audio-reader', 'Korektor audiobooków')
        cls.other_reader = create_member('other-reader', 'Korektor audiobooków')
        cls.member = create_member('audio-editor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Audiobookowa antologia', status=Anthology.Status.READY)
        cls.text = Text.objects.create(title='Głos', anthology=cls.book, length=1234, tags='smoki', genre='fantasy')
        cls.text.authors.add(Author.objects.create(first_name='Prywatne', last_name='Nazwisko', pseudonym='Podpis'))
        cls.url = reverse('core:audiobook_detail', args=[cls.text.pk])
        cls.list_url = reverse('core:audiobooks')
        cls.queue_url = reverse('core:audio_proofreading')
        cls.assign_url = reverse('core:assign_audio_proofreader', args=[cls.text.pk])
        cls.claim_url = reverse('core:claim_audio_proofreading', args=[cls.text.pk])
        cls.public = reverse('core:external_audiobooks')

    def token(self, user=None):
        return edit_token(user or self.admin, self.text)

    def post(self, action, user=None, **fields):
        """Akcja formularza strony audiobooka wysłana jako `user` (domyślnie administrator)."""
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.url, {'action': action, '_edit_version': self.token(user), **fields})

    def start(self, user=None, day=None):
        """Rozpoczęcie etapu; day=False – bez pola daty, '' – data nieznana, None – dzisiaj."""
        fields = {} if day is False else {'stage-date': str(day or timezone.localdate()) if day != '' else ''}
        return self.post('start_stage', user, **fields)

    def finish(self, user=None, day=None):
        """Zakończenie bieżącego etapu; znaczenie `day` jak w start()."""
        fields = {'stage_id': self.audio().active_stage_id}
        if day is not False:
            fields['stage-date'] = '' if day == '' else str(day or timezone.localdate())
        return self.post('finish_stage', user, **fields)

    def audio(self):
        return Audiobook.objects.get(text=self.text)

    def assign(self, person, user=None):
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.assign_url, {'proofreader': person.pk if person else '',
                                                  '_edit_version': self.token(user)})

    def claim(self, user):
        self.client.force_login(user)
        return self.client.post(self.claim_url, {'_edit_version': self.token(user)})
