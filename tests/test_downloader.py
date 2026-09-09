from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
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
    client = MagicMock(spec=AcademicHttpClient)
    return client


@pytest.mark.asyncio
async def test_process_doi_success(downloader, mock_http_client, temp_output_dir):
    doi = "10.1234/test.1"

    # Mock OpenAlex response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "best_oa_location": {"pdf_url": "https://example.com/test.pdf"},
        "title": "Test Title",
    }
    mock_http_client.get = AsyncMock(return_value=mock_response)

    # Mock aiohttp session and response
    mock_session = AsyncMock(spec=aiohttp.ClientSession)
    mock_get = AsyncMock()
    mock_get.raise_for_status = MagicMock()
    mock_session.get.return_value = mock_get

    mock_get.__aenter__.return_value = mock_get
    mock_get.headers = {"Content-Type": "application/pdf"}

    # Mock iter_chunked
    async def mock_iter_chunked(*args, **kwargs):
        yield b"%PDF-1.4\n"  # Valid PDF header
        yield b"Mock PDF Content" * 1024  # realistic payload size
        yield b"\n%%EOF\n"  # trailer marker

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
    mock_response.status_code = 200
    mock_response.json.return_value = {"best_oa_location": {"pdf_url": None}}
    mock_http_client.get = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock(spec=aiohttp.ClientSession)

    result = await downloader.process_doi(mock_session, mock_http_client, doi)

    assert result.success is False
    assert result.was_oa is False
    assert "Not Open Access" in result.error_message


@pytest.mark.asyncio
async def test_process_doi_not_found(downloader, mock_http_client):
    doi = "10.1234/test.3"

    # Mock OpenAlex response (404 Not Found exception)
    mock_http_client.get = AsyncMock(side_effect=Exception("404 Not Found"))

    mock_session = AsyncMock(spec=aiohttp.ClientSession)

    result = await downloader.process_doi(mock_session, mock_http_client, doi)

    assert result.success is False
    assert result.was_oa is False
    assert "Not Open Access" in result.error_message


@pytest.mark.asyncio
async def test_process_doi_invalid_pdf(downloader, mock_http_client, temp_output_dir):
    doi = "10.1234/test.4"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "best_oa_location": {"pdf_url": "https://example.com/paywall.html"}
    }
    mock_http_client.get = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock(spec=aiohttp.ClientSession)
    mock_get = AsyncMock()
    mock_get.raise_for_status = MagicMock()
    mock_session.get.return_value = mock_get

    mock_get.__aenter__.return_value = mock_get
    mock_get.headers = {"Content-Type": "text/html"}  # Paywall hit!

    async def mock_iter_chunked(*args, **kwargs):
        yield b"<html><head><title>Login to access article</title></head></html>"

    mock_get.content.iter_chunked = mock_iter_chunked

    result = await downloader.process_doi(mock_session, mock_http_client, doi)

    assert result.success is False
    assert result.was_oa is True
    assert result.error_message == "Failed to download or invalid PDF"


@pytest.mark.asyncio
async def test_process_doi_publisher_pattern_fallback(downloader, mock_http_client, temp_output_dir):
    """When the OpenAlex OA URL is Cloudflare-blocked (HTML hull), the IEEE
    direct-PDF pattern must be attempted and can rescue the download."""
    doi = "10.1109/ICCV.2023.01234"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "best_oa_location": {
            "pdf_url": "https://ieeexplore.ieee.org/document/10209481"
        }
    }
    mock_http_client.get = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock(spec=aiohttp.ClientSession)

    async def make_response(header_bytes, content_type):
        r = AsyncMock()
        r.raise_for_status = MagicMock()
        r.__aenter__ = AsyncMock(return_value=r)
        r.headers = {"Content-Type": content_type}

        async def mk_chunked(*a, **k):
            yield header_bytes
            yield b"payload" * 2048
            if content_type == "application/pdf":
                yield b"\n%%EOF\n"

        r.content.iter_chunked = mk_chunked
        return r

    html_resp = await make_response(
        b"<html><body>Checking your browser...</body></html>", "text/html"
    )
    pdf_resp = await make_response(b"%PDF-1.6\n", "application/pdf")
    mock_session.get.side_effect = [html_resp, pdf_resp]

    result = await downloader.process_doi(mock_session, mock_http_client, doi)

    assert result.success is True
    assert result.file_path is not None
    assert result.was_oa is True
    urls = []
    for c in mock_session.get.call_args_list:
        urls.append(c[0][0])
    assert any("stampPDF/getPDF.jsp" in u for u in urls), urls


@pytest.mark.asyncio
async def test_process_doi_proxy_subdomain_attempt(downloader, mock_http_client, temp_output_dir):
    """Cloudflare-blocks both the OA URL and the direct PDF; the SNL-style
    subdomain-proxy rewrite rescues the download."""
    doi = "10.1109/ICCV.2023.01234"
    proxied_downloader = AsyncPDFDownloader(
        output_dir=temp_output_dir,
        proxy_url="https://www.sndl1.arn.dz",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "best_oa_location": {"pdf_url": "https://ieeexplore.ieee.org/document/10209481"}
    }
    mock_http_client.get = AsyncMock(return_value=mock_response)

    mock_session = AsyncMock(spec=aiohttp.ClientSession)

    async def make_response(header_bytes, content_type):
        r = AsyncMock()
        r.raise_for_status = MagicMock()
        r.__aenter__ = AsyncMock(return_value=r)
        r.headers = {"Content-Type": content_type}

        async def mk_chunked(*a, **k):
            yield header_bytes
            yield b"payload" * 2048
            if content_type == "application/pdf":
                yield b"\n%%EOF\n"
            yield b""

        r.content.iter_chunked = mk_chunked
        return r

    html = await make_response(b"<html><body>Checking your browser...</body></html>", "text/html")
    pdf = await make_response(b"%PDF-1.6\n", "application/pdf")

    mock_session.get.side_effect = [html, html, pdf]

    result = await proxied_downloader.process_doi(mock_session, mock_http_client, doi)

    assert result.success is True
    assert result.was_oa is True
    urls = [c[0][0] for c in mock_session.get.call_args_list]
    assert any("ieeexplore-ieee-org.www.sndl1.arn.dz" in u for u in urls), urls
