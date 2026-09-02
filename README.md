# Scholar PDF Kit

[![CI Status](https://github.com/mouadh/Nexus-Scholar-Suite/actions/workflows/scholar-pdf-kit-ci.yml/badge.svg)](https://github.com/mouadh/Nexus-Scholar-Suite/actions)
[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Scholar PDF Kit** is a Python-based utility designed to automate the discovery and retrieval of Open Access (OA) academic literature.

By leveraging the open scholarly infrastructure provided by [OpenAlex](https://openalex.org/), this toolkit circumvents commercial academic paywalls legally, resolving Digital Object Identifiers (DOIs) directly to their hosted PDF files across university repositories and open archives.

## System Architecture

The package is built with a focus on concurrency and data integrity:
1. **Resolution**: Queries OpenAlex APIs to determine OA status and locate direct PDF endpoints.
2. **Concurrent Retrieval**: Utilizes `aiohttp` to manage asynchronous, high-throughput PDF downloads.
3. **Integrity Validation**: Analyzes downloaded file byte signatures (magic bytes) to ensure successful PDF retrieval and automatically discards HTML paywall redirects.

## Installation

Ensure the `git` and `uv` package managers are installed on your system. 

```bash
# Clone the repository
git clone https://github.com/mouadh/scholar-pdf-kit.git

# Navigate to the toolkit directory
cd scholar-pdf-kit

# Install the package and its dependencies
uv pip install -e .
```

## Command Line Interface (CLI)

The package exposes a Typer-based CLI for both targeted and bulk literature retrieval.

### Single Document Retrieval
Provide a DOI directly to the CLI:
```bash
uv run scholar-pdf --doi 10.1371/journal.pbio.3000246
```

**Example Output:**
```text
Starting download process for 1 DOIs...
Downloading PDFs... ---------------------------------------- 100%
                               Download Summary                                
+-----------------------------------------------------------------------------+
| DOI                          | Status  | Details                            |
|------------------------------+---------+------------------------------------|
| 10.1371/journal.pbio.3000246 | Success | downloads\10.1371_journal.pbio.30… |
+-----------------------------------------------------------------------------+
Successfully downloaded 1/1 PDFs.
```

### Multiple Document Retrieval
Chain multiple DOIs within a single command:
```bash
uv run scholar-pdf --doi 10.1371/journal.pbio.3000246 --doi 10.1038/35057062
```

**Example Output:**
```text
Starting download process for 2 DOIs...
Downloading PDFs... ---------------------------------------- 100%
                               Download Summary                                
+-----------------------------------------------------------------------------+
| DOI                          | Status  | Details                            |
|------------------------------+---------+------------------------------------|
| 10.1371/journal.pbio.3000246 | Success | downloads\10.1371_journal.pbio.30… |
| 10.1038/35057062             | Success | downloads\10.1038_35057062.pdf     |
+-----------------------------------------------------------------------------+
Successfully downloaded 2/2 PDFs.
```

### Bulk Retrieval via JSON / Included Literature
For systematic literature reviews, integrate directly with `scholar-search-kit` outputs (`included.json` or `results.json`):
```bash
uv run scholar-pdf download --input literature/included.json --output papers/pdfs/ --smart-names
```

### Manual PDF Ingestion
If an open-access PDF was obtained manually or from an institutional proxy:
```bash
uv run scholar-pdf ingest my_paper.pdf --doi 10.1038/s41586-023-0001 --output papers/pdfs/ --smart-names
```

### Section-Aware Markdown Extraction
Convert PDFs into structured Markdown with YAML frontmatter for downstream RAG indexing:
```bash
uv run scholar-pdf extract papers/pdfs/ --output papers/extracted/ --engine pymupdf
```

### CLI Arguments Reference
- `download`: Download OA PDFs from DOI arguments or candidate JSON files.
- `ingest`: Safely copy and validate local PDFs with metadata tagging.
- `extract`: Convert PDFs into structured Markdown with YAML frontmatter.

## Documentation

Comprehensive documentation is available in the `docs/` directory:
- [Tutorial](docs/tutorial.md): A step-by-step guide to executing bulk downloads.
- [API Reference](docs/api_reference.md): Technical documentation for programmatic usage.

## License
This project is licensed under the MIT License.
