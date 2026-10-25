"""Order and subject phrases for recruitment mail; independent of team permissions."""

ROLE_CHOICES = (
    ('editors', 'Redakcja'),
    ('proofreaders', 'Korekta'),
    ('verifiers', 'Weryfikacja'),
    ('reviewers', 'Recenzje'),
    ('narrators', 'Lektor'),
    ('audio_proofreaders', 'Korekta audiobooków'),
    ('illustrators', 'Ilustracje'),
    ('typesetters', 'Skład i e-booki'),
    ('post_layout', 'Korekta poskładowa'),
    ('promotion', 'Promocja i social media'),
    ('other', 'Inny pomysł'),
)

# Role do ręcznego wyboru w adminie (dawne działy), których formularz nie wysyła – nie trafiają
# do ROLE_CHOICES, więc nie są rozpoznawane w temacie ani treści maila.
MANUAL_ROLE_CHOICES = (
    ('sound', 'Dźwiękowcy'),
    ('designers', 'Graficy'),
)
