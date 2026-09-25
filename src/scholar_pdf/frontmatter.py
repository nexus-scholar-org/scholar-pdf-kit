"""Bound extracted-file frontmatter: emission, parsing, and body measurement.

Packet E2 section 6.6 makes the YAML header of an authoritative extracted file
*bound* to the sidecar record: a file whose frontmatter disagrees with the sidecar
is not authoritative (``E2-NEG-033``), and an edited or truncated file is detected
on replay (``E2-NEG-040``).

Two distinctions in this module are load-bearing:

``extracted_sha256`` (body bytes)
    The checksum of the extracted text *body*, emitted as a frontmatter key.  It
    is deliberately not the checksum of the whole file, because the frontmatter
    cannot contain its own file checksum.  It is recorded identically in the
    sidecar record so the two can be compared.
``extracted_file_sha256`` (whole file bytes)
    The checksum of the complete committed file.  It lives only in the sidecar and
    is what makes a later edit of the body, the ``# {title}`` heading, *or* the
    frontmatter itself detectable.

The usefulness measurement in :func:`measure_extracted_body` implements the
ordered rule of packet E2 section 6.7(5) exactly:

1. strip the YAML frontmatter block;
2. strip the unconditional ``# {title}`` heading the legacy emitter writes --
   that heading is not extracted content, and leaving it in is exactly how a stub
   body would clear a naive length check (``E2-NEG-013``);
3. require a non-empty stripped remainder (subsumed by the count below);
4. count the stripped body in Unicode characters after whitespace collapsing and
   require ``>= usability_profile.minimum_character_count``.

``yaml`` is imported function-locally: it is a declared dependency of the
authoritative path, but importing the kit package must not import it (nor any
heavy engine) at import time (``E2-NEG-032``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

#: Legacy key set preserved by E2 (``extract.py`` dropped empty values).
LEGACY_KEYS = (
    "workspace_id",
    "doi",
    "title",
    "authors",
    "year",
    "extraction_engine",
    "extracted_at",
)

#: Lineage keys E2 adds; these are *never* dropped even when a legacy key is.
BINDING_KEYS = (
    "document_id",
    "study_id",
    "source_sha256",
    "acquisition_manifest_id",
    "acquisition_manifest_sha256",
    "extraction_engine_version",
    "extraction_requested_engine",
    "extraction_status",
    "content_status",
    "extracted_sha256",
)

#: The complete emitted key set: legacy keys first, then the binding keys.
FRONTMATTER_KEYS = LEGACY_KEYS + BINDING_KEYS

#: Keys whose presence is mandatory on an authoritative extracted file.
REQUIRED_BOUND_KEYS = (
    "document_id",
    "study_id",
    "source_sha256",
    "acquisition_manifest_id",
    "acquisition_manifest_sha256",
    "extraction_engine",
    "extraction_engine_version",
    "extraction_requested_engine",
    "extraction_status",
    "content_status",
    "extracted_sha256",
    "workspace_id",
)

_WHITESPACE_RE = re.compile(r"\s+")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(?P<block>.*?\n)---[ \t]*(?:\n|\Z)", re.DOTALL)
#: The legacy stub body.  It is a *failure* marker, never content, so it is
#: rejected explicitly rather than merely failing the length rule.
_LEGACY_STUB_RE = re.compile(r"^Extracted content from \S+$", re.MULTILINE)


class FrontmatterError(ValueError):
    """An extracted file is not a valid bound-frontmatter document."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BodyMeasurement:
    """The ordered section 6.7(5) evaluation of one extracted body."""

    body: str
    character_count: int
    heading_stripped: bool
    frontmatter_stripped: bool


@dataclass(frozen=True)
class CommittedFile:
    """The exact bytes committed for one identity-addressed extracted file."""

    text: str
    data: bytes
    extracted_sha256: str
    file_sha256: str


