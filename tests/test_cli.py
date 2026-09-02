import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from typer.testing import CliRunner

from scholar_pdf.cli import app
from scholar_pdf.downloader import DownloadResult

runner = CliRunner()


@patch("scholar_pdf.cli.AsyncPDFDownloader.process_doi", new_callable=AsyncMock)
def test_cli_download_single_doi(mock_process_doi, tmp_path):
    mock_process_doi.return_value = DownloadResult(
        doi="10.1234/test", success=True, file_path=tmp_path / "test.pdf", was_oa=True
    )

    result = runner.invoke(app, ["download", "--doi", "10.1234/test", "--output", str(tmp_path)])

    assert result.exit_code == 0
    assert "Starting download process for 1 DOIs" in result.stdout
    assert "Success" in result.stdout
    assert "10.1234/test" in result.stdout


@patch("scholar_pdf.cli.AsyncPDFDownloader.process_doi", new_callable=AsyncMock)
def test_cli_download_from_file(mock_process_doi, tmp_path):
    input_file = tmp_path / "input.json"
    input_data = [
        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1111/1"},
        {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.2222/2"},
        {"id": "https://openalex.org/W3"},  # No DOI
    ]
    with open(input_file, "w", encoding="utf-8") as f:
        json.dump(input_data, f)

    mock_process_doi.side_effect = [
        DownloadResult(
            doi="10.1111/1", success=True, file_path=tmp_path / "1.pdf", was_oa=True
        ),
        DownloadResult(
            doi="10.2222/2",
            success=False,
            error_message="Not Open Access",
            was_oa=False,
        ),
    ]

    result = runner.invoke(app, ["download", "--input", str(input_file), "--output", str(tmp_path)])

    assert result.exit_code == 0
    assert "Starting download process for 2 DOIs" in result.stdout
    assert "Success" in result.stdout
    assert "Paywalled" in result.stdout


def test_cli_no_input():
    result = runner.invoke(app, ["download"])
    assert result.exit_code == 0
    assert "No DOIs provided to download" in result.stdout
