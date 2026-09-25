"""Typed models for the deterministic WP01-E1 acquired-document boundary."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .canonical import canonical_fingerprint, normalize_doi

REQUEST_SCHEMA_VERSION = "pdf-acquisition-request-v1"
MANIFEST_SCHEMA_VERSION = "pdf-acquisition-manifest-v1"
MANIFEST_TYPE = "pdf_acquisition_manifest"
CONTRACT_VERSION = "1.0.0"
PDF_MEDIA_TYPE = "application/pdf"
DEFAULT_VALIDATION_PROFILE = "strict-pdf"
DEFAULT_VALIDATION_PROFILE_VERSION = "1.0.0"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^ART-[A-Za-z0-9][A-Za-z0-9._-]*$")
_WORKSPACE_ID_RE = re.compile(r"^WSP-[A-Za-z0-9][A-Za-z0-9._-]*$")
_RUN_ID_RE = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9._-]*$")
_STUDY_ID_RE = re.compile(r"^(?:STU|SCI)-[A-Za-z0-9][A-Za-z0-9._-]*$")
_DOCUMENT_ID_RE = re.compile(r"^DOC-[0-9a-f]{32}$")
_MANIFEST_ID_RE = re.compile(r"^ACQ-[0-9a-f]{32}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class AcquisitionModel(BaseModel):
    """Strict, JSON-compatible base model for the acquisition boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AcquisitionStatus(StrEnum):
    ACQUIRED = "ACQUIRED"
    REUSED = "REUSED"
    UNRESOLVED = "UNRESOLVED"
    NOT_FOUND = "NOT_FOUND"
    NETWORK_FAILED = "NETWORK_FAILED"
    INVALID_CONTENT = "INVALID_CONTENT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AccessStatus(StrEnum):
    VERIFIED_OPEN_ACCESS = "VERIFIED_OPEN_ACCESS"
    USER_PROVIDED = "USER_PROVIDED"
    UNRESOLVED = "UNRESOLVED"
    RESTRICTED_CONFIRMED = "RESTRICTED_CONFIRMED"


class AcquisitionSourceKind(StrEnum):
    OPENALEX = "OPENALEX"
    UNPAYWALL = "UNPAYWALL"
    PUBLISHER_PATTERN = "PUBLISHER_PATTERN"
    USER_PATH = "USER_PATH"
    OTHER = "OTHER"


class SourceMode(StrEnum):
    DISCOVERY = "DISCOVERY"
    USER_PATH = "USER_PATH"


class OperationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MethodProvenance(StrEnum):
    HUMAN = "HUMAN"
    EXTERNAL_PROVIDER = "EXTERNAL_PROVIDER"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"


class AcquisitionStage(StrEnum):
    PREFLIGHT = "PREFLIGHT"
    TRANSPORT = "TRANSPORT"
    STAGING = "STAGING"
    VALIDATION = "VALIDATION"
    PROMOTION = "PROMOTION"
    MANIFEST = "MANIFEST"
    AUDIT = "AUDIT"
    COMPLETE = "COMPLETE"


