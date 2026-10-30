"""The shared DOCX preparation order; rendering only consumes the result.

The caller validates resource links and runs this inside the bounded worker.
No HTTP, mailbox or database dependencies belong here.
"""
from io import BytesIO

if __package__:
    from .document_progress import report
    from .document_rebuild import rebuild_docx
    from .document_formatting import normalize_docx
    from .document_whitespace import normalize_spacing_docx
    from .odkurzacz import clean_docx, ALL_EDITORIAL_RULES
else:
    from document_progress import report
    from document_rebuild import rebuild_docx
    from document_formatting import normalize_docx
    from document_whitespace import normalize_spacing_docx
    from odkurzacz import clean_docx, ALL_EDITORIAL_RULES


REPLACEMENT_KINDS = ('word', 'pattern')


def _valid_replacements(replacements):
    """Lista zamian słownikowych z konfiguracji zadania: tylko oczekiwane klucze i typy."""
    if replacements is None:
        return None
    if not isinstance(replacements, (list, tuple)) or len(replacements) > 5000:
        raise ValueError('Invalid replacements')
    result = []
    for item in replacements:
        if (not isinstance(item, dict) or set(item) - {'checked'} != {'kind', 'source', 'target'}
                or item['kind'] not in REPLACEMENT_KINDS
                or not all(isinstance(item[key], str) for key in ('source', 'target')) or not item['source']
                or not isinstance(item.get('checked', False), bool)):
            raise ValueError('Invalid replacements')
        result.append({'kind': item['kind'], 'source': item['source'], 'target': item['target'],
                       **({'checked': True} if item.get('checked') else {})})
    return result


def prepare_docx(source, *, rebuild=False, normalize_formatting=False,
                 cleaner_rules=(), allow_omissions=False, use_cleaner=False, justify=False, remove_soft_whitespace=False,
                 track_changes=False, replacements=None, summary=None, keep_page_format=False):
    """Kolejne kroki przygotowania DOCX.

    track_changes, replacements i summary trafiają do Odkurzacza (clean_docx),
    keep_page_format do przebudowy.
    """
    if not isinstance(cleaner_rules, (list, tuple)) or set(cleaner_rules) - set(ALL_EDITORIAL_RULES):
        raise ValueError('Invalid cleaner rules')
    replacements = _valid_replacements(replacements)
    if summary is not None and not isinstance(summary, dict):
        raise ValueError('Invalid summary')
    source.seek(0)
    payload = source.read()
    steps = []
    # Miękkie entery zamieniamy najpierw: przed przebudową (która pomija punktory list – punkt listy z miękkim
    # enterem musi zostać rozpoznany jako punkt i nie zostać podzielony) i przed Odkurzaczem (jego reguły
    # widzą już osobne akapity).
    if remove_soft_whitespace:
        steps.append((normalize_spacing_docx, {}))
    if rebuild:
        steps.append((rebuild_docx, {'allow_omissions': allow_omissions, 'keep_page_format': bool(keep_page_format)}))
    if normalize_formatting:
        steps.append((normalize_docx, {'justify': justify}))
    if cleaner_rules or use_cleaner:
        steps.append((clean_docx, {'rules': cleaner_rules, 'track_changes': bool(track_changes),
                                   'replacements': replacements, 'summary': summary}))
    for operation, options in steps:
        report({'rebuild_docx': 'Przebudowa DOCX', 'normalize_docx': 'Ujednolicanie formatowania',
                'clean_docx': 'Odkurzanie tekstu', 'normalize_spacing_docx': 'Zamiana miękkich enterów i spacji'}.get(getattr(operation, '__name__', ''), 'Przygotowanie DOCX'))
        with operation(BytesIO(payload), **options) as result:
            payload = result.read()
    return BytesIO(payload)