def sha256_bytes(data: bytes) -> str:
    """Return the canonical ``sha256:`` digest of *data*."""

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def collapse_whitespace(value: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends."""

    return _WHITESPACE_RE.sub(" ", value).strip()


def measure_extracted_body(text: str) -> BodyMeasurement:
    """Apply the ordered section 6.7(5) rule to a complete extracted file.

    Accepts the *whole committed file* (frontmatter included) and returns the
    stripped body together with its whitespace-collapsed character count.
    """

    remainder = text
    frontmatter_stripped = False
    match = _FRONTMATTER_RE.match(remainder)
    if match is not None:
        remainder = remainder[match.end() :]
        frontmatter_stripped = True
    stripped = remainder.lstrip("\n")
    heading_stripped = False
    # The legacy emitter writes ``# {title}`` unconditionally; a leading ATX
    # heading of level 1 is the heading form, so strip exactly one.
    heading = re.match(r"\A#[ \t]+(?P<title>[^\n]*)\n", stripped)
    if heading is not None:
        stripped = stripped[heading.end() :]
        heading_stripped = True
    body = stripped.strip()
    return BodyMeasurement(
        body=body,
        character_count=len(collapse_whitespace(body)),
        heading_stripped=heading_stripped,
        frontmatter_stripped=frontmatter_stripped,
    )


def is_legacy_stub(body: str) -> bool:
    """Return ``True`` when *body* is the legacy ``Extracted content from`` stub.

    The stub is a parse-failure marker.  Publishing it as ``VALID`` is precisely
    the current behavior packet E2 forbids (``E2-NEG-013``), so the extraction
    service refuses it explicitly instead of relying on a length threshold.
    """

    return _LEGACY_STUB_RE.fullmatch(body.strip()) is not None


def render_frontmatter(values: dict[str, Any]) -> str:
    """Render the YAML block for *values* in the declared key order."""

    import yaml

    ordered = {
        key: values[key] for key in FRONTMATTER_KEYS if values.get(key)
    }
    return yaml.dump(ordered, sort_keys=False, allow_unicode=True).strip()


def build_frontmatter_values(
    *,
    document_id: str,
    study_id: str,
    source_sha256: str,
    acquisition_manifest_id: str,
    acquisition_manifest_sha256: str,
    extraction_engine: str,
    extraction_engine_version: str,
    extraction_requested_engine: str,
    extraction_status: str,
    content_status: str,
    extracted_sha256: str | None = None,
    workspace_id: str,
    doi: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    extracted_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the bound frontmatter mapping for one committed extraction.

    ``title``/``authors``/``year``/``doi`` are caller- or runtime-supplied
    provenance only.  Nothing here derives a value from a filename, a URL, or a
    regular expression over a path (``E2-NEG-043``); the legacy ``extract.py``
    heuristic that did exactly that is confined to the non-authoritative surface
    (packet E2 section 7.2).

    ``extracted_sha256`` is accepted for symmetry but is *derived* by
    :func:`compose_extracted_file` from the exact emitted body bytes, so a caller
    cannot commit a file whose recorded body digest does not match its body.
    """

    return {
        "workspace_id": workspace_id,
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": year,
        "extraction_engine": extraction_engine,
        "extracted_at": extracted_at,
        "document_id": document_id,
        "study_id": study_id,
        "source_sha256": source_sha256,
        "acquisition_manifest_id": acquisition_manifest_id,
        "acquisition_manifest_sha256": acquisition_manifest_sha256,
        "extraction_engine_version": extraction_engine_version,
        "extraction_requested_engine": extraction_requested_engine,
        "extraction_status": extraction_status,
        "content_status": content_status,
        "extracted_sha256": extracted_sha256,
    }


def compose_extracted_file(
    *,
    body: str,
    frontmatter: dict[str, Any],
) -> CommittedFile:
    """Commit *body* with its bound frontmatter and return the exact bytes.

    The emitted file is ``frontmatter block`` + ``# {title}`` heading (when a
    caller-supplied title exists) + the extracted body, which is the same shape
    the legacy emitter produced so downstream readers keep working.

    The emitted body segment is ``body.strip()``, and ``extracted_sha256`` is
    *derived here* from exactly those bytes rather than trusted from the caller.
    That is the only self-consistent construction available: a file cannot carry
    its own checksum, and deriving the body digest at the single point where the
    body is committed guarantees that re-reading the file with
    :func:`measure_extracted_body` reproduces the recorded value.
    """

    import yaml

    normalized_body = body.strip()
    body_sha256 = sha256_bytes(normalized_body.encode("utf-8"))
    values = dict(frontmatter)
    values["extracted_sha256"] = body_sha256
    ordered = {
        key: values[key] for key in FRONTMATTER_KEYS if values.get(key)
    }
    missing = [key for key in REQUIRED_BOUND_KEYS if not ordered.get(key)]
    if missing:
        raise FrontmatterError(
            "FRONTMATTER_BINDING_INCOMPLETE",
            f"bound frontmatter keys are missing: {', '.join(missing)}",
        )
    header = yaml.dump(ordered, sort_keys=False, allow_unicode=True).strip()
    title = ordered.get("title")
    segments = ["---", header, "---", ""]
    if isinstance(title, str) and title.strip():
        segments.append(f"# {title.strip()}\n")
    segments.append(f"{normalized_body}\n")
    text = "\n".join(segments)
    data = text.encode("utf-8")
    return CommittedFile(
        text=text,
        data=data,
        extracted_sha256=body_sha256,
        file_sha256=sha256_bytes(data),
    )


def parse_bound_frontmatter(data: bytes) -> tuple[dict[str, Any], str]:
    """Parse a committed extracted file into its frontmatter and stripped body."""

    import yaml

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FrontmatterError(
            "FRONTMATTER_UNREADABLE", "the extracted file is not valid UTF-8"
        ) from error
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise FrontmatterError(
            "FRONTMATTER_MISSING", "the extracted file has no YAML frontmatter block"
        )
    try:
        parsed = yaml.safe_load(match.group("block")) or {}
    except yaml.YAMLError as error:
        raise FrontmatterError(
            "FRONTMATTER_UNREADABLE", "the extracted frontmatter is not valid YAML"
        ) from error
    if not isinstance(parsed, dict):
        raise FrontmatterError(
            "FRONTMATTER_UNREADABLE", "the extracted frontmatter is not a mapping"
        )
    return parsed, measure_extracted_body(text).body


def verify_bound_frontmatter(
    data: bytes,
    *,
    document_id: str,
    source_sha256: str,
    acquisition_manifest_sha256: str,
    acquisition_manifest_id: str,
    extracted_sha256: str,
    file_sha256: str,
) -> str:
    """Verify a committed file against its sidecar bindings and return the body.

    Any disagreement -- a missing key, a changed value, a mutated body, or a
    mutated file -- fails closed, because a file that does not match its sidecar
    is not authoritative (``E2-NEG-033``) and a mutated one must not be silently
    re-published as current (``E2-NEG-040``).
    """

    if sha256_bytes(data) != file_sha256:
        raise FrontmatterError(
            "EXTRACTED_FILE_MUTATED",
            "the committed extracted file no longer matches its recorded checksum",
        )
    values, body = parse_bound_frontmatter(data)
    expected = {
        "document_id": document_id,
        "study_id": values.get("study_id"),
        "source_sha256": source_sha256,
        "acquisition_manifest_id": acquisition_manifest_id,
        "acquisition_manifest_sha256": acquisition_manifest_sha256,
        "extracted_sha256": extracted_sha256,
        "content_status": values.get("content_status"),
    }
    for key, expected_value in expected.items():
        actual = values.get(key)
        if actual is None:
            raise FrontmatterError(
                "FRONTMATTER_BINDING_MISSING",
                f"the extracted frontmatter has no {key} binding",
            )
        if expected_value is None:
            continue
        if str(actual) != str(expected_value):
            raise FrontmatterError(
                "FRONTMATTER_BINDING_MISMATCH",
                f"the extracted frontmatter {key} disagrees with the sidecar",
            )
    for key in REQUIRED_BOUND_KEYS:
        if not values.get(key):
            raise FrontmatterError(
                "FRONTMATTER_BINDING_MISSING",
                f"the extracted frontmatter has no {key} binding",
            )
    if sha256_bytes(body.encode("utf-8")) != extracted_sha256:
        raise FrontmatterError(
            "EXTRACTED_BODY_MUTATED",
            "the extracted body no longer matches the recorded extracted_sha256",
        )
    return body
