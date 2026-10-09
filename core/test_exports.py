"""CSV export of author data: access control and spreadsheet-formula protection."""
import csv
from datetime import date, datetime
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from authors.models import Author
from core.exports import _safe_filename, csv_response, export_reviews_csv, safe_csv_value
from texts.models import Anthology, Review


def parse(response):
    text = response.content.decode('utf-8')
    assert text.startswith('﻿')
    return list(csv.reader(StringIO(text[1:]), delimiter=';'))


class SafeValueTests(TestCase):
    def test_formulas_are_neutralised_without_changing_content(self):
        for value in ('=SUM(A1)', '+48', '-1', '@cmd', ' =1', '\t=1', '＝1'):
            with self.subTest(value=value):
                self.assertEqual(safe_csv_value(value), "'" + value)

    def test_plain_values_and_types(self):
        self.assertEqual(safe_csv_value('Ala'), 'Ala')
        self.assertEqual(safe_csv_value(None), '')
        self.assertEqual(safe_csv_value(True), 'Tak')
        self.assertEqual(safe_csv_value(False), 'Nie')
        self.assertEqual(safe_csv_value(date(2026, 1, 2)), '2026-01-02')
        aware = timezone.make_aware(datetime(2026, 1, 2, 12, 0, 0))
        self.assertEqual(safe_csv_value(aware), '2026-01-02 12:00:00+01:00')

    def test_control_character_prefix_is_neutralised(self):
        self.assertEqual(safe_csv_value('​=1'), "'​=1")

    def test_filename_cannot_escape_or_inject(self):
        self.assertEqual(_safe_filename('../../a\r\n.csv'), 'a.csv')
        self.assertEqual(_safe_filename(''), 'eksport.csv')
        self.assertEqual(_safe_filename('...'), 'eksport.csv')


class ExportAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin', 'admin@example.test', 'x')
        cls.staff = User.objects.create_user('staff', 'staff@example.test', 'x', is_staff=True)
        book = Anthology.objects.create(title='=Antologia')
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.test')
        cls.review = Review.objects.create(title='Tytuł', anthology=book, author=author,
                                           author_first_name='Jan', author_last_name='Autor', length=100)

    def test_only_superuser_can_export(self):
        with self.assertRaises(PermissionDenied):
            csv_response(user=self.staff, headers=('a',), rows=[])
        with self.assertRaises(PermissionDenied):
            export_reviews_csv(self.staff, Review.objects.all())

    def test_review_export_rows_headers_and_protection(self):
        response = export_reviews_csv(self.admin, Review.objects.all(), filename='zgłoszenia.csv')
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertIn('attachment', response['Content-Disposition'])
        rows = parse(response)
        self.assertEqual(rows[0], ['Nabór', 'Autor', 'Tytuł', 'Status'])
        self.assertEqual(rows[1][0], "'=Antologia")
        self.assertEqual(rows[1][2], 'Tytuł')
        self.assertEqual(len(rows), 2)

    def test_export_rejects_other_models(self):
        with self.assertRaises(TypeError):
            export_reviews_csv(self.admin, Author.objects.all())
