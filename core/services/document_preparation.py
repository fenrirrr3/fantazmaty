"""The shared DOCX preparation order; rendering only consumes the result.

The caller validates resource links and runs this inside the bounded worker.
No HTTP, mailbox or database dependencies belong here.
"""
from io import BytesIO

if __package__:
    from .document_rebuild import rebuild_docx
    from .document_formatting import normalize_docx
    from .odkurzacz import clean_docx, ALL_EDITORIAL_RULES, DEFAULT_EDITORIAL_RULES
else:
    from document_rebuild import rebuild_docx
    from document_formatting import normalize_docx
    from odkurzacz import clean_docx, ALL_EDITORIAL_RULES, DEFAULT_EDITORIAL_RULES


def prepare_docx(source, *, rebuild=False, normalize_formatting=False,
                 cleaner_rules=(), allow_omissions=False, use_cleaner=False, justify=False, remove_soft_whitespace=False):
    if not isinstance(cleaner_rules, (list, tuple)) or set(cleaner_rules) - set(ALL_EDITORIAL_RULES):
        raise ValueError('Invalid cleaner rules')
    source.seek(0)
    payload = source.read()
    steps = []
    if rebuild:
        steps.append((rebuild_docx, {'allow_omissions': allow_omissions}))
    if normalize_formatting:
        steps.append((normalize_docx, {'justify': justify}))
    if cleaner_rules or use_cleaner:
        steps.append((clean_docx, {'rules': cleaner_rules}))
    if remove_soft_whitespace:
        if __package__:
            from .document_whitespace import normalize_spacing_docx
        else:
            from document_whitespace import normalize_spacing_docx
        steps.append((normalize_spacing_docx, {}))
    for operation, options in steps:
        with operation(BytesIO(payload), **options) as result:
            payload = result.read()
    return BytesIO(payload)
