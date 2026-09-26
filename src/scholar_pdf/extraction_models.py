"""Typed models for the deterministic WP01-E2 extracted-text boundary.

This module owns the serialized shape of the E2 boundary: the extraction
request, the per-attempt engine provenance, the byte-bearing
``ExtractedDocumentRecord``, the per-item outcome, and the kit-owned
``pdf-extraction-manifest-v1`` sidecar that is the inseparable provenance record
for committed extracted bytes.

Design constraints this module encodes (packet E2 sections 6.1-6.7):

* ``document_id`` is always the E1 identity.  A record cannot invent one, and a
  record whose ``source_sha256``/``study_id`` pair is not the E1 pair is rejected
  by the service before this model is built.
* A *byte-bearing* record is defined by the existence of committed extracted
  bytes, so only ``EXTRACTED``/``PARTIAL``/``REUSED`` may carry
  ``extracted_path``/``extracted_sha256``.  ``FAILED``/``NEEDS_OCR`` documents
  therefore never carry a fabricated path.
* ``extraction_status`` -> ``DocumentContentStatus`` is a fixed, one-way
  projection.  A projected status is never silently upgraded.
* Every engine attempt records a version; a fallback chain is mandatory exactly
  when the effective engine is not the requested engine, and its order must
  agree with the recorded attempt order.
* ``usability_profile`` is a required versioned field so a later threshold
  change can never silently reclassify a committed document.

Two checksums are recorded for a committed extraction, and the distinction is
deliberate:

``extracted_sha256``
    the checksum of the *extracted body* bytes.  It is the same value in the
    sidecar record and in the file's frontmatter, so the frontmatter can never
    be self-referential.
``extracted_file_sha256``
    the checksum of the *whole committed file* bytes.  It is recorded in the
    sidecar only, and it is the witness that makes a later edit of the body, the
    title heading, or the frontmatter detectable on replay
    (``E2-NEG-033``/``E2-NEG-040``) rather than silently re-published.

The model layer reuses E1 primitives instead of restating them: the strict
frozen base model, the identifier/sha256/path validators, ``StructuredError``,
``ManifestOperation``, ``ManifestReference`` (the ``ACQ-`` sidecar reference),
``ArtifactReference`` (the accepted screening parent), ``ProducerProvenance``,
``OperationStatus``, and the ``AccessStatus``/``MethodProvenance``/acquisition
attempt vocabularies that E2 must preserve rather than rewrite.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from .acquisition_models import (
    CONTRACT_VERSION,
    PDF_MEDIA_TYPE,
    AcceptedParentBinding,
    AccessAssertion,
    AccessStatus,
    AcquisitionAttempt,
    AcquisitionModel,
    ArtifactReference,
    ManifestOperation,
    ManifestReference,
    MethodProvenance,
    OperationStatus,
    ParentArtifactInput,
    ProducerProvenance,
    StructuredError,
    WorkspaceRootBinding,
    validate_portable_relative_path,
)
from .canonical import canonical_fingerprint, normalize_doi

REQUEST_SCHEMA_VERSION = "pdf-extraction-request-v1"
EXTRACTION_MANIFEST_SCHEMA_VERSION = "pdf-extraction-manifest-v1"
EXTRACTION_MANIFEST_TYPE = "pdf_extraction_manifest"
OPERATION_SCHEMA_VERSION = "pdf-extraction-operation-v1"
OPERATION_NAME = "extract_pdf"
CONTRACT_ARTIFACT_TYPE = "document_manifest"
CONTRACT_ACCEPTANCE_NOT_PERFORMED = "not_performed_by_kit"
#: The only Contract artifact type an extraction candidate may declare as input.
SCREENING_DECISIONS_ARTIFACT_TYPE = "screening_decisions"

#: Canonical workspace-relative sidecar placement (mirrors E1's acquisition run
#: directory convention).
SIDECAR_STORAGE_PREFIX = "literature/extraction"
#: Canonical identity-addressed extracted-text directory (the workspace layout,
#: alongside E1's ``pdfs/acquired/``).
DEFAULT_STORAGE_PREFIX = "extracted"
#: E1's acquisition run directory, which the sidecar must reference exactly.
ACQUISITION_STORAGE_PREFIX = "literature/acquisition"

#: Versioned usefulness policy.  The numeric default is a declared kit
#: configuration, not a fact about a document, which is exactly why the profile
#: (name + version + threshold) is a required sidecar field.
USABILITY_PROFILE_NAME = "useful-extracted-text"
USABILITY_PROFILE_VERSION = "1.0.0"
DEFAULT_MINIMUM_CHARACTER_COUNT = 200

COMMIT_INTENT_SUFFIX = ".extraction-commit-intent.json"
COMMIT_INTENT_SCHEMA_VERSION = "pdf-extraction-commit-intent-v1"

_ARTIFACT_ID_RE = re.compile(r"^ART-[A-Za-z0-9][A-Za-z0-9._-]*$")
_WORKSPACE_ID_RE = re.compile(r"^WSP-[A-Za-z0-9][A-Za-z0-9._-]*$")
_RUN_ID_RE = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9._-]*$")
_STUDY_ID_RE = re.compile(r"^(?:STU|SCI)-[A-Za-z0-9][A-Za-z0-9._-]*$")
_DOCUMENT_ID_RE = re.compile(r"^DOC-[0-9a-f]{32}$")
_ACQUISITION_MANIFEST_ID_RE = re.compile(r"^ACQ-[0-9a-f]{32}$")
_MANIFEST_ID_RE = re.compile(r"^EXT-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENGINE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_ENGINE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

ENGINE_VERSION_UNKNOWN = "unknown"


class ExtractionModel(AcquisitionModel):
    """Strict, frozen base model for the extraction boundary.

    E2 deliberately reuses the E1 base configuration (``extra="forbid"``,
    ``frozen=True``) rather than defining a second model policy: a strict frozen
    record is the property that makes a sidecar mutation detectable.
    """


class ExtractionStatus(StrEnum):
    """Committed per-item domain status (distinct from ``OperationStatus``)."""

    EXTRACTED = "EXTRACTED"
    REUSED = "REUSED"
    PARTIAL = "PARTIAL"
    NO_TEXT_LAYER = "NO_TEXT_LAYER"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ExtractionStage(StrEnum):
    PREFLIGHT = "PREFLIGHT"
    ENGINE = "ENGINE"
    VALIDATION = "VALIDATION"
    PROMOTION = "PROMOTION"
    SIDECAR = "SIDECAR"
    CANDIDATE = "CANDIDATE"
    AUDIT = "AUDIT"
    COMPLETE = "COMPLETE"


class ExtractionEngine(StrEnum):
    """Declared engine names.  Engine availability is never assumed."""

    PYMUPDF = "PYMUPDF"
    DOCLING = "DOCLING"
    GROBID = "GROBID"


class ExtractionOutputFormat(StrEnum):
    MARKDOWN = "MARKDOWN"
    TEI_XML = "TEI_XML"


class DocumentContentStatus(StrEnum):
    """Kit-local restatement of the frozen ``DocumentContentStatus`` values.

    The frozen Contract v1 enum is ``VALID|PARTIAL|FAILED|NEEDS_OCR``.  E2 must
    not import the harness package, so the four values are restated here as the
    projection target of :data:`EXTRACTION_CONTENT_STATUS`.  The candidate
    builder emits exactly these strings and nothing else.
    """

    VALID = "VALID"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NEEDS_OCR = "NEEDS_OCR"


class ExtractionMethod(StrEnum):
    """Kit-local restatement of the frozen ``MethodProvenance`` values.

    E1 preserves ``acquisition_method`` with the three members it needs.  E2 adds
    the engine-side values (``HEURISTIC`` for a model-backed engine,
    ``EXTERNAL_PROVIDER`` for a provider engine) and must use *only* these six
    frozen values, so the complete frozen vocabulary is restated rather than
    partially re-declared.  No value is minted here.
    """

    HUMAN = "HUMAN"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    HEURISTIC = "HEURISTIC"
    LLM = "LLM"
    EXTERNAL_PROVIDER = "EXTERNAL_PROVIDER"
    COMPOSED = "COMPOSED"


class FallbackReason(StrEnum):
    """Closed vocabulary for a substituted engine attempt."""

    ENGINE_NOT_INSTALLED = "ENGINE_NOT_INSTALLED"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    ENGINE_ERROR = "ENGINE_ERROR"
    ENGINE_OUTPUT_UNUSABLE = "ENGINE_OUTPUT_UNUSABLE"
    ENGINE_LICENSE_MISSING = "ENGINE_LICENSE_MISSING"


class AttemptResult(StrEnum):
    """Result of one engine attempt."""

    TEXT_EXTRACTED = "TEXT_EXTRACTED"
    NO_TEXT = "NO_TEXT"
    ERROR = "ERROR"


#: Only these statuses can own committed extracted bytes.
BYTE_BEARING_STATUSES = frozenset(
    {
        ExtractionStatus.EXTRACTED,
        ExtractionStatus.PARTIAL,
        ExtractionStatus.REUSED,
    }
)
#: Determined outcomes that own no bytes but must stay visible.
DETERMINED_FAILURE_STATUSES = frozenset(
    {
        ExtractionStatus.NO_TEXT_LAYER,
        ExtractionStatus.EXTRACTION_FAILED,
    }
)
#: Pre-commit outcomes: no record is emitted for them.
PRE_COMMIT_STATUSES = frozenset({ExtractionStatus.CANCELLED, ExtractionStatus.FAILED})

#: Fixed, one-way projection from the kit status to the frozen content status.
EXTRACTION_CONTENT_STATUS: dict[ExtractionStatus, DocumentContentStatus] = {
    ExtractionStatus.EXTRACTED: DocumentContentStatus.VALID,
    ExtractionStatus.REUSED: DocumentContentStatus.VALID,
    ExtractionStatus.PARTIAL: DocumentContentStatus.PARTIAL,
    ExtractionStatus.NO_TEXT_LAYER: DocumentContentStatus.NEEDS_OCR,
    ExtractionStatus.EXTRACTION_FAILED: DocumentContentStatus.FAILED,
}

#: The frozen content statuses that require an ``extracted_path``
#: (``models.py:503-510``).
DOCUMENT_MANIFEST_REQUIRED_STATUSES = frozenset(
    {DocumentContentStatus.VALID, DocumentContentStatus.PARTIAL}
)
#: Engine -> frozen extraction method (packet E2 section 6.3 rule 5).
ENGINE_METHODS: dict[ExtractionEngine, ExtractionMethod] = {
    ExtractionEngine.PYMUPDF: ExtractionMethod.DETERMINISTIC_RULE,
    ExtractionEngine.DOCLING: ExtractionMethod.HEURISTIC,
    ExtractionEngine.GROBID: ExtractionMethod.EXTERNAL_PROVIDER,
}

#: Deterministic engine order used when a requested engine is unavailable and
#: the request allows a fallback.
DEFAULT_ENGINE_FALLBACK_ORDER: tuple[ExtractionEngine, ...] = (
    ExtractionEngine.PYMUPDF,
)


def project_content_status(status: ExtractionStatus) -> DocumentContentStatus:
    """Project a committed ``ExtractionStatus`` onto the frozen content status."""

    try:
        return EXTRACTION_CONTENT_STATUS[status]
    except KeyError as error:  # pragma: no cover - guarded by the model layer
        raise ValueError(
            f"{status.value} is not a projectable extraction status"
        ) from error


def extraction_method_for_engine(engine: str | None) -> ExtractionMethod:
    """Map an engine name onto the frozen method its output yields.

    An unknown or absent engine yields ``HEURISTIC`` so a committed record can
    never claim a stronger provenance than the evidence supports.
    """

    try:
        return ENGINE_METHODS[ExtractionEngine(engine)]
    except (KeyError, ValueError):
        return ExtractionMethod.HEURISTIC


def determined_outcome_method(
    attempts: Sequence[ExtractionAttempt],
) -> ExtractionMethod:
    """Derive the truthful ``extraction_method`` for a document that owns no bytes.

    A ``FAILED``/``NEEDS_OCR`` ``DocumentRecord`` still carries a mandatory
    ``extraction_method`` in the frozen model (only ``extracted_path`` is
    conditional), so the value has to describe how the outcome was reached:

    1. If any recorded attempt ran a provider/daemon engine, the outcome was
       produced through an external service, so ``EXTERNAL_PROVIDER`` is the
       honest value.  This takes precedence because a provider attempt changes
       the provenance of the determination even when a later local engine also
       ran and failed.
    2. Otherwise, if at least one engine attempt is recorded, the method of the
       *last* attempt is the provenance of the determination -- that attempt is
       the one that produced the final unusable result.
    3. Otherwise no engine ever ran (a structural rejection, or a failure raised
       before the chain started).  The determination was then made entirely by a
       deterministic rule over the request and lineage, so
       ``DETERMINISTIC_RULE`` is recorded.  Claiming ``EXTERNAL_PROVIDER`` or
       ``HEURISTIC`` here would assert a model or provider involvement that did
       not happen -- exactly the fabrication this field exists to prevent.

    The result is never ``None``: the frozen model requires the field on every
    record, and a null there is a rejection, not a permitted absence.
    """

    methods = [
        ENGINE_METHODS.get(ExtractionEngine(attempt.engine), None)
        for attempt in attempts
    ]
    provider_methods = [
        method for method in methods if method is ExtractionMethod.EXTERNAL_PROVIDER
    ]
    if provider_methods:
        return ExtractionMethod.EXTERNAL_PROVIDER
    determined = [method for method in methods if method is not None]
    if determined:
        return determined[-1]
    return ExtractionMethod.DETERMINISTIC_RULE


def compute_extraction_idempotency_key(
    *,
    workspace_id: str,
    run_id: str,
    acquisition_manifest_ref: ManifestReference,
    documents: Iterable[tuple[str, str, str]],
) -> str:
    """Compute the request-side idempotency key exactly as packet E2 7.4 states it.

    The payload covers the *request* side only -- workspace, run, the canonical E1
    manifest reference, and the ``{document_id, source_sha256, requested_engine}``
    document set -- and deliberately excludes timestamps, retry counts, attempt
    ordinals, and transient diagnostics, so a retry that succeeds where a previous
    attempt committed ``EXTRACTION_FAILED`` keeps this key while minting a new
    ``EXT-`` id (the section 6.7(9) successor).
    """

    source_payload = {
        "schema_version": EXTRACTION_MANIFEST_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "acquisition_manifest_ref": acquisition_manifest_ref.model_dump(mode="json"),
        "documents": sorted(
            (
                {
                    "document_id": document_id,
                    "source_sha256": source_sha256,
                    "requested_engine": requested_engine,
                }
                for document_id, source_sha256, requested_engine in documents
            ),
            key=lambda document: (
                document["document_id"],
                document["requested_engine"],
            ),
        ),
    }
    return canonical_fingerprint(source_payload)


def utc_now() -> str:
    """Return the current instant as an RFC3339 UTC string."""

    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _validate_sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError("value must be sha256:<64 lowercase hex characters>")
    return value


def _validate_optional_sha256(value: str | None) -> str | None:
    return _validate_sha256(value) if value is not None else None


def _validate_engine_name(value: str) -> str:
    """Validate a recorded engine name (``PYMUPDF``/``DOCLING``/``GROBID``)."""

    if _ENGINE_NAME_RE.fullmatch(value) is None:
        raise ValueError("engine name must be an uppercase registry token")
    return value


def _validate_utc_timestamp(value: str) -> str:
    if _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("timestamp must be an RFC3339 UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must be an RFC3339 instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:  # pragma: no cover
        raise ValueError("timestamp must carry an explicit UTC offset")
    if parsed.utcoffset().total_seconds() != 0:  # pragma: no cover
        raise ValueError("timestamp must be UTC")
    return value


class UsabilityProfile(ExtractionModel):
    """Versioned usefulness rule that makes every ``content_status`` explainable."""

    name: str = Field(default=USABILITY_PROFILE_NAME, min_length=1)
    version: str = Field(default=USABILITY_PROFILE_VERSION, min_length=1)
    minimum_character_count: int = Field(default=DEFAULT_MINIMUM_CHARACTER_COUNT, ge=1)

    @classmethod
    def default_profile(cls) -> UsabilityProfile:
        return cls()


class ExtractionRequest(ExtractionModel):
    """One immutable, parent-bound request for one acquired study's documents."""

    schema_version: str = REQUEST_SCHEMA_VERSION
    workspace_id: str
    workspace_root: Path
    run_id: str
    study_id: str
    protocol_fingerprint: str
    corpus_fingerprint: str
    screening_decisions: ParentArtifactInput
    acquisition_manifest_id: str
    acquisition_manifest_sha256: str
    acquisition_manifest_path: str
    document_ids: list[str] | None = None
    requested_engine: str = "pymupdf"
    grobid_url: str | None = None
    page_range: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # There is deliberately no request-level byte-verification switch.  Packet E2
    # section 6.7 rule 2 makes source-byte verification mandatory for every
    # commit, so a per-request opt-out would be a silent no-op override of a
    # mandatory check.  The service constructor's `verify_source_bytes` flag is
    # the single authority, and the base model forbids extra fields, so a config
    # that still carries this key is rejected explicitly instead of ignored.
    allow_fallback: bool = True
    storage_prefix: str = DEFAULT_STORAGE_PREFIX
    usability_profile: UsabilityProfile = Field(
        default_factory=UsabilityProfile.default_profile
    )

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != REQUEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported extraction request schema: {value}")
        return value

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, value: str) -> str:
        if _WORKSPACE_ID_RE.fullmatch(value) is None:
            raise ValueError("workspace_id must use the registered WSP- prefix")
        return value

    @field_validator("workspace_root")
    @classmethod
    def require_absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if _RUN_ID_RE.fullmatch(value) is None:
            raise ValueError("run_id must use the registered RUN- prefix")
        return value

    @field_validator("study_id")
    @classmethod
    def validate_study_id(cls, value: str) -> str:
        if _STUDY_ID_RE.fullmatch(value) is None:
            raise ValueError("study_id must use STU- or legacy SCI- prefix")
        return value

    @field_validator("protocol_fingerprint", "corpus_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("screening_decisions")
    @classmethod
    def validate_screening_parent(
        cls, value: ParentArtifactInput
    ) -> ParentArtifactInput:
        if value.artifact_type != "screening_decisions":
            raise ValueError(
                "document_manifest may declare only a screening_decisions parent"
            )
        return value

    @field_validator("acquisition_manifest_id")
    @classmethod
    def validate_acquisition_manifest_id(cls, value: str) -> str:
        if _ACQUISITION_MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("acquisition_manifest_id must be an opaque ACQ- digest")
        return value

    @field_validator("acquisition_manifest_sha256")
    @classmethod
    def validate_acquisition_checksum(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("acquisition_manifest_path")
    @classmethod
    def validate_acquisition_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("document_ids must be null or a non-empty list")
        if len(value) != len(set(value)):
            raise ValueError("document_ids must be unique")
        for document_id in value:
            if _DOCUMENT_ID_RE.fullmatch(document_id) is None:
                raise ValueError("document_ids must be opaque DOC- digests")
        return value

    @field_validator("requested_engine")
    @classmethod
    def normalize_requested_engine(cls, value: str) -> str:
        """Normalize the request token; the *registry* decides what is supported.

        An unknown engine is a structured runtime error, not a model-validation
        crash and never a silent fallback, so the request stores the normalized
        token and the service resolves it against the engine registry.
        """

        normalized = value.strip().lower()
        if _ENGINE_TOKEN_RE.fullmatch(normalized) is None:
            raise ValueError("requested_engine must be a lowercase engine token")
        return normalized

    @field_validator("metadata")
    @classmethod
    def bound_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Accept only caller-supplied metadata keys; never derive them.

        A filename, URL, or path regex may not supply ``title``/``doi``/
        ``workspace_id``: those are provenance claims, not parsing conveniences
        (``E2-NEG-043``).
        """

        allowed = {"title", "authors", "year", "doi"}
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ValueError(
                f"metadata keys must be caller-supplied provenance only: {unexpected}"
            )
        normalized = dict(value)
        if normalized.get("doi"):
            normalized["doi"] = normalize_doi(str(normalized["doi"]))
        if normalized.get("title") is not None and (
            not isinstance(normalized["title"], str) or not normalized["title"].strip()
        ):
            raise ValueError("metadata title must be a non-empty string")
        if normalized.get("authors") is not None and not isinstance(
            normalized["authors"], list
        ):
            raise ValueError("metadata authors must be a list")
        if normalized.get("year") is not None:
            year = normalized["year"]
            if isinstance(year, bool) or not isinstance(year, int):
                raise ValueError("metadata year must be an integer")
            if not 1000 <= year <= 9999:
                raise ValueError("metadata year must be a four-digit year")
        return normalized

    @field_validator("storage_prefix")
    @classmethod
    def validate_storage_prefix(cls, value: str) -> str:
        normalized = validate_portable_relative_path(value)
        if normalized.startswith("literature/") or normalized == "literature":
            raise ValueError("storage_prefix must not shadow workspace state")
        return normalized


class ExtractionAttempt(ExtractionModel):
    """One recorded engine attempt (packet E2 section 6.3)."""

    ordinal: int = Field(ge=1)
    engine: str
    engine_version: str = Field(min_length=1, max_length=120)
    output_format: ExtractionOutputFormat
    request_shape: dict[str, Any] = Field(default_factory=dict)
    result: AttemptResult
    effective: bool = False
    page_count: int | None = Field(default=None, ge=0)
    character_count: int | None = Field(default=None, ge=0)
    text_layer_present: bool | None = None
    diagnostic_code: str | None = Field(default=None, max_length=128)
    diagnostic_message: str | None = Field(default=None, max_length=500)
    attempted_at: str | None = None

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, value: str) -> str:
        return _validate_engine_name(value)

    @field_validator("attempted_at")
    @classmethod
    def validate_attempted_at(cls, value: str | None) -> str | None:
        return _validate_utc_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def version_is_always_recorded(self) -> Self:
        """An attempt without a version is rejected, never omitted.

        A runtime that reports no version records the explicit ``unknown``
        marker *and* a bounded diagnostic; the marker alone is not evidence.
        """

        if not self.engine_version.strip():
            raise ValueError("an engine attempt must record an engine version")
        if (
            self.engine_version.strip().lower() == ENGINE_VERSION_UNKNOWN
            and self.diagnostic_code is None
        ):
            raise ValueError("an unknown engine version requires a bounded diagnostic")
        if self.effective and self.result is not AttemptResult.TEXT_EXTRACTED:
            raise ValueError("only a text-extracting attempt can be effective")
        return self


class FallbackStep(ExtractionModel):
    """One substituted engine attempt with a mandatory reason."""

    engine: str
    engine_version: str = Field(min_length=1, max_length=120)
    reason: FallbackReason
    detail: str | None = Field(default=None, max_length=500)

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, value: str) -> str:
        return _validate_engine_name(value)


class ExtractedDocumentRecord(ExtractionModel):
    """A byte-bearing committed extraction (packet E2 section 6.2).

    Every lineage field is copied from the accepted E1 record or the accepted E1
    manifest; ``document_id`` and ``source_sha256`` are byte-for-byte the E1
    values.  ``access_status``, ``acquisition_method``, ``acquisition_attempts``,
    ``selected_source``, ``selected_source_url``, and ``access_assertion`` are
    preserved from E1 and never re-derived.
    """

    document_id: str
    document_identity_algorithm_version: str = "v1"
    study_id: str
    acquisition_manifest_id: str
    acquisition_manifest_sha256: str
    acquisition_manifest_path: str
    source_sha256: str
    byte_length: int = Field(gt=0)
    media_type: str = PDF_MEDIA_TYPE
    source_workspace_relative_path: str
    extraction_status: ExtractionStatus
    content_status: DocumentContentStatus
    extraction_method: ExtractionMethod
    requested_engine: str
    requested_engine_version: str = Field(min_length=1, max_length=120)
    effective_engine: str
    effective_engine_version: str = Field(min_length=1, max_length=120)
    fallback_chain: list[FallbackStep] = Field(default_factory=list)
    degradation_reasons: list[str] = Field(default_factory=list)
    attempts: list[ExtractionAttempt] = Field(min_length=1)
    page_count: int = Field(ge=0)
    character_count: int = Field(ge=0)
    extracted_sha256: str
    extracted_file_sha256: str
    extracted_path: str
    extraction_output_format: ExtractionOutputFormat
    access_status: AccessStatus
    acquisition_method: MethodProvenance
    acquisition_attempts: list[AcquisitionAttempt] = Field(min_length=1)
    access_assertion: AccessAssertion | None = None
    selected_source: str
    selected_source_url: str | None = None
    normalized_doi: str | None = None

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if _DOCUMENT_ID_RE.fullmatch(value) is None:
            raise ValueError("document_id must be an opaque DOC- digest")
        return value

    @field_validator("document_identity_algorithm_version")
    @classmethod
    def validate_document_identity_version(cls, value: str) -> str:
        if value != "v1":
            raise ValueError("unsupported document identity algorithm version")
        return value

    @field_validator("study_id")
    @classmethod
    def validate_study_id(cls, value: str) -> str:
        if _STUDY_ID_RE.fullmatch(value) is None:
            raise ValueError("study_id must use STU- or legacy SCI- prefix")
        return value

    @field_validator("acquisition_manifest_id")
    @classmethod
    def validate_acquisition_manifest_id(cls, value: str) -> str:
        if _ACQUISITION_MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("acquisition_manifest_id must be an opaque ACQ- digest")
        return value

    @field_validator(
        "acquisition_manifest_sha256",
        "source_sha256",
        "extracted_sha256",
        "extracted_file_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator(
        "acquisition_manifest_path",
        "source_workspace_relative_path",
        "extracted_path",
    )
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("media_type")
    @classmethod
    def require_pdf(cls, value: str) -> str:
        if value != PDF_MEDIA_TYPE:
            raise ValueError("an extracted record's media_type must be application/pdf")
        return value

    @field_validator("requested_engine", "effective_engine")
    @classmethod
    def validate_engine(cls, value: str) -> str:
        return _validate_engine_name(value)

    @field_validator("normalized_doi")
    @classmethod
    def normalize_record_doi(cls, value: str | None) -> str | None:
        return normalize_doi(value) if value is not None else None

    @model_validator(mode="after")
    def coherent_record(self) -> Self:
        if self.extraction_status not in BYTE_BEARING_STATUSES:
            raise ValueError("only EXTRACTED/PARTIAL/REUSED records may carry bytes")
        if self.content_status is not project_content_status(self.extraction_status):
            raise ValueError(
                "content_status must be the fixed projection of extraction_status"
            )
        if (self.extraction_status is ExtractionStatus.PARTIAL) != (
            self.content_status is DocumentContentStatus.PARTIAL
        ):
            raise ValueError(
                "extraction_status PARTIAL and content_status PARTIAL must agree"
            )
        ordinals = [attempt.ordinal for attempt in self.attempts]
        if ordinals != list(range(1, len(self.attempts) + 1)):
            raise ValueError("attempt ordinals must be contiguous and ordered")
        attempted = [attempt.engine for attempt in self.attempts]
        if self.effective_engine not in attempted:
            raise ValueError(
                "the effective engine must be one of the recorded attempts"
            )
        effective_attempts = [attempt for attempt in self.attempts if attempt.effective]
        if len(effective_attempts) != 1:
            raise ValueError("exactly one attempt must be the effective one")
        if effective_attempts[0].engine != self.effective_engine:
            raise ValueError("the effective attempt must name the effective engine")
        if self.effective_engine != self.requested_engine:
            if not self.fallback_chain:
                raise ValueError(
                    "effective != requested requires a reason-recorded fallback chain"
                )
            effective_ordinal = effective_attempts[0].ordinal
            expected_chain = attempted[: effective_ordinal - 1]
            if [step.engine for step in self.fallback_chain] != expected_chain:
                raise ValueError(
                    "the fallback chain order must match the recorded attempt order"
                )
        elif self.fallback_chain:
            raise ValueError(
                "effective == requested must not claim a substituted fallback step"
            )
        if self.content_status is DocumentContentStatus.PARTIAL and not (
            self.fallback_chain
            or self.degradation_reasons
            or any(attempt.diagnostic_code for attempt in self.attempts)
        ):
            raise ValueError("PARTIAL content requires an explicit degradation reason")
        suffix = {
            ExtractionOutputFormat.MARKDOWN: ".md",
            ExtractionOutputFormat.TEI_XML: ".tei.xml",
        }[self.extraction_output_format]
        if PurePosixPath(self.extracted_path).name != f"{self.document_id}{suffix}":
            raise ValueError("extracted_path must be identity addressed")
        return self


class ExtractionItemOutcome(ExtractionModel):
    """One row of ``item_outcomes``: the truthful account for a single document."""

    study_id: str
    document_id: str | None = None
    extraction_status: ExtractionStatus
    requested_engine: str
    requested_engine_version: str | None = Field(default=None, max_length=120)
    effective_engine: str | None = None
    effective_engine_version: str | None = Field(default=None, max_length=120)
    content_status: DocumentContentStatus | None = None
    extraction_method: ExtractionMethod | None = None
    page_count: int | None = Field(default=None, ge=0)
    character_count: int | None = Field(default=None, ge=0)
    source_sha256: str | None = None
    acquisition_manifest_id: str
    acquisition_manifest_sha256: str
    extracted_path: str | None = None
    extracted_sha256: str | None = None
    extracted_file_sha256: str | None = None
    attempts: list[ExtractionAttempt] = Field(default_factory=list)
    fallback_chain: list[FallbackStep] = Field(default_factory=list)
    degradation_reasons: list[str] = Field(default_factory=list)
    prior_outcome_manifest_id: str | None = None
    prior_outcome_status: ExtractionStatus | None = None
    stage: ExtractionStage = ExtractionStage.COMPLETE
    error: StructuredError | None = None
    warning: StructuredError | None = None

    @property
    def is_superseded(self) -> bool:
        """True when this row is a retained prior outcome, not the current truth.

        A superseded row exists so section 6.7(9)'s successor keeps the prior
        determined outcome visible; the current row for that document is the
        byte-bearing record the successor committed.
        """

        return self.prior_outcome_manifest_id is not None

    @field_validator("study_id")
    @classmethod
    def validate_study_id(cls, value: str) -> str:
        if _STUDY_ID_RE.fullmatch(value) is None:
            raise ValueError("study_id must use STU- or legacy SCI- prefix")
        return value

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str | None) -> str | None:
        if value is not None and _DOCUMENT_ID_RE.fullmatch(value) is None:
            raise ValueError("document_id must be an opaque DOC- digest")
        return value

    @field_validator("acquisition_manifest_id")
    @classmethod
    def validate_acquisition_manifest_id(cls, value: str) -> str:
        if _ACQUISITION_MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("acquisition_manifest_id must be an opaque ACQ- digest")
        return value

    @field_validator(
        "acquisition_manifest_sha256",
        "source_sha256",
        "extracted_sha256",
        "extracted_file_sha256",
    )
    @classmethod
    def validate_outcome_sha256(cls, value: str | None) -> str | None:
        return _validate_optional_sha256(value)

    @field_validator("extracted_path")
    @classmethod
    def validate_outcome_path(cls, value: str | None) -> str | None:
        return validate_portable_relative_path(value) if value is not None else None

    @field_validator("requested_engine", "effective_engine")
    @classmethod
    def validate_outcome_engine(cls, value: str | None) -> str | None:
        return _validate_engine_name(value) if value is not None else None

    @field_validator("prior_outcome_manifest_id")
    @classmethod
    def validate_prior_manifest_id(cls, value: str | None) -> str | None:
        if value is not None and _MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("prior_outcome_manifest_id must be an opaque EXT- digest")
        return value

    @model_validator(mode="after")
    def coherent_outcome(self) -> Self:
        byte_bearing = self.extraction_status in BYTE_BEARING_STATUSES
        if byte_bearing:
            if self.document_id is None:
                raise ValueError("a byte-bearing outcome requires the E1 document_id")
            if self.extracted_path is None or self.extracted_sha256 is None:
                raise ValueError(
                    "a byte-bearing outcome requires extracted_path and extracted_sha256"
                )
            if self.extracted_file_sha256 is None:
                raise ValueError(
                    "a byte-bearing outcome requires the committed file checksum"
                )
            if self.extraction_status is ExtractionStatus.REUSED:
                # A REUSED row re-reports a *committed* record, so its content
                # status is the committed one (section 6.7 rule 6: derived from
                # the recorded degradation reasons), not a fixed projection of
                # REUSED -- a degraded commit must stay PARTIAL on replay
                # instead of being silently upgraded to VALID.  The committed
                # record itself keeps the strict projection
                # (``coherent_record``); only the replayed row is relaxed here.
                if self.content_status not in {
                    DocumentContentStatus.VALID,
                    DocumentContentStatus.PARTIAL,
                }:
                    raise ValueError(
                        "a REUSED row must report the committed VALID or PARTIAL "
                        "content status"
                    )
            elif self.content_status is not project_content_status(
                self.extraction_status
            ):
                raise ValueError("content_status must be the fixed projection")
            if self.error is not None:
                raise ValueError("a byte-bearing outcome cannot contain an error")
        else:
            if self.extracted_path is not None or self.extracted_sha256 is not None:
                raise ValueError(
                    "FAILED/NEEDS_OCR outcomes must not carry an extracted_path"
                )
            if self.extraction_status in DETERMINED_FAILURE_STATUSES and (
                self.error is None and self.warning is None
            ):
                raise ValueError(
                    "a determined failure requires an explicit error or warning"
                )
            if self.content_status is not None and self.content_status not in {
                DocumentContentStatus.FAILED,
                DocumentContentStatus.NEEDS_OCR,
            }:
                raise ValueError(
                    "a non-byte-bearing outcome can only be FAILED or NEEDS_OCR"
                )
        # Supersession is opt-in: a current outcome carries neither prior field,
        # and a superseded one must carry *both*.  Exactly one of the two is an
        # incoherent half-record and must never reach a sidecar.
        if (self.prior_outcome_manifest_id is None) != (
            self.prior_outcome_status is None
        ):
            raise ValueError(
                "a superseded outcome must record both its prior manifest and status"
            )
        if self.prior_outcome_status in BYTE_BEARING_STATUSES:
            raise ValueError("only a superseded non-committed outcome can be replaced")
        return self


class ArtifactRecordProjection(ExtractionModel):
    """The identity + truthful content status of a document that owns no bytes.

    A determined ``EXTRACTION_FAILED``/``NO_TEXT_LAYER`` document still belongs
    in the candidate set (``E2-NEG-014``), but it has no ``extracted_path``.
    This projection is the minimal, explicit shape that carries exactly those
    three identity fields, the frozen content status, and the *mandatory*
    ``extraction_method``: the frozen ``DocumentRecord`` requires a method on
    every record (only ``extracted_path`` is conditional), so the builder
    cannot accidentally reach into a record for a path and cannot drop the
    method the way a failed document has no bytes.
    """

    document_id: str
    study_id: str
    source_sha256: str
    content_status: DocumentContentStatus
    extraction_method: ExtractionMethod

    @field_validator("document_id")
    @classmethod
    def validate_projection_document_id(cls, value: str) -> str:
        if _DOCUMENT_ID_RE.fullmatch(value) is None:
            raise ValueError("document_id must be an opaque DOC- digest")
        return value

    @field_validator("source_sha256")
    @classmethod
    def validate_projection_source_hash(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def projection_is_non_committed(self) -> Self:
        if self.content_status in DOCUMENT_MANIFEST_REQUIRED_STATUSES:
            raise ValueError("a non-committed projection must be FAILED or NEEDS_OCR")
        return self

    @classmethod
    def from_outcome(cls, outcome: ExtractionItemOutcome) -> ArtifactRecordProjection:
        """Project a determined item outcome onto the candidate record shape."""

        if outcome.extraction_status in BYTE_BEARING_STATUSES:
            raise ValueError("a byte-bearing outcome is projected from its record")
        if outcome.document_id is None or outcome.source_sha256 is None:
            raise ValueError(
                "only a document-identified determined outcome can be projected"
            )
        content_status = project_content_status(outcome.extraction_status)
        return cls(
            document_id=outcome.document_id,
            study_id=outcome.study_id,
            source_sha256=outcome.source_sha256,
            content_status=content_status,
            extraction_method=determined_outcome_method(outcome.attempts),
        )


class ExtractionManifestReference(ExtractionModel):
    """Self-reference of the E2 sidecar (the sidecar's own ``EXT-`` identity)."""

    manifest_id: str
    manifest_type: str = EXTRACTION_MANIFEST_TYPE
    workspace_relative_path: str
    artifact_checksum: str

    @field_validator("manifest_id")
    @classmethod
    def validate_manifest_id(cls, value: str) -> str:
        if _MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("manifest_id must be an opaque EXT- digest")
        return value

    @field_validator("manifest_type")
    @classmethod
    def validate_manifest_type(cls, value: str) -> str:
        if value != EXTRACTION_MANIFEST_TYPE:
            raise ValueError("manifest_type must be pdf_extraction_manifest")
        return value

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("artifact_checksum")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        return _validate_sha256(value)


class DocumentManifestCandidate(ExtractionModel):
    """A *non-authoritative* frozen ``document_manifest`` candidate.

    ``artifact_id`` is derived, not minted: it is a pure function of the
    candidate payload (see ``contract_candidate``), so an identical payload
    reproduces the same identity and any payload change is a different artifact
    rather than a silent overwrite of an existing one.  ``contract_acceptance``
    is fixed to ``not_performed_by_kit``: only a harness caller performing
    ``accept_artifact`` can make such an artifact authoritative, and this kit
    never writes a Contract registry entry and never claims acceptance.
    """

    artifact_id: str
    artifact_type: str = CONTRACT_ARTIFACT_TYPE
    payload_sha256: str
    contract_acceptance: str = CONTRACT_ACCEPTANCE_NOT_PERFORMED
    payload: dict[str, Any]

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _ARTIFACT_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "candidate artifact_id must use the registered ART- prefix"
            )
        return value

    @field_validator("artifact_type")
    @classmethod
    def validate_artifact_type(cls, value: str) -> str:
        if value != CONTRACT_ARTIFACT_TYPE:
            raise ValueError("candidate artifact_type must be document_manifest")
        return value

    @field_validator("payload_sha256")
    @classmethod
    def validate_payload_checksum(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("contract_acceptance")
    @classmethod
    def validate_acceptance_claim(cls, value: str) -> str:
        if value != CONTRACT_ACCEPTANCE_NOT_PERFORMED:
            raise ValueError(
                "the PDF kit can never report Contract acceptance as performed"
            )
        return value

    @model_validator(mode="after")
    def payload_matches_checksum(self) -> Self:
        if canonical_fingerprint(self.payload) != self.payload_sha256:
            raise ValueError("candidate payload_sha256 does not match the payload")
        if self.payload.get("artifact_id") != self.artifact_id:
            raise ValueError("candidate payload must carry its own artifact_id")
        return self


class ExtractionManifest(ExtractionModel):
    """The ``pdf-extraction-manifest-v1`` sidecar: the E2 commit marker."""

    schema_version: str = EXTRACTION_MANIFEST_SCHEMA_VERSION
    manifest_type: str = EXTRACTION_MANIFEST_TYPE
    manifest_id: str
    manifest_identity_algorithm_version: str = "v1"
    workspace_id: str
    run_id: str
    protocol_fingerprint: str
    corpus_fingerprint: str
    acquisition_manifest_ref: ManifestReference
    screening_decisions_ref: ArtifactReference
    parent_lineage_sha256: str
    producer: ProducerProvenance
    usability_profile: UsabilityProfile
    records: list[ExtractedDocumentRecord] = Field(default_factory=list)
    item_outcomes: list[ExtractionItemOutcome] = Field(min_length=1)
    idempotency_key: str
    manifest_payload_fingerprint: str
    artifact_checksum: str
    committed_at: str
    operation: ManifestOperation

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != EXTRACTION_MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported extraction manifest schema: {value}")
        return value

    @field_validator("manifest_type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        if value != EXTRACTION_MANIFEST_TYPE:
            raise ValueError("manifest_type must be pdf_extraction_manifest")
        return value

    @field_validator("manifest_id")
    @classmethod
    def validate_manifest_id(cls, value: str) -> str:
        if _MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("manifest_id must be an opaque EXT- digest")
        return value

    @field_validator("manifest_identity_algorithm_version")
    @classmethod
    def validate_manifest_identity_version(cls, value: str) -> str:
        if value != "v1":
            raise ValueError("unsupported extraction manifest identity version")
        return value

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, value: str) -> str:
        if _WORKSPACE_ID_RE.fullmatch(value) is None:
            raise ValueError("workspace_id must use the registered WSP- prefix")
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if _RUN_ID_RE.fullmatch(value) is None:
            raise ValueError("run_id must use the registered RUN- prefix")
        return value

    @field_validator(
        "protocol_fingerprint",
        "corpus_fingerprint",
        "parent_lineage_sha256",
        "idempotency_key",
        "manifest_payload_fingerprint",
        "artifact_checksum",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("committed_at")
    @classmethod
    def validate_committed_at(cls, value: str) -> str:
        return _validate_utc_timestamp(value)

    @model_validator(mode="after")
    def coherent_manifest(self) -> Self:
        # The sidecar carries its own extraction `run_id`; the E1 acquisition it
        # references lives under *E1's* run.  So the E1 path is checked against
        # the E1 storage convention and its own identity name -- not against
        # `self.run_id`, which is a different run by construction.
        acquisition_path = PurePosixPath(
            self.acquisition_manifest_ref.workspace_relative_path
        )
        prefix_parts = PurePosixPath(ACQUISITION_STORAGE_PREFIX).parts
        expected_acquisition_name = f"{self.acquisition_manifest_ref.manifest_id}.json"
        if (
            acquisition_path.name != expected_acquisition_name
            or len(acquisition_path.parts) != len(prefix_parts) + 2
            or acquisition_path.parts[: len(prefix_parts)] != prefix_parts
            or not acquisition_path.parts[len(prefix_parts)].startswith("RUN-")
        ):
            raise ValueError(
                "the acquisition reference must target its E1 run path exactly"
            )
        if self.screening_decisions_ref.artifact_id[:4] == "ACQ-":
            raise ValueError("the screening parent reference must not target an ACQ-")
        # One *current* row per document, not per study: a single E1 study may
        # commit several ``DOC-*`` records, and each one owns its own extraction
        # truth.  A row without a document identity is a pre-commit rejection
        # that never reached a document, and it keeps ``""`` as its sort/order key.
        #
        # Section 6.7(9) permits exactly one extra shape: a *superseded*
        # non-byte-bearing row for a document that this sidecar supersedes.  Such
        # a row is retained so the prior outcome stays visible by reference, and
        # it must name the prior ``EXT-`` id and status it was superseded from --
        # an unexplained second row for a document would be relabelled history.
        outcome_keys = [
            (item.study_id, item.document_id or "") for item in self.item_outcomes
        ]
        if outcome_keys != sorted(outcome_keys):
            raise ValueError("item outcomes must be sorted deterministically")
        current_keys = [
            (item.study_id, item.document_id or "")
            for item in self.item_outcomes
            if not item.is_superseded
        ]
        if len(current_keys) != len(set(current_keys)):
            raise ValueError("item outcomes must contain one current row per document")
        for item in self.item_outcomes:
            if not item.is_superseded:
                continue
            if item.document_id is None:
                raise ValueError("a superseded outcome must carry a document identity")
            if item.extraction_status in BYTE_BEARING_STATUSES:
                raise ValueError(
                    "a superseded outcome must be a non-byte-bearing prior status"
                )
            if (
                item.prior_outcome_manifest_id is None
                or item.prior_outcome_status is None
            ):
                raise ValueError(
                    "a superseded outcome must reference its prior EXT- id and status"
                )
        # Section 6.7(9) retains the prior outcome *alongside* the new
        # byte-bearing record, so a superseded row whose document has no current
        # byte-bearing row in this sidecar is retained history for a recovery that
        # never happened.
        current_committed = {
            item.document_id
            for item in self.item_outcomes
            if item.extraction_status in BYTE_BEARING_STATUSES
        }
        for item in self.item_outcomes:
            if item.is_superseded and item.document_id not in current_committed:
                raise ValueError(
                    "a superseded outcome requires the successor record it was replaced by"
                )
        record_order = [
            (record.study_id, record.document_id) for record in self.records
        ]
        if record_order != sorted(record_order):
            raise ValueError("records must be sorted deterministically")
        if len({record.document_id for record in self.records}) != len(self.records):
            raise ValueError("records must have unique document identities")
        if len({record.extracted_path for record in self.records}) != len(self.records):
            raise ValueError("records must have unique extracted paths")
        for record in self.records:
            if (
                record.acquisition_manifest_id
                != self.acquisition_manifest_ref.manifest_id
                or record.acquisition_manifest_sha256
                != self.acquisition_manifest_ref.artifact_checksum
                or record.acquisition_manifest_path
                != self.acquisition_manifest_ref.workspace_relative_path
            ):
                raise ValueError(
                    "every record must embed the accepted acquisition reference"
                )
        committed = {
            item.document_id
            for item in self.item_outcomes
            if item.extraction_status in BYTE_BEARING_STATUSES
            and not item.is_superseded
        }
        if committed != {record.document_id for record in self.records}:
            raise ValueError("records and byte-bearing item outcomes must agree")
        by_document = {record.document_id: record for record in self.records}
        for outcome in self.item_outcomes:
            if outcome.is_superseded:
                # A retained superseded row is sidecar history: it states what a
                # *prior* manifest claimed for this document, not what this
                # manifest commits.  Binding it against the current record would
                # make every section 6.7(9) successor commit unconstructible.
                continue
            record = by_document.get(outcome.document_id or "")
            if record is None:
                continue
            if (
                outcome.extracted_path != record.extracted_path
                or outcome.extracted_sha256 != record.extracted_sha256
                or outcome.content_status != record.content_status
                or outcome.character_count != record.character_count
            ):
                raise ValueError("outcome and record bindings must agree")
        committed_count = len(self.records)
        # A retained superseded row is sidecar history, not a requested document,
        # so the batch-status invariants below count only the current rows.
        requested_count = sum(
            1 for item in self.item_outcomes if not item.is_superseded
        )
        if self.operation.status is OperationStatus.SUCCESS:
            if committed_count != requested_count:
                raise ValueError("SUCCESS sidecar requires every item to commit")
        elif self.operation.status is OperationStatus.PARTIAL:
            if not 0 < committed_count < requested_count:
                raise ValueError("PARTIAL sidecar requires a committed subset")
        elif committed_count:
            raise ValueError("a FAILED/CANCELLED sidecar cannot carry records")
        return self


class ExtractionOperationData(ExtractionModel):
    manifest_reference: ExtractionManifestReference | None = None
    item_outcomes: list[ExtractionItemOutcome]
    committed_count: int = Field(ge=0)
    requested_count: int = Field(ge=1)
    candidate: DocumentManifestCandidate | None = None

    @model_validator(mode="after")
    def coherent_data(self) -> Self:
        if self.requested_count != len(self.item_outcomes):
            raise ValueError("requested_count must match item_outcomes")
        committed = sum(
            item.extraction_status in BYTE_BEARING_STATUSES
            for item in self.item_outcomes
        )
        if self.committed_count != committed:
            raise ValueError("committed_count must match byte-bearing outcomes")
        if self.candidate is not None and not committed:
            raise ValueError(
                "a Contract candidate requires at least one byte-bearing record"
            )
        return self


class ExtractionBatchOutcome(ExtractionModel):
    """Standard API/CLI operation envelope for one extraction run."""

    schema_version: str = OPERATION_SCHEMA_VERSION
    contract_version: str = CONTRACT_VERSION
    operation: str = OPERATION_NAME
    stage: ExtractionStage = ExtractionStage.COMPLETE
    run_id: str
    status: OperationStatus
    data: ExtractionOperationData
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[StructuredError] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def supported_operation_schema(cls, value: str) -> str:
        if value != OPERATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported extraction operation schema: {value}")
        return value

    @field_validator("operation")
    @classmethod
    def supported_operation(cls, value: str) -> str:
        if value != OPERATION_NAME:
            raise ValueError(f"unsupported extraction operation: {value}")
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if _RUN_ID_RE.fullmatch(value) is None:
            raise ValueError("run_id must use the registered RUN- prefix")
        return value

    @model_validator(mode="after")
    def coherent_envelope(self) -> Self:
        if self.status in {OperationStatus.FAILED, OperationStatus.CANCELLED} and not (
            self.errors or self.warnings
        ):
            raise ValueError("FAILED/CANCELLED envelopes require diagnostics")
        if self.status is OperationStatus.SUCCESS and self.errors:
            raise ValueError("SUCCESS envelopes cannot contain errors")
        if self.status is OperationStatus.PARTIAL and not (
            self.errors or self.warnings
        ):
            raise ValueError("PARTIAL envelopes must explain degradation")
        if self.artifacts:
            raise ValueError(
                "an extraction envelope exposes no accepted Contract artifact; the "
                "candidate stays a non-authoritative reference"
            )
        if self.data.candidate is not None and self.data.committed_count == 0:
            raise ValueError("a candidate requires a committed extraction record")
        return self


class ExtractionRunConfig(ExtractionModel):
    """Serializable CLI input; absolute roots are inputs and never committed."""

    requests: list[ExtractionRequest] = Field(min_length=1)
    accepted_parents: list[AcceptedParentBinding] = Field(min_length=1)
    workspace_bindings: dict[str, WorkspaceRootBinding]
    producer: ProducerProvenance

    @model_validator(mode="after")
    def sufficient_registry(self) -> Self:
        parent_ids = [parent.artifact_id for parent in self.accepted_parents]
        if len(parent_ids) != len(set(parent_ids)):
            raise ValueError("accepted parent registry IDs must be unique")
        for request in self.requests:
            if request.screening_decisions.artifact_id not in parent_ids:
                raise ValueError("run config is missing the screening parent entry")
            binding = self.workspace_bindings.get(request.workspace_id)
            if binding is None:
                raise ValueError("run config is missing the workspace root binding")
            if request.workspace_root != binding.canonical_root:
                raise ValueError(
                    "request workspace_root does not match its accepted binding"
                )
        return self
