"""Walidacja linków audiobooków: YouTube, HearThis, dodatkowe części nagrania i folder MegaNZ."""
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.audiobook_validators import (validate_audio_links, validate_hearthis_url, validate_mega_url,
                                       validate_youtube_url)


class MediaUrlValidationTests(SimpleTestCase):
    def test_only_http_links_to_the_service_host_without_credentials_or_odd_ports(self):
        for validator, good, bad in [
                (validate_youtube_url,
                 ('https://youtu.be/film', 'http://m.youtube.com/watch?v=x', 'https://www.youtube.com:443/watch?v=x'),
                 ('javascript:alert(1)', 'ftp://youtube.com/x', 'https://youtube.com.evil.test/',
                  'https://user@youtube.com/', 'https://youtube.com:8080/', 'https://hearthis.at/x', 'http://[::1')),
                (validate_hearthis_url,
                 ('https://hearthis.at/fantazmaty/x/', 'http://www.hearthis.at:80/x'),
                 ('https://hearthis.at.evil.test/file', 'https://a:b@hearthis.at/', 'https://youtu.be/film')),
                (validate_mega_url,
                 ('https://mega.nz/folder/x#key', 'https://www.mega.co.nz/', 'https://mega.nz:443/'),
                 ('javascript:alert(1)', 'http://mega.nz/', 'https://mega.nz.evil.test/', 'https://mega.nz@evil.test/',
                  'https://user@mega.nz/', 'https://mega.nz:8443/', 'https://[mega.nz')),
        ]:
            for url in good:
                with self.subTest(validator=validator.__name__, url=url):
                    validator(url)
            for url in bad:
                with self.subTest(validator=validator.__name__, url=url), self.assertRaises(ValidationError):
                    validator(url)


class AdditionalLinksValidationTests(SimpleTestCase):
    def link(self, **values):
        return {'service': 'youtube', 'part': 2, 'url': 'https://youtu.be/part-2', **values}

    def test_valid_parts_of_both_services(self):
        # Walidator Django zwraca None; poprawna lista (także pusta) nie zgłasza ValidationError.
        links = [self.link(), self.link(service='hearthis', url='https://hearthis.at/part-2/'),
                 self.link(part=100, url='https://youtu.be/part-100')]
        for value in ([], links):
            with self.subTest(count=len(value)):
                self.assertIsNone(validate_audio_links(value))

    def test_rejects_wrong_shape_values_and_unsafe_or_repeated_links(self):
        too_many = [self.link(part=1 + n % 100, url=f'https://youtu.be/{n}') for n in range(101)]
        for value in (
                {'service': 'youtube'}, 'https://youtu.be/x', too_many,
                ['https://youtu.be/x'], [{**self.link(), 'label': 'x'}], [{'service': 'youtube', 'part': 2}],
                [self.link(service='vimeo')], [self.link(part=True)], [self.link(part='2')], [self.link(part=0)],
                [self.link(part=101)], [self.link(url=None)], [self.link(url='')],
                [self.link(url='https://youtu.be/' + 'x' * 1000)],
                [self.link(url='javascript:alert(1)')], [self.link(service='hearthis')],
                [self.link(), self.link(part=3)]):
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValidationError):
                validate_audio_links(value)
