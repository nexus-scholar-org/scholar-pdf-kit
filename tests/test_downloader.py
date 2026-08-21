import pytest
import aiohttp
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from scholar_pdf.downloader import AsyncPDFDownloader
from scholar_search.http_client import AcademicHttpClient

@pytest.fixture
def temp_output_dir(tmp_path):
    return tmp_path / "downloads"

@pytest.fixture
def downloader(temp_output_dir):
    return AsyncPDFDownloader(output_dir=temp_output_dir)

@pytest.fixture
def mock_http_client():
    return MagicMock(spec=AcademicHttpClient)

@pytest.mark.asyncio
async def test_process_doi_success(downloader, mock_http_client, temp_output_dir):
    doi = "10.1234/test.1"
    
    # Mock OpenAlex response
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "best_oa_location": {
            "pdf_url": "https://example.com/test.pdf"
        }
    }
    mock_http_client.get.return_value = mock_response
    
    # Mock aiohttp session and response
    mock_session = AsyncMock(spec=aiohttp.ClientSession)
    mock_get = AsyncMock()
    mock_session.get.return_value = mock_get
    
    mock_get.__aenter__.return_value = mock_get
    mock_get.headers = {"Content-Type": "application/pdf"}
    
    # Mock iter_chunked
    async def mock_iter_chunked(*args, **kwargs):
        yield b"%PDF-1.4\n" # Valid PDF header
        yield b"Mock PDF Content"
        
    mock_get.content.iter_chunked = mock_iter_chunked
    
    result = await downloader.process_doi(mock_session, mock_http_client, doi)
    
    assert result.success is True
    assert result.doi == doi
    assert result.was_oa is True
    assert result.file_path == temp_output_dir / "10.1234_test.1.pdf"
    assert result.file_path.exists()

@pytest.mark.asyncio
async def test_process_doi_not_oa(downloader, mock_http_client):
    doi = "10.1234/test.2"
    
    # Mock OpenAlex response (No PDF URL)
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "best_oa_location": {
            "pdf_url": None
        }
    }
    mock_http_client.get.return_value = mock_response
    
    mock_session = AsyncMock(spec=aiohttp.ClientSession)
    
    result = await downloader.process_doi(mock_session, mock_http_client, doi)
    
    assert result.success is False
    assert result.was_oa is False
    assert result.error_message == "Not Open Access or no PDF link"

@pytest.mark.asyncio
async def test_process_doi_not_found(downloader, mock_http_client):
    doi = "10.1234/test.3"
    
    # Mock OpenAlex response (404 Not Found exception)
    mock_http_client.get.side_effect = Exception("404 Not Found")
    
    mock_session = AsyncMock(spec=aiohttp.ClientSession)
    
    result = await downloader.process_doi(mock_session, mock_http_client, doi)
    
    assert result.success is False
    assert result.error_message == "DOI not found in OpenAlex"

@pytest.mark.asyncio
async def test_process_doi_invalid_pdf(downloader, mock_http_client):
    doi = "10.1234/test.4"
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "best_oa_location": {
            "pdf_url": "https://example.com/paywall.html"
        }
    }
    mock_http_client.get.return_value = mock_response
    
    mock_session = AsyncMock(spec=aiohttp.ClientSession)
    mock_get = AsyncMock()
    mock_session.get.return_value = mock_get
    
    mock_get.__aenter__.return_value = mock_get
    mock_get.headers = {"Content-Type": "text/html"} # Paywall hit!
    
    result = await downloader.process_doi(mock_session, mock_http_client, doi)
    
    assert result.success is False
    assert result.was_oa is True
    assert result.error_message == "Failed to download or invalid PDF"
