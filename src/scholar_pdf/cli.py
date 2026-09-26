# ruff: noqa: B008 - Typer intentionally uses option/argument calls in defaults.
import asyncio
import json
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

# Force UTF-8 on Windows to prevent Rich console UnicodeEncodeError on OEM code pages
if sys.platform == "win32":
    with suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

from .acquisition import PDFAcquisitionService, WorkspaceManagerCliAuditSink
from .acquisition_models import AcquisitionRunConfig, OperationStatus
from .config import settings
from .downloader import AsyncPDFDownloader
from .extract import DoclingEngine, GrobidEngine
from .extraction import PDFExtractionService
from .extraction_models import ExtractionRunConfig
from .publisher_patterns import PROXY_STYLES

app = typer.Typer(
    help=(
        "Scholar PDF Kit: automated PDF discovery, deterministic acquisition, "
        "and parent-bound text extraction."
    )
)
console = Console()

_ACQUISITION_EXIT_CODES = {
    OperationStatus.SUCCESS: 0,
    OperationStatus.PARTIAL: 2,
    OperationStatus.FAILED: 1,
    OperationStatus.CANCELLED: 130,
}


def _write_json_atomic(path: Path, payload: str) -> None:
    """Write a CLI summary without exposing a partially written JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _print_acquisition_human(outcome) -> None:
    """Render the canonical envelope without changing its status values."""

    table = Table(title="PDF Acquisition")
    table.add_column("Study", style="cyan")
    table.add_column("Acquisition", style="bold")
    table.add_column("Access")
    table.add_column("Document")
    table.add_column("Diagnostic")
    for item in outcome.data.item_outcomes:
        diagnostic = item.error.code if item.error is not None else ""
        if not diagnostic and item.warning is not None:
            diagnostic = item.warning.code
        table.add_row(
            item.study_id,
            item.acquisition_status.value,
            item.access_status.value,
            item.document_id or "-",
            diagnostic,
        )
    console.print(table)
    console.print(f"Operation status: {outcome.status.value}")
    reference = outcome.data.manifest_reference
    if reference is not None:
        console.print(
            f"Manifest: {reference.manifest_id} ({reference.workspace_relative_path})"
        )
    for error in outcome.errors:
        console.print(f"[red]{error.code}:[/red] {error.message}")


def _print_extraction_human(outcome) -> None:
    """Render the canonical E2 envelope without changing its status values."""

    table = Table(title="PDF Text Extraction (WP01-E2)")
    table.add_column("Study", style="cyan")
    table.add_column("Extraction")
    table.add_column("Content")
    table.add_column("Document")
    table.add_column("Engine")
    table.add_column("Diagnostic")
    for item in outcome.data.item_outcomes:
        diagnostic = item.error.code if item.error is not None else ""
        if not diagnostic and item.warning is not None:
            diagnostic = item.warning.code
        # The engine that actually produced the outcome: an item outcome exposes
        # `effective_engine` (the committed engine) and falls back to the
        # requested one when the chain never reached an effective engine.
        engine = item.effective_engine or item.requested_engine or "-"
        table.add_row(
            item.study_id,
            item.extraction_status.value,
            item.content_status.value if item.content_status is not None else "-",
            item.document_id or "-",
            engine,
            diagnostic,
        )
    console.print(table)
    console.print(f"Operation status: {outcome.status.value}")
    reference = outcome.data.manifest_reference
    if reference is not None:
        console.print(
            f"Sidecar: {reference.manifest_id} ({reference.workspace_relative_path})"
        )
    # `ExtractionOperationData` exposes the candidate as `candidate`; there is no
    # `document_manifest_candidate` attribute, so naming the wrong one would raise
    # an AttributeError on every real run.
    candidate = outcome.data.candidate
    if candidate is not None:
        # A candidate is an in-memory payload, not a file: it has no workspace
        # path, so only its identity and payload checksum are reported.  The
        # kit never publishes it and never claims Contract acceptance.
        console.print(
            "Contract candidate: "
            f"{candidate.artifact_id} "
            f"payload_sha256={candidate.payload_sha256} "
            f"(NON-AUTHORITATIVE; acceptance={candidate.contract_acceptance})"
        )
    for error in outcome.errors:
        console.print(f"[red]{error.code}:[/red] {error.message}")


@app.command("extract-run")
def extract_run(
    config: Path = typer.Argument(
        ..., exists=True, readable=True, help="ExtractionRunConfig JSON input."
    ),
    audit_logger: Path = typer.Option(
        ...,
        "--audit-logger",
        exists=True,
        readable=True,
        help="Canonical workspace-manager log_event.py path.",
    ),
    output: Path = typer.Option(
        None, "--output", "-o", help="Optional path for the JSON summary."
    ),
    human: bool = typer.Option(
        False, "--human", help="Render a human table in addition to JSON output."
    ),
) -> None:
    """Extract screened PDF text through the E2 domain service (authoritative)."""

    try:
        run_config = ExtractionRunConfig.model_validate_json(
            config.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise typer.BadParameter(
            f"could not read a valid ExtractionRunConfig: {error}"
        ) from error

    if not run_config.requests:
        raise typer.BadParameter("ExtractionRunConfig.requests must not be empty")

    audit_sink = WorkspaceManagerCliAuditSink(
        workspace_root=run_config.requests[0].workspace_root,
        logger_path=audit_logger,
    )
    service = PDFExtractionService.from_config(run_config, audit_sink=audit_sink)
    outcome = asyncio.run(service.extract(run_config.requests))
    serialized = outcome.model_dump_json(indent=2)
    if output is not None:
        _write_json_atomic(output, serialized)
    typer.echo(serialized)
    if human:
        _print_extraction_human(outcome)
    raise typer.Exit(code=_ACQUISITION_EXIT_CODES[outcome.status])


@app.command("acquire")
def acquire(
    config: Path = typer.Argument(
        ..., exists=True, readable=True, help="AcquisitionRunConfig JSON input."
    ),
    audit_logger: Path = typer.Option(
        ...,
        "--audit-logger",
        exists=True,
        readable=True,
        help="Canonical workspace-manager log_event.py path.",
    ),
    output: Path = typer.Option(
        None, "--output", "-o", help="Optional path for the JSON summary."
    ),
    human: bool = typer.Option(
        False, "--human", help="Render a human table in addition to JSON output."
    ),
) -> None:
    """Acquire one or more parent-bound PDFs through the E1 domain service."""

    try:
        run_config = AcquisitionRunConfig.model_validate_json(
            config.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise typer.BadParameter(
            f"could not read a valid AcquisitionRunConfig: {error}"
        ) from error

    audit_sink = WorkspaceManagerCliAuditSink(
        workspace_root=run_config.requests[0].workspace_root,
        logger_path=audit_logger,
    )
    service = PDFAcquisitionService.from_config(run_config, audit_sink=audit_sink)
    outcome = asyncio.run(service.acquire(run_config.requests))
    serialized = outcome.model_dump_json(indent=2)
    if output is not None:
        _write_json_atomic(output, serialized)
    typer.echo(serialized)
    if human:
        _print_acquisition_human(outcome)
    raise typer.Exit(code=_ACQUISITION_EXIT_CODES[outcome.status])


def _export_results(export_format: str, output_dir: Path, successful_results: list):
    """Helper to export results, appending to existing files if they exist."""
    if not successful_results:
        return

    export_path = output_dir / f"download_summary.{export_format}"

    if export_format.lower() == "json":
        export_data = []
        if export_path.exists():
            with (
                suppress(OSError, json.JSONDecodeError),
                open(export_path, "r", encoding="utf-8") as f,
            ):
                export_data = json.load(f)

        # Append new successful results, avoiding duplicate DOIs
        existing_dois = {item["doi"] for item in export_data}
        for res in successful_results:
            if res.doi not in existing_dois and res.metadata:
                export_data.append(
                    {
                        "doi": res.doi,
                        "file_path": str(res.file_path),
                        "metadata": res.metadata,
                    }
                )

        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2)
        console.print(f"[green]Exported metadata to {export_path}[/green]")

    elif export_format.lower() == "bibtex":
        bibtex_entries = []
        for res in successful_results:
            if not res.metadata:
                continue
            md = res.metadata
            author = md.get("author", "Unknown")
            year = md.get("year", "Unknown")
            title = md.get("title", "Unknown")
            key = f"{author}{year}".replace(" ", "")
            bibtex = f"@article{{{key},\n  title={{{title}}},\n  author={{{author}}},\n  year={{{year}}},\n  doi={{{res.doi}}}\n}}\n"
            bibtex_entries.append(bibtex)

        with open(export_path, "a", encoding="utf-8") as f:
            f.write("\n".join(bibtex_entries) + "\n")
        console.print(f"[green]Exported BibTeX to {export_path}[/green]")
    else:
        console.print(
            f"[yellow]Unknown export format: {export_format}. Use 'json' or 'bibtex'.[/yellow]"
        )


@app.command("download")
def download(
    dois: list[str] = typer.Option(
        None,
        "--doi",
        "-d",
        help="Specific DOI to download (can be specified multiple times)",
    ),
    input_file: Path = typer.Option(
        None,
        "--input",
        "-i",
        help="JSON file containing results from scholar-search-kit",
    ),
    output_dir: Path = typer.Option(
        Path("downloads"), "--output", "-o", help="Directory to save PDFs"
    ),
    max_concurrent: int = typer.Option(
        5, "--max-concurrent", "-c", help="Maximum concurrent downloads"
    ),
    smart_names: bool = typer.Option(
        False, "--smart-names", help="Rename downloaded PDFs using Author_Year_Title"
    ),
    export_format: str = typer.Option(
        None,
        "--export",
        help="Export successfully downloaded metadata (json or bibtex)",
    ),
    proxy: str = typer.Option(
        None,
        "--proxy",
        help="Institutional proxy URL (overrides PROXY_URL / .env), e.g. https://www.sndl1.arn.dz",
    ),
    proxy_style: str = typer.Option(
        "auto", "--proxy-style", help="Proxy style: auto | ezproxy | subdomain | prefix"
    ),
    strict_validate: bool = typer.Option(
        False,
        "--strict-validate",
        help="Require pypdf structural validation of downloads",
    ),
):
    """Resolve DOIs via OpenAlex and download Open Access PDFs."""

    if proxy_style not in PROXY_STYLES:
        raise typer.BadParameter(
            f"--proxy-style must be one of {', '.join(PROXY_STYLES)}"
        )
    settings.download_dir = output_dir
    settings.max_concurrent_downloads = max_concurrent

    doi_list = list(dois) if dois else []

    # Parse input file if provided
    if input_file and input_file.exists():
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    doi = None
                    if "external_ids" in item and item["external_ids"].get("doi"):
                        doi = item["external_ids"]["doi"]
                    elif "doi" in item:
                        doi = item["doi"]

                    if doi and doi not in doi_list:
                        doi_list.append(doi)
        except (
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as e:
            console.print(f"[bold red]Error parsing input file:[/bold red] {e}")
            raise typer.Exit(1)

    if not doi_list:
        console.print("[bold yellow]No DOIs provided to download.[/bold yellow]")
        raise typer.Exit(0)

    console.print(
        f"[bold blue]Starting download process for {len(doi_list)} DOIs...[/bold blue]"
    )

    downloader = AsyncPDFDownloader(
        output_dir=output_dir,
        use_smart_names=smart_names,
        proxy_url=proxy,
        proxy_style=proxy_style,
        structural_validation=strict_validate,
    )

    async def run_downloads():
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Downloading PDFs...", total=len(doi_list))

            import aiohttp
            from scholar_search.http_client import AcademicHttpClient

            results = []
            http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
            try:
                async with aiohttp.ClientSession() as session:
                    # We process concurrently but update progress bar as each finishes
                    tasks = [
                        downloader.process_doi(session, http_client, doi)
                        for doi in doi_list
                    ]
                    for coro in asyncio.as_completed(tasks):
                        res = await coro
                        results.append(res)
                        progress.advance(task)
            finally:
                await http_client.close()

            return results

    # Run the event loop
    results = asyncio.run(run_downloads())

    # Display summary
    table = Table(title="Download Summary")
    table.add_column("DOI", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Details")

    success_count = 0
    for res in results:
        if res.success:
            success_count += 1
            table.add_row(res.doi, "[green]Success[/green]", str(res.file_path))
        elif res.access_status == "VERIFIED_OPEN_ACCESS":
            table.add_row(
                res.doi,
                "[red]Failed[/red]",
                res.error_message or "Open-access source could not be acquired",
            )
        elif res.access_status == "RESTRICTED_CONFIRMED":
            table.add_row(
                res.doi,
                "[red]Restricted[/red]",
                res.error_message or "Access restricted",
            )
        else:
            table.add_row(
                res.doi,
                "[yellow]Unresolved[/yellow]",
                res.error_message or "No legal-copy determination was made",
            )

    console.print(table)
    console.print(
        f"[bold green]Successfully downloaded {success_count}/{len(doi_list)} PDFs.[/bold green]"
    )

    if export_format:
        successful_results = [res for res in results if res.success]
        _export_results(export_format, output_dir, successful_results)
    if success_count != len(doi_list):
        raise typer.Exit(1)


@app.command("ingest")
def ingest(
    pdf_path: Path = typer.Argument(
        ..., help="Path to the manually downloaded PDF file"
    ),
    doi: str = typer.Option(..., "--doi", "-d", help="The DOI associated with the PDF"),
    output_dir: Path = typer.Option(
        Path("downloads"), "--output", "-o", help="Directory to save PDFs"
    ),
    smart_names: bool = typer.Option(
        False, "--smart-names", help="Rename downloaded PDF using Author_Year_Title"
    ),
    export_format: str = typer.Option(
        None, "--export", help="Export successfully ingested metadata (json or bibtex)"
    ),
    proxy: str = typer.Option(
        None, "--proxy", help="Institutional proxy URL (overrides PROXY_URL / .env)"
    ),
    proxy_style: str = typer.Option(
        "auto", "--proxy-style", help="Proxy style: auto | ezproxy | subdomain | prefix"
    ),
    strict_validate: bool = typer.Option(
        False, "--strict-validate", help="Require pypdf structural validation on ingest"
    ),
):
    """Manually ingest a PDF into the toolkit, bypassing download."""
    if proxy_style not in PROXY_STYLES:
        raise typer.BadParameter(
            f"--proxy-style must be one of {', '.join(PROXY_STYLES)}"
        )
    settings.download_dir = output_dir

    console.print(f"[bold blue]Ingesting {pdf_path} for DOI {doi}...[/bold blue]")
    downloader = AsyncPDFDownloader(
        output_dir=output_dir,
        use_smart_names=smart_names,
        proxy_url=proxy,
        proxy_style=proxy_style,
        structural_validation=strict_validate,
    )

    async def run_ingest():
        from scholar_search.http_client import AcademicHttpClient

        http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
        try:
            return await downloader.ingest_pdf(http_client, pdf_path, doi)
        finally:
            await http_client.close()

    res = asyncio.run(run_ingest())

    if res.success:
        console.print(
            f"[bold green]Successfully ingested PDF to {res.file_path}[/bold green]"
        )
        if export_format:
            _export_results(export_format, output_dir, [res])
    else:
        console.print(f"[bold red]Failed to ingest PDF: {res.error_message}[/bold red]")


@app.command("extract")
def extract(
    pdf_path: Path = typer.Argument(
        ..., help="Path to the PDF file or directory of PDFs"
    ),
    output_dir: Path = typer.Option(
        Path("markdown"),
        "--output",
        "-o",
        help="Directory to save the extracted Markdown",
    ),
    engine: str = typer.Option("docling", help="Extraction engine: docling or grobid"),
    grobid_url: str = typer.Option(
        "http://localhost:8070", help="Grobid service URL if using grobid"
    ),
):
    """LEGACY, NON-AUTHORITATIVE: extract raw Markdown from a PDF.

    This command writes to an arbitrary output directory and produces no
    acquisition/extraction lineage, no bound frontmatter, and no sidecar
    manifest. It can never write the authoritative `extracted/<document_id>.md`
    contract path. Use `extract-run` for the WP01-E2 deterministic pipeline.
    """

    console.print(
        "[bold yellow]Warning:[/bold yellow] 'extract' is non-authoritative. "
        "Outputs are unversioned and untraceable to a screened document. "
        "Use 'extract-run' for the WP01-E2 deterministic pipeline."
    )
    if not pdf_path.exists():
        console.print(f"[red]Path does not exist: {pdf_path}[/red]")
        raise typer.Exit(1)

    pdfs = []
    if pdf_path.is_file() and pdf_path.suffix.lower() == ".pdf":
        pdfs.append(pdf_path)
    elif pdf_path.is_dir():
        pdfs.extend(list(pdf_path.glob("*.pdf")))
    else:
        console.print(
            "[red]Input must be a PDF file or a directory containing PDFs.[/red]"
        )
        raise typer.Exit(1)

    if not pdfs:
        console.print("[yellow]No PDFs found.[/yellow]")
        raise typer.Exit(0)

    console.print(
        f"[bold blue]Extracting {len(pdfs)} PDFs using {engine}...[/bold blue]"
    )

    success = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting...", total=len(pdfs))

        for pdf in pdfs:
            progress.update(task, description=f"Extracting {pdf.name}...")
            try:
                if engine.lower() == "docling":
                    out = DoclingEngine.extract_markdown(pdf, output_dir)
                    console.print(f"[green]Extracted {pdf.name} -> {out}[/green]")
                elif engine.lower() == "grobid":
                    out = GrobidEngine.extract_markdown(pdf, output_dir, grobid_url)
                    console.print(f"[green]Extracted {pdf.name} -> {out}[/green]")
                else:
                    console.print(f"[red]Unknown engine: {engine}[/red]")
                    raise typer.Exit(1)
                success += 1
            except Exception as e:  # noqa: BLE001 - report each extractor failure
                console.print(f"[red]Failed to extract {pdf.name}: {e}[/red]")
            finally:
                progress.advance(task)

    console.print(
        f"[bold green]Successfully extracted {success}/{len(pdfs)} files to {output_dir}.[/bold green]"
    )


if __name__ == "__main__":
    app()
