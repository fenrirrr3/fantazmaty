"""Pomocniki testów bezpieczeństwa i infrastruktury."""
from pathlib import Path
from tempfile import TemporaryDirectory


class TemporaryActivitySpoolMixin:
    """Każdy test zapisuje dziennik aktywności do własnego, sprzątanego katalogu (``self.spool``)."""

    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.spool = Path(temporary.name)
        override = self.settings(ACTIVITY_SPOOL_DIR=self.spool)
        override.enable()
        self.addCleanup(override.disable)
