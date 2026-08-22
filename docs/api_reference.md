# Scholar PDF Kit: API Reference

This document provides the API contracts for the core components of `scholar-pdf-kit`.

## `AsyncPDFDownloader`
The core asynchronous downloader that takes DOIs, queries OpenAlex for Open Access locations, and concurrently downloads the PDFs using `aiohttp`.

```python
import asyncio
from pathlib import Path
from scholar_pdf.downloader import AsyncPDFDownloader

async def download_papers():
    output_dir = Path("./my_pdfs")
    downloader = AsyncPDFDownloader(output_dir=output_dir)
    
    dois = ["10.1234/example1", "10.1234/example2"]
    
    # download_batch automatically sets up the aiohttp ClientSession
    results = await downloader.download_batch(dois)
    
    for res in results:
        print(f"{res.doi} -> Success: {res.success}, Path: {res.file_path}")

asyncio.run(download_papers())
```

## `DownloadResult`
A dataclass returned by `download_batch` or `process_doi` containing the resolution status of a DOI.

```python
@dataclass
class DownloadResult:
    doi: str
    success: bool
    file_path: Optional[Path] = None
    error_message: Optional[str] = None
    was_oa: bool = False
```

## `OAResult` & `OALocation`
Pydantic v2 models representing the OpenAlex metadata schema (which mirrors the Unpaywall data standard).

```python
from scholar_pdf.models import OAResult

# Automatically extracts the best PDF url from the OpenAlex JSON metadata
pdf_url = oa_result.best_pdf_url
```