def validate_portable_relative_path(value: str) -> str:
    """Validate a durable workspace-relative POSIX path."""

    if not value or "\\" in value or "\x00" in value:
        raise ValueError("path must be a non-empty workspace-relative POSIX path")
    if _DRIVE_RE.match(value) or value.startswith("/"):
        raise ValueError("path must not be absolute or contain a drive prefix")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] in {"", "."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must remain inside the workspace")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("path must use its normalized POSIX spelling")
    return value


def _validate_sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError("value must be sha256:<64 lowercase hex characters>")
    return value


def _validate_prefixed_id(value: str, prefix: str) -> str:
    if not value.startswith(prefix) or not value[len(prefix) :]:
        raise ValueError(f"identifier must start with {prefix} and have a suffix")
    return value


class ParentArtifactInput(AcquisitionModel):
    artifact_id: str
    artifact_type: str
    sha256: str
    workspace_relative_path: str

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _ARTIFACT_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id must use the registered ART- prefix")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class ParentArtifactInputs(AcquisitionModel):
    corpus_snapshot: ParentArtifactInput
    screening_decisions: ParentArtifactInput

    @model_validator(mode="after")
    def exact_types(self) -> Self:
        if self.corpus_snapshot.artifact_type != "corpus_snapshot":
            raise ValueError("corpus_snapshot parent has the wrong artifact_type")
        if self.screening_decisions.artifact_type != "screening_decisions":
            raise ValueError("screening_decisions parent has the wrong artifact_type")
        return self


class AccessAssertion(AcquisitionModel):
    supplied_by: str = Field(min_length=1, max_length=320)
    permission_basis: str = Field(min_length=1, max_length=1000)


class AcquisitionRequest(AcquisitionModel):
    """One immutable, parent-bound request for a single accepted study."""

    schema_version: str = REQUEST_SCHEMA_VERSION
    workspace_id: str
    workspace_root: Path
    run_id: str
    study_id: str
    protocol_fingerprint: str
    corpus_fingerprint: str
    inputs: ParentArtifactInputs
    source_mode: SourceMode
    validation_profile: str = DEFAULT_VALIDATION_PROFILE
    validation_profile_version: str = DEFAULT_VALIDATION_PROFILE_VERSION
    doi: str | None = None
    source_doi: str | None = None
    source_kind: AcquisitionSourceKind = AcquisitionSourceKind.OTHER
    requested_source: str | None = None
    selected_source_url: str | None = None
    source_path: Path | None = None
    access_assertion: AccessAssertion | None = None
    access_status: AccessStatus = AccessStatus.UNRESOLVED
    provider_evidence: dict[str, Any] = Field(default_factory=dict)
    title_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    storage_prefix: str = "pdfs/acquired"
    institutional_gateway_url: str | None = None
    forward_proxy_url: str | None = None
    allow_external_source: bool = False
    external_source_label: str | None = None

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != REQUEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported acquisition request schema: {value}")
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

    @field_validator("doi", "source_doi")
    @classmethod
    def normalize_optional_doi(cls, value: str | None) -> str | None:
        return normalize_doi(value) if value is not None else None

    @field_validator("storage_prefix")
    @classmethod
    def validate_storage_prefix(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("external_source_label")
    @classmethod
    def validate_external_label(cls, value: str | None) -> str | None:
        if (
            value is not None
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is None
        ):
            raise ValueError("external_source_label must be a short opaque label")
        return value

    @field_validator("workspace_root", "source_path")
    @classmethod
    def require_absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("workspace_root and source_path must be absolute")
        return value

    @model_validator(mode="after")
    def source_contract(self) -> Self:
        if self.source_mode is SourceMode.USER_PATH:
            if self.source_path is None:
                raise ValueError("USER_PATH requires source_path")
            if self.access_assertion is None:
                raise ValueError("USER_PATH requires access_assertion")
            if self.source_kind is not AcquisitionSourceKind.USER_PATH:
                raise ValueError("USER_PATH requires source_kind=USER_PATH")
            if self.access_status is not AccessStatus.USER_PROVIDED:
                raise ValueError("USER_PATH requires access_status=USER_PROVIDED")
            if (
                self.selected_source_url is not None
                or self.requested_source is not None
            ):
                raise ValueError("USER_PATH must not fabricate a network source")
        else:
            if self.source_path is not None or self.access_assertion is not None:
                raise ValueError("DISCOVERY forbids source_path/access_assertion")
            if self.source_kind is AcquisitionSourceKind.USER_PATH:
                raise ValueError("DISCOVERY cannot use source_kind=USER_PATH")
            if self.access_status is AccessStatus.USER_PROVIDED:
                raise ValueError("DISCOVERY cannot claim USER_PROVIDED access")
            if self.access_status is AccessStatus.VERIFIED_OPEN_ACCESS:
                open_access = self.provider_evidence.get("open_access")
                locations = (
                    self.provider_evidence.get("best_oa_location"),
                    self.provider_evidence.get("primary_location"),
                )
                has_oa_evidence = (
                    self.provider_evidence.get("is_oa") is True
                    or self.provider_evidence.get("oa_status")
                    in {"gold", "green", "hybrid", "bronze"}
                    or bool(self.provider_evidence.get("url_for_pdf"))
                    or (
                        isinstance(open_access, dict)
                        and open_access.get("is_oa") is True
                    )
                    or any(
                        isinstance(location, dict)
                        and bool(location.get("pdf_url") or location.get("url_for_pdf"))
                        for location in locations
                    )
                )
                if not has_oa_evidence:
                    raise ValueError(
                        "VERIFIED_OPEN_ACCESS requires preserved provider OA evidence"
                    )
            if self.access_status is AccessStatus.RESTRICTED_CONFIRMED:
                if self.provider_evidence.get("restricted") is not True:
                    raise ValueError(
                        "RESTRICTED_CONFIRMED requires explicit named-source evidence"
                    )
                if not any(
                    isinstance(self.provider_evidence.get(key), str)
                    and self.provider_evidence[key].strip()
                    for key in ("source", "publisher")
                ):
                    raise ValueError(
                        "RESTRICTED_CONFIRMED requires a named provider source"
                    )
            if (
                self.access_status is AccessStatus.VERIFIED_OPEN_ACCESS
                and self.provider_evidence.get("restricted") is True
            ):
                raise ValueError(
                    "access evidence cannot claim both verified OA and restriction"
                )
        if (
            self.source_path is not None
            and not self.allow_external_source
            and not self.source_path.is_relative_to(self.workspace_root)
        ):
            raise ValueError("external USER_PATH requires allow_external_source=true")
        return self


class AcceptedParentBinding(AcquisitionModel):
    """One exact accepted Contract v1 parent and its verified payload."""

    artifact_id: str
    artifact_type: str
    sha256: str
    workspace_relative_path: str
    workspace_id: str
    run_id: str
    protocol_fingerprint: str
    corpus_fingerprint: str
    payload: dict[str, Any]

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _ARTIFACT_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id must use the registered ART- prefix")
        return value

    @field_validator("artifact_type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in {"corpus_snapshot", "screening_decisions"}:
            raise ValueError("E1 accepts only corpus and screening parent types")
        return value

    @field_validator("sha256", "protocol_fingerprint", "corpus_fingerprint")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

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

    @model_validator(mode="after")
    def payload_envelope_matches(self) -> Self:
        expected = {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "protocol_fingerprint": self.protocol_fingerprint,
            "corpus_fingerprint": self.corpus_fingerprint,
        }
        for field, expected_value in expected.items():
            if self.payload.get(field) != expected_value:
                raise ValueError(
                    f"accepted parent payload {field} does not match binding"
                )
        if canonical_fingerprint(self.payload) != self.sha256:
            raise ValueError(
                "accepted parent sha256 does not match its canonical payload"
            )
        return self


class WorkspaceRootBinding(AcquisitionModel):
    """Canonical root binding verified before any output path is touched."""

    workspace_id: str
    canonical_root: Path
    binding_fingerprint: str
    binding_algorithm_version: str = "v1"

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, value: str) -> str:
        if _WORKSPACE_ID_RE.fullmatch(value) is None:
            raise ValueError("workspace_id must use the registered WSP- prefix")
        return value

    @field_validator("canonical_root")
    @classmethod
    def require_absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("canonical_root must be absolute")
        return value

    @field_validator("binding_fingerprint")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class AcquisitionAttempt(AcquisitionModel):
    ordinal: int = Field(ge=1)
    source_kind: AcquisitionSourceKind
    requested_source: str
    result: AcquisitionStatus
    resolved_source: str | None = None
    gateway_used: bool = False
    forward_proxy_used: bool = False
    observed_media_type: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    diagnostic_code: str | None = Field(default=None, max_length=128)
    diagnostic_message: str | None = Field(default=None, max_length=500)
    provider_evidence: dict[str, Any] = Field(default_factory=dict)
    attempted_at: str | None = None


class AcquiredDocumentRecord(AcquisitionModel):
    document_id: str
    document_identity_algorithm_version: str = "v1"
    study_id: str
    source_kind: AcquisitionSourceKind
    source_sha256: str
    byte_length: int = Field(gt=0)
    media_type: str = PDF_MEDIA_TYPE
    workspace_relative_path: str
    acquisition_status: AcquisitionStatus
    access_status: AccessStatus
    access_assertion: AccessAssertion | None = None
    selected_source_url: str | None = None
    selected_source: str
    normalized_doi: str | None = None
    validation_profile: str
    validation_profile_version: str
    acquisition_method: MethodProvenance
    attempts: list[AcquisitionAttempt] = Field(min_length=1)

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

    @field_validator("source_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("media_type")
    @classmethod
    def require_pdf(cls, value: str) -> str:
        if value != PDF_MEDIA_TYPE:
            raise ValueError("validated acquired media_type must be application/pdf")
        return value

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("normalized_doi")
    @classmethod
    def normalize_doi(cls, value: str | None) -> str | None:
        return normalize_doi(value) if value is not None else None

    @model_validator(mode="after")
    def coherent_source(self) -> Self:
        if self.acquisition_status not in {
            AcquisitionStatus.ACQUIRED,
            AcquisitionStatus.REUSED,
        }:
            raise ValueError("only ACQUIRED/REUSED records may carry bytes")
        if self.source_kind is AcquisitionSourceKind.USER_PATH:
            if self.selected_source_url is not None:
                raise ValueError("USER_PATH selected_source_url must be null")
            if self.access_status is not AccessStatus.USER_PROVIDED:
                raise ValueError("USER_PATH record requires USER_PROVIDED access")
            if self.access_assertion is None:
                raise ValueError("USER_PATH record requires access_assertion")
            validate_portable_relative_path(self.selected_source)
        elif self.selected_source_url is None:
            raise ValueError("network records require selected_source_url")
        return self


class StructuredError(AcquisitionModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(AcquisitionModel):
    """Validation evidence attached to a structured acquisition outcome."""

    profile: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    valid: bool
    media_type: str = PDF_MEDIA_TYPE
    byte_length: int = Field(ge=0)
    source_sha256: str | None = None
    structural: bool = False

    @field_validator("source_sha256")
    @classmethod
    def validate_optional_sha256(cls, value: str | None) -> str | None:
        return _validate_sha256(value) if value is not None else None


class AcquisitionItemOutcome(AcquisitionModel):
    study_id: str
    source_kind: AcquisitionSourceKind
    acquisition_status: AcquisitionStatus
    requested_source: str | None
    normalized_doi: str | None
    validation_profile: str
    validation_profile_version: str
    document_id: str | None = None
    selected_source: str | None = None
    selected_source_url: str | None = None
    workspace_relative_path: str | None = None
    access_status: AccessStatus
    attempts: list[AcquisitionAttempt] = Field(default_factory=list)
    stage: AcquisitionStage = AcquisitionStage.COMPLETE
    validation_result: ValidationResult | None = None
    source_sha256: str | None = None
    warning: StructuredError | None = None
    error: StructuredError | None = None

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

    @field_validator("normalized_doi")
    @classmethod
    def normalize_outcome_doi(cls, value: str | None) -> str | None:
        return normalize_doi(value) if value is not None else None

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_outcome_path(cls, value: str | None) -> str | None:
        return validate_portable_relative_path(value) if value is not None else None

    @field_validator("source_sha256")
    @classmethod
    def validate_optional_outcome_sha256(cls, value: str | None) -> str | None:
        return _validate_sha256(value) if value is not None else None

    @model_validator(mode="after")
    def coherent_outcome(self) -> Self:
        committed = self.acquisition_status in {
            AcquisitionStatus.ACQUIRED,
            AcquisitionStatus.REUSED,
        }
        if committed != (self.document_id is not None):
            raise ValueError("committed item outcomes require exactly one document_id")
        if committed and (
            self.selected_source is None or self.workspace_relative_path is None
        ):
            raise ValueError(
                "committed item outcomes require selected source and final path"
            )
        if committed and (
            self.source_sha256 is None
            or self.validation_result is None
            or not self.validation_result.valid
        ):
            raise ValueError(
                "committed item outcomes require successful validation evidence"
            )
        if (
            committed
            and self.validation_result is not None
            and self.validation_result.source_sha256 != self.source_sha256
        ):
            raise ValueError("outcome checksum and validation checksum must agree")
        if self.source_kind is AcquisitionSourceKind.USER_PATH and committed:
            if self.selected_source_url is not None:
                raise ValueError("USER_PATH outcome selected_source_url must be null")
            if self.selected_source is not None:
                validate_portable_relative_path(self.selected_source)
        if (
            self.acquisition_status
            in {
                AcquisitionStatus.UNRESOLVED,
                AcquisitionStatus.NOT_FOUND,
                AcquisitionStatus.NETWORK_FAILED,
                AcquisitionStatus.INVALID_CONTENT,
                AcquisitionStatus.IDENTITY_MISMATCH,
                AcquisitionStatus.CANCELLED,
                AcquisitionStatus.FAILED,
            }
            and self.error is None
        ):
            raise ValueError("non-committed item outcomes require an error")
        if committed and self.error is not None:
            raise ValueError("committed item outcomes cannot contain an error")
        return self


class ParentArtifactRef(AcquisitionModel):
    artifact_id: str
    artifact_type: str
    sha256: str
    workspace_id: str
    protocol_fingerprint: str
    corpus_fingerprint: str

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        return _validate_prefixed_id(value, "ART-")

    @field_validator("artifact_type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in {"corpus_snapshot", "screening_decisions"}:
            raise ValueError("unsupported acquisition parent artifact type")
        return value

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, value: str) -> str:
        return _validate_prefixed_id(value, "WSP-")

    @field_validator("sha256", "protocol_fingerprint", "corpus_fingerprint")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class ParentArtifactRefs(AcquisitionModel):
    corpus_snapshot: ParentArtifactRef
    screening_decisions: ParentArtifactRef


class ProducerProvenance(AcquisitionModel):
    package: str = Field(default="scholar-pdf-kit", min_length=1)
    version: str = Field(min_length=1)
    commit: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")


class ManifestOperation(AcquisitionModel):
    status: OperationStatus
    errors: list[StructuredError] = Field(default_factory=list)
    warnings: list[StructuredError] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent_status(self) -> Self:
        if (
            self.status in {OperationStatus.FAILED, OperationStatus.CANCELLED}
            and not self.errors
            and not self.warnings
        ):
            raise ValueError("FAILED/CANCELLED manifest operations need diagnostics")
        if self.status is OperationStatus.SUCCESS and self.errors:
            raise ValueError("SUCCESS manifest operation cannot contain errors")
        if self.status is OperationStatus.PARTIAL and not (
            self.errors or self.warnings
        ):
            raise ValueError("PARTIAL manifest operation must explain degradation")
        return self


class E2EmbeddedReference(AcquisitionModel):
    """Reference E2 must carry into its extracted-document lineage sidecar."""

    acquisition_manifest_id: str = Field(
        validation_alias=AliasChoices("acquisition_manifest_id", "manifest_id")
    )
    acquisition_manifest_type: str = Field(
        default=MANIFEST_TYPE,
        validation_alias=AliasChoices("acquisition_manifest_type", "manifest_type"),
    )
    acquisition_manifest_path: str = Field(
        validation_alias=AliasChoices(
            "acquisition_manifest_path", "workspace_relative_path"
        )
    )
    # E1 embeds the location reference without hashing the self-referential
    # manifest into itself.  E2 copies the final E1 artifact_checksum into its
    # own document record (or provenance sidecar) after loading this manifest.
    acquisition_manifest_sha256: str | None = None

    @property
    def manifest_id(self) -> str:
        """Compatibility accessor for the provisional E1 field name."""

        return self.acquisition_manifest_id

    @property
    def manifest_type(self) -> str:
        """Compatibility accessor for the provisional E1 field name."""

        return self.acquisition_manifest_type

    @property
    def workspace_relative_path(self) -> str:
        """Compatibility accessor for the provisional E1 field name."""

        return self.acquisition_manifest_path

    @field_validator("acquisition_manifest_id")
    @classmethod
    def validate_manifest_id(cls, value: str) -> str:
        return _validate_prefixed_id(value, "ACQ-")

    @field_validator("acquisition_manifest_type")
    @classmethod
    def validate_manifest_type(cls, value: str) -> str:
        if value != MANIFEST_TYPE:
            raise ValueError("E2 reference must target pdf_acquisition_manifest")
        return value

    @field_validator("acquisition_manifest_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("acquisition_manifest_sha256")
    @classmethod
    def validate_optional_checksum(cls, value: str | None) -> str | None:
        return _validate_sha256(value) if value is not None else None


class AcquiredDocumentManifest(AcquisitionModel):
    schema_version: str = MANIFEST_SCHEMA_VERSION
    manifest_type: str = MANIFEST_TYPE
    manifest_id: str
    manifest_identity_algorithm_version: str = "v1"
    workspace_id: str
    run_id: str
    protocol_fingerprint: str
    corpus_fingerprint: str
    parent_refs: ParentArtifactRefs
    parent_lineage_sha256: str
    producer: ProducerProvenance
    records: list[AcquiredDocumentRecord]
    item_outcomes: list[AcquisitionItemOutcome] = Field(min_length=1)
    idempotency_key: str
    manifest_payload_fingerprint: str
    artifact_checksum: str
    e2_reference: E2EmbeddedReference
    operation: ManifestOperation

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported acquisition manifest schema: {value}")
        return value

    @field_validator("manifest_identity_algorithm_version")
    @classmethod
    def validate_manifest_identity_version(cls, value: str) -> str:
        if value != "v1":
            raise ValueError("unsupported acquisition manifest identity version")
        return value

    @field_validator("manifest_type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        if value != MANIFEST_TYPE:
            raise ValueError("manifest_type must be pdf_acquisition_manifest")
        return value

    @field_validator("manifest_id")
    @classmethod
    def validate_manifest_id(cls, value: str) -> str:
        if _MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("manifest_id must be an opaque ACQ- digest")
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

    @model_validator(mode="after")
    def coherent_manifest(self) -> Self:
        study_ids = [item.study_id for item in self.item_outcomes]
        if len(study_ids) != len(set(study_ids)):
            raise ValueError("manifest item outcomes must contain one row per study")
        outcome_order = [
            (item.study_id, item.document_id or "") for item in self.item_outcomes
        ]
        if outcome_order != sorted(outcome_order):
            raise ValueError("manifest item outcomes must be sorted deterministically")
        record_order = [
            (record.study_id, record.document_id) for record in self.records
        ]
        if record_order != sorted(record_order):
            raise ValueError("manifest records must be sorted deterministically")
        if len({record.document_id for record in self.records}) != len(self.records):
            raise ValueError("manifest records must have unique document identities")
        if len({record.workspace_relative_path for record in self.records}) != len(
            self.records
        ):
            raise ValueError("manifest records must have unique workspace paths")
        if self.e2_reference.acquisition_manifest_id != self.manifest_id:
            raise ValueError("E2 embedded reference must target this manifest")
        if self.e2_reference.acquisition_manifest_type != self.manifest_type:
            raise ValueError("E2 embedded reference has the wrong manifest type")
        if self.e2_reference.acquisition_manifest_sha256 is not None:
            raise ValueError("E1 cannot embed a self-referential manifest checksum")
        if not self.e2_reference.acquisition_manifest_path.endswith(
            f"/{self.manifest_id}.json"
        ):
            raise ValueError("E2 embedded reference has the wrong manifest path")
        for record in self.records:
            if PurePosixPath(record.workspace_relative_path).name != (
                f"{record.document_id}.pdf"
            ):
                raise ValueError(
                    "record final path must be document-identity addressed"
                )
        committed = [item for item in self.item_outcomes if item.document_id]
        if len(committed) != len(self.records):
            raise ValueError("every committed item outcome requires one record")
        if {item.document_id for item in committed} != {
            record.document_id for record in self.records
        }:
            raise ValueError("item outcomes and records must bind the same documents")
        records_by_id = {record.document_id: record for record in self.records}
        for outcome in committed:
            record = records_by_id[outcome.document_id]
            if (
                outcome.study_id != record.study_id
                or outcome.source_kind != record.source_kind
                or outcome.selected_source != record.selected_source
                or outcome.selected_source_url != record.selected_source_url
                or outcome.workspace_relative_path != record.workspace_relative_path
                or outcome.normalized_doi != record.normalized_doi
                or outcome.validation_profile != record.validation_profile
                or outcome.validation_profile_version
                != record.validation_profile_version
                or outcome.access_status != record.access_status
                or outcome.source_sha256 != record.source_sha256
                or (
                    outcome.validation_result is not None
                    and outcome.validation_result.byte_length != record.byte_length
                )
            ):
                raise ValueError("committed outcome and record bindings must agree")
        committed_count = len(committed)
        requested_count = len(self.item_outcomes)
        if self.operation.status is OperationStatus.SUCCESS:
            if committed_count != requested_count:
                raise ValueError("SUCCESS manifest requires every item to commit")
        elif self.operation.status is OperationStatus.PARTIAL:
            if not 0 < committed_count < requested_count:
                raise ValueError("PARTIAL manifest requires a committed subset")
        elif committed_count:
            raise ValueError("FAILED/CANCELLED manifest cannot carry records")
        return self


class ArtifactReference(AcquisitionModel):
    artifact_id: str
    path: str
    sha256: str

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        return _validate_prefixed_id(value, "ART-")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class ManifestReference(AcquisitionModel):
    manifest_id: str
    manifest_type: str = MANIFEST_TYPE
    workspace_relative_path: str
    artifact_checksum: str

    @field_validator("manifest_id")
    @classmethod
    def validate_manifest_id(cls, value: str) -> str:
        return _validate_prefixed_id(value, "ACQ-")

    @field_validator("manifest_type")
    @classmethod
    def validate_manifest_type(cls, value: str) -> str:
        if value != MANIFEST_TYPE:
            raise ValueError("manifest_type must be pdf_acquisition_manifest")
        return value

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("artifact_checksum")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class AcquisitionOperationData(AcquisitionModel):
    manifest_reference: ManifestReference | None = None
    item_outcomes: list[AcquisitionItemOutcome]
    committed_count: int = Field(ge=0)
    requested_count: int = Field(ge=1)

    @model_validator(mode="after")
    def coherent_data(self) -> Self:
        if self.requested_count != len(self.item_outcomes):
            raise ValueError("requested_count must match item_outcomes")
        committed = sum(item.document_id is not None for item in self.item_outcomes)
        if self.committed_count != committed:
            raise ValueError("committed_count must match committed item outcomes")
        return self


class AcquisitionBatchOutcome(AcquisitionModel):
    """Standard API/CLI operation envelope for one acquisition run."""

    schema_version: str = "pdf-acquisition-operation-v1"
    contract_version: str = CONTRACT_VERSION
    operation: str = "acquire_pdf"
    stage: AcquisitionStage = AcquisitionStage.COMPLETE
    run_id: str
    status: OperationStatus
    data: AcquisitionOperationData
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[StructuredError] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def supported_operation_schema(cls, value: str) -> str:
        if value != "pdf-acquisition-operation-v1":
            raise ValueError(f"unsupported acquisition operation schema: {value}")
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
        committed = self.data.committed_count
        if (
            self.status is OperationStatus.SUCCESS
            and committed != self.data.requested_count
        ):
            raise ValueError("SUCCESS envelopes require every requested item to commit")
        if (
            self.status in {OperationStatus.FAILED, OperationStatus.CANCELLED}
            and committed
        ):
            raise ValueError(
                "FAILED/CANCELLED envelopes cannot contain committed items"
            )
        return self


class AcquisitionRunConfig(AcquisitionModel):
    """Serializable CLI input; absolute roots are inputs and never committed."""

    requests: list[AcquisitionRequest] = Field(min_length=1)
    accepted_parents: list[AcceptedParentBinding] = Field(min_length=2)
    workspace_bindings: dict[str, WorkspaceRootBinding]
    producer: ProducerProvenance

    @model_validator(mode="after")
    def sufficient_registry(self) -> Self:
        parent_ids = [parent.artifact_id for parent in self.accepted_parents]
        if len(parent_ids) != len(set(parent_ids)):
            raise ValueError("accepted parent registry IDs must be unique")
        for request in self.requests:
            required = {
                request.inputs.corpus_snapshot.artifact_id,
                request.inputs.screening_decisions.artifact_id,
            }
            if not required.issubset(parent_ids):
                raise ValueError("run config is missing an accepted parent entry")
            binding = self.workspace_bindings.get(request.workspace_id)
            if binding is None:
                raise ValueError("run config is missing the workspace root binding")
            if request.workspace_root != binding.canonical_root:
                raise ValueError(
                    "request workspace_root does not match its accepted binding"
                )
        return self
