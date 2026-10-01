"""Updated editorial rules from the desktop cleaner, with web limits."""
import re
from io import BytesIO
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# Reguły są wykonywane w ustalonej kolejności, niezależnie od kolejności kliknięć.
EDITORIAL_RULES = (
    ('spaces', 'Podwójne i wielokrotne spacje'),
    ('trim', 'Spacje na początku i końcu akapitu'),
    ('tabs', 'Tabulatory i mieszanki tabulatorów ze spacjami → jedna spacja'),
    ('empty_paragraphs', 'Wielokrotne puste akapity → jeden pusty akapit'),
    ('before_punct', 'Spacje przed znakami interpunkcyjnymi'),
    ('after_punct', 'Brakujące spacje po interpunkcji (z wyjątkami)'),
    ('inside_brackets', 'Spacje wewnątrz nawiasów'),
    ('inside_quotes', 'Spacje wewnątrz cudzysłowów'),
    ('outside_brackets', 'Brakujące spacje wokół nawiasów'),
    ('quotes', 'Proste i angielskie cudzysłowy → polskie „…”'),
    ('ellipsis', 'Wielokropek → …'),
    ('duplicate_punct', 'Powtórzone przecinki, średniki, dwukropki i podwójne kropki'),
    ('quote_punct', 'Przecinek / kropka poza cudzysłowem w prostych cytatach'),
    ('hyphen_dash', 'Łącznik użyty jako myślnik → półpauza'),
    ('dash_style', 'Pauza jako myślnik → półpauza –'),
    ('dash_spaces', 'Odstępy przy myślnikach i początku kwestii dialogowej'),
    ('range_dash', 'Łącznik w zakresach liczbowych → półpauza'),
    ('range_spaces', 'Usuwanie spacji wewnątrz zakresów liczbowych'),
    ('single_nbsp', 'Spacja nierozdzielająca po a, i, o, u, w, z'),
    ('unit_nbsp', 'Spacja nierozdzielająca między liczbą a jednostką'),
    ('unit_space', 'Brakująca spacja między liczbą a jednostką'),
    ('reference_nbsp', 'Spacja nierozdzielająca: 2025 r., s. 15, nr 3'),
    ('reference_space', 'Brakująca spacja: 2025r., s.15, nr3'),
    ('initials', 'Odstępy przy inicjałach i nazwisku'),
    ('abbreviations', 'Usuń spacje wewnątrz skrótów: m. in. → m.in., t. j. → tj., t. zw. → tzw.'),
    ('dates_times', 'Daty DD.MM.RRRR i godziny HH:MM (jednoznaczne zapisy)'),
    ('decimal', 'Przecinek dziesiętny przy jednostkach i kwotach'),
    ('digit_groups', 'Grupowanie dużych liczb spacją nierozdzielającą'),
    ('temperature', 'Temperatura: 20 °C / 20 °F / 20 K'),
    ('pronouns_lower', 'Zaimki osobowe małą literą – poza początkiem zdania i akapitu'),
    ('user_word_corrections', 'Własne zamiany słownikowe: 23 pozycje z fleksją (pikap / przekonujący / oddziałujący)'),
)
EDITORIAL_RULES += (('sentence_case', 'Wielka litera po . ? ! – z wyjątkami dla skrótów, liczb i dialogów'),)
ALL_EDITORIAL_RULES = frozenset(key for key, _ in EDITORIAL_RULES)
DEFAULT_EDITORIAL_RULES = ALL_EDITORIAL_RULES - {'pronouns_lower', 'user_word_corrections', 'empty_paragraphs'}
NBSP = '\u00a0'
H = r'[ \u00a0\u202f]'
LETTERS = 'A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż'
LOWER = 'a-ząćęłńóśźż'
UPPER = 'A-ZĄĆĘŁŃÓŚŹŻ'
UNITS = (r'(?:km/h|m/s|kg|mg|µg|μg|km|dm|cm|mm|µm|nm|ml|mL|'
         r'kHz|MHz|GHz|Hz|kPa|MPa|Pa|kN|kJ|kW|MW|kV|mA|kΩ|'
         r'kB|MB|GB|TB|ms|min|ha|bar|PLN|zł|EUR|USD|'
         r'[kmc]m[²³]|m[²³]|[gtmlLshNJWTAVΩBK])')
QUANTITIES = UNITS + r'|osób|szt\.?|egzemplarzy|mieszkańców|punktów'
NUMBER = r'(?<![\w.,])[-−]?\d+(?:[.,]\d+)?'

