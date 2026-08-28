# Fulltext Extraction Guide (Docling & Grobid)

`scholar-pdf-kit` provides fulltext extraction utilities to convert raw PDFs into structured Markdown or TEI XML for downstream RAG and language model processing.

---

## 1. Extraction Engines

| Engine | Flag | Output Format | Purpose |
| :--- | :--- | :--- | :--- |
| **Docling** | `--engine docling` (default) | Structured Markdown (`.md`) | Preserves document hierarchy, tables, code blocks, and headers for RAG chunking. |
| **Grobid** | `--engine grobid` | TEI XML (`.tei.xml`) | Structured XML with parsed affiliations, author metadata, and citation references. |

---

## 2. Command Line Usage

```bash
# Extract Markdown from a single PDF
uv run scholar-pdf extract downloads/sample.pdf --output markdown/

# Extract all PDFs in a directory
uv run scholar-pdf extract downloads/ --output markdown/ --engine docling

# Extract TEI XML using a local Grobid container
uv run scholar-pdf extract downloads/ --output tei/ --engine grobid --grobid-url http://localhost:8070
```

---

## 3. Grobid Docker Deployment

To use the Grobid extraction engine, launch the provided compose file:
```bash
docker compose -f docker-compose.grobid.yml up -d
```
The REST endpoint will be accessible at `http://localhost:8070`.

---

## 4. Programmatic Usage in Python

```python
from pathlib import Path
from scholar_pdf.extract import DoclingEngine, GrobidEngine

pdf_file = Path("downloads/sample.pdf")
output_dir = Path("markdown_output")

# 1. Extract Markdown via Docling
md_path = DoclingEngine.extract_markdown(pdf_file, output_dir)
print(f"Generated Markdown: {md_path}")

# 2. Extract TEI XML via Grobid
tei_path = GrobidEngine.extract_markdown(pdf_file, output_dir, grobid_url="http://localhost:8070")
print(f"Generated TEI XML: {tei_path}")
```
