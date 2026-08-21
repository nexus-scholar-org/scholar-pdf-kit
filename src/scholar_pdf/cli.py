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

@app.command()
def download(
    dois: list[str] = typer.Option(None, "--doi", "-d", help="Specific DOI to download (can be specified multiple times)"),
    input_file: Path = typer.Option(None, "--input", "-i", help="JSON file containing results from scholar-search-kit"),
    output_dir: Path = typer.Option(Path("downloads"), "--output", "-o", help="Directory to save PDFs"),
    max_concurrent: int = typer.Option(5, "--max-concurrent", "-c", help="Maximum concurrent downloads")
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
    
    downloader = AsyncPDFDownloader(output_dir=output_dir)
    
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

if __name__ == "__main__":
    app()
