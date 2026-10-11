"""Komenda createsuperuser: adres e-mail do logowania jest wymagany, znormalizowany i unikalny, także w trybie
interaktywnym; konto dostaje imię i nazwisko."""
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


class TTY(StringIO):
    def isatty(self):
        return True


class CreateSuperuserTests(TestCase):
    ENVIRONMENT = {'DJANGO_SUPERUSER_PASSWORD': 'Mocne-haslo-927', 'DJANGO_SUPERUSER_FIRST_NAME': 'Anna',
                   'DJANGO_SUPERUSER_LAST_NAME': 'Admin'}

    def create(self, environment_email='', **options):
        environment = {**self.ENVIRONMENT, 'DJANGO_SUPERUSER_EMAIL': environment_email}
        with patch.dict('os.environ', environment):
            call_command('createsuperuser', username='anna', interactive=False, stdout=StringIO(), **options)
        return get_user_model().objects.get(username='anna')

    def test_environment_email_is_normalized_and_names_are_required(self):
        user = self.create(' Anna@Example.TEST ')
        self.assertTrue(user.is_superuser)
        self.assertEqual((user.email, user.first_name, user.last_name), ('anna@example.test', 'Anna', 'Admin'))
        self.assertTrue(user.check_password('Mocne-haslo-927'))

    def test_missing_invalid_or_taken_email_is_refused(self):
        get_user_model().objects.create_user('zajety', 'zajety@example.test')
        for email, options, message in (
            ('', {}, 'Podaj adres e-mail'),
            ('to-nie-adres', {}, 'poprawny adres email'),
            ('', {'email': 'ZAJETY@example.test'}, 'już istnieje'),
        ):
            with self.subTest(email=email, options=options):
                with self.assertRaises(CommandError) as caught:
                    self.create(email, **options)
                self.assertIn(message, str(caught.exception))
        self.assertFalse(get_user_model().objects.filter(username='anna').exists())

    def test_interactive_prompt_repeats_until_email_is_valid(self):
        answers = iter(['anna', 'zajety@example.test', 'Nowa@Example.test', 'Anna', 'Admin'])
        get_user_model().objects.create_user('zajety', 'zajety@example.test')
        errors = StringIO()
        with patch('builtins.input', side_effect=lambda *_: next(answers)), \
                patch('getpass.getpass', return_value='Mocne-haslo-927'):
            call_command('createsuperuser', interactive=True, stdin=TTY(), stdout=StringIO(), stderr=errors)
        self.assertIn('Konto z tym adresem e-mail już istnieje.', errors.getvalue())
        self.assertEqual(get_user_model().objects.get(username='anna').email, 'nowa@example.test')
