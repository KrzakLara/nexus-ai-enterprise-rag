import pytest
from backend.app.parsers import extract_contract_fields, extract_pages, split_page
from backend.app.retrieval import tokenize


def test_contract_extraction_with_page_evidence():
    pages = ['PREAMBLE', 'Counterparty: Example GmbH\nEnd date: 2027-03-31\nAnnual value: EUR 500']
    fields = extract_contract_fields(pages)
    assert fields['counterparty']['value'] == 'Example GmbH'
    assert fields['expiration_date']['page'] == 2
    assert fields['annual_value']['method'] == 'regex'
    assert fields['annual_value']['status'] == 'needs_review'


def test_chunk_split_preserves_evidence_with_overlap():
    text = ('The agreement lasts 24 months. ' * 90).strip()
    chunks = split_page(text, max_chars=280, overlap=40)
    assert len(chunks) > 2
    assert all(0 < len(chunk) <= 280 for chunk in chunks)
    assert 'agreement' in chunks[0]


def test_plain_text_parser():
    assert extract_pages('hello.txt', b'First policy paragraph.')[0] == 'First policy paragraph.'


def test_blank_doc_and_unsupported_format_are_rejected():
    with pytest.raises(ValueError):
        extract_pages('blank.txt', b'   ')
    with pytest.raises(ValueError):
        extract_pages('image.png', b'not a document')


def test_unicode_tokenizer():
    assert tokenize('Ugovor istječe 2027. godine') == ['ugovor', 'istječe', '2027', 'godine']