PERSONAL_PRONOUNS = frozenset('''
ja mnie mi mną ty ciebie cię tobie ci tobą
on jego go niego jemu mu niemu nim
ona jej niej ją nią ono je nie
my nas nam nami wy was wam wami
oni one ich nich im nimi
'''.split())
PRONOUN_RE = re.compile(r'\b(?:' + '|'.join(
    sorted({form.capitalize() for form in PERSONAL_PRONOUNS}
           | {form.upper() for form in PERSONAL_PRONOUNS}, key=lambda value: (-len(value), value))
) + r')\b')


def _lower_personal_pronouns(text):
    """Reguła pisowni form z listy, bez rozstrzygania ich funkcji gramatycznej.

    Chroni początki zdań, akapitów i wypowiedzi po dwukropku. Przy skrótach
    mogących kończyć zdanie zachowuje wielką literę. Po granicy nietekstowego
    XML również zachowujemy pierwszy zaimek, bo brakuje pełnego kontekstu.
    """
    protected_spans = [(match.start(), match.end()) for match in TECHNICAL_RE.finditer(text)]
    span_index = 0

    def replace(match):
        nonlocal span_index
        while span_index < len(protected_spans) and protected_spans[span_index][1] <= match.start():
            span_index += 1
        if span_index < len(protected_spans) and protected_spans[span_index][0] < match.end():
            return match[0]
        prefix = text[:match.start()].rsplit('\n', 1)[-1]
        # Pomijamy znaki okalające pierwsze słowo, np. „On” lub – On.
        significant = prefix.rstrip(' \t\r\u00a0\u202f„“”"\'«»()[]{}–—-')
        if not significant or significant[-1] in '!?…:':
            return match[0]
        if significant.endswith('.'):
            # Te skróty zazwyczaj wprowadzają dalszy ciąg wypowiedzi.
            continuation = re.search(
                r'\b(?:np\.|m\.in\.|tj\.|tzw\.|por\.|zob\.)$', significant, re.IGNORECASE)
            if continuation is None:
                return match[0]
        return match[0].lower()

    return PRONOUN_RE.sub(replace, text)

# Chronimy adresy, ścieżki oraz identyfikatory przed wszystkimi regułami tekstowymi.
TECHNICAL_RE = re.compile(
    r'https?://[^\s<>„”"]+|www\.[^\s<>„”"]+|'
    r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|'
    r'\b[A-Za-z]:\\[^\r\n<>"|]+|'
    r'\b(?:[a-z0-9-]+\.)+(?:pl|com|org|net|edu|gov|eu|io|co|de|uk)'
    r'(?:[/?#][^\s<>„”"]*)?|'
    r'\b(?:\d{1,3}\.){3}\d{1,3}\b|'
    r'\b[vV]?\d+(?:\.\d+){2,}\b|'
    r'\b(?:ISBN|ISSN|PESEL|NIP|REGON|IBAN|tel\.|telefon|konto|kod)'
    r'[ \t:]*[+\d][\d \u00a0./-]*\d', re.IGNORECASE)
DATE_RE = re.compile(
    r'(?<![\w.])(?:(?P<iso_y>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_d>\d{1,2})|'
    r'(?P<d>\d{1,2})(?P<sep>[./-])(?P<m>\d{1,2})(?P=sep)(?P<y>\d{4}))(?!\w|\.\d)')
TIME_RE = re.compile(r'(?<![\w:])\d{1,2}:\d{2}(?::\d{2})?(?![\w:])')



def _normalize_dates_times(text):
    from datetime import date

    def date_repl(match):
        year = int(match['iso_y'] or match['y'])
        month = int(match['iso_m'] or match['m'])
        day = int(match['iso_d'] or match['d'])
        try:
            date(year, month, day)
        except ValueError:
            return match[0]
        return f'{day:02d}.{month:02d}.{year:04d}'

    text = DATE_RE.sub(date_repl, text)
    # Kropka w godzinie jest rozpoznawana tylko po „godz.” lub „o”.
    text = re.sub(r'\b(godz\.?|o)(' + H + r'+)(\d{1,2})\.(\d{2})(?!\d)',
                  lambda m: f'{m[1]}{m[2]}{int(m[3]):02d}:{m[4]}'
                  if int(m[3]) < 24 and int(m[4]) < 60 else m[0], text)

    def time_repl(match):
        parts = match[0].split(':')
        if int(parts[0]) < 24 and all(int(part) < 60 for part in parts[1:]):
            return ':'.join(f'{int(part):02d}' for part in parts)
        return match[0]

    return TIME_RE.sub(time_repl, text)


