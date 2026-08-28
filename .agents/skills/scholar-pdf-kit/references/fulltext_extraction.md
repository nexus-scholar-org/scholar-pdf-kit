# Fulltext Extraction Reference (Docling & Grobid)

`scholar-pdf-kit` includes built-in fulltext extraction capabilities to convert raw PDFs into structured Markdown or TEI XML for downstream RAG and LLM consumption.

---

## 1. Extraction Engines

| Engine | Command Flag | Output Format | Best Used For |
| :--- | :--- | :--- | :--- |
| **Docling** | `--engine docling` (default) | Structured Markdown (`.md`) | Reading text, tables, headers, and code directly into LLM prompts and vector chunkers. |
| **Grobid** | `--engine grobid` | TEI XML (`.tei.xml`) | Deep bibliographic parsing, section labeling, and citation extraction. |

---

## 2. CLI Usage

```bash
# Extract Markdown from a single PDF using Docling
uv run scholar-pdf extract downloads/my_paper.pdf --output markdown/

# Extract all PDFs in a folder to Markdown
uv run scholar-pdf extract downloads/ --output markdown/ --engine docling

# Extract TEI XML using a local Grobid container
uv run scholar-pdf extract downloads/ --output tei/ --engine grobid --grobid-url http://localhost:8070
```

---

## 3. Running Grobid via Docker

If using the Grobid engine, run the official container using the provided compose file:
```bash
docker compose -f docker-compose.grobid.yml up -d
```
Service runs on port `8070`.

---

## 4. Programmatic Usage in Python

```python
from pathlib import Path
from scholar_pdf.extract import DoclingEngine, GrobidEngine

pdf_file = Path("downloads/sample.pdf")
output_dir = Path("markdown_output")

# 1. Extract Markdown with Docling
md_path = DoclingEngine.extract_markdown(pdf_file, output_dir)
print(f"Generated Markdown at: {md_path}")

# 2. Extract TEI XML with Grobid
tei_path = GrobidEngine.extract_markdown(pdf_file, output_dir, grobid_url="http://localhost:8070")
print(f"Generated TEI XML at: {tei_path}")
```
