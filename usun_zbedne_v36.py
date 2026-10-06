"""Run from the project root after applying v36. Removes only verified obsolete files."""
from pathlib import Path
import argparse
import hashlib

EXPECTED = {'gatunki_i_tagi.json': 'c73835161109f14a445e04ee294ff4fe68d550599f739ed2f52e0dc587788e72', 'pliki_do_podmiany.txt': '03ffe55f98e9ec5cf60524d36ade84c00daa4808ec5b5a379ac3c03d820fa824', 'core/static/core/workflow-repeat.js': '671497481d7bab7d9f0257c9ce842039f5ee040cc84324763853167f9766b11c'}

def main():
    parser=argparse.ArgumentParser(description='Usuń potwierdzone zbędne pliki v36; bez zmian w bazie.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--dry-run', action='store_true')
    options=parser.parse_args()
    root=Path(options.root).resolve()
    if not (root/'manage.py').is_file() or not (root/'fantazmaty/settings.py').is_file():
        raise SystemExit('Podaj katalog projektu zawierający manage.py i fantazmaty/settings.py.')
    targets=[]
    for relative, digest in EXPECTED.items():
        target=(root/relative).resolve()
        if not target.is_relative_to(root):
            raise SystemExit('Plik wskazuje poza katalog projektu. Niczego nie usunięto.')
        if not target.exists():continue
        if not target.is_file() or hashlib.sha256(target.read_bytes().replace(b'\r\n', b'\n')).hexdigest()!=digest:
            raise SystemExit('Plik zmieniono od audytu; niczego nie usunięto: '+relative)
        targets.append(target)
    for target in targets:
        print(('Do usunięcia: ' if options.dry_run else 'Usunięto: ')+str(target.relative_to(root)))
        if not options.dry_run:target.unlink()
    print('Gotowe. Plików: '+str(len(targets))+'. Bazy danych nie zmieniono.')

if __name__=='__main__':main()
