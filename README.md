# Scholar PDF Kit

[![CI Status](https://github.com/mouadh/Nexus-Scholar-Suite/actions/workflows/scholar-pdf-kit-ci.yml/badge.svg)](https://github.com/mouadh/Nexus-Scholar-Suite/actions)
[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Scholar PDF Kit** is a Python-based utility designed to automate the discovery and retrieval of Open Access (OA) academic literature.

By using metadata and legally available copies from sources such as [OpenAlex](https://openalex.org/), the toolkit resolves Digital Object Identifiers (DOIs) to authorized or open-access PDF endpoints. It does not infer legal access from an HTTP response or bypass access controls.

## System Architecture

The package is built with a focus on concurrency and data integrity:
1. **Resolution**: Queries OpenAlex APIs to determine OA status and locate direct PDF endpoints.
2. **Concurrent Retrieval**: Utilizes `aiohttp` to manage asynchronous, high-throughput PDF downloads.
3. **Integrity Validation**: Analyzes downloaded file byte signatures and PDF structure to reject incomplete, malformed, encrypted, or non-PDF responses.

## Installation

Ensure the `git` and `uv` package managers are installed on your system. 

```bash
# Clone the repository
git clone https://github.com/mouadh/scholar-pdf-kit.git

# Navigate to the toolkit directory
cd scholar-pdf-kit

# Install the E1 API/CLI and base dependencies
uv pip install .

# In the toolkit monorepo, add the legacy search-provider adapter
uv pip install -e ".[search]"
```

The base wheel keeps Contract v1 acquisition, validation, and atomic publication
self-contained. The deprecated `download`/`ingest` provider paths require the
optional `search` extra because their HTTP client is owned by
`scholar-search-kit`.

## Command Line Interface (CLI)

The package exposes a Typer-based CLI for both targeted and bulk literature retrieval.

### Single Document Retrieval
Provide a DOI directly to the CLI:
```bash
uv run scholar-pdf download --doi 10.1371/journal.pbio.3000246
```

**Example Output:**
```text
Starting download process for 1 DOIs...
Downloading PDFs... ---------------------------------------- 100%
                               Download Summary                                
+-----------------------------------------------------------------------------+
| DOI                          | Status  | Details                            |
|------------------------------+---------+------------------------------------|
| 10.1371/journal.pbio.3000246 | Success | downloads/10.1371_journal.pbio.30… |
+-----------------------------------------------------------------------------+
Successfully downloaded 1/1 PDFs.
```

### Multiple Document Retrieval
Chain multiple DOIs within a single command:
```bash
uv run scholar-pdf download --doi 10.1371/journal.pbio.3000246 --doi 10.1038/35057062
```

**Example Output:**
```text
Starting download process for 2 DOIs...
Downloading PDFs... ---------------------------------------- 100%
                               Download Summary                                
+-----------------------------------------------------------------------------+
| DOI                          | Status  | Details                            |
|------------------------------+---------+------------------------------------|
| 10.1371/journal.pbio.3000246 | Success | downloads/10.1371_journal.pbio.30… |
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

### Contract v1 parent-bound acquisition (WP01-E1)

The `acquire` API and command are the authoritative path for Contract v1
research workspaces. They require a canonical `AcquisitionRunConfig`, accepted
corpus/screening parents, a canonical workspace-root binding, and the
workspace-manager audit logger. The service verifies the parent lineage before
writing any artifact, streams into a same-directory staging file, validates the
bytes, atomically promotes content to `pdfs/acquired/<document_id>.pdf`, and
then publishes the deterministic acquisition manifest.

```bash
uv run scholar-pdf acquire acquisition-config.json \
  --audit-logger /path/to/workspace-manager/scripts/log_event.py
```

The command emits JSON. Exit codes are `0` for success, `2` for a partial
batch, `1` for a failed batch, and `130` for cancellation. The manifest records
`SUCCESS`, `PARTIAL`, `FAILED`, or `CANCELLED`; an unresolved provider result is
not a paywall determination, and a provider or transport failure is not an
empty successful result. Content identities are deterministic `DOC-*` identities
derived from the validated bytes and scoped by workspace and study. Manifests contain
only workspace-relative POSIX paths, parent hashes, validation results,
attempts, and redacted source evidence—never absolute paths, credentials, or
provider tokens.

The programmatic entry point is `PDFAcquisitionService.acquire(...)`; use an
injected transport and audit sink for hermetic tests. Replaying the same batch
is idempotent, while a different semantic request in the same run, stale parent
lineage, changed bound bytes, or path traversal fails closed. `USER_PATH`
sources are copied only when the researcher has explicitly authorized the
read-only input; network sources never fabricate legal-access status from a
successful HTTP response. MCP acquisition is explicitly unsupported in E1; the
E1 MCP surface must return `UNSUPPORTED_CAPABILITY` until its separately governed
implementation is available.

### Section-Aware Markdown Extraction
Convert PDFs into structured Markdown with YAML frontmatter for downstream RAG indexing:
```bash
uv run scholar-pdf extract papers/pdfs/ --output papers/extracted/ --engine docling
```

### CLI Arguments Reference
- `acquire`: Execute a parent-bound Contract v1 acquisition batch and emit a deterministic manifest.
- `download`: Legacy DOI/provider retrieval. Files are validated and atomically published under content-addressed `DOC-*` names.
- `ingest`: Validate and atomically publish a local PDF under a content-addressed name.
- `extract`: Convert PDFs into structured Markdown with YAML frontmatter (`docling` or `grobid`).

For legacy downloads, `access_status` is the truthful access projection;
`was_oa` remains only as a deprecated compatibility field. A successful HTTP
response alone does not establish that a source is open access.

## Documentation

Comprehensive documentation is available in the `docs/` directory:
- [Tutorial](docs/tutorial.md): A step-by-step guide to executing bulk downloads.
- [API Reference](docs/api_reference.md): Technical documentation for programmatic usage.

## License
This project is licensed under the MIT License.
