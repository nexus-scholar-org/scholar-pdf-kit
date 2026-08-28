# Smart Naming, Ingestion, and Export

This guide covers filename formatting schemes, manual PDF ingestion, and bibliographic metadata export.

---

## 1. Filename Formatting Schemes

### Default DOI Naming
Replaces filesystem-restricted characters (`/`, `\`, `:`) in the DOI with underscores:
- DOI `10.1038/s41586-020-2649-2` $\rightarrow$ `10.1038_s41586-020-2649-2.pdf`

### Smart Naming (`--smart-names`)
When enabled, metadata is extracted from OpenAlex/Unpaywall and formatted as:
```text
{publication_year}_{first_author_surname}_{sanitized_title}.pdf
```
- Example: `2020_Brown_Language_Models_are_Few_Shot_Learners.pdf`
- Titles are automatically truncated to 50 alphanumeric characters to prevent Windows path length errors.

---

## 2. Manual Ingestion (`scholar-pdf ingest`)

When a PDF has been acquired outside the automated downloader (e.g. from an author copy or institutional subscription), you can manually ingest it into the managed library:

```bash
uv run scholar-pdf ingest path/to/manual_paper.pdf --doi 10.1038/35057062 --smart-names --export json
```

### Ingestion Steps
1. Queries OpenAlex/Unpaywall to hydrate metadata for the DOI.
2. Validates PDF magic bytes (`%PDF-`).
3. Formats and copies the file into the destination download directory.
4. Appends record to `download_summary.json` or `download_summary.bibtex`.

---

## 3. Metadata Export Formats

Using `--export <format>` outputs an aggregated summary of all successfully downloaded documents:

- **JSON (`--export json`)**:
  Appends to `downloads/download_summary.json`:
  ```json
  [
    {
      "doi": "10.1371/journal.pbio.3000246",
      "file_path": "downloads/2019_McKiernan_Point_of_View_How_open_science.pdf",
      "metadata": {
        "title": "Point of View: How open science helps researchers succeed",
        "author": "McKiernan",
        "year": "2019"
      }
    }
  ]
  ```
- **BibTeX (`--export bibtex`)**:
  Appends formatted BibTeX entries to `downloads/download_summary.bibtex`.