# Własna lista użytkownika. To jawne zamiany redakcyjne, a nie ocena
# poprawności przez słownik Worda. Niektóre formy mogą być poprawne
# w innym znaczeniu; wyłączenie opcji wyłącza całą poniższą listę.
# Warianty docelowe: pikap, przekonujący, oddziałujący.
# Fleksja korzysta z jawnych końcówek, ponieważ model językowy nie musi
# rozpoznawać lematów wyrazów zapisanych błędnie.
USER_WORD_EXACT = {
    'niechcąco': 'niechcący',
    'zaczym': 'za czym',
    'władnie': 'władny',
    'niewiadomo': 'nie wiadomo',
    'terefere': 'tere-fere',
    'inąd': 'skądinąd',
    'wszechczasów': 'wszech czasów',
    'zapewnie': 'zapewne',
}


SENTENCE_ABBREVIATIONS = frozenset("""
np. m.in. tj. tzn. tzw. itd. itp. ew. ewent. prawdop. przyp. dosł. przen.
pot. właśc. zob. por. patrz. cyt. ww. jw. j.w. cd. cdn. c.d. c.d.n.
p. prof. hab. doc. inż. lic. lek. med. wet. mec. adw. red. dyr. kier.
zast. sekr. prez. min. amb. sen. jun. ks. o. s. św. bł. śp. ś.p.
kard. abp. bp. oo. ss. gen. bryg. dyw. kpt. por. ppor. chor. sierż.
plut. kpr. st. mł. szt. asp. kom. podkom. nadkom. insp. podinsp. nadinsp.
ul. al. pl. os. bud. lok. m. woj. pow. gm. r. w. godz. tyg. mies. kw.
ub. br. bm. n.e. p.n.e. ok. tys. szt. egz. poz. proc. maks. str. t.
cz. rozdz. z. wyd. wydawn. oprac. przeł. tłum. il. rys. ryc. tab. fot.
bibliogr. art. ust. lit. par. pol. ang. niem. fr. ros. łac. gr. wł.
hiszp. czes. ukr. hist. daw. arch. żart. iron. pej. wulg. książk. poet.
sp. sp.j. sp.k. sp.p. o.o. etc. ibid. sic. vs. dr. mgr. nr. mjr. płk. ppłk.
""".split()) | frozenset(('z o.o.', 'sp. z o.o.', 'et al.', 'op. cit.'))


def _abbreviation_pattern(value):
    # Optional spaces belong only between abbreviation parts, never after its
    # final full stop. Otherwise masking also consumes the sentence's spacing.
    parts = value.split('.')
    pattern = ''
    for index, part in enumerate(parts):
        pattern += re.escape(part).replace(r'\ ', H + '+')
        if index < len(parts) - 1:
            pattern += r'\.'
            if index < len(parts) - 2:
                pattern += H + '*'
    return pattern


CAPITALIZATION_ABBREVIATION_RE = re.compile(
    r'(?<!\w)(?:' + '|'.join(
        _abbreviation_pattern(value)
        for value in sorted(SENTENCE_ABBREVIATIONS, key=lambda x: (-len(x), x))
    ) + r')|\b(?:[' + UPPER + r']\.){2,}', re.IGNORECASE)
ABBREVIATION_RE = CAPITALIZATION_ABBREVIATION_RE
FILE_NAME_RE = re.compile(r'(?<![\w.])[\w-]+(?:\.[\w-]+)*\.(?:docx?|pdf|epub|mobi|txt|rtf|odt|xlsx?|csv|zip|rar|jpg|jpeg|png|gif|py|html?|js|css)(?!\w)', re.IGNORECASE)


