import aiohttp
import asyncio
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import settings
from .validator import clean_invalid_pdf
from scholar_search.http_client import AcademicHttpClient

@dataclass
class DownloadResult:
    doi: str
    success: bool
    file_path: Optional[Path] = None
    error_message: Optional[str] = None
    was_oa: bool = False

class AsyncPDFDownloader:
    """Asynchronous PDF Downloader using aiohttp."""
    
    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or settings.download_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        
    def _safe_filename(self, doi: str) -> str:
        """Converts a DOI to a safe filename."""
        safe_doi = doi.replace("/", "_").replace("\\", "_").replace(":", "_")
        return f"{safe_doi}.pdf"

    async def download_pdf(self, session: aiohttp.ClientSession, url: str, dest_path: Path) -> bool:
        """Downloads a PDF from a URL to a specific path."""
        headers = {"User-Agent": f"scholar-pdf-kit/0.1.0 (mailto:{settings.mailto})"}
        timeout = aiohttp.ClientTimeout(total=settings.download_timeout)
        
        try:
            async with self.semaphore:
                async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as response:
                    response.raise_for_status()
                    
                    # Ensure content type is PDF if provided
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "text/html" in content_type:
                        return False # Hit a paywall or login page
                        
                    with open(dest_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
            
            # Post-download validation using magic bytes
            return clean_invalid_pdf(dest_path)
            
        except Exception as e:
            if dest_path.exists():
                dest_path.unlink()
            return False

    async def process_doi(self, session: aiohttp.ClientSession, http_client: AcademicHttpClient, doi: str) -> DownloadResult:
        """Processes a single DOI: resolves OA status via OpenAlex and downloads if possible."""
        try:
            # Run the synchronous cached HTTP client in a thread
            def fetch_metadata():
                url = f"https://api.openalex.org/works/https://doi.org/{doi}"
                try:
                    response = http_client.get(url, params={"mailto": settings.mailto})
                    return response.json()
                except Exception as e:
                    # E.g. 404
                    return None
            
            data = await asyncio.to_thread(fetch_metadata)
            
            if not data:
                return DownloadResult(doi=doi, success=False, error_message="DOI not found in OpenAlex")
                
            best_oa = data.get("best_oa_location", {})
            pdf_url = None
            if best_oa:
                pdf_url = best_oa.get("pdf_url")
                
            if not pdf_url:
                return DownloadResult(doi=doi, success=False, was_oa=False, error_message="Not Open Access or no PDF link")
                
            dest_path = self.output_dir / self._safe_filename(doi)
            
            # Skip if already downloaded
            if dest_path.exists() and clean_invalid_pdf(dest_path):
                return DownloadResult(doi=doi, success=True, file_path=dest_path, was_oa=True)
                
            success = await self.download_pdf(session, pdf_url, dest_path)
            
            if success:
                return DownloadResult(doi=doi, success=True, file_path=dest_path, was_oa=True)
            else:
                return DownloadResult(doi=doi, success=False, was_oa=True, error_message="Failed to download or invalid PDF")
                
        except Exception as e:
            return DownloadResult(doi=doi, success=False, error_message=str(e))

    async def download_batch(self, dois: list[str]) -> list[DownloadResult]:
        """Downloads a batch of DOIs concurrently."""
        http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
        async with aiohttp.ClientSession() as session:
            tasks = [self.process_doi(session, http_client, doi) for doi in dois]
            results = await asyncio.gather(*tasks)
            return results
