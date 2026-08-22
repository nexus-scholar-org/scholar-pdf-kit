# Scholar PDF Kit: Tutorial

This tutorial demonstrates how to use `scholar-pdf-kit` to automatically bypass academic paywalls and discover Open Access (OA) PDFs for a list of DOIs.

## Command Line Interface

### 1. Download specific DOIs
You can explicitly provide DOIs to download. 

```bash
scholar-pdf download -d 10.1038/s41586-020-2649-2 -d 10.1126/science.12345 --output ./my_pdfs
```

### 2. Download from a JSON file (Integration)
The kit is designed to seamlessly integrate with `scholar-search-kit`. If you exported search results as a JSON file, you can pass it directly:

```bash
scholar-pdf download -i search_results.json --output ./my_pdfs
```
The CLI automatically parses the JSON, extracts any DOIs found inside, and attempts to download the Open Access PDFs.

### 3. Concurrency Limits
To avoid overloading network interfaces or exceeding IO limits, set the maximum concurrent downloads:
```bash
scholar-pdf download -d 10.1234/test -c 10
```

## Python API

You can script your own concurrent download pipelines:

```python
import asyncio
from scholar_pdf.downloader import AsyncPDFDownloader

async def main():
    dois = ["10.1038/s41586-020-2649-2"]
    downloader = AsyncPDFDownloader()
    
    results = await downloader.download_batch(dois)
    for r in results:
        if r.success:
            print(f"Downloaded: {r.file_path}")
        else:
            print(f"Failed {r.doi}: {r.error_message}")

asyncio.run(main())
```
