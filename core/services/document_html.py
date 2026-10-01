"""Preserve paragraph boundaries hidden by Mammoth's compact list markup."""


def list_paragraphs(root):
    from lxml import html

    blocks = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ol', 'ul', 'table', 'blockquote'}
    for item in root.xpath('.//li'):
        paragraph = html.Element('p')
        paragraph.text, item.text = item.text, None
        for child in list(item):
            if child.tag in blocks:
                if paragraph.text or len(paragraph):
                    item.insert(item.index(child), paragraph)
                paragraph = html.Element('p')
                paragraph.text, child.tail = child.tail, None
            else:
                paragraph.append(child)
        if paragraph.text or len(paragraph):
            item.append(paragraph)
