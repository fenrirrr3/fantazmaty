"""Komenda check_runtime: pokazuje interpreter i pilnuje wersji Django przypiętej w requirements.txt."""
import re
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase



class CheckRuntimeCommandTests(SimpleTestCase):
    def test_required_version_is_the_pinned_one_and_others_are_rejected(self):
        """Wersja wymagana przez komendę to ta z requirements.txt (bez osobnej kopii w kodzie); inne są odrzucane."""
        pinned = re.search(r'^Django==(\S+)$', (Path(settings.BASE_DIR) / 'requirements.txt').read_text(), re.M)[1]
        for version, error in ((pinned, None), ('5.2.17', f'Wymagane Django {pinned}.'), ('6.0', f'Wymagane Django {pinned}.')):
            with self.subTest(version=version), patch('django.get_version', return_value=version):
                out = StringIO()
                if error:
                    with self.assertRaisesMessage(CommandError, error):
                        call_command('check_runtime', stdout=out)
                else:
                    call_command('check_runtime', stdout=out)
                    self.assertIn('Wersja Django zgodna z projektem i testami.', out.getvalue())
                self.assertIn(f'Interpreter: {sys.executable}\nDjango: {version}', out.getvalue())

    def test_missing_pin_is_reported(self):
        """Bez przypiętej wersji w requirements.txt komenda zgłasza błąd zamiast przepuszczać dowolną wersję."""
        with patch('core.management.commands.check_runtime.pinned_django_version', return_value=None):
            with self.assertRaisesMessage(CommandError, 'Nie udało się odczytać przypiętej wersji Django'):
                call_command('check_runtime', stdout=StringIO())
