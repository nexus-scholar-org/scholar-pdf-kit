import os
from pathlib import Path

def is_valid_pdf(file_path: Path) -> bool:
    """
    Validates if a file is a valid PDF by checking its magic bytes.
    A valid PDF file should start with %PDF-
    """
    if not file_path.exists() or file_path.stat().st_size < 5:
        return False
        
    try:
        with open(file_path, "rb") as f:
            header = f.read(5)
            return header == b"%PDF-"
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
