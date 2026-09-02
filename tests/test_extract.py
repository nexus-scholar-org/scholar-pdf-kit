from pathlib import Path
from scholar_pdf.extract import PyMuPDFEngine


def test_extract_markdown_frontmatter(tmp_path: Path):
    dummy_pdf = tmp_path / "2024_chen_grounded.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4\nMock PDF Content")

    out_dir = tmp_path / "extracted"
    metadata = {
        "workspace_id": "SCI-000412",
        "doi": "10.1038/s41586-024-0001",
        "title": "Grounded Language Models",
        "year": 2024,
    }

    out_md = PyMuPDFEngine.extract_markdown(dummy_pdf, out_dir, metadata=metadata)
    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")

    assert "workspace_id: SCI-000412" in content
    assert "doi: 10.1038/s41586-024-0001" in content
    assert "title: Grounded Language Models" in content
    assert "extraction_engine: pymupdf" in content
    assert "# Grounded Language Models" in content
