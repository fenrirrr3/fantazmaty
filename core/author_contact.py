"""Contact information comes only from the author profile."""

def stored_author_phone(author):
    return (author.phone_number or '').strip()
