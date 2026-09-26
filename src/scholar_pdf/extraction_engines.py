"""Extraction engine adapters and the engine registry (packet E2 section 6.3).

Three boundaries matter here and each one is enforced structurally rather than by
convention:

**Import cheapness** (``E2-NEG-032``).  Every heavy dependency (``fitz``,
``docling``, ``requests``) is imported inside the adapter method that needs it.
Importing this module -- which the kit's public ``__init__`` does -- must not
import an engine.

**Unsupported is not unavailable.**  An engine token outside the registry is a
structural rejection (``E2-013``/``E2-NEG-010``): the service fails the run
without falling back, because silently substituting a different engine for a name
the caller asked for is exactly the "engine selection is not exposed
consistently" defect the packet opens with.  A *known* engine that cannot run
here is a fallback decision with a recorded reason.

**A failure is a value, not an exception that escapes.**  Each adapter converts
its own failure mode into a typed :class:`EngineFailure` carrying a closed
:class:`FallbackReason`, so the service records the attempt (packet E2 6.3 rule 4)
instead of losing the document.  Secrets never reach a diagnostic: the
sanitizers below strip query strings, tokens, and proxy credentials before a
message is ever recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .extraction_models import (
    ENGINE_METHODS,
    ENGINE_VERSION_UNKNOWN,
    ExtractionEngine,
    ExtractionOutputFormat,
    FallbackReason,
)

#: Bound on every recorded diagnostic message, mirroring the E1 attempt budget.
MAX_DIAGNOSTIC_MESSAGE = 500
MAX_DIAGNOSTIC_CODE = 128

_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth|token|secret|password|passwd|cookie|session|"
    r"proxy|forward[_-]?proxy|authorization|api_key)\b\s*[:=]\s*[^\s&;'\"]+"
)
_URL_QUERY_RE = re.compile(r"([?&])([A-Za-z0-9_.\-]+)=([^&\s]+)")
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/@\s]*:[^/@\s]*@")

#: Redaction placeholder used for every removed secret fragment.
REDACTED = "REDACTED"


def sanitize_diagnostic(
    message: str | None, *, limit: int = MAX_DIAGNOSTIC_MESSAGE
) -> str:
    """Bound and redact an engine diagnostic message.

    E1 already refuses to serialize access tokens into a manifest; E2 applies the
    same discipline to engine output, because a GROBID endpoint URL or a verbose
    library traceback is an equally easy place for a credential to leak
    (packet E2 6.3 rule 4).
    """

    if message is None:
        return ""
    text = _USERINFO_RE.sub(rf"\1{REDACTED}@", str(message))
    text = _URL_QUERY_RE.sub(rf"\1{REDACTED}={REDACTED}", text)
    text = _SECRET_QUERY_RE.sub(REDACTED, text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def bound_diagnostic_code(code: str | None) -> str | None:
    """Return a bounded, uppercase diagnostic code or ``None``."""

    if code is None:
        return None
    normalized = " ".join(str(code).split()).upper().replace(" ", "_")
    if not normalized:
        return None
    if len(normalized) > MAX_DIAGNOSTIC_CODE:
        return normalized[: MAX_DIAGNOSTIC_CODE - 1] + "…"
    return normalized


class EngineFailure(Exception):
    """A typed engine failure carrying a mandatory fallback reason."""

    def __init__(
        self,
        reason: FallbackReason,
        code: str,
        message: str | None = None,
    ) -> None:
        self.reason = reason
        self.code = bound_diagnostic_code(code) or "ENGINE_ERROR"
        self.message = sanitize_diagnostic(message)
        super().__init__(f"{self.code}: {self.message}" if self.message else self.code)


class UnsupportedExtractionEngine(Exception):
    """The requested engine token is not in the declared registry."""

    def __init__(self, requested_engine: str) -> None:
        self.requested_engine = requested_engine
        self.code = "UNSUPPORTED_ENGINE"
        self.message = (
            f"requested engine {requested_engine!r} is not a declared engine; "
            "an unknown engine is rejected and never silently substituted"
        )
        super().__init__(self.message)


@dataclass(frozen=True)
class EngineExtractionResult:
    """One successful engine output, before any usefulness evaluation."""

    text: str
    output_format: ExtractionOutputFormat
    page_count: int
    text_layer_present: bool | None = None
    degradation_reasons: tuple[str, ...] = ()
    request_shape: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExtractionEngineAdapter(Protocol):
    """The injectable engine surface used by :class:`~scholar_pdf.extraction.PDFExtractionService`."""

    name: ExtractionEngine

    def resolve(self) -> None:
        """Raise :class:`EngineFailure` when the engine cannot run here."""

    def version(self) -> str:
        """Return the runtime engine version, or the explicit ``unknown`` marker."""

    def extract(
        self,
        data: bytes,
        *,
        grobid_url: str | None = None,
        page_range: str | None = None,
    ) -> EngineExtractionResult:
        """Extract text from verified PDF *data*."""


class PyMuPDFTextEngine:
    """Deterministic rule-based local engine (``DETERMINISTIC_RULE``)."""

    name = ExtractionEngine.PYMUPDF
    output_format = ExtractionOutputFormat.MARKDOWN

    def resolve(self) -> None:
        try:
            import fitz  # noqa: F401  (import probe only)
        except ImportError as error:
            raise EngineFailure(
                FallbackReason.ENGINE_NOT_INSTALLED,
                "PYMUPDF_NOT_INSTALLED",
                "PyMuPDF (fitz) is not installed in this environment",
            ) from error

    def version(self) -> str:
        try:
            import fitz
        except ImportError:
            return ENGINE_VERSION_UNKNOWN
        reported = getattr(fitz, "__version__", None) or getattr(
            getattr(fitz, "version", None), "__doc__", None
        )
        if isinstance(reported, str) and reported.strip():
            return reported.strip().splitlines()[0].strip()
        return ENGINE_VERSION_UNKNOWN

    def extract(
        self,
        data: bytes,
        *,
        grobid_url: str | None = None,
        page_range: str | None = None,
    ) -> EngineExtractionResult:
        import fitz

        if grobid_url is not None:
            raise EngineFailure(
                FallbackReason.ENGINE_UNAVAILABLE,
                "ENGINE_OPTION_UNSUPPORTED",
                "PyMuPDF does not accept an external provider endpoint",
            )
        pages: list[str] = []
        degradation: list[str] = []
        page_count = 0
        try:
            with fitz.open(stream=data, filetype="pdf") as document:
                if page_range:
                    selected = _select_pages(document, page_range)
                else:
                    selected = list(document)
                page_count = document.page_count
                for page in selected:
                    try:
                        text = page.get_text("text") or ""
                    except Exception:  # noqa: BLE001 - recorded as degradation
                        degradation.append("PAGE_EXTRACTION_ERROR")
                        pages.append("")
                        continue
                    if not text.strip():
                        degradation.append("PAGE_WITHOUT_TEXT_LAYER")
                    pages.append(text)
        except EngineFailure:
            raise
        except Exception as error:
            raise EngineFailure(
                FallbackReason.ENGINE_ERROR,
                "PYMUPDF_PARSE_FAILED",
                f"PyMuPDF could not parse the verified bytes: {error}",
            ) from error
        body = "\n\n".join(part.strip() for part in pages if part.strip())
        return EngineExtractionResult(
            text=body,
            output_format=ExtractionOutputFormat.MARKDOWN,
            page_count=page_count,
            text_layer_present=any(page.strip() for page in pages),
            degradation_reasons=tuple(dict.fromkeys(degradation)),
            request_shape=({"page_range": page_range} if page_range else {}),
        )


def _select_pages(document: Any, page_range: str) -> list[Any]:
    """Resolve a ``1-3``/``1,4-6`` page range against an open document."""

    try:
        numbers: list[int] = []
        for chunk in str(page_range).split(","):
            token = chunk.strip()
            if not token:
                continue
            if "-" in token:
                start_text, _, end_text = token.partition("-")
                start = int(start_text)
                end = int(end_text)
            else:
                start = end = int(token)
            if start < 1 or end < start or end > document.page_count:
                raise ValueError(f"page range {token!r} is outside the document")
            numbers.extend(range(start - 1, end))
        if not numbers:
            raise ValueError("the page range selected no pages")
        return [document.load_page(index) for index in numbers]
    except (TypeError, ValueError) as error:
        raise EngineFailure(
            FallbackReason.ENGINE_ERROR,
            "PAGE_RANGE_INVALID",
            f"the requested page range is not usable: {error}",
        ) from error


class DoclingTextEngine:
    """Model-backed local engine (``HEURISTIC``)."""

    name = ExtractionEngine.DOCLING
    output_format = ExtractionOutputFormat.MARKDOWN

    def resolve(self) -> None:
        try:
            import docling  # noqa: F401  (import probe only)
        except ImportError as error:
            raise EngineFailure(
                FallbackReason.ENGINE_NOT_INSTALLED,
                "DOCLING_NOT_INSTALLED",
                "docling is not installed in this environment",
            ) from error

    def version(self) -> str:
        try:
            from importlib.metadata import version

            return version("docling")
        except Exception:  # noqa: BLE001 - a runtime without metadata is honest
            return ENGINE_VERSION_UNKNOWN

    def extract(
        self,
        data: bytes,
        *,
        grobid_url: str | None = None,
        page_range: str | None = None,
    ) -> EngineExtractionResult:
        if grobid_url is not None:
            raise EngineFailure(
                FallbackReason.ENGINE_UNAVAILABLE,
                "ENGINE_OPTION_UNSUPPORTED",
                "docling does not accept an external provider endpoint",
            )
        try:
            from docling.document_converter import DocumentConverter

            converter = DocumentConverter()
            result = converter.convert(data)
        except Exception as error:
            raise EngineFailure(
                FallbackReason.ENGINE_ERROR,
                "DOCLING_CONVERSION_FAILED",
                f"docling could not convert the verified bytes: {error}",
            ) from error
        text = getattr(result, "document", None)
        body = getattr(text, "export_to_markdown", None)
        rendered = body() if callable(body) else str(text or "")
        pages = getattr(getattr(result, "document", None), "pages", None) or {}
        return EngineExtractionResult(
            text=rendered or "",
            output_format=ExtractionOutputFormat.MARKDOWN,
            page_count=len(pages),
            text_layer_present=bool(rendered and rendered.strip()),
            request_shape=({"page_range": page_range} if page_range else {}),
        )


class GrobidProviderEngine:
    """External provider engine (``EXTERNAL_PROVIDER``); TEI output."""

    name = ExtractionEngine.GROBID
    output_format = ExtractionOutputFormat.TEI_XML

    def __init__(self, *, timeout: float = 120.0, session: Any | None = None) -> None:
        self.timeout = timeout
        self._session = session

    def resolve(self) -> None:
        try:
            import requests  # noqa: F401  (import probe only)
        except ImportError as error:
            raise EngineFailure(
                FallbackReason.ENGINE_NOT_INSTALLED,
                "GROBID_CLIENT_NOT_INSTALLED",
                "requests is not installed; a GROBID client is required",
            ) from error

    def version(self) -> str:
        return ENGINE_VERSION_UNKNOWN

    def extract(
        self,
        data: bytes,
        *,
        grobid_url: str | None = None,
        page_range: str | None = None,
    ) -> EngineExtractionResult:
        if not grobid_url:
            raise EngineFailure(
                FallbackReason.ENGINE_UNAVAILABLE,
                "GROBID_ENDPOINT_UNCONFIGURED",
                "no grobid_url was supplied for a GROBID extraction",
            )
        if page_range:
            raise EngineFailure(
                FallbackReason.ENGINE_UNAVAILABLE,
                "ENGINE_OPTION_UNSUPPORTED",
                "GROBID does not accept a document page range",
            )
        try:
            import requests

            owned = self._session is None
            session = self._session or requests.Session()
            try:
                response = session.post(
                    grobid_url,
                    files={"input": ("document.pdf", data, "application/pdf")},
                    data={
                        "output": "tei",
                        "consolidateHeader": "0",
                        "teiCoordinates": "",
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.content
            finally:
                if owned:
                    # Only an E2-owned client is closed; an externally supplied
                    # session belongs to the caller (packet E2 7.5).
                    session.close()
        except EngineFailure:
            raise
        except Exception as error:
            raise EngineFailure(
                FallbackReason.ENGINE_UNAVAILABLE,
                "GROBID_REQUEST_FAILED",
                f"the GROBID provider request failed: {error}",
            ) from error
        text = payload.decode("utf-8", errors="replace")
        return EngineExtractionResult(
            text=text,
            output_format=ExtractionOutputFormat.TEI_XML,
            page_count=text.count("<page "),
            text_layer_present=True,
            request_shape={"grobid_url": grobid_url},
        )


class FakeEngine:
    """Scripted engine used by the packet's hermetic tests.

    The scripted behaviour is declared by the test, so no engine test needs a
    network, a licensed model, or a real PDF parser to be deterministic.
    """

    def __init__(
        self,
        name: ExtractionEngine,
        *,
        version_value: str = "1.2.3",
        result: EngineExtractionResult | None = None,
        failure: EngineFailure | None = None,
        resolve_failure: EngineFailure | None = None,
    ) -> None:
        self.name = name
        self.output_format = (
            result.output_format
            if result is not None
            else ExtractionOutputFormat.MARKDOWN
        )
        self._version = version_value
        self._result = result
        self._failure = failure
        self._resolve_failure = resolve_failure
        self.calls: list[dict[str, Any]] = []

    def resolve(self) -> None:
        if self._resolve_failure is not None:
            raise self._resolve_failure

    def version(self) -> str:
        return self._version

    def extract(
        self,
        data: bytes,
        *,
        grobid_url: str | None = None,
        page_range: str | None = None,
    ) -> EngineExtractionResult:
        self.calls.append(
            {
                "byte_length": len(data),
                "grobid_url": grobid_url,
                "page_range": page_range,
            }
        )
        if self._failure is not None:
            raise self._failure
        if self._result is None:
            raise EngineFailure(
                FallbackReason.ENGINE_ERROR,
                "FAKE_ENGINE_UNSCRIPTED",
                "the fake engine has no scripted result",
            )
        return self._result


class EngineRegistry:
    """Declared engine tokens, their adapters, and their frozen methods."""

    def __init__(
        self, engines: dict[ExtractionEngine, ExtractionEngineAdapter]
    ) -> None:
        self._engines = dict(engines)

    @property
    def declared(self) -> tuple[str, ...]:
        return tuple(sorted(engine.value for engine in self._engines))

    def __contains__(self, token: object) -> bool:
        try:
            return ExtractionEngine(str(token).strip().upper()) in self._engines
        except ValueError:
            return False

    def get(self, token: str) -> ExtractionEngineAdapter:
        """Return the adapter for *token* or reject the engine structurally.

        A request carries the lowercase registry token while a record carries the
        uppercase engine name (packet E2 6.3), so the token is case-normalized
        here -- and *only* here.  An unknown token raises
        :class:`UnsupportedExtractionEngine` instead of resolving to a default,
        so ``E2-NEG-010`` holds at the registry boundary.
        """

        normalized = str(token).strip()
        try:
            engine = ExtractionEngine(normalized.upper())
        except ValueError as error:
            raise UnsupportedExtractionEngine(normalized) from error
        try:
            return self._engines[engine]
        except KeyError as error:
            raise UnsupportedExtractionEngine(normalized) from error

    def method_for(self, engine: ExtractionEngine) -> Any:
        """Return the frozen ``extraction_method`` an engine yields."""

        return ENGINE_METHODS[engine]


def default_engine_registry() -> EngineRegistry:
    """The declared production registry: one adapter per E2 engine name."""

    return EngineRegistry(
        {
            ExtractionEngine.PYMUPDF: PyMuPDFTextEngine(),
            ExtractionEngine.DOCLING: DoclingTextEngine(),
            ExtractionEngine.GROBID: GrobidProviderEngine(),
        }
    )


def engine_chain(
    registry: EngineRegistry,
    requested_engine: str,
    *,
    allow_fallback: bool,
    fallback_order: tuple[str, ...],
) -> tuple[list[str], list[ExtractionEngineAdapter]]:
    """Resolve the ordered engine chain for a request.

    The requested engine is always first.  A fallback is appended only when the
    request allows one, and the requested engine is never dropped from the chain,
    so the recorded ``fallback_chain`` stays a faithful substitution list
    (packet E2 6.3 rules 2 and 3).
    """

    requested = registry.get(requested_engine)
    chain: list[ExtractionEngineAdapter] = [requested]
    if not allow_fallback:
        # The chain carries *engine tokens*, not adapters: the requested adapter
        # has no ``.value``, so the single-engine chain must read its name (the
        # same shape the fallback branch below returns).
        return [requested.name.value], chain
    for token in fallback_order:
        if token == requested_engine:
            continue
        chain.append(registry.get(token))
    return [engine.name.value for engine in chain], chain
