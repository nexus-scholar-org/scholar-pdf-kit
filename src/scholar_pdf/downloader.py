from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
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
from .validator import is_valid_pdf, validate_pdf_structure

if TYPE_CHECKING:
    from scholar_search.http_client import AcademicHttpClient

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    doi: str
    success: bool
    file_path: Path | None = None
    error_message: str | None = None
    # Deprecated compatibility projection.  Use access_status/status for new
    # callers; a successful HTTP response alone never sets this to True.
    was_oa: bool = False
    metadata: dict | None = None
    access_status: str | None = None
    status: str | None = None


class AsyncPDFDownloader:
    """Asynchronous PDF Downloader using aiohttp."""

    def __init__(
        self,
        output_dir: Path | None = None,
        use_smart_names: bool = False,
        proxy_url: str | None = None,
        proxy_style: str = "auto",
        structural_validation: bool | None = None,
        institutional_gateway_url: str | None = None,
        forward_proxy_url: str | None = None,
    ):
        self.output_dir = output_dir or settings.download_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.use_smart_names = use_smart_names
        # ``proxy_url`` is retained as a deprecated alias for the historical
        # gateway configuration.  Transport proxying is now explicit.
        self.institutional_gateway_url = (
            institutional_gateway_url
            if institutional_gateway_url is not None
            else (proxy_url if proxy_url is not None else settings.proxy_url) or ""
        )
        self.forward_proxy_url = forward_proxy_url or ""
        self.proxy_url = self.institutional_gateway_url
        self.proxy_style = proxy_style or settings.proxy_style or "auto"
        self.structural_validation = (
            settings.pdf_structural_validation
            if structural_validation is None
            else structural_validation
        )
        if (
            self.institutional_gateway_url
            and self.forward_proxy_url
            and self.institutional_gateway_url == self.forward_proxy_url
        ):
            raise ValueError(
                "institutional_gateway_url and forward_proxy_url must be distinct"
            )

    def _safe_filename(self, doi: str, metadata: dict | None = None) -> str:
        """Converts a DOI to a safe filename, optionally using metadata."""
        if self.use_smart_names and metadata:
            title = metadata.get("title", "")
            author = metadata.get("author", "")
            year = metadata.get("year", "0000")

            if title and author:
                # Sanitize title and author
                safe_title = "".join(
                    c for c in title[:50] if c.isalnum() or c in (" ", "_")
                ).replace(" ", "_")
                safe_author = "".join(
                    c for c in author if c.isalnum() or c in (" ", "_")
                ).replace(" ", "_")
                return f"{year}_{safe_author}_{safe_title}.pdf"

        safe_doi = doi.replace("/", "_").replace("\\", "_").replace(":", "_")
        return f"{safe_doi}.pdf"

    def _content_path(self, digest: str) -> Path:
        """Return the authoritative content/identity-addressed final path."""

        return self.output_dir / f"DOC-{digest[:32]}.pdf"

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _candidate_is_valid(self, path: Path) -> bool:
        if not is_valid_pdf(path):
            return False
        return not self.structural_validation or validate_pdf_structure(path)

    def _new_staging_path(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".pdf-acquisition-", suffix=".tmp", dir=self.output_dir
        )
        os.close(descriptor)
        staging = Path(name)
        staging.unlink(missing_ok=True)
        return staging

    def _promote_staging(self, staging: Path, final: Path) -> bool:
        """Publish a validated file without replacing a concurrent writer."""

        digest = self._digest(staging)
        if final.is_symlink():
            raise OSError("content destination must not be a symlink")
        try:
            if staging.resolve(strict=True) == final.resolve(strict=True):
                return True
        except OSError:
            pass
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            if (
                final.stat().st_size != staging.stat().st_size
                or self._digest(final) != digest
                or not self._candidate_is_valid(final)
            ):
                raise OSError("content-addressed destination is occupied")
            staging.unlink(missing_ok=True)
            return False
        try:
            # A hard link publishes the already-validated inode atomically and
            # cannot overwrite a destination created by another process.
            os.link(staging, final)
        except FileExistsError:
            if (
                final.is_symlink()
                or final.stat().st_size != staging.stat().st_size
                or self._digest(final) != digest
                or not self._candidate_is_valid(final)
            ):
                raise OSError("content-addressed destination is occupied")
            staging.unlink(missing_ok=True)
            return False
        staging.unlink(missing_ok=True)
        return True

    def _promote_candidate(self, staging: Path) -> tuple[Path, bool]:
        """Promote validated bytes to their content identity path."""

        digest = self._digest(staging)
        final = self._content_path(digest)
        return final, self._promote_staging(staging, final)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def download_pdf(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest_path: Path,
        proxy_url: str = "",
        forward_proxy_url: str | None = None,
    ) -> bool:
        """Download, validate, and atomically publish to ``dest_path``."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/pdf,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=settings.download_timeout)
        transport_proxy = (
            forward_proxy_url if forward_proxy_url is not None else proxy_url
        ).strip()
        staging = self._new_staging_path()
        if dest_path.is_symlink():
            staging.unlink(missing_ok=True)
            raise OSError("destination must not be a symlink")
        try:
            async with (
                self.semaphore,
                session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=True,
                    proxy=transport_proxy or None,
                ) as response,
            ):
                response.raise_for_status()
                with staging.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(8192):
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())

            if not self._candidate_is_valid(staging):
                return False
            self._promote_staging(staging, dest_path)
            return True
        finally:
            staging.unlink(missing_ok=True)

    async def fetch_openalex_metadata(
        self, http_client: AcademicHttpClient, doi: str
    ) -> dict | None:
        url = f"https://api.openalex.org/works/https://doi.org/{doi}"
        try:
            response = await http_client.get(url, params={"mailto": settings.mailto})
            if response.status_code == 200:
                return response.json()
        except Exception as error:  # noqa: BLE001 - provider adapters vary
            logger.debug("OpenAlex metadata lookup failed for %s: %s", doi, error)
        return None

    async def fetch_unpaywall_metadata(
        self, http_client: AcademicHttpClient, doi: str
    ) -> dict | None:
        url = f"https://api.unpaywall.org/v2/{doi}"
        try:
            response = await http_client.get(url, params={"email": settings.mailto})
            if response.status_code == 200:
                return response.json()
        except Exception as error:  # noqa: BLE001 - provider adapters vary
            logger.debug("Unpaywall metadata lookup failed for %s: %s", doi, error)
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
                metadata["author"] = (
                    z_authors[0].get("family")
                    or z_authors[0].get("name", "").split(" ")[-1]
                )
        return metadata

    @staticmethod
    def _provider_access_status(*payloads: dict | None) -> str:
        """Project only explicit provider OA evidence, never HTTP success."""

        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            if payload.get("restricted") is True and any(
                isinstance(payload.get(key), str) and payload[key].strip()
                for key in ("source", "publisher")
            ):
                return "RESTRICTED_CONFIRMED"
            for location_key in ("best_oa_location", "primary_location"):
                location = payload.get(location_key)
                if isinstance(location, dict) and (
                    location.get("pdf_url") or location.get("url_for_pdf")
                ):
                    return "VERIFIED_OPEN_ACCESS"
            open_access = payload.get("open_access")
            if isinstance(open_access, dict) and open_access.get("is_oa") is True:
                return "VERIFIED_OPEN_ACCESS"
            if payload.get("is_oa") is True or payload.get("url_for_pdf"):
                return "VERIFIED_OPEN_ACCESS"
            if payload.get("oa_status") in {"gold", "green", "hybrid", "bronze"}:
                return "VERIFIED_OPEN_ACCESS"
        return "UNRESOLVED"

    async def process_doi(
        self, session: aiohttp.ClientSession, http_client: AcademicHttpClient, doi: str
    ) -> DownloadResult:
        """Resolve OA evidence and atomically acquire a PDF for one DOI."""
        try:
            openalex_data = await self.fetch_openalex_metadata(http_client, doi)
            metadata: dict = {}
            pdf_url = None
            if openalex_data:
                for location_key in ("best_oa_location", "primary_location"):
                    location = openalex_data.get(location_key) or {}
                    if isinstance(location, dict):
                        pdf_url = location.get("pdf_url") or location.get("url_for_pdf")
                        if pdf_url:
                            break
                pdf_url = pdf_url or openalex_data.get("url_for_pdf")
                metadata = self.extract_metadata(openalex_data, "openalex")

            unpaywall_data = None
            if not pdf_url:
                unpaywall_data = await self.fetch_unpaywall_metadata(http_client, doi)
                if unpaywall_data:
                    for location_key in ("best_oa_location", "primary_location"):
                        location = unpaywall_data.get(location_key) or {}
                        if isinstance(location, dict):
                            pdf_url = location.get("url_for_pdf") or location.get(
                                "pdf_url"
                            )
                            if pdf_url:
                                break
                    pdf_url = pdf_url or unpaywall_data.get("url_for_pdf")
                    if not metadata.get("title"):
                        metadata = self.extract_metadata(unpaywall_data, "unpaywall")

            access_status = self._provider_access_status(openalex_data, unpaywall_data)
            was_oa = access_status == "VERIFIED_OPEN_ACCESS"
            if not pdf_url:
                return DownloadResult(
                    doi=doi,
                    success=False,
                    was_oa=was_oa,
                    status="UNRESOLVED",
                    access_status=access_status,
                    metadata=metadata or None,
                    error_message=(
                        "No legal open-access PDF URL was resolved from "
                        "OpenAlex/Unpaywall; this is not a paywall determination."
                    ),
                )

            candidates: list[str] = [pdf_url]
            direct_url = None
            if settings.enable_publisher_direct_patterns:
                direct_url = resolve_doi_to_publisher_pdf(doi) or (
                    compute_direct_pdf_from_landing_url(pdf_url)
                )
                if direct_url and direct_url != pdf_url:
                    candidates.append(direct_url)

            if self.institutional_gateway_url and not is_proxied_url(
                pdf_url, self.institutional_gateway_url
            ):
                for candidate in list(candidates):
                    proxied = rewrite_via_proxy(
                        candidate,
                        self.institutional_gateway_url,
                        style=self.proxy_style,
                    )
                    if proxied != candidate:
                        candidates.append(proxied)

            last_error: str | None = None
            for candidate in candidates:
                candidate_path = self._new_staging_path()
                try:
                    success = await self.download_pdf(
                        session,
                        candidate,
                        candidate_path,
                        forward_proxy_url=self.forward_proxy_url or None,
                    )
                    if not success:
                        last_error = "Failed to download or invalid PDF"
                        continue
                    final_path, _ = self._promote_candidate(candidate_path)
                    return DownloadResult(
                        doi=doi,
                        success=True,
                        file_path=final_path,
                        was_oa=was_oa,
                        status="ACQUIRED",
                        access_status=access_status,
                        metadata=metadata or None,
                    )
                except Exception as error:  # noqa: BLE001 - provider adapters vary
                    last_error = str(error)
                    logger.debug(
                        "PDF candidate failed for %s via a redacted provider URL: %s",
                        doi,
                        error,
                    )
                finally:
                    candidate_path.unlink(missing_ok=True)

            return DownloadResult(
                doi=doi,
                success=False,
                was_oa=was_oa,
                status="FAILED",
                access_status=access_status,
                metadata=metadata or None,
                error_message=last_error or "Failed to download or invalid PDF",
            )
        except Exception as error:  # noqa: BLE001 - provider adapters vary
            return DownloadResult(
                doi=doi,
                success=False,
                was_oa=False,
                status="FAILED",
                access_status="UNRESOLVED",
                error_message=str(error),
            )

    async def download_batch(self, dois: list[str]) -> list[DownloadResult]:
        """Downloads a batch of DOIs concurrently and closes owned clients."""
        from scholar_search.http_client import AcademicHttpClient

        http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
        try:
            async with aiohttp.ClientSession() as session:
                tasks = [self.process_doi(session, http_client, doi) for doi in dois]
                return await asyncio.gather(*tasks)
        finally:
            await http_client.close()

    async def ingest_pdf(
        self, http_client: AcademicHttpClient, pdf_path: Path, doi: str
    ) -> DownloadResult:
        """Atomically ingest a local PDF without overwriting existing bytes."""
        if not pdf_path.exists():
            return DownloadResult(
                doi=doi,
                success=False,
                status="FAILED",
                access_status="UNRESOLVED",
                error_message="Provided PDF path does not exist.",
            )

        staging: Path | None = None
        try:
            openalex_data = await self.fetch_openalex_metadata(http_client, doi)
            unpaywall_data = None
            metadata: dict = {}
            if openalex_data:
                metadata = self.extract_metadata(openalex_data, "openalex")
            else:
                unpaywall_data = await self.fetch_unpaywall_metadata(http_client, doi)
                if unpaywall_data:
                    metadata = self.extract_metadata(unpaywall_data, "unpaywall")

            access_status = self._provider_access_status(openalex_data, unpaywall_data)
            staging = self._new_staging_path()
            shutil.copyfile(pdf_path, staging)
            if not self._candidate_is_valid(staging):
                return DownloadResult(
                    doi=doi,
                    success=False,
                    status="FAILED",
                    access_status=access_status,
                    metadata=metadata or None,
                    error_message="The provided file is not a valid PDF.",
                )
            final_path, _ = self._promote_candidate(staging)
            return DownloadResult(
                doi=doi,
                success=True,
                file_path=final_path,
                was_oa=access_status == "VERIFIED_OPEN_ACCESS",
                status="ACQUIRED",
                access_status=access_status,
                metadata=metadata or None,
            )
        except Exception as error:  # noqa: BLE001 - provider adapters vary
            return DownloadResult(
                doi=doi,
                success=False,
                status="FAILED",
                access_status="UNRESOLVED",
                error_message=str(error),
            )
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
