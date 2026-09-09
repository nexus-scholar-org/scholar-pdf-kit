"""Publisher direct-PDF endpoint patterns and proxy routing.

Bypasses Cloudflare/WAFs by computing direct PDF download URLs for known
publishers (IEEE, Elsevier/ScienceDirect, MDPI, Springer, arXiv) rather
than following the OA landing-page redirect chain.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urlparse, urlunparse

logger = logging.getLogger(__name__)

# Minimum size in bytes to consider a downloaded file a real PDF rather than
# an HTML block-page or stub.  Caught by a genuine PDF header (%PDF-) *and*
# a size floor of ~10 KB.
MIN_PDF_SIZE_BYTES = 10 * 1024

# ---------------------------------------------------------------------------
# DOI-based publisher direct-PDF patterns
# ---------------------------------------------------------------------------
# Each entry: (doi_prefix_regex, landing_page_pattern, direct_pdf_template)
# The landing_page_pattern is used to detect *whether* a URL belongs to this
# publisher; the direct_pdf_template is the final resolved PDF URL.

_PUBLISHER_PATTERNS: list[dict[str, str | re.Pattern[str]]] = [
    {
        "name": "ieee",
        "doi_prefix": re.compile(r"^10\.1109/"),
        "direct_pdf": "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={doi_suffix}",
        "landing_pattern": re.compile(r"ieeexplore\.ieee\.org/document/(\d+)"),
    },
    {
        "name": "elsevier",
        "doi_prefix": re.compile(r"^10\.1016/"),
        "direct_pdf": "https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true",
        "landing_pattern": re.compile(r"sciencedirect\.com/science/article/pii/([A-Z0-9]+)", re.IGNORECASE),
    },
    {
        "name": "mdpi",
        "doi_prefix": re.compile(r"^10\.3390/"),
        "direct_pdf": "https://www.mdpi.com/{mdpi_path}/pdf",
        "landing_pattern": re.compile(r"mdpi\.com/(\d+[^/]*?)/(?:html|pdf)", re.IGNORECASE),
    },
    {
        "name": "springer",
        "doi_prefix": re.compile(r"^10\.(1007|1140)/"),
        "direct_pdf": "https://link.springer.com/content/pdf/{doi}.pdf",
        "landing_pattern": re.compile(r"link\.springer\.com/(?:article|chapter)/(\S+)"),
    },
    {
        "name": "arxiv",
        "doi_prefix": re.compile(r"^10\.48550/"),
        "direct_pdf": "https://arxiv.org/pdf/{arxiv_id}.pdf",
        "landing_pattern": re.compile(r"arxiv\.org/(?:abs|pdf)/(\d+\.\d+)"),
    },
]


def resolve_doi_to_publisher_pdf(doi: str) -> str | None:
    """
    Given a DOI, return a direct-PDF URL for known publishers, or None.

    For IEEE DOIs (10.1109/...), this computes the IEEE stamp URL directly
    from the DOI suffix, bypassing Cloudflare-protected landing pages.
    For Elsevier (10.1016/...), we derive the PII from the landing page URL
    when available, or attempt a direct ScienceDirect PDF construct.
    """
    if not doi:
        return None

    doi = doi.strip()

    # IEEE: 10.1109/<conference>.<year>.<id>  → direct stamp URL
    if doi.startswith("10.1109/"):
        arnumber = doi.split("/")[-1]
        # The IEEE suffix before the last dot is the arnumber
        parts = arnumber.split(".")
        if parts:
            num = parts[-1]
            url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={num}"
            logger.debug("IEEE direct PDF pattern: %s → %s", doi, url)
            return url

    # Springer: 10.1007/... or 10.1140/...  → Springer content PDF
    if doi.startswith(("10.1007/", "10.1140/")):
        url = f"https://link.springer.com/content/pdf/{doi}.pdf"
        logger.debug("Springer direct PDF pattern: %s → %s", doi, url)
        return url

    # arXiv DOI: 10.48550/<arxiv.id>  → arxiv.org/pdf/
    if doi.startswith("10.48550/"):
        arxiv_id = doi.removeprefix("10.48550/")
        url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        logger.debug("arXiv direct PDF pattern: %s → %s", doi, url)
        return url

    return None


def compute_direct_pdf_from_landing_url(landing_url: str) -> str | None:
    """
    Given a publisher landing page URL, return the direct-PDF endpoint URL
    for known publishers, or None if no pattern matches.
    """
    if not landing_url:
        return None

    # IEEE landing page → stamp PDF
    match = _PUBLISHER_PATTERNS[0]["landing_pattern"].search(landing_url)
    if match:
        arnumber = match.group(1)
        return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"

    # Elsevier / ScienceDirect → ScienceDirect PDF (need PII)
    match = _PUBLISHER_PATTERNS[1]["landing_pattern"].search(landing_url)
    if match:
        pii = match.group(1)
        return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true"

    # MDPI → pdf endpoint
    match = _PUBLISHER_PATTERNS[2]["landing_pattern"].search(landing_url)
    if match:
        return landing_url.rstrip("/") + "/pdf" if not landing_url.endswith("/pdf") else None

    # Springer → /content/pdf/<doi>.pdf
    match = _PUBLISHER_PATTERNS[3]["landing_pattern"].search(landing_url)
    if match:
        doi_suffix = match.group(1)
        return f"https://link.springer.com/content/pdf/{doi_suffix}.pdf"

    # arXiv → /pdf/<id>.pdf
    match = _PUBLISHER_PATTERNS[4]["landing_pattern"].search(landing_url)
    if match:
        arxiv_id = match.group(1)
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    return None


PROXY_STYLES = ("auto", "subdomain", "ezproxy", "prefix")

# Institutional/consortium proxy domains which serve remote access by
# prefixing a rewritten host as a subdomain of the proxy host, e.g.
#   https://ieeexplore.ieee.org/d/x            (direct)
#   https://ieeexplore-ieee-org.www.sndl1.arn.dz/d/x   (via consortium proxy)
# Covered families: Algeria SNL (*.arn.dz), EZproxy (*.ezproxy.*), OpenAthens.
_SUBDOMAIN_PROXY_SUFFIXES = (".arn.dz", ".openathens.net", ".ezproxy.")


def _proxy_netloc(proxy_url: str) -> str:
    url = proxy_url if "://" in proxy_url else f"https://{proxy_url}"
    return urlparse(url).netloc or proxy_url.strip()


def proxy_style(proxy_url: str) -> str:
    """Detect the proxy style from a proxy base URL.

    - ``ezproxy``    EZproxy base: ``https://proxy.uni.edu/login?url=``
    - ``subdomain``  host-prefix style: ``https://www.sndl1.arn.dz``
    - ``prefix``     generic origin-prefix: ``http://proxy:3128``
    """
    p = (proxy_url or "").lower()
    if "login?url=" in p:
        return "ezproxy"
    host = _proxy_netloc(p).lower()
    if host.endswith(_SUBDOMAIN_PROXY_SUFFIXES):
        return "subdomain"
    return "prefix"


def is_proxied_url(url: str, proxy_url: str) -> bool:
    """True if ``url`` already routes through the given proxy host."""
    if not url or not proxy_url:
        return False
    netloc = _proxy_netloc(proxy_url)
    return bool(netloc) and f".{netloc}".lower() in urlparse(url).netloc.lower()


def rewrite_via_subdomain(url: str, proxy_host: str) -> str:
    """Rewrite ``https://host.example.org/p/q`` to ``https://host-example-org.<proxy>/p/q``.

    Dots in the target host become dashes and the result is prefixed to the
    proxy host, exactly the scheme consortium gateways (SNL ``*.arn.dz``,
    EZproxy ``*.ezproxy.*``, OpenAthens) use.  Idempotent: an already-proxied
    URL is returned unchanged.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return url
    sub = host.replace(".", "-")
    new_host = f"{sub}.{proxy_host.strip().lstrip('.').rstrip('/')}"
    return urlunparse((parsed.scheme, new_host, parsed.path, parsed.params, parsed.query, parsed.fragment))


def rewrite_via_proxy(url: str, proxy_url: str, style: str = "auto") -> str:
    """
    Rewrite a URL to route through an institutional proxy.

    Supported styles (``style="auto"`` detects from the proxy base):

    - ``ezproxy``:   ``https://proxy.uni.edu/login?url=<original_url>``
    - ``subdomain``: ``https://<host-with-dashes>.<proxy.uni.edu>/<path>``
    - ``prefix``:    ``http://proxy:3128/<original_url>``
    """
    if not url or not proxy_url:
        return url

    resolved = proxy_style(proxy_url) if style == "auto" else style
    if resolved == "ezproxy":
        proxy_url = proxy_url.rstrip("/")
        sep = "&" if "?" in proxy_url else "?"
        return f"{proxy_url}{sep}{quote(url, safe=':/?=&#')}"

    proxy_host = _proxy_netloc(proxy_url)
    if not proxy_host:
        return url

    if resolved == "subdomain":
        return rewrite_via_subdomain(url, proxy_host)

    # Generic HTTP proxy: proxy_host/<original_url>
    return f"{proxy_url.rstrip('/')}/{url.lstrip('/')}"
