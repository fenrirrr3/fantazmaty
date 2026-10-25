from core.audiobook_validators import validate_allowed_url


def validate_drive_url(value):
    validate_allowed_url(value, ('drive.google.com', 'docs.google.com'),
                         'Wklej link HTTPS z drive.google.com lub docs.google.com.')
