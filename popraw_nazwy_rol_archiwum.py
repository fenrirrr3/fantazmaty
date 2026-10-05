#!/usr/bin/env python3
"""Replace only historical role labels. No database connection or data changes."""
import ast
from datetime import datetime
import os
from pathlib import Path
import stat
import tempfile


REPLACEMENT = '''def archive_work_label(text, kind, number):
    """Use ordinary role names and execution numbering for historical work."""
    if not is_archive_text(text):
        return None
    from workflow.catalog import all_stage_roles
    from workflow.models import WorkflowRoleAssignment
    role = all_stage_roles().get(kind, kind)
    label = dict(WorkflowRoleAssignment.Role.choices).get(role)
    if label:
        return f'{label} (wyk. {number})' if number > 1 else label
    return None
'''


def main():
    project = Path.cwd()
    path = project / 'workflow' / 'archive_executions.py'
    if not (project / 'manage.py').is_file() or not path.is_file():
        raise SystemExit('Uruchom skrypt w katalogu CMS zawierającym manage.py i workflow/archive_executions.py.')
    if path.is_symlink():
        raise SystemExit('Plik jest dowiązaniem symbolicznym. Nie zmieniono go automatycznie.')
    original = path.read_bytes()
    source = original.decode('utf-8-sig')
    tree = ast.parse(source, filename=str(path))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'archive_work_label']
    if len(functions) != 1:
        raise SystemExit('Nie znaleziono dokładnie jednej funkcji archive_work_label. Niczego nie zmieniono.')
    node = functions[0]
    if (node.decorator_list or [a.arg for a in node.args.args] != ['text', 'kind', 'number']
            or node.args.posonlyargs or node.args.kwonlyargs or node.args.vararg or node.args.kwarg):
        raise SystemExit('Funkcja ma nieoczekiwaną postać. Niczego nie zmieniono.')
    if not any(isinstance(n, ast.FunctionDef) and n.name == 'is_archive_text' for n in tree.body):
        raise SystemExit('Brakuje ograniczenia do tekstów archiwalnych. Niczego nie zmieniono.')
    lines = source.splitlines(keepends=True)
    newline = '\r\n' if '\r\n' in source else '\n'
    replacement = REPLACEMENT.replace('\n', newline)
    updated = ''.join(lines[:node.lineno-1]) + replacement + ''.join(lines[node.end_lineno:])
    encoded = (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + updated.encode('utf-8')
    compile(updated, str(path), 'exec')
    if encoded == original:
        print('Nazwy są już poprawione w tym katalogu. Bazy danych nie zmieniono.')
        print('Kliknij Reload w Web PythonAnywhere. Jeśli stare napisy pozostają, sprawdź katalog kodu wskazany przez konfigurację WSGI.')
        return
    backup = path.with_name(path.name + '.before-labels-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with backup.open('xb') as stream:
        stream.write(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.archive-labels-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if path.read_bytes() != original:
            raise RuntimeError('Plik zmienił się w trakcie pracy; przerwano podmianę.')
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(f'Poprawiono wyłącznie funkcję archive_work_label w {path}.')
    print(f'Kopia poprzedniego pliku: {backup}')
    print('Redaktor; Korektor 1; Weryfikator 1. Kolejne wykonania: (wyk. 2), (wyk. 3) itd.')
    print('Pozostałe numery ról zachowano. Nie zmieniono danych ani przypisań w bazie.')
    print('Teraz kliknij Reload w zakładce Web PythonAnywhere. Migrate i collectstatic nie są potrzebne.')


if __name__ == '__main__':
    main()
