import os
import sys
from pathlib import Path
from typing import Optional

def _win32_longpath(path: Path) -> str:
    """Prefix path with \\?\ on Windows to bypass 260 char limit."""
    abs_path = os.path.abspath(str(path))
    if sys.platform == "win32" and not abs_path.startswith("\\\\?\\"):
        return "\\\\?\\" + abs_path
    return abs_path

class DoclingEngine:
    @staticmethod
    def extract_markdown(pdf_path: Path, output_dir: Path) -> Path:
        """Runs Docling on a PDF and returns the path to the extracted Markdown file."""
        try:
            from docling.document_converter import DocumentConverter
        except ImportError:
            raise ImportError("Docling is not installed. Please install scholar-pdf-kit[extract]")

        output_dir.mkdir(parents=True, exist_ok=True)
        
        converter = DocumentConverter()
        result = converter.convert(str(pdf_path))
        markdown_text = result.document.export_to_markdown()
        
        out_file = output_dir / f"{pdf_path.stem}.md"
        out_file.write_text(markdown_text, encoding="utf-8")
        return out_file

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
