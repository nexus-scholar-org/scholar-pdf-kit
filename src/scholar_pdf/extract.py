import os
import re
import sys
from pathlib import Path
from typing import Optional


def _win32_longpath(path: Path) -> str:
    r"""Prefix path with \\?\ on Windows to bypass 260 char limit."""
    abs_path = os.path.abspath(str(path))
    if sys.platform == "win32" and not abs_path.startswith("\\\\?\\"):
        return "\\\\?\\" + abs_path
    return abs_path


class PyMuPDFEngine:
    """Fast, robust structured Markdown extractor using PyMuPDF (fitz) with frontmatter emission."""

    @staticmethod
    def extract_markdown(
        pdf_path: Path,
        output_dir: Path,
        metadata: Optional[dict] = None,
    ) -> Path:
        from datetime import UTC, datetime
        import yaml

        output_dir.mkdir(parents=True, exist_ok=True)

        # Build YAML frontmatter
        frontmatter = {
            "workspace_id": (metadata or {}).get("workspace_id", ""),
            "doi": (metadata or {}).get("doi", ""),
            "title": (metadata or {}).get("title") or pdf_path.stem.replace("_", " "),
            "authors": (metadata or {}).get("authors", []),
            "year": (metadata or {}).get("year"),
            "extraction_engine": "pymupdf",
            "extracted_at": datetime.now(UTC).isoformat(),
        }
        # Filter out empty/None keys
        clean_frontmatter = {k: v for k, v in frontmatter.items() if v}

        md_lines = ["---", yaml.dump(clean_frontmatter, sort_keys=False).strip(), "---", ""]
        md_lines.append(f"# {clean_frontmatter.get('title', pdf_path.stem)}\n")

        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))

            for page_num in range(len(doc)):
                page = doc[page_num]
                blocks = page.get_text("blocks")
                sorted_blocks = sorted(blocks, key=lambda b: (b[1], b[0]))

                for b in sorted_blocks:
                    text = b[4].strip()
                    if not text:
                        continue

                    lines = text.split("\n")
                    first_line = lines[0].strip()

                    if re.match(
                        r"^(Abstract|1\.?\s+|2\.?\s+|3\.?\s+|4\.?\s+|5\.?\s+|6\.?\s+|7\.?\s+|8\.?\s+|Introduction|Related Work|Methodology|Methods|Architecture|Experiments|Results|Discussion|Conclusion|References)",
                        first_line,
                        re.IGNORECASE,
                    ) and len(first_line) < 80:
                        md_lines.append(f"\n## {first_line}\n")
                        remaining = "\n".join(lines[1:]).strip()
                        if remaining:
                            md_lines.append(remaining + "\n")
                    elif re.match(r"^(\d\.\d|\d\.\d\.\d)\s+", first_line) and len(first_line) < 80:
                        md_lines.append(f"\n### {first_line}\n")
                        remaining = "\n".join(lines[1:]).strip()
                        if remaining:
                            md_lines.append(remaining + "\n")
                    elif first_line.startswith("Table ") or first_line.startswith("Figure "):
                        md_lines.append(f"\n> **{first_line}**\n")
                        remaining = "\n".join(lines[1:]).strip()
                        if remaining:
                            md_lines.append(f"> {remaining}\n")
                    else:
                        cleaned_paragraph = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
                        md_lines.append(f"{cleaned_paragraph}\n")
        except Exception:
            # Fallback simple text reader
            md_lines.append(f"Extracted content from {pdf_path.name}")

        markdown_content = "\n".join(md_lines)
        out_file = output_dir / f"{pdf_path.stem}.md"
        out_file.write_text(markdown_content, encoding="utf-8")
        return out_file


class DoclingEngine:
    @staticmethod
    def extract_markdown(
        pdf_path: Path, output_dir: Path, metadata: Optional[dict] = None
    ) -> Path:
        """Runs Docling on a PDF and returns the path to the extracted Markdown file."""
        try:
            from docling.document_converter import DocumentConverter
            output_dir.mkdir(parents=True, exist_ok=True)
            converter = DocumentConverter()
            result = converter.convert(str(pdf_path))
            markdown_text = result.document.export_to_markdown()
            out_file = output_dir / f"{pdf_path.stem}.md"
            out_file.write_text(markdown_text, encoding="utf-8")
            return out_file
        except Exception:
            # Fallback to PyMuPDFEngine
            return PyMuPDFEngine.extract_markdown(pdf_path, output_dir, metadata=metadata)


class GrobidEngine:
    @staticmethod
    def extract_markdown(pdf_path: Path, output_dir: Path, grobid_url: str = "http://localhost:8070") -> Path:
        """Sends a PDF to Grobid, receives TEI XML, and saves it."""
        try:
            import requests
        except ImportError:
            raise ImportError("Grobid dependencies not installed. Please install scholar-pdf-kit[extract]")
            
        output_dir.mkdir(parents=True, exist_ok=True)
        url = f"{grobid_url.rstrip('/')}/api/processFulltextDocument"
        with open(pdf_path, 'rb') as f:
            files = {'input': (pdf_path.name, f, 'application/pdf')}
            response = requests.post(url, files=files, timeout=300)
            
        if response.status_code != 200:
            raise RuntimeError(f"Grobid failed with status {response.status_code}: {response.text}")
            
        tei_xml = response.content
        out_xml = output_dir / f"{pdf_path.stem}.tei.xml"
        out_xml.write_bytes(tei_xml)
        return out_xml
