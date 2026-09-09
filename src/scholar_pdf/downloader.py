import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from scholar_search.http_client import AcademicHttpClient
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings
from .publisher_patterns import (
    compute_direct_pdf_from_landing_url,
    is_proxied_url,
    resolve_doi_to_publisher_pdf,
    rewrite_via_proxy,
)
from .validator import clean_invalid_pdf, validate_pdf_structure

logger = logging.getLogger(__name__)

@dataclass
class DownloadResult:
    doi: str
    success: bool
    file_path: Path | None = None
    error_message: str | None = None
    was_oa: bool = False
    metadata: dict | None = None

class AsyncPDFDownloader:
    """Asynchronous PDF Downloader using aiohttp."""
    
    def __init__(
        self,
        output_dir: Path | None = None,
        use_smart_names: bool = False,
        proxy_url: str | None = None,
        proxy_style: str = "auto",
        structural_validation: bool | None = None,
    ):
        self.output_dir = output_dir or settings.download_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.use_smart_names = use_smart_names
        self.proxy_url = (proxy_url if proxy_url is not None else settings.proxy_url) or ""
        self.proxy_style = proxy_style or settings.proxy_style or "auto"
        self.structural_validation = (
            settings.pdf_structural_validation if structural_validation is None else structural_validation
        )
        
    def _safe_filename(self, doi: str, metadata: dict | None = None) -> str:
        """Converts a DOI to a safe filename, optionally using metadata."""
        if self.use_smart_names and metadata:
            title = metadata.get("title", "")
            author = metadata.get("author", "")
            year = metadata.get("year", "0000")
            
            if title and author:
                # Sanitize title and author
                safe_title = "".join(c for c in title[:50] if c.isalnum() or c in (" ", "_")).replace(" ", "_")
                safe_author = "".join(c for c in author if c.isalnum() or c in (" ", "_")).replace(" ", "_")
                return f"{year}_{safe_author}_{safe_title}.pdf"
                
        safe_doi = doi.replace("/", "_").replace("\\", "_").replace(":", "_")
        return f"{safe_doi}.pdf"

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True
    )
    async def download_pdf(self, session: aiohttp.ClientSession, url: str, dest_path: Path, proxy_url: str = "") -> bool:
        """Downloads a PDF from a URL to a specific path."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/pdf,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=settings.download_timeout)

        try:
            async with self.semaphore:
                proxy = proxy_url.strip() or None
                async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True, proxy=proxy) as response:
                    response.raise_for_status()

                    # Check if the final URL looks like a PDF
                    final_url = str(response.url)
                    is_pdf_url = final_url.lower().endswith('.pdf')

                    # Check content type (but don't reject on HTML if URL suggests PDF)
                    content_type = response.headers.get("Content-Type", "").lower()
                    is_pdf_content_type = "application/pdf" in content_type

                    # Download the content first
                    with open(dest_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)

            # Post-download validation using magic bytes - this is the authoritative check
            # If the file has %PDF- magic bytes, it's a valid PDF regardless of headers
            if clean_invalid_pdf(dest_path):
                return True

            # If magic bytes check failed, check if we got HTML (paywall/login page)
            if dest_path.exists():
                with open(dest_path, "rb") as f:
                    header = f.read(100)
                if b"<html" in header.lower() or b"<!doctype html" in header.lower():
                    return False  # Definitely an HTML page

            return False

        except Exception as e:
            if dest_path.exists():
                dest_path.unlink()
            raise e  # Let tenacity handle retries

    async def fetch_openalex_metadata(self, http_client: AcademicHttpClient, doi: str) -> dict | None:
        url = f"https://api.openalex.org/works/https://doi.org/{doi}"
        try:
            response = await http_client.get(url, params={"mailto": settings.mailto})
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        return None
        
    async def fetch_unpaywall_metadata(self, http_client: AcademicHttpClient, doi: str) -> dict | None:
        url = f"https://api.unpaywall.org/v2/{doi}"
        try:
            response = await http_client.get(url, params={"email": settings.mailto})
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        return None

    def extract_metadata(self, oa_data: dict, source: str) -> dict:
        metadata = {"title": "", "author": "", "year": ""}
        if source == "openalex":
            metadata["title"] = oa_data.get("title") or ""
            metadata["year"] = str(oa_data.get("publication_year", ""))
            authorships = oa_data.get("authorships", [])
            if authorships:
                author_name = authorships[0].get("author", {}).get("display_name", "")
                # Get last name
                metadata["author"] = author_name.split(" ")[-1] if author_name else ""
        elif source == "unpaywall":
            metadata["title"] = oa_data.get("title") or ""
            metadata["year"] = str(oa_data.get("year", ""))
            z_authors = oa_data.get("z_authors", [])
            if z_authors:
                metadata["author"] = z_authors[0].get("family") or z_authors[0].get("name", "").split(" ")[-1]
        return metadata

    async def process_doi(self, session: aiohttp.ClientSession, http_client: AcademicHttpClient, doi: str) -> DownloadResult:
        """Processes a single DOI: resolves OA status via OpenAlex and Unpaywall, and downloads."""
        try:
            # 1. Try OpenAlex
            data = await self.fetch_openalex_metadata(http_client, doi)
            pdf_url = None
            metadata = {}
            
            if data:
                best_oa = data.get("best_oa_location", {})
                if best_oa:
                    pdf_url = best_oa.get("pdf_url")
                metadata = self.extract_metadata(data, "openalex")
                
            # 2. Fallback to Unpaywall if OpenAlex failed or had no PDF URL
            if not pdf_url:
                unpaywall_data = await self.fetch_unpaywall_metadata(http_client, doi)
                if unpaywall_data:
                    best_oa = unpaywall_data.get("best_oa_location", {})
                    if best_oa:
                        pdf_url = best_oa.get("url_for_pdf")
                    if not metadata.get("title"):
                        metadata = self.extract_metadata(unpaywall_data, "unpaywall")
            
            if not pdf_url:
                return DownloadResult(doi=doi, success=False, was_oa=False, error_message="Not Open Access or no PDF link found in OpenAlex/Unpaywall")
                
            dest_path = self.output_dir / self._safe_filename(doi, metadata)
            
            # Skip if already downloaded
            if dest_path.exists() and clean_invalid_pdf(dest_path):
                return DownloadResult(doi=doi, success=True, file_path=dest_path, was_oa=True, metadata=metadata)

            proxy_url = self.proxy_url

            # ---- Attempt 1: primary OA URL (optionally via institutional proxy) ----
            try:
                success = await self.download_pdf(session, pdf_url, dest_path, proxy_url)
            except Exception:
                success = False

            # ---- Attempt 2: publisher direct-PDF endpoint (Cloudflare/WAF bypass) ----
            if not success and settings.enable_publisher_direct_patterns:
                direct_url = resolve_doi_to_publisher_pdf(doi) or compute_direct_pdf_from_landing_url(pdf_url)
                if direct_url and direct_url != pdf_url:
                    if dest_path.exists():
                        dest_path.unlink()
                    try:
                        success = await self.download_pdf(session, direct_url, dest_path, proxy_url)
                    except Exception as e:
                        logger.debug(f"Publisher direct-PDF fallback failed for {doi} via {direct_url}: {e}")
                        success = False

            # ---- Attempt 3: same URLs again through the proxy (if not already proxied) ----
            if not success and proxy_url and not is_proxied_url(pdf_url, proxy_url):
                if dest_path.exists():
                    dest_path.unlink()
                for candidate in (pdf_url, resolve_doi_to_publisher_pdf(doi) or "", compute_direct_pdf_from_landing_url(pdf_url) or ""):
                    if not candidate:
                        continue
                    proxied = rewrite_via_proxy(candidate, proxy_url, style=self.proxy_style)
                    if proxied == candidate:
                        continue
                    try:
                        success = await self.download_pdf(session, proxied, dest_path, proxy_url)
                        if success:
                            break
                    except Exception as e:
                        logger.debug(f"Proxied fallback failed for {doi} via {proxied}: {e}")
                        success = False
            
            if success:
                return DownloadResult(doi=doi, success=True, file_path=dest_path, was_oa=True, metadata=metadata)
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

    async def ingest_pdf(self, http_client: AcademicHttpClient, pdf_path: Path, doi: str) -> DownloadResult:
        """Manually ingest a downloaded PDF into the toolkit."""
        import shutil
        
        if not pdf_path.exists():
            return DownloadResult(doi=doi, success=False, error_message="Provided PDF path does not exist.")
            
        try:
            # 1. Fetch metadata
            data = await self.fetch_openalex_metadata(http_client, doi)
            metadata = {}
            if data:
                metadata = self.extract_metadata(data, "openalex")
            else:
                unpaywall_data = await self.fetch_unpaywall_metadata(http_client, doi)
                if unpaywall_data:
                    metadata = self.extract_metadata(unpaywall_data, "unpaywall")
            
            # 2. Compute safe filename and copy
            dest_path = self.output_dir / self._safe_filename(doi, metadata)
            
            # Copy the file safely
            shutil.copy2(pdf_path, dest_path)
            
            # 3. Verify it is a valid PDF
            if not clean_invalid_pdf(dest_path):
                return DownloadResult(doi=doi, success=False, error_message="The provided file is not a valid PDF.")
            if self.structural_validation and not validate_pdf_structure(dest_path):
                return DownloadResult(
                    doi=doi,
                    success=False,
                    error_message="The provided file failed structural PDF validation.",
                )

            return DownloadResult(doi=doi, success=True, file_path=dest_path, metadata=metadata)
            
        except Exception as e:
            return DownloadResult(doi=doi, success=False, error_message=str(e))
