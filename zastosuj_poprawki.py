"""Remove only unused source files from this patch; no database operations."""
from pathlib import Path

root = Path(__file__).resolve().parent
if not (root / 'manage.py').is_file() or not (root / 'core').is_dir():
    raise SystemExit('Umieść skrypt w głównym katalogu projektu, obok manage.py.')

REMOVED = ('authors/views.py', 'core/views/_init_.py', 'people/views.py', 'texts/static/texts/admin/texts/review/change_form.html', 'texts/views.py', 'workflow/views.py')
targets = []
for name in REMOVED:
    file = root / name
    if not file.resolve().is_relative_to(root):
        raise SystemExit('Ścieżka poza projektem: ' + name)
    if file.exists() and not file.is_file():
        raise SystemExit('Oczekiwano pliku: ' + name)
    targets.append((name, file))
for name, file in targets:
    if file.exists():
        file.unlink()
        print('Usunięto: ' + name)
print('Porządek zakończony. Baza, migracje i konfiguracja lokalna pozostały bez zmian.')
