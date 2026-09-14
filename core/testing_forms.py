"""Test helper: submit the version obtained from a real rendered form."""
import re
from html import unescape
from django.urls import resolve, reverse


def post_form(client, url, data=None, **kwargs):
    data = dict(data or {})
    if '_edit_version' not in data:
        match = resolve(url.split('?')[0])
        form_url = url
        if match.namespace == 'core' and 'text_id' in match.kwargs:
            form_url = reverse('core:assigned_text_detail', args=[match.kwargs['text_id']])
        elif match.namespace == 'core' and 'review_id' in match.kwargs:
            form_url = reverse('core:assigned_review_detail', args=[match.kwargs['review_id']])
        response = client.get(form_url)
        found = re.search(r'name="_edit_version" value="([^"]+)"', response.content.decode())
        if found:
            data['_edit_version'] = unescape(found.group(1))
    return client.post(url, data, **kwargs)
