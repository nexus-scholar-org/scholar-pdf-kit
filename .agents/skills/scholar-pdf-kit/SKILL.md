---
name: scholar-pdf-kit
description: Instructions for using the scholar-pdf-kit Python API and CLI to download Open Access PDFs.
---

# `scholar-pdf-kit` Skill Instructions

You are an expert agent equipped with the `scholar-pdf-kit`. This toolkit allows you to legally bypass academic paywalls by fetching Open Access PDFs using the OpenAlex API infrastructure.

## Core Capabilities
1. **Resolve DOIs**: Determine if a DOI has a legal, free PDF available.
2. **Download PDFs**: Concurrently download PDFs while avoiding publisher HTML paywall traps.
3. **Integration**: Consume JSON output from `scholar-search-kit` to bulk-download literature.

## How to use the CLI

The primary interface is the `scholar-pdf` command. Ensure you are running it via `uv run` inside the project context or with the package installed.

### 1. Download a single DOI
```bash
uv run scholar-pdf --doi 10.7717/peerj.4375 --output my_pdfs/
```

### 2. Download multiple DOIs
```bash
uv run scholar-pdf --doi 10.1234/abc --doi 10.5678/def
```

### 3. Bulk Download from JSON
If you previously used `scholar-search-kit` to generate a `results.json` file, you can pass it directly:
```bash
uv run scholar-pdf --input results.json --output downloaded_pdfs/
```

## How to use the Python API

If you need to write a custom Python script to interact with the toolkit programmatically, use the `AsyncPDFDownloader`. 

**Critical Rule:** Because the downloader uses asynchronous `aiohttp`, you must run it inside an `asyncio` event loop.

### Example Script
```python
import asyncio
import aiohttp
from pathlib import Path
from scholar_search.http_client import AcademicHttpClient
from scholar_pdf.downloader import AsyncPDFDownloader

async def main():
    dois = ["10.7717/peerj.4375", "10.1371/journal.pbio.3000246"]
    
    # 1. Initialize the HTTP Client (from scholar-search-kit) for rate-limiting
    http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
    
    # 2. Initialize the Downloader
    output_dir = Path("my_downloads")
    downloader = AsyncPDFDownloader(output_dir=output_dir)
    
    # 3. Process the downloads
    results = []
    async with aiohttp.ClientSession() as session:
        tasks = [downloader.process_doi(session, http_client, doi) for doi in dois]
        for coro in asyncio.as_completed(tasks):
            res = await coro
            results.append(res)
            
    # 4. Handle Results
    for res in results:
        if res.success:
            print(f"Downloaded: {res.doi} -> {res.file_path}")
        else:
            print(f"Failed: {res.doi} -> {res.error_message}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Agent Guidelines

- **Always verify paths**: When passing `--input` or `--output`, ensure the directories exist or that the tool will create them.
- **Paywalls**: Not all DOIs are Open Access. If a download fails with "Not Open Access", inform the user that a legal free copy does not exist in the open infrastructure. Do not attempt to scrape proprietary publisher sites.
- **Dependencies**: `scholar-pdf-kit` natively relies on `scholar-search-kit` for the `AcademicHttpClient`. If writing a Python script, ensure both packages are accessible in the environment.
