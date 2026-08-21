# Scholar PDF Kit Tutorial

This tutorial provides a step-by-step walkthrough of utilizing the `scholar-pdf-kit` CLI to resolve Open Access (OA) metadata and retrieve academic PDFs.

## Prerequisites
Ensure the toolkit is installed in your local environment.

```bash
cd scholar-pdf-kit
uv pip install -e .
```

## 1. Single Document Retrieval

The most direct way to use the toolkit is by supplying a single Digital Object Identifier (DOI). 

### Scenario A: Open Access Exists
Let's attempt to retrieve a known Open Access publication from PLOS Biology.

```bash
uv run scholar-pdf --doi 10.1371/journal.pbio.3000246
```

**Expected Output:**
The system queries OpenAlex, identifies the Open Access PDF endpoint, and downloads the file to the `downloads/` directory.
```text
Starting download process for 1 DOIs...
Downloading PDFs... ---------------------------------------- 100%
                  Download Summary                  
+------------------------------------+-----------+-----------------------------------------------+
| DOI                                | Status    | Details                                       |
|------------------------------------+-----------+-----------------------------------------------|
| 10.1371/journal.pbio.3000246       | Success   | downloads/10.1371_journal.pbio.3000246.pdf    |
+------------------------------------+-----------+-----------------------------------------------+
Successfully downloaded 1/1 PDFs.
```

### Scenario B: Paywalled / No Open Access
Now let's attempt to retrieve an older, paywalled publication from Nature.

```bash
uv run scholar-pdf --doi 10.1038/35057062
```

**Expected Output:**
The system determines via OpenAlex that no legal Open Access PDF exists. It terminates gracefully without attempting to bypass illegal paywalls.
```text
Starting download process for 1 DOIs...
Downloading PDFs... ---------------------------------------- 100%
                  Download Summary                  
+--------------------+-----------+-----------------+
| DOI                | Status    | Details         |
|--------------------+-----------+-----------------|
| 10.1038/35057062   | Paywalled | Not Open Access |
+--------------------+-----------+-----------------+
Successfully downloaded 0/1 PDFs.
```

## 2. Bulk Retrieval via JSON Pipeline

For systematic literature reviews, DOIs are rarely processed one by one. The `scholar-pdf-kit` is designed to ingest JSON output directly from the `scholar-search-kit`.

Suppose you have generated a `results.json` file containing metadata for 50 papers.

```bash
uv run scholar-pdf --input results.json --output ./literature_review_pdfs --max-concurrent 10
```

**Key Parameters:**
- `--input`: Parses the JSON array and extracts all valid DOIs.
- `--output`: Redirects the retrieved PDFs to a designated project folder.
- `--max-concurrent`: Increases the asynchronous download limit to 10 simultaneous connections, drastically reducing total retrieval time.

The resulting table will summarize which PDFs were successfully retrieved and which remain locked behind paywalls, allowing researchers to prioritize their reading accordingly.
