#!/usr/bin/env python3
"""Export Dropbox folder links to a CSV; independent of Django, standard library only."""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ApiError(Exception):
    def __init__(self, status, tag=''):
        self.status = status
        super().__init__(f'Dropbox HTTP {status}' + (f' ({tag})' if tag else ''))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Dropbox:
    def __init__(self, token, namespace=None):
        self.headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        if namespace:
            self.headers['Dropbox-API-Path-Root'] = json.dumps({'.tag': 'namespace_id', 'namespace_id': namespace})
        self.opener = build_opener(NoRedirect())

    def call(self, endpoint, payload):
        for attempt in range(3):
            req = Request('https://api.dropboxapi.com/2/' + endpoint,
                          data=json.dumps(payload).encode('utf-8'), headers=self.headers, method='POST')
            try:
                with self.opener.open(req, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    try: delay = min(max(float(exc.headers.get('Retry-After', '2')), 1), 30)
                    except ValueError: delay = 2
                    time.sleep(delay)
                    continue
                # Never print response bodies, headers or tokens.
                raise ApiError(exc.code) from None
            except (URLError, TimeoutError, OSError, ValueError):
                raise ApiError('brak odpowiedzi', 'sprawdź połączenie i ponów odczyt') from None

    def folders(self, root, recursive=False, include_root=False):
        if root:
            item = self.call('files/get_metadata', {'path':root})
            if item.get('.tag') != 'folder':
                raise ValueError('Wskazana ścieżka nie jest folderem.')
            if include_root:
                yield item
        result = self.call('files/list_folder', {'path':root, 'recursive':recursive})
        while True:
            for item in result['entries']:
                if item.get('.tag') == 'folder':
                    yield item
            if not result.get('has_more'):
                break
            result = self.call('files/list_folder/continue', {'cursor':result['cursor']})

    def existing_link(self, folder):
        result = self.call('sharing/list_shared_links', {'path':folder['id'], 'direct_only':True})
        while True:
            for link in result['links']:
                # Do not return a link to the parent folder or a different item.
                same = link.get('id') == folder['id'] or (
                    bool(folder.get('path_lower')) and link.get('path_lower') == folder['path_lower'])
                if same and link.get('.tag') == 'folder':
                    return link['url']
            if not result.get('has_more'):
                return ''
            result = self.call('sharing/list_shared_links', {'cursor':result['cursor']})

    def folder_link(self, folder, create=False, visibility=None):
        link = self.existing_link(folder)
        if link:
            return link, 'istniejący'
        if not create:
            return '', 'brak linku'
        try:
            result = self.call('sharing/create_shared_link_with_settings', {
                'path':folder['id'], 'settings':{'requested_visibility':visibility}})
            return result['url'], 'utworzony'
        except ApiError as exc:
            # Another request may have created the link. Never retry a timed-out creation blindly.
            if exc.status == 409:
                link = self.existing_link(folder)
                if link:
                    return link, 'istniejący'
            raise


def cell(value):
    value = str(value or '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def export(client, root, output, *, recursive=False, create=False, visibility=None, include_root=False):
    count = errors = 0
    # Exclusive creation avoids sharing folders and then accidentally overwriting an old report.
    with Path(output).open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=';')
        writer.writerow(['folder_id','folder_path','folder_name','file_url','result','error'])
        stream.flush()
        for folder in client.folders(root, recursive, include_root):
            try:
                link, result = client.folder_link(folder, create, visibility)
                error = ''
            except ApiError as exc:
                link, result, error = '', 'błąd', str(exc)
                errors += 1
                # Avoid repeating a global token/permission failure for every folder.
                if exc.status in (401, 403):
                    writer.writerow([cell(folder['id']),cell(folder.get('path_display')),cell(folder['name']),link,result,error])
                    stream.flush()
                    raise
            writer.writerow([cell(folder['id']),cell(folder.get('path_display')),cell(folder['name']),link,result,error])
            stream.flush()
            count += 1
    return count, errors


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='Ścieżka Dropbox, np. /Antologie/Nowa antologia (nie adres WWW)')
    parser.add_argument('--output', default='dropbox-folder-links.csv')
    parser.add_argument('--include-root', action='store_true', help='Uwzględnij także sam folder wskazany przez --root')
    parser.add_argument('--recursive', action='store_true', help='Uwzględnij wszystkie poziomy podfolderów')
    parser.add_argument('--create-missing', action='store_true', help='Utwórz brakujące linki; zmienia udostępnianie w Dropbox')
    parser.add_argument('--visibility', choices=['public','team_only'], help='Wymagane przy tworzeniu; public = dostęp dla posiadaczy linku')
    parser.add_argument('--namespace-id', help='Opcjonalna przestrzeń Dropbox zespołu')
    args=parser.parse_args(argv)
    if args.create_missing and not args.visibility:
        parser.error('--create-missing wymaga jawnego --visibility public lub team_only')
    token=os.environ.get('DROPBOX_ACCESS_TOKEN', '').strip()
    if not token:
        parser.error('Ustaw zmienną środowiskową DROPBOX_ACCESS_TOKEN.')
    root=args.root.rstrip('/')
    if root and not root.startswith('/'):
        parser.error('--root musi być ścieżką rozpoczynającą się od /')
    try:
        count, errors=export(Dropbox(token,args.namespace_id),root,args.output,
            recursive=args.recursive,create=args.create_missing,visibility=args.visibility,include_root=args.include_root)
    except FileExistsError:
        print('Plik wynikowy już istnieje. Wybierz inną nazwę --output.',file=sys.stderr)
        return 2
    except (ApiError, OSError, ValueError) as exc:
        print(f'Przerwano: {exc}. Zapisane wcześniej wiersze pozostają w pliku.',file=sys.stderr)
        return 1
    print(f'Zapisano folderów: {count}; błędów: {errors}. Plik: {args.output}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