def _capitalize_sentences(text):
    protected = set()
    for pattern in (TECHNICAL_RE, DATE_RE, TIME_RE, FILE_NAME_RE, CAPITALIZATION_ABBREVIATION_RE):
        for match in pattern.finditer(text):
            if pattern is CAPITALIZATION_ABBREVIATION_RE and match.start() in protected:
                continue
            # URLs can include trailing punctuation. Protect only their body.
            end = match.end()
            if pattern is TECHNICAL_RE:
                end = match.start() + len(match[0].rstrip('.,;:!?)]}'))
            protected.update(range(match.start(), end))

    def replace(match):
        marks, gap, letter = match.groups()
        start = match.start()
        if any(pos in protected for pos in range(start, start + len(marks))):
            return match[0]
        if '.' in marks:
            if len(marks) != 1 or (start and text[start - 1] in '.…'):
                return match[0]  # ellipsis, not a sentence boundary
            if start and text[start - 1].isdigit():
                return match[0]  # ordinal, time, version, date
            if re.search(r'(?<!\w)[' + UPPER + r']$', text[:start]):
                return match[0]  # an initial
        return marks + gap + letter.upper()

    # Deliberately do not cross a dash, quotation mark or parenthesis.
    # In particular '. – powiedział' / '? – zapytała' stay untouched.
    return re.sub(r'([.!?]+)(' + H + r'*)([' + LOWER + r'])', replace, text)



def _replacement_case(source, replacement):
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


