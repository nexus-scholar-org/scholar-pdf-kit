---
name: scholar-pdf-kit
description: Instructions for using the scholar-pdf-kit Python API and CLI to discover, download, validate, and extract Open Access PDFs.
---

# `scholar-pdf-kit` Skill Instructions

You are an expert academic research agent equipped with `scholar-pdf-kit`. This toolkit automatically resolves DOIs to legal Open Access PDFs via OpenAlex and Unpaywall, validates binary integrity, and extracts structured text.

## Core Capabilities
1. **Open Access DOI Resolution**: Resolve DOIs to direct PDF endpoints across open repositories.
2. **Concurrent & Resilient Downloading**: Asynchronous retrieval with exponential backoff and paywall HTML redirect rejection.
3. **Magic Byte Validation**: Verifies binary `%PDF-` signature and removes corrupted or redirected files.
4. **Smart Naming & Metadata Export**: Formats filenames as `{year}_{author}_{title}.pdf` and exports metadata to JSON or BibTeX.
5. **Fulltext Extraction**: Converts PDFs to Markdown (Docling) or TEI XML (Grobid).

---

## Quick CLI Cheat-Sheet

All commands should be executed via `uv run`:

```bash
# 1. Download by DOI(s)
uv run scholar-pdf download --doi 10.1371/journal.pbio.3000246 --output downloads/

# 2. Bulk Download from scholar-search-kit Results with Smart Naming
uv run scholar-pdf download --input results.json --output downloads/ --smart-names --export json

# 3. Manually Ingest a Local PDF into the Managed Library
uv run scholar-pdf ingest my_paper.pdf --doi 10.1038/35057062 --smart-names

# 4. Extract Structured Markdown using Docling
uv run scholar-pdf extract downloads/ --output markdown/ --engine docling
```

---

## Programmatic Python API

> **CRITICAL RULE**: `AsyncPDFDownloader.download_batch`, `process_doi`, and `ingest_pdf` are asynchronous and must be awaited inside an `asyncio` event loop.

```python
import asyncio
from pathlib import Path
from scholar_pdf.downloader import AsyncPDFDownloader

async def main():
    dois = [
        "10.1371/journal.pbio.3000246",
        "10.7717/peerj.4375"
    ]

    # 1. Initialize Downloader
    downloader = AsyncPDFDownloader(
        output_dir=Path("downloads"),
        use_smart_names=True
    )

    # 2. Batch Download (Async)
    results = await downloader.download_batch(dois)

    # 3. Process Results
    for res in results:
        if res.success:
            print(f"Downloaded: {res.doi} -> {res.file_path}")
        else:
            print(f"Failed / Paywalled: {res.doi} -> {res.error_message}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Detailed References

For advanced workflows, underlying mechanisms, and fulltext extraction configurations, read these reference files on demand:

- [OA Resolution & Download Mechanics](references/resolution_and_download.md): OpenAlex/Unpaywall fallback, semaphore concurrency, HTML trap detection, and magic byte validation.
- [Smart Naming, Ingestion & Export](references/naming_and_ingestion.md): `{year}_{author}_{title}.pdf` format, manual PDF ingestion, and JSON/BibTeX export.
- [Fulltext Extraction](references/fulltext_extraction.md): Converting PDFs to Markdown via Docling and TEI XML via Grobid.
- [Pipeline Integration](references/pipeline_integration.md): End-to-end chaining from `scholar-search-kit` to downstream RAG stores.

---

## Agent Guidelines & Best Practices

- **Paywall Recognition**: Not all academic literature is Open Access. If resolution reports `was_oa=False`, clearly inform the user that no legal Open Access copy is available. Do not attempt to bypass commercial paywalls with web scrapers.
- **Path Verification**: Always verify that target download and extraction directories exist or allow the tool to create them automatically.
- **Handoff from Search**: When receiving a JSON export from `scholar-search-kit`, pass it directly with `--input <file.json>`.
