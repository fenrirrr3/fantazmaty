"""Remove only the obsolete v23 files listed below; never touch a database."""
import argparse
from pathlib import Path

OBSOLETE = ('core/selectors/history.py', 'core/test_historical_assignments.py', 'settings.py')


def main():
    parser = argparse.ArgumentParser(description='Porządki po aktualizacji 24. Bez --apply: tylko podgląd.')
    parser.add_argument('--apply', action='store_true', help='Usuń wymienione stare pliki.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if not (root / 'manage.py').is_file() or not (root / 'core').is_dir():
        parser.error('Umieść skrypt obok manage.py w katalogu projektu.')
    targets = []
    for relative in OBSOLETE:
        source = root / relative
        candidates = [source]
        if source.suffix == '.py':
            candidates.extend((source.parent / '__pycache__').glob(source.stem + '.*.pyc'))
        for file in candidates:
            if not file.resolve().is_relative_to(root):
                parser.error(f'Ścieżka wychodzi poza projekt: {relative}')
            if file.is_file():
                targets.append(file)
    for file in targets:
        print(('Usuwam: ' if args.apply else 'Do usunięcia: ') + str(file.relative_to(root)))
        if args.apply:
            file.unlink()
    print(f'Plików: {len(targets)}. ' + ('Gotowe.' if args.apply else 'Aby usunąć, uruchom ponownie z --apply.'))


if __name__ == '__main__':
    main()
