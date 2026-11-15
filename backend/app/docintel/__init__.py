"""Phase 3 document intelligence (modular monolith).

    structure.py   normalized document representation (pages, blocks, lines, tables, bounding boxes)
    parsing.py     format detection and layout-aware parsing (PDF, scanned PDF, DOCX, XLSX, TXT, MD)
    ocr.py         Tesseract OCR for pages without a text layer
    tables.py      table grids, merged cells, header mapping, table reconstruction from OCR words
    normalize.py   locale-aware parsing of dates, money, percentages, durations, IBANs
    classify.py    deterministic contract / invoice / unknown classifier with explicit uncertainty
    schemas.py     Pydantic v2 extraction schemas (contract, invoice) with evidence per field
    rules.py       offline deterministic extraction provider
    llm.py         optional, explicitly configured LLM extraction provider (validated, grounded)
    validation.py  field-level business rules (dates, currencies, invoice arithmetic)
    grounding.py   evidence checks: right document, valid page, value supported by the evidence
    service.py     processing pipeline, persistence, idempotent re-processing
    review.py      human review lifecycle with optimistic concurrency and audit events
    compare.py     multi-document comparison and structured queries over authorized documents
"""
