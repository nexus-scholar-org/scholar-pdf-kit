# Open Access Resolution & Download Mechanics

This guide details how `scholar-pdf-kit` discovers Open Access (OA) academic literature, manages high-throughput concurrent downloads, verifies file authenticity, and prevents paywall HTML redirect traps.

---

## 1. Dual-Engine OA Resolution

The toolkit resolves Digital Object Identifiers (DOIs) using a two-stage fallback process:

1. **OpenAlex API**: Queries `https://api.openalex.org/works/https://doi.org/{doi}` using polite `mailto` crawler headers. If `best_oa_location.pdf_url` is present, it is selected as the primary endpoint.
2. **Unpaywall API Fallback**: If OpenAlex does not return a direct PDF link or fails, the toolkit queries `https://api.unpaywall.org/v2/{doi}` for `best_oa_location.url_for_pdf`.
3. **Paywall Status**: If neither registry provides an open PDF URL, the document is flagged as `was_oa=False` ("Not Open Access").

---

## 2. Asynchronous Download Architecture

- **Concurrency Control**: Managed via `asyncio.Semaphore(max_concurrent)` (default: 5) to prevent network socket exhaustion and avoid CDN rate limiting.
- **Paywall / HTML Trap Rejection**: Inspects HTTP response headers (`Content-Type: text/html`). If HTML is received instead of binary PDF stream, the download aborts immediately.
- **Exponential Backoff**: Uses `tenacity` with exponential backoff (2 to 10 seconds, up to 3 retry attempts) to handle transient socket disconnects.
- **Polite Crawling**: Automatically attaches `User-Agent: scholar-pdf-kit/0.1.0 (mailto:{mailto})`.

---

## 3. Magic Byte Integrity Validation

`scholar-pdf-kit` validates every downloaded file against standard PDF magic bytes:

```python
def is_valid_pdf(file_path: Path) -> bool:
    if not file_path.exists() or file_path.stat().st_size < 5:
        return False
    with open(file_path, "rb") as f:
        return f.read(5) == b"%PDF-"
```

If the magic bytes do not match `%PDF-`, the file is automatically purged (`clean_invalid_pdf`) and marked as failed.