# Każda para zawiera pełny wzorzec wyrazu i jego docelową postać.
# Granice wyrazu zapobiegają zmianom wewnątrz innych słów.
USER_WORD_PATTERNS = tuple((re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in (
        (r'\bpickup(?P<ending>owi|ów|om|ami|ach|em|ie|a|y|u)?\b', r'pikap\g<ending>'),
        (r'\btonicem\b', r'tonikiem'),
        (r'\btonicy\b', r'toniki'),
        (r'\btonic(?P<ending>owi|ów|om|ami|ach|iem|a|u|i)?\b', r'tonik\g<ending>'),
        (r'\bmentlik(?P<ending>owi|ów|om|ami|ach|iem|a|u|i)?\b', r'mętlik\g<ending>'),
        (r'\b(?P<neg>nie)?przekonywując(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b',
         r'\g<neg>przekonując\g<ending>'),
        (r'\b(?P<neg>nie)?oddziaływując(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b',
         r'\g<neg>oddziałując\g<ending>'),
        (r'\b(?P<neg>nie)?zadawalając(?P<ending>ego|emu|ymi|ych|ej|ym|y|a|e|ą|o)?\b',
         r'\g<neg>zadowalając\g<ending>'),
        # Czasownik: miałczeć, miałczę, miałczysz, miałczała, miałczeli…
        # Obejmuje też imiesłowy i rzeczownik odczasownikowy.
        (r'\bmiałcz(?P<ending>eć|ę|ysz|y|ymy|ycie|ą|'
         r'ałem|ałam|ałeś|ałaś|ał|ała|ało|ałyśmy|ałyście|ały|'
         r'eliśmy|eliście|eli|ałbym|ałbyś|ałby|ałabym|ałabyś|ałaby|ałoby|'
         r'elibyśmy|elibyście|eliby|ałybyśmy|ałybyście|ałyby|'
         r'my|cie|ąc|ący|ąca|ące|ącą|ącego|ącej|ącemu|ącym|ącymi|ących|'
         r'enie|enia|eniu|eniem|eń|eniom|eniami|eniach)?\b', r'miaucz\g<ending>'),
        # Zmieniamy wadliwy bezokolicznik oraz formy z wadliwym „-leli”.
        # Poprawne „wymyślał”, „wymyślę”, „wymyślenie” pozostają bez zmian.
        (r'\bwymyśle(?P<ending>ć|liśmy|liście|li|libyśmy|libyście|liby)\b',
         r'wymyśli\g<ending>'),
        (r'\bdomyśle(?P<ending>ć|liśmy|liście|li|libyśmy|libyście|liby)\b',
         r'domyśli\g<ending>'),
        (r'\bdopatrzeć\b', r'dopatrzyć'),
        (r'\bdopatrza(?P<ending>łem|łam|łeś|łaś|ł|ła|ło|łyśmy|łyście|ły|'
         r'łbym|łbyś|łby|łabym|łabyś|łaby|łoby|łybyśmy|łybyście|łyby)\b',
         r'dopatrzy\g<ending>'),
        (r'\bdopatrze(?P<ending>liśmy|liście|li|libyśmy|libyście|liby)\b',
         r'dopatrzy\g<ending>'),
        (r'\bkarnister\b', r'kanister'),
        (r'\bkarnistr(?P<ending>owi|ów|om|ami|ach|em|ze|a|y)\b', r'kanistr\g<ending>'),
        (r'\bmassmedi(?P<ending>ami|ach|ów|om|a)\b', r'mass medi\g<ending>'),
        # Nazwy mieszkańców: wielka litera także w formach odmienionych.
        (r'\beuropejczyk(?P<ending>owi|ów|om|ami|ach|iem|a|u)?\b',
         r'Europejczyk\g<ending>'),
        (r'\beuropejczycy\b', r'Europejczycy'),
        (r'\bazjat(?P<ending>ami|ach|ów|om|a|y|ę|ą|o)\b', r'Azjat\g<ending>'),
        (r'\bazjaci(?P<ending>e)?\b', r'Azjaci\g<ending>'),
    ))
USER_WORD_EXACT_RE = re.compile(r'\b(?:' + '|'.join(map(re.escape, USER_WORD_EXACT)) + r')\b', re.IGNORECASE)
COMMEDIA_RE = re.compile(
    r'\b(?P<noun>komedi(?:ami|ach|om|a|i|ę|ą|o|e))'
    r'(?P<space>[ \t\u00a0\u202f]+)(?P<term>dellarte)\b', re.IGNORECASE)


def _correct_user_words(text):
    text = USER_WORD_EXACT_RE.sub(
        lambda match: _replacement_case(match[0], USER_WORD_EXACT[match[0].lower()]), text)
    for pattern, replacement in USER_WORD_PATTERNS:
        def replace(match, template=replacement):
            # Końcówki są normalizowane do małych liter przed odtworzeniem
            # zapisu całego wyrazu, np. PICKUPAMI → PIKAPAMI.
            value = match.expand(template)
            if not template.startswith(('Europejczy', 'Azjat', 'Azjaci')):
                value = value.lower()
            else:
                value = value[:1].upper() + value[1:].lower()
            return _replacement_case(match[0], value)
        text = pattern.sub(replace, text)
    text = COMMEDIA_RE.sub(
        lambda match: match['noun'] + match['space']
        + _replacement_case(match['term'], 'dell’arte'), text)
    return text


def correct_editorial_text(text, enabled=None, trim_start=True, trim_end=True):
    """Korekta fragmentu tekstu; puste enabled wyłącza wszystkie reguły.

    Regexy nie ustalają znaczenia zdania. Niejednoznaczne konstrukcje
    pozostają bez zmian, np. 1.234 bez jednostki, samotne cudzysłowy,
    pojedynczy łącznik wewnątrz wyrazu i godzina 9.30 bez kontekstu.
    """
    enabled = DEFAULT_EDITORIAL_RULES if enabled is None else frozenset(enabled)
    unknown = enabled - ALL_EDITORIAL_RULES
    if unknown:
        raise ValueError(f'Nieznane reguły korekty: {sorted(unknown)}')
    if not enabled or not text:
        return text

    protected = {}

    def mask_value(value):
        # Znaki prywatne nie pasują do \w, cyfr ani liter w naszych regexach.
        index = 0xF0000 + len(protected)
        while chr(index) in text or chr(index) in protected:
            index += 1
        marker = chr(index)
        protected[marker] = value
        return marker

    def protect_technical(match):
        value = match[0]
        core = value.rstrip('.,;:!?)]}')
        return mask_value(core) + value[len(core):] if core else value

    # Daty przed ochroną wersji (np. 01.02.2025 wygląda też jak numer wersji).
    def date_protect(match):
        value = _normalize_dates_times(match[0]) if 'dates_times' in enabled else match[0]
        return mask_value(value)

    # Adresy muszą być chronione zanim rozpoznamy datę wewnątrz URL.
    address_pattern = TECHNICAL_RE.pattern.split(r'|\b(?:\d{1,3}\.)')[0]
    text = re.sub(address_pattern, protect_technical, text, flags=re.IGNORECASE)
    text = DATE_RE.sub(date_protect, text)
    text = TECHNICAL_RE.sub(protect_technical, text)
    if 'dates_times' in enabled:
        text = _normalize_dates_times(text)
    text = TIME_RE.sub(lambda m: mask_value(m[0]), text)
    if 'sentence_case' in enabled:
        text = FILE_NAME_RE.sub(lambda m: mask_value(m[0]), text)
    if 'user_word_corrections' in enabled:
        text = _correct_user_words(text)

    if 'tabs' in enabled:
        text = re.sub(r'[ \t\u00a0\u202f]*\t[ \t\u00a0\u202f]*', ' ', text)
    if 'spaces' in enabled:
        text = re.sub(r' {2,}', ' ', text)
    if 'abbreviations' in enabled:
        for pattern, replacement in (
            (r'\bm\.' + H + r'*in\.', 'm.in.'),
            (r'\bt\.' + H + r'*j\.', 'tj.'),
            (r'\bt\.' + H + r'*zw\.', 'tzw.'),
        ):
            text = re.sub(pattern, replacement, text)
    if 'quotes' in enabled:
        text = re.sub(r'“([^“”\n]+)”', r'„\1”', text)
        text = re.sub(r'„([^„“”\n]+)“', r'„\1”', text)
        text = re.sub(r'(?<!\w)"([^"\n]*[' + LETTERS + r'][^"\n]*)"(?!\w)',
                      r'„\1”', text)
    if 'inside_quotes' in enabled:
        text = re.sub(r'([„“])' + H + r'+', r'\1', text)
        text = re.sub(H + r'+([”])', r'\1', text)
        text = re.sub(r'"([^"\n]+)"', lambda m: '"' + m[1].strip(' \u00a0\u202f') + '"', text)
    if 'ellipsis' in enabled:
        text = re.sub(r'\.{3,}', '…', text)
    if 'duplicate_punct' in enabled:
        text = re.sub(r'([,;:])\1+', r'\1', text)
        text = re.sub(r'(?<!\.)\.{2}(?!\.)', '.', text)
    if 'quote_punct' in enabled:
        text = re.sub(r'„([^„”\n]+),”', r'„\1”,', text)
        # Tylko mały cytowany fragment, bez innych granic zdań wewnątrz.
        text = re.sub(r'„([' + LOWER + r'][^„”\n.!?]*)\.”', r'„\1”.', text)
    if 'inside_brackets' in enabled:
        text = re.sub(r'([(\[{])' + H + r'+', r'\1', text)
        text = re.sub(H + r'+([)\]}])', r'\1', text)
    if 'outside_brackets' in enabled:
        text = re.sub(r'(?<=[' + LETTERS + r'”])(?=[(\[{])', ' ', text)
        text = re.sub(r'(?<=[)\]}])(?=[' + LETTERS + r'])', ' ', text)
    if 'before_punct' in enabled:
        text = re.sub(H + r'+(?=[,.;:?!])', '', text)
    if 'hyphen_dash' in enabled:
        text = re.sub(r'(?<= )-(?= )', '–', text)
        text = re.sub(
            r'(^|\n)' + H + r'*-' + H + r'*(?=[' + LETTERS + r'„“"])',
            lambda match: (match[1] + '– ')
            if trim_start or match[1] == '\n' else match[0], text,
        )
    if 'dash_style' in enabled:
        text = text.replace('—', '–')
    if 'range_dash' in enabled:
        text = re.sub(r'(?<![\w.,-])(\d+)' + H + r'*-'+ H + r'*(\d+)(?![\w-]|[.,]\d)',
                      lambda m: m[0].replace('-', '–'), text)
    if 'range_spaces' in enabled:
        text = re.sub(r'(?<![\w.,-])(\d+)' + H + r'*([–-])' + H + r'*(\d+)(?![\w-]|[.,]\d)',
                      r'\1\2\3', text)
    if 'dash_spaces' in enabled:
        def dash_space(match):
            left = text[:match.start()].rstrip(' \u00a0\u202f')
            right = text[match.end():].lstrip(' \u00a0\u202f')
            if (not left and not trim_start) or (not right and not trim_end):
                return match[0]
            if left[-1:].isdigit() and right[:1].isdigit():
                return match[0]
            if right[:1].isdigit() and (not left or left[-1:] in '(=:'):
                return match[0]  # potencjalny znak minus
            return (' ' if left and not left.endswith('\n') else '') + match[1] + (' ' if right else '')
        text = re.sub(H + r'*([–—])' + H + r'*', dash_space, text)
    if 'decimal' in enabled:
        text = re.sub(r'(?<![\w.,])(\d+)\.(\d{1,2})(?=' + H + r'*(?:' + UNITS + r'|%|°' + H + r'*[CF])(?!\w))',
                      r'\1,\2', text)
    if 'unit_space' in enabled:
        text = re.sub('(' + NUMBER + r')(?=' + UNITS + r'(?!\w))', r'\1 ', text)
    if 'digit_groups' in enabled:
        def group_digits(match):
            digits = re.sub(H, '', match[0])
            if digits.startswith('0'):
                return match[0]
            before = text[:match.start()]
            if re.search(r'\b(?:nr|numer|kod|id|tel\.?)' + H + r'*[:#]?' + H + r'*$', before, re.I):
                return match[0]
            after = text[match.end():]
            quantity = re.match(H + r'*(?:' + QUANTITIES + r')(?!\w)', after)
            if len(digits) >= 9 and not quantity:
                return match[0]  # możliwy numer telefonu lub identyfikator
            return _group_number(digits)
        text = re.sub(r'(?<![\w.,])(\d{1,3}(?:' + H + r'\d{3})+|\d{5,})(?!\w|\.\d)', group_digits, text)
    if 'unit_nbsp' in enabled:
        text = re.sub(r'(?<=\d)' + H + r'+(?=' + UNITS + r'(?!\w))', NBSP, text)
    if 'reference_space' in enabled:
        text = re.sub(r'\b(\d{4})(?=r\.)', r'\1 ', text)
        text = re.sub(r'\b(s\.|nr)(?=\d)', r'\1 ', text)
    if 'reference_nbsp' in enabled:
        text = re.sub(r'\b(\d{4})' + H + r'+(?=r\.)', r'\1' + NBSP, text)
        text = re.sub(r'\b(s\.|nr)' + H + r'+(?=\d)', r'\1' + NBSP, text)
    if 'initials' in enabled:
        text = re.sub(r'\b([' + UPPER + r'])' + H + r'+\.', r'\1.', text)
        text = re.sub(r'\b([' + UPPER + r']\.)' + H + r'*(?=[' + UPPER + r']\.)', r'\1' + NBSP, text)
        text = re.sub(r'\b([' + UPPER + r']\.)' + H + r'*(?=[' + UPPER + r'][' + LOWER + r']{2,}\b)',
                      r'\1' + NBSP, text)
    if 'temperature' in enabled:
        text = re.sub('(' + NUMBER + r')' + H + r'*°' + H + r'*([CF])(?!\w)', r'\1' + NBSP + r'°\2', text)
        text = re.sub('(' + NUMBER + r')' + H + r'*K(?!\w)', r'\1' + NBSP + 'K', text)
    if 'after_punct' in enabled:
        # Zachowaj wewnętrzne kropki skrótów. Liczby nie są celem tej reguły.
        def protect_abbreviation(match):
            tail = text[match.end():match.end() + 1]
            # Granica po skrócie wymaga spacji; jego wewnętrzne kropki są chronione.
            gap = ' ' if tail and re.fullmatch('[' + LETTERS + '„“\"]', tail) else ''
            return mask_value(match[0]) + gap
        text = ABBREVIATION_RE.sub(protect_abbreviation, text)
        text = re.sub(r'(?<=[,;:!?])(?=[' + LETTERS + r'„“"])', ' ', text)
        text = re.sub(r'(?<=\.)(?=[' + LETTERS + r'„“"])', ' ', text)
        # Przecinek przed liczbą wymaga spacji, chyba że tworzy liczbę dziesiętną.
        text = re.sub(r'(?<!\d),(?=\d)', ', ', text)
        text = re.sub(r'(?<=[;!?])(?=\d)', ' ', text)
        text = re.sub(r'(?<!\d):(?=\d)', ': ', text)
    if 'single_nbsp' in enabled:
        text = re.sub(r'(?<!\w)([aAiIoOuUwWzZ])' + H + r'+(?=\S)', r'\1' + NBSP, text)
    # Przywróć najpierw późniejsze maski, gdyby zawierały wcześniejsze.
    for marker, value in reversed(list(protected.items())):
        text = text.replace(marker, value)
    if 'pronouns_lower' in enabled:
        # Po przywróceniu masek dostępne są kropki kończące skróty i zdania.
        text = _lower_personal_pronouns(text)
    if 'sentence_case' in enabled:
        text = _capitalize_sentences(text)
    if 'trim' in enabled:
        if trim_start:
            text = re.sub(r'^' + H + r'+', '', text)
        if trim_end:
            text = re.sub(H + r'+$', '', text)
    return text


def _group_number(digits):
    groups = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    return NBSP.join(reversed(groups))


def _rewrite_text_nodes(slots, corrected):
    """Zmieniaj wyłącznie tekst i tabulatory; nie przebudowuj całego akapitu."""
    from bisect import bisect_right
    from difflib import SequenceMatcher

    original = ''.join(value for _, value in slots)
    if original == corrected:
        return
    ends, offset = [], 0
    for _, value in slots:
        offset += len(value)
        ends.append(offset)
    chunks = [[] for _ in slots]
    for tag, a, b, c, d in SequenceMatcher(None, original, corrected, autojunk=False).get_opcodes():
        if tag == 'equal':
            position = a
            while position < b:
                index = bisect_right(ends, position)
                stop = min(b, ends[index])
                chunks[index].append(original[position:stop])
                position = stop
        elif tag in ('insert', 'replace'):
            index = min(bisect_right(ends, a), len(slots) - 1)
            chunks[index].append(corrected[c:d])
    for (node, before), pieces in zip(slots, chunks):
        after = ''.join(pieces)
        if before == after:
            continue
        if node.tag == qn('w:t'):
            node.text = after
            node.set(qn('xml:space'), 'preserve')
        elif node.tag == qn('w:tab'):
            # Wstawione znaki mogą towarzyszyć zachowanemu tabulatorowi.
            for i, piece in enumerate(after.split('\t')):
                if i:
                    node.addprevious(OxmlElement('w:tab'))
                if piece:
                    replacement = OxmlElement('w:t')
                    replacement.text = piece
                    replacement.set(qn('xml:space'), 'preserve')
                    node.addprevious(replacement)
            node.getparent().remove(node)


def _safe_empty_paragraph(element):
    if element.tag != qn('w:p'):
        return False
    for child in element:
        if child.tag == qn('w:pPr'):
            if any(child.find(qn('w:' + name)) is not None
                   for name in ('sectPr', 'numPr', 'pageBreakBefore')):
                return False
        elif child.tag == qn('w:r'):
            for node in child:
                if node.tag == qn('w:rPr'):
                    continue
                if node.tag == qn('w:t') and not (node.text or '').strip(' \u00a0\u202f\t'):
                    continue
                if node.tag == qn('w:tab'):
                    continue
                return False
        else:
            return False
    return True


def apply_editorial_corrections(doc, enabled=None):
    enabled = DEFAULT_EDITORIAL_RULES if enabled is None else frozenset(enabled)
    if enabled - ALL_EDITORIAL_RULES:
        raise ValueError('Nieznana reguła korekty edytorskiej.')
    if not enabled:
        return
    field_depth = 0
    for para in doc.paragraphs:
        groups, slots = [], []

        def barrier():
            if slots:
                groups.append(list(slots))
                slots.clear()
            groups.append(None)

        # Korekta widzi sąsiednie runy, ale nie ingeruje w pola, hiperłącza,
        # rysunki, przypisy, podziały wierszy ani znaczniki dokumentu.
        for child in para._p:
            if child.tag == qn('w:pPr'):
                continue
            if child.tag != qn('w:r'):
                barrier()
                continue
            for node in child:
                if node.tag == qn('w:rPr'):
                    continue
                if node.tag == qn('w:fldChar'):
                    kind = node.get(qn('w:fldCharType'))
                    if kind == 'begin':
                        field_depth += 1
                    elif kind == 'end':
                        field_depth = max(0, field_depth - 1)
                    barrier()
                elif field_depth:
                    barrier()
                elif node.tag == qn('w:t'):
                    if node.text:
                        slots.append((node, node.text))
                elif node.tag == qn('w:tab'):
                    slots.append((node, '\t'))
                else:
                    barrier()
        if slots:
            groups.append(slots)
        for index, group in enumerate(groups):
            if group:
                original = ''.join(value for _, value in group)
                corrected = correct_editorial_text(original, enabled,
                                                   trim_start=index == 0,
                                                   trim_end=index == len(groups) - 1)
                _rewrite_text_nodes(group, corrected)
    if 'empty_paragraphs' in enabled:
        previous_empty = False
        for element in list(doc._element.body):
            empty = _safe_empty_paragraph(element)
            if empty and previous_empty:
                element.getparent().remove(element)
            else:
                previous_empty = empty



def clean_docx(source, rules):
    """Zwróć dokument w pamięci; nie zapisuj tekstu użytkownika na serwerze."""
    source.seek(0)
    document = Document(source)
    lengths = [len(paragraph.text) for paragraph in document.paragraphs]
    if sum(lengths) > 500_000 or any(length > 20_000 for length in lengths):
        raise ValueError("Dokument przekracza limit 500 000 znaków lub 20 000 znaków w akapicie.")
    apply_editorial_corrections(document, frozenset(rules))
    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output
