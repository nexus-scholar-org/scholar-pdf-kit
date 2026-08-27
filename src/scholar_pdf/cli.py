import typer
import asyncio
import json
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from .downloader import AsyncPDFDownloader
from .config import settings

app = typer.Typer(help="Scholar PDF Kit: Bypassing paywalls for automated Open Access discovery.")
console = Console()

def _export_results(export_format: str, output_dir: Path, successful_results: list):
    """Helper to export results, appending to existing files if they exist."""
    if not successful_results:
        return
        
    export_path = output_dir / f"download_summary.{export_format}"
    
    if export_format.lower() == "json":
        export_data = []
        if export_path.exists():
            try:
                with open(export_path, "r", encoding="utf-8") as f:
                    export_data = json.load(f)
            except Exception:
                pass
                
        # Append new successful results, avoiding duplicate DOIs
        existing_dois = {item["doi"] for item in export_data}
        for res in successful_results:
            if res.doi not in existing_dois and res.metadata:
                export_data.append({
                    "doi": res.doi,
                    "file_path": str(res.file_path),
                    "metadata": res.metadata
                })
                
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
        console.print(f"[yellow]Unknown export format: {export_format}. Use 'json' or 'bibtex'.[/yellow]")

@app.command("download")
def download(
    dois: list[str] = typer.Option(None, "--doi", "-d", help="Specific DOI to download (can be specified multiple times)"),
    input_file: Path = typer.Option(None, "--input", "-i", help="JSON file containing results from scholar-search-kit"),
    output_dir: Path = typer.Option(Path("downloads"), "--output", "-o", help="Directory to save PDFs"),
    max_concurrent: int = typer.Option(5, "--max-concurrent", "-c", help="Maximum concurrent downloads"),
    smart_names: bool = typer.Option(False, "--smart-names", help="Rename downloaded PDFs using Author_Year_Title"),
    export_format: str = typer.Option(None, "--export", help="Export successfully downloaded metadata (json or bibtex)")
):
    """Resolve DOIs via OpenAlex and download Open Access PDFs."""
    
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
        except Exception as e:
            console.print(f"[bold red]Error parsing input file:[/bold red] {e}")
            raise typer.Exit(1)
            
    if not doi_list:
        console.print("[bold yellow]No DOIs provided to download.[/bold yellow]")
        raise typer.Exit(0)
        
    console.print(f"[bold blue]Starting download process for {len(doi_list)} DOIs...[/bold blue]")
    
    downloader = AsyncPDFDownloader(output_dir=output_dir, use_smart_names=smart_names)
    
    async def run_downloads():
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("[cyan]Downloading PDFs...", total=len(doi_list))
            
            import aiohttp
            from scholar_search.http_client import AcademicHttpClient
            
            results = []
            http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
            
            async with aiohttp.ClientSession() as session:
                # We process concurrently but update progress bar as each finishes
                tasks = [downloader.process_doi(session, http_client, doi) for doi in doi_list]
                for coro in asyncio.as_completed(tasks):
                        res = await coro
                        results.append(res)
                        progress.advance(task)
                        
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
        elif res.was_oa:
            table.add_row(res.doi, "[red]Failed[/red]", res.error_message or "Download error")
        else:
            table.add_row(res.doi, "[yellow]Paywalled[/yellow]", "Not Open Access")
            
    console.print(table)
    console.print(f"[bold green]Successfully downloaded {success_count}/{len(doi_list)} PDFs.[/bold green]")
    
    if export_format:
        successful_results = [res for res in results if res.success]
        _export_results(export_format, output_dir, successful_results)

@app.command("ingest")
def ingest(
    pdf_path: Path = typer.Argument(..., help="Path to the manually downloaded PDF file"),
    doi: str = typer.Option(..., "--doi", "-d", help="The DOI associated with the PDF"),
    output_dir: Path = typer.Option(Path("downloads"), "--output", "-o", help="Directory to save PDFs"),
    smart_names: bool = typer.Option(False, "--smart-names", help="Rename downloaded PDF using Author_Year_Title"),
    export_format: str = typer.Option(None, "--export", help="Export successfully ingested metadata (json or bibtex)")
):
    """Manually ingest a PDF into the toolkit, bypassing download."""
    settings.download_dir = output_dir
    
    console.print(f"[bold blue]Ingesting {pdf_path} for DOI {doi}...[/bold blue]")
    downloader = AsyncPDFDownloader(output_dir=output_dir, use_smart_names=smart_names)
    
    async def run_ingest():
        from scholar_search.http_client import AcademicHttpClient
        http_client = AcademicHttpClient(name="openalex-pdf", rate_limit=10)
        return await downloader.ingest_pdf(http_client, pdf_path, doi)
        
    res = asyncio.run(run_ingest())
    
    if res.success:
        console.print(f"[bold green]Successfully ingested PDF to {res.file_path}[/bold green]")
        if export_format:
            _export_results(export_format, output_dir, [res])
    else:
        console.print(f"[bold red]Failed to ingest PDF: {res.error_message}[/bold red]")

if __name__ == "__main__":
    app()
