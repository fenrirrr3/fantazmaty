"""Pomocniki testów obszaru ludzi: dane formularza panelu admina tak, jak wysłałaby je przeglądarka."""
from lxml import html


def admin_form_data(client, url, form_id, **changes):
    """Wartości wszystkich pól formularza admina (z tokenem wersji i formsetami) z podanymi zmianami."""
    form = html.fromstring(client.get(url).content).xpath(f'//form[@id="{form_id}"]')[0]
    data = {}
    for field in form.xpath('.//input[@name] | .//select[@name] | .//textarea[@name]'):
        if field.get('type') in ('checkbox', 'radio') and field.get('checked') is None:
            continue
        if field.tag == 'select':
            selected = [option.get('value', '') for option in field.xpath('./option[@selected]')]
            data[field.get('name')] = selected if field.get('multiple') is not None else (selected or [''])[0]
        else:
            data[field.get('name')] = field.get('value', '') if field.tag == 'input' else field.text or ''
    data.update(changes)
    return {name: value for name, value in data.items() if value is not None}
