# API Reference

This document outlines the programmatic interfaces available within `scholar-pdf-kit` for developers integrating the toolkit into custom Python pipelines.

## `AsyncPDFDownloader`

The core class responsible for orchestrating concurrent PDF downloads and metadata resolution.

### Import Path
```python
from scholar_pdf.downloader import AsyncPDFDownloader
```

### Initialization
```python
def __init__(self, output_dir: Optional[Path] = None):
```
- **`output_dir`** (`Path`, optional): The directory where successfully validated PDFs will be saved. Defaults to `downloads/` or the path specified in `.env`.

### Methods

#### `process_doi`
```python
async def process_doi(self, session: aiohttp.ClientSession, http_client: AcademicHttpClient, doi: str) -> DownloadResult:
```
Resolves the Open Access status of a given DOI via OpenAlex and downloads the PDF if available.
- **Parameters:**
  - `session` (`aiohttp.ClientSession`): An active asynchronous HTTP session for streaming binary PDF data.
  - `http_client` (`AcademicHttpClient`): The synchronous caching HTTP client from `scholar-search-kit` used for rate-limited metadata resolution.
  - `doi` (`str`): The target Digital Object Identifier.
- **Returns:** A `DownloadResult` dataclass instance.

#### `download_batch`
```python
async def download_batch(self, dois: list[str]) -> list[DownloadResult]:
```
Processes an array of DOIs concurrently.
- **Parameters:**
  - `dois` (`list[str]`): An array of DOIs.
- **Returns:** A list of `DownloadResult` instances.

---

## `DownloadResult`

A dataclass representing the final state of a retrieval attempt.

### Attributes
- **`doi`** (`str`): The requested DOI.
- **`success`** (`bool`): Indicates if the PDF was successfully downloaded and validated.
- **`file_path`** (`Optional[Path]`): The local file path to the PDF (if successful).
- **`error_message`** (`Optional[str]`): Explanatory text if the retrieval failed (e.g., "Not Open Access").
- **`was_oa`** (`bool`): Indicates if OpenAlex reported the document as Open Access, regardless of whether the physical download succeeded.

---

## Validation Utility

### `clean_invalid_pdf`
```python
def clean_invalid_pdf(file_path: Path) -> bool:
```
Inspects the file signature (magic bytes) to guarantee the downloaded file is a valid PDF structure (`%PDF-`). Deletes the file if invalid.
- **Returns:** `True` if valid, `False` if invalid and deleted.
