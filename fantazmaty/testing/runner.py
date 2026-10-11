"""Runner testów: wycisza wydruki migracji danych podczas tworzenia bazy testowej.

Migracje danych (np. people 0017, workflow 0011/0012) raportują na stdout, co zmieniły – to przydatne przy
wdrożeniu, ale w testach (pusta baza, same zera) zaśmieca wynik. Przekierowanie obejmuje tylko
``setup_databases``; testy migracji, które same przechwytują wydruk (``redirect_stdout``), działają bez zmian.
Komunikaty Django o tworzeniu bazy idą na stderr i zostają. Przy ``-v 2`` i wyżej nic nie jest wyciszane,
a jeśli przygotowanie bazy się nie powiedzie, przechwycony wydruk trafia na stderr razem z błędem.
"""
import contextlib
import io
import sys

from django.test.runner import DiscoverRunner


class QuietMigrationsDiscoverRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        if self.verbosity >= 2:
            return super().setup_databases(**kwargs)
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                return super().setup_databases(**kwargs)
        except BaseException:
            sys.stderr.write(captured.getvalue())
            raise
