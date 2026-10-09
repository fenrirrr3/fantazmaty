"""Test helper: run document programs end to end without a background process.

The view always creates a background job. Tests patch ``launch`` so the job
runs in-process (with the real converter), then follow the status page to the
download, which is what a browser without JavaScript does.
"""
from unittest.mock import patch

from django.urls import reverse

from core.services import program_jobs as jobs


def run_program(client, data, **extra):
    """POST a program form; return the download response or the page that explains why not."""
    with patch.object(jobs, 'launch', side_effect=jobs.execute):
        response = client.post(reverse('core:programs'), data, **extra)
    if response.status_code != 302:
        return response
    page = client.get(response['Location'])
    if page.status_code == 200 and page.context['state'].get('state') == 'done':
        return client.get(page.context['download_url'])
    return page
