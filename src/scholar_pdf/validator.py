import os
import re
from pathlib import Path

# Real PDFs are at least ~10 KB; HTML block-pages saved from a paywall are
# typically a few KB.  This guards against saving stub pages as PDFs even
# when the first bytes happen to contain a %PDF- marker.
MIN_PDF_SIZE_BYTES = 10 * 1024

# The PDF spec allows up to 1024 bytes of leading garbage before the header
# and requires the %%EOF marker on the trailer (within the last 1024 bytes
# for non-linearized files).  We scan a generous tail window to tolerate
# sloppy producers without rejecting real files.
_HEADER_SCAN = 1024
_TRAILER_SCAN = 8 * 1024

_HEADER_RE = re.compile(rb"%PDF-\d+\.\d+")


def is_valid_pdf(file_path: Path) -> bool:
    """
    Validate a candidate PDF by binary signature only (no external deps).

    A real PDF must be at least ``MIN_PDF_SIZE_BYTES`` (10 KB), carry a
    ``%PDF-<major>.<minor>`` header within the first 1024 bytes, and end with
    a ``%%EOF`` trailer marker within the last 8 KB.  This is deliberately
    stricter than a 5-byte prefix check: forged ``%PDF-``-prefixed HTML abuse
    prepends three tokens to an HTML page, which fails the trailer test.
    """
    if not file_path.exists() or file_path.stat().st_size < 5:
        return False

    try:
        if file_path.stat().st_size < MIN_PDF_SIZE_BYTES:
            return False

        size = file_path.stat().st_size
        with open(file_path, "rb") as f:
            head = f.read(_HEADER_SCAN)
            # %PDF-<major>.<minor> must appear within the first 1024 bytes
            # (the spec permits leading garbage before the header).
            if _HEADER_RE.search(head) is None:
                return False

            f.seek(max(0, size - _TRAILER_SCAN))
            tail = f.read(_TRAILER_SCAN)
            return b"%%EOF" in tail
    except (OSError, ValueError, OverflowError):
        return False


def validate_pdf_structure(file_path: Path) -> bool:
    """
    Stronger, optional structural validation via pypdf.

    Returns True when the file actually parses as a PDF.  Encrypted PDFs
    (password-protected) parse fine after decryption and are treated as valid;
    files that pypdf cannot make sense of at all return False.
    """
    if not is_valid_pdf(file_path):
        return False
    try:
        import pypdf
    except ImportError:
        # pypdf declared as a dependency but not installed in this env:
        # binary signature validation is authoritative, do not fail closed.
        return True

    try:
        with open(file_path, "rb") as f:
            pypdf.PdfReader(f, strict=False)
        return True
    except pypdf.errors.WrongPasswordError:
        return True
    except pypdf.errors.PdfReadError as exc:
        # "File has not been decrypted" means it is a real but encrypted PDF.
        return "decrypted" in str(exc).lower()
    except Exception:
        return False


def clean_invalid_pdf(file_path: Path) -> bool:
    """
    Checks if a file is a valid PDF and deletes it if it is not.
    Returns True if the file was kept, False if it was deleted.
    """
    if is_valid_pdf(file_path):
        return True

    if file_path.exists():
        try:
            os.remove(file_path)
        except OSError:
            pass

    return False