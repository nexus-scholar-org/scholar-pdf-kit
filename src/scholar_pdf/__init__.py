"""Scholar PDF Kit: Automated Open Access Discovery and PDF downloader."""

from .downloader import AsyncPDFDownloader, DownloadResult
from .extract import DoclingEngine, GrobidEngine, PyMuPDFEngine
from .validator import clean_invalid_pdf, is_valid_pdf

__version__ = "0.1.0"
__all__ = [
    "AsyncPDFDownloader",
    "DownloadResult",
    "DoclingEngine",
    "GrobidEngine",
    "PyMuPDFEngine",
    "clean_invalid_pdf",
    "is_valid_pdf",
]
