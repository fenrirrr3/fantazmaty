"""PDF wytycznych ze stron zewnętrznych – te same bloki co na stronie (core.public_guidelines).

A4, tytuł, nagłówki, akapity z łamaniem wierszy, listy z punktorami lub numerami, pogrubienia, kursywa,
klikalne linki i numery stron. Czcionka z polskimi znakami jak w PDF-ach programów (Nimbus Roman).
"""
from pathlib import Path

from core.services.document_pdf import PDF_CHARACTERS, add_document_font

FONT = 'Document'
MARGIN = 25  # mm, lewy i prawy
TOP, BOTTOM = 22, 22
BODY = 11.5  # pt
LEADING = 1.45
TEXT, LINK, MUTED, RULE = (32, 43, 59), (23, 97, 176), (88, 103, 122), (186, 199, 214)
# Nagłówek: (rozmiar pt, odstęp przed, odstęp po) w mm.
HEADINGS = {1: (15, 6, 2.5), 2: (12.5, 4.5, 1.5)}
PARAGRAPH_GAP, ITEM_GAP = 3, 1.2
MARKER_INDENT = 2.5


def line_height(size):
    return size * LEADING * 25.4 / 72


def render_guidelines_pdf(blocks, title):
    """Bloki wytycznych → bajty PDF."""
    from fpdf import FPDF

    texts = [title, *(run.text for block in blocks
                      for line in (*block.lines, *(line for item in block.items for line in item))
                      for run in line)]
    alias = next(f'{{nb{number}}}' for number in range(len(texts) + 1)
                 if not any(f'{{nb{number}}}' in text for text in texts))

    class GuidelinesPDF(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_x(MARGIN)
            self.set_font(FONT, '', 9)
            self.set_text_color(*MUTED)
            self.cell(self.w - 2 * MARGIN, 5, f'{title} · strona {self.page_no()} z {alias}', align='C')

    pdf = GuidelinesPDF(format='A4')
    add_document_font(pdf, Path(__file__).with_name('fonts'), FONT)
    pdf.alias_nb_pages(alias)
    pdf.set_title(title)
    pdf.set_author('Fantazmaty')
    pdf.set_creator('Fantazmaty CMS')
    pdf.set_lang('pl')
    pdf.set_margins(MARGIN, TOP, MARGIN)
    pdf.set_auto_page_break(True, BOTTOM)
    pdf.add_page()

    def write(lines, size, bold=False):
        height = line_height(size)
        for index, line in enumerate(lines):
            if index:
                pdf.ln(height)
            previous = ''
            for run in line:
                text = run.text.translate(PDF_CHARACTERS)
                style = ('B' if bold or run.bold else '') + ('I' if run.italic else '') + ('U' if run.href else '')
                pdf.set_font(FONT, style, size)
                pdf.set_text_color(*(LINK if run.href else TEXT))
                # fpdf2 łamie każdy fragment osobno: pierwsze słowo fragmentu (np. linku), które nie mieści się
                # w reszcie wiersza, przeniósłby po literze – przenosimy je w całości.
                first = text.split(' ', 1)[0]
                if (first and previous[-1:].isspace() and pdf.x > pdf.l_margin + 0.01
                        and pdf.get_string_width(first) > pdf.w - pdf.r_margin - pdf.x):
                    pdf.ln(height)
                pdf.write(height, text, link=run.href)
                previous = text
        pdf.ln(height)

    pdf.set_font(FONT, 'B', 20)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, line_height(20) * 0.85, title.translate(PDF_CHARACTERS))
    pdf.ln(5)
    body = line_height(BODY)
    for block in blocks:
        if block.kind == 'heading':
            size, before, after = HEADINGS[block.level]
            # Nagłówek nie zostaje sam na dole strony: razem z nim mieszczą się dwa wiersze tekstu.
            if pdf.will_page_break(before + line_height(size) + 2 * body):
                pdf.add_page()
            elif pdf.get_y() > pdf.t_margin + 1:
                pdf.ln(before)
            write(block.lines, size, bold=True)
            pdf.ln(after)
        elif block.kind == 'paragraph':
            write(block.lines, BODY)
            pdf.ln(PARAGRAPH_GAP)
        elif block.kind == 'list':
            pdf.set_font(FONT, '', BODY)
            markers = [f'{block.start + index}.' if block.ordered else '•' for index in range(len(block.items))]
            text_x = MARGIN + MARKER_INDENT + max(pdf.get_string_width(marker) for marker in markers) + 2.5
            for marker, item in zip(markers, block.items):
                if pdf.will_page_break(body):
                    pdf.add_page()
                pdf.set_left_margin(text_x)
                pdf.set_x(MARGIN + MARKER_INDENT)
                pdf.set_font(FONT, '', BODY)
                pdf.set_text_color(*TEXT)
                pdf.cell(text_x - MARGIN - MARKER_INDENT, body, marker)
                write(item, BODY)
                pdf.set_left_margin(MARGIN)
                pdf.ln(ITEM_GAP)
            pdf.ln(PARAGRAPH_GAP - ITEM_GAP)
        else:
            y = pdf.get_y() + 1
            pdf.set_draw_color(*RULE)
            pdf.line(MARGIN, y, pdf.w - MARGIN, y)
            pdf.ln(PARAGRAPH_GAP + 2)
    return bytes(pdf.output())
