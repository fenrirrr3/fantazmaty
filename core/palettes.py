"""Kolory odznak ról i etapów, ustalane po stronie serwera.

Szablony dodają do odznak atrybut data-palette, a themes.css przypisuje mu
parę kolorów. Wcześniej robił to skrypt po załadowaniu strony (mignięcie
niepokolorowanych odznak i druga kopia tej samej wiedzy w JavaScripcie).
"""
import re

from django.utils.html import format_html

ROLE_PALETTES = {
    'editor': 'editing', 'editing_reviewer': 'editing', 'editing': 'editing', 'author_editing': 'editing',
    'editing_coordinator': 'coordinator', 'verification_coordinator': 'coordinator',
    'coordinator_control': 'coordinator', 'coordinator_verification_control': 'coordinator',
    'reviewer': 'reviewer', 'styling': 'styling', 'ready': 'ready', 'ready_for_editing': 'ready-for-editing',
    'proofreader_1': 'proofreading', 'proofreader_2': 'proofreading',
    'proofreader_3': 'proofreading', 'proofreader_4': 'proofreading',
    'verifier_1': 'verification', 'verifier_2': 'verification', 'verifier_3': 'verification',
    'verifier_4': 'verification', 'verifier_5': 'verification', 'verifier_6': 'verification',
    'verifier_7': 'verification',
}

STAGE_PALETTES = {
    'editing': 'editing', 'author_editing': 'editing', 'editing_review': 'editing',
    'editing_control': 'coordinator', 'coordinator_control': 'coordinator', 'editor_control': 'editing',
    'ready_for_editing': 'ready-for-editing', 'ready': 'ready', 'withdrawn': 'withdrawn', 'styling': 'styling',
    'first_verification': 'verification', 'second_verification': 'verification',
    'third_verification': 'verification', 'fourth_verification': 'verification',
    'fifth_verification': 'verification', 'sixth_verification': 'verification',
    'seventh_verification': 'verification',
    'first_proofreading': 'proofreading', 'second_proofreading': 'proofreading',
    'third_proofreading': 'proofreading', 'fourth_proofreading': 'proofreading',
}

# Team role names ("Redaktor", "Koordynator redakcji", ...) in order of precedence.
LABEL_RULES = (
    (re.compile(r'^prawa ręka$'), 'right-hand'),
    (re.compile(r'^ilustrator$'), 'illustrator'),
    (re.compile(r'^grafik$'), 'designer'),
    (re.compile(r'^lektor$'), 'narrator'),
    (re.compile(r'^składacz$'), 'typesetter'),
    (re.compile(r'^dźwiękowiec$'), 'sound-engineer'),
    (re.compile(r'koordynator|^k\. redakcji$|^k\. weryfikacji$'), 'coordinator'),
    (re.compile(r'recenz'), 'reviewer'),
    (re.compile(r'gotow'), 'ready'),
    (re.compile(r'do redakcji'), 'ready-for-editing'),
    (re.compile(r'styl'), 'styling'),
    (re.compile(r'weryfik|kontr\. wer'), 'verification'),
    (re.compile(r'korekt'), 'proofreading'),
    (re.compile(r'redak|redaktor|plik u autora'), 'editing'),
)


def label_palette(label):
    value = str(label or '').strip().lower()
    return next((palette for pattern, palette in LABEL_RULES if pattern.search(value)), '')


def role_palette(code, label=''):
    return ROLE_PALETTES.get(str(code or '')) or label_palette(label)


def stage_palette(code, label=''):
    return STAGE_PALETTES.get(str(code or '')) or (label_palette(label) if label else '')


def palette_attr(palette):
    """' data-palette="…"' or nothing: an empty attribute would hide the badge colours."""
    return format_html(' data-palette="{}"', palette) if palette else ''
