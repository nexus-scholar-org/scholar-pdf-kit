"""The E2 extraction service: parent-bound, deterministic, fail-closed.

This module owns the packet E2 commit state machine (section 7.3) for one
same-workspace/run batch:

1. preflight the accepted parents, the E1 acquisition manifest, its verified
   bytes, the workspace-root binding, containment, the requested engine, and the
   idempotency key -- *before* any output I/O;
2. run the engine chain per document, recording every attempt;
3. validate against the recorded usefulness rule and stage the committed bytes in
   the destination directory;
4. promote atomically at the identity-addressed path with no-replace semantics;
5. publish the ``pdf-extraction-manifest-v1`` sidecar, which is the commit marker;
6. construct the deterministic, non-authoritative Contract candidate;
7. append exactly one canonical workspace-manager audit event.

Two boundaries are load-bearing.

**E1 is reused, never reimplemented.**  Canonicalization, the document identity,
path containment, the lock file, staging, and the atomic no-replace promotion are
all E1 code paths; this service calls them through its
:class:`~scholar_pdf.acquisition.PDFAcquisitionService` collaborator (constructed
from the same accepted parents and bindings when the caller does not supply one)
rather than restating them.  Two copies of the same locking rule would be a
correctness bug waiting to happen.

**A failure is never a success.**  The legacy extractor wrote the stub line
``Extracted content from {name}`` and reported success on a parse failure.  Here
an engine failure, a below-threshold body, a stub body, and a missing text layer
are each a *determined* per-item outcome with an explicit reason, a
``FAILED``/``NEEDS_OCR`` frozen status, and no ``extracted_path``.  A rejection
before the engine chain (parent, lineage, or source bytes) is not even downgraded
to a failed document: no engine ran against verified bytes, so no document record
exists at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .acquisition import (
    AcquisitionCommitError,
    AcquisitionPreflightError,
    InMemoryAuditSink,
    PDFAcquisitionService,
    WorkspaceManagerCliAuditSink,
)
from .acquisition import AuditSink as AuditSinkProtocol
from .acquisition_models import (
    AcceptedParentBinding,
    AcquiredDocumentManifest,
    AcquiredDocumentRecord,
    ArtifactReference,
    ManifestOperation,
    ManifestReference,
    OperationStatus,
    ProducerProvenance,
    StructuredError,
    WorkspaceRootBinding,
)
from .canonical import (
    canonical_fingerprint,
    deterministic_document_id,
    deterministic_extraction_manifest_id,
)
from .contract_candidate import (
    CandidateContractError,
    build_document_manifest_candidate,
)
from .extraction_engines import (
    EngineFailure,
    EngineRegistry,
    ExtractionEngineAdapter,
    UnsupportedExtractionEngine,
    bound_diagnostic_code,
    default_engine_registry,
    engine_chain,
    sanitize_diagnostic,
)
from .extraction_models import (
    ACQUISITION_STORAGE_PREFIX,
    BYTE_BEARING_STATUSES,
    COMMIT_INTENT_SCHEMA_VERSION,
    DEFAULT_ENGINE_FALLBACK_ORDER,
    ENGINE_VERSION_UNKNOWN,
    EXTRACTION_MANIFEST_SCHEMA_VERSION,
    EXTRACTION_MANIFEST_TYPE,
    PRE_COMMIT_STATUSES,
    SCREENING_DECISIONS_ARTIFACT_TYPE,
    SIDECAR_STORAGE_PREFIX,
    ArtifactRecordProjection,
    AttemptResult,
    DocumentContentStatus,
    DocumentManifestCandidate,
    ExtractedDocumentRecord,
    ExtractionAttempt,
    ExtractionBatchOutcome,
    ExtractionItemOutcome,
    ExtractionManifest,
    ExtractionManifestReference,
    ExtractionMethod,
    ExtractionOperationData,
    ExtractionOutputFormat,
    ExtractionRequest,
    ExtractionRunConfig,
    ExtractionStage,
    ExtractionStatus,
    FallbackReason,
    FallbackStep,
    compute_extraction_idempotency_key,
    determined_outcome_method,
    extraction_method_for_engine,
    utc_now,
)
from .frontmatter import (
    CommittedFile,
    FrontmatterError,
    build_frontmatter_values,
    compose_extracted_file,
    is_legacy_stub,
    measure_extracted_body,
    parse_bound_frontmatter,
    sha256_bytes,
    verify_bound_frontmatter,
)

#: The one frontmatter key section 6.6 declares "provenance only; never in the
#: identity payload": ``extracted_at``, and nothing else.  Two runs of the same
#: document therefore produce the same body and the same ``extracted_sha256`` but
#: different file bytes, so this key is the only difference that may coalesce onto
#: the file already committed (see
#: ``ExtractionService._coalesce_existing_extraction``).  Adding a second key here
#: is a deliberate widening of what may differ between two runs of one identity,
#: not a refactor: it must be justified against section 6.6 and the identity
#: payload in section 6.4.
PROVENANCE_ONLY_FRONTMATTER_KEYS = frozenset({"extracted_at"})

_LOGGER = logging.getLogger(__name__)

#: The canonical extraction action name in the workspace-manager journal.
EXTRACTION_AUDIT_ACTION = "PDF_TEXT_EXTRACTION"

#: Deterministic diagnostic codes attached to a determined (non-byte-bearing) item.
REASON_UNUSABLE_OUTPUT = "ENGINE_OUTPUT_UNUSABLE"
REASON_BELOW_THRESHOLD = "USEFULNESS_BELOW_THRESHOLD"
REASON_STUB_OUTPUT = "LEGACY_STUB_OUTPUT_REJECTED"
REASON_NO_TEXT_LAYER = "NO_EXTRACTABLE_TEXT_LAYER"
REASON_CHAIN_EXHAUSTED = "ENGINE_CHAIN_EXHAUSTED"
REASON_ENGINE_SUBSTITUTED = "ENGINE_SUBSTITUTED"
REASON_PAGE_DEGRADED = "PAGE_WITHOUT_TEXT_LAYER"
#: Section 6.1: a cancelled item is accounted for and is never authoritative.
REASON_EXTRACTION_CANCELLED = "EXTRACTION_CANCELLED"

#: Per-sidecar lock suffix, mirroring E1's per-manifest lock.
SIDECAR_LOCK_SUFFIX = ".json.lock"

#: The suffix of a promoted-but-unbound extracted file's recovery marker.
EXTRACTED_SUFFIXES = {
    ExtractionOutputFormat.MARKDOWN: ".md",
    ExtractionOutputFormat.TEI_XML: ".tei.xml",
}

#: Non-volatile extraction inputs that packet 7.4's idempotency key deliberately
#: does not cover, because they are not part of the request-side document set:
#: they change the work and its bytes, so a rerun that changes them under an
#: existing key is a changed payload (``IDEMPOTENCY_CONFLICT``, E2-NEG-018) and
#: never a silent reuse.  They are recorded per attempt in ``request_shape``.
NON_KEYED_REQUEST_KEYS = ("grobid_url", "page_range")

#: Section 6.7(9) names the two determined statuses a retry may supersede.
#: ``CANCELLED`` was never an authoritative determination and a ``FAILED`` commit
#: row is a rejection rather than a determination, so neither is a supersedable
#: prior outcome of a byte-bearing commit.
SUPERSEDABLE_DETERMINED_STATUSES = frozenset(
    {ExtractionStatus.EXTRACTION_FAILED, ExtractionStatus.NO_TEXT_LAYER}
)

#: The fallback reasons that mean "this engine could not run here at all".  They
#: are what makes a repair *observable without running an engine*: whether an
#: adapter resolves now is a cheap probe, so a provider that came back up, an
#: engine installed since, or a library whose version moved is a deterministically
#: detectable environment change rather than a guess.
AVAILABILITY_FALLBACK_REASONS = frozenset(
    {
        FallbackReason.ENGINE_NOT_INSTALLED,
        FallbackReason.ENGINE_UNAVAILABLE,
        FallbackReason.ENGINE_LICENSE_MISSING,
    }
)


class ExtractionFault(StrEnum):
    """Deterministic fault-injection points for the E2 atomicity tests."""

    ENGINE = "engine"
    VALIDATION = "validation"
    CONTENT_MOVE = "content_move"
    SIDECAR_REPLACE = "sidecar_replace"
    AUDIT_APPEND = "audit_append"


class ExtractionPreflightError(Exception):
    """A fail-closed rejection that happens before any output I/O."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_error(self) -> StructuredError:
        return StructuredError(
            code=self.code,
            message=self.message,
            retryable=False,
            details=self.details,
        )


class ExtractionCommitError(Exception):
    """A committed-state or atomic-publication failure with bounded detail."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_error(self) -> StructuredError:
        return StructuredError(
            code=self.code,
            message=self.message,
            retryable=True,
            details=self.details,
        )


#: Request-level refusal codes: the request is invalid *before* any extraction
#: attempt, so the batch is refused rather than determined.
#:
#: Section 7.3(1) makes path containment a preflight verification and states that
#: "no extraction and no output are produced before this succeeds".  A
#: containment violation is therefore a refusal of the request, not a determined
#: extraction failure, and it must never mint an ``EXT-`` sidecar, occupy the
#: idempotency key space, or be replayed.
#:
#: The other request-level refusal codes this service defines (the parent,
#: lineage, acquisition-manifest, workspace-binding, and replay-rejection codes
#: raised as :class:`ExtractionPreflightError`) are already refused in preflight
#: by :meth:`PDFExtractionService._preflight_failure` with no sidecar, so they
#: cannot reach the commit path and are deliberately absent here.  Nothing is
#: added: a code belongs in this set only if the codebase already raises it as a
#: containment refusal.
REFUSAL_ERROR_CODES: frozenset[str] = frozenset({"PATH_OUTSIDE_WORKSPACE"})


@dataclass(frozen=True)
class _PreparedExtraction:
    """One snapshot-verified request plus the E1 context it may extract."""

    request: ExtractionRequest
    screening: AcceptedParentBinding
    workspace_root: Path
    manifest: AcquiredDocumentManifest
    manifest_path: Path
    documents: tuple[AcquiredDocumentRecord, ...]
    idempotency_key: str
    acquisition_ref: ManifestReference


@dataclass(frozen=True)
class _StagedExtraction:
    """One document's per-item result inside a batch."""

    record: ExtractedDocumentRecord | None
    outcome: ExtractionItemOutcome
    promoted_new: bool = False


@dataclass(frozen=True)
class _ChainResult:
    """The raw result of running one engine chain over verified PDF bytes."""

    attempts: tuple[ExtractionAttempt, ...]
    fallback: tuple[FallbackStep, ...]
    text: str
    effective_engine: str
    effective_version: str
    output_format: ExtractionOutputFormat
    page_count: int
    character_count: int
    text_layer_present: bool | None
    engine_degradation: tuple[str, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class _Evaluation:
    """The decided per-item extraction truth for one document."""

    status: ExtractionStatus
    content_status: DocumentContentStatus
    text: str
    output_format: ExtractionOutputFormat
    page_count: int
    character_count: int
    effective_engine: str
    effective_version: str
    degradation_reasons: tuple[str, ...]
    diagnostic_code: str | None
    diagnostic_message: str | None


class PDFExtractionService:
    """Own the E2 API while delegating engines, E1 verification, and audit."""

    def __init__(
        self,
        *,
        accepted_parents: Sequence[AcceptedParentBinding],
        workspace_bindings: Mapping[str, WorkspaceRootBinding],
        producer: ProducerProvenance,
        audit_sink: AuditSinkProtocol,
        engines: EngineRegistry | None = None,
        acquisition_service: PDFAcquisitionService | None = None,
        fault_injector: Any | None = None,
        fallback_order: tuple[str, ...] = tuple(
            engine.value.lower() for engine in DEFAULT_ENGINE_FALLBACK_ORDER
        ),
        verify_source_bytes: bool = True,
    ) -> None:
        self.accepted_parents = tuple(
            parent.model_copy(deep=True) for parent in accepted_parents
        )
        self.workspace_bindings = {
            workspace_id: binding.model_copy(deep=True)
            for workspace_id, binding in workspace_bindings.items()
        }
        self.producer = producer.model_copy(deep=True)
        self.audit_sink = audit_sink
        self.engines = engines or default_engine_registry()
        self.fault_injector = fault_injector
        self.fallback_order = tuple(fallback_order)
        self.verify_source_bytes = verify_source_bytes
        self._acquisition = acquisition_service or PDFAcquisitionService(
            accepted_parents=self.accepted_parents,
            workspace_bindings=self.workspace_bindings,
            producer=self.producer,
            audit_sink=audit_sink,
            fault_injector=fault_injector,
        )
        self._parent_by_id = {
            parent.artifact_id: parent for parent in self.accepted_parents
        }
        if len(self._parent_by_id) != len(self.accepted_parents):
            raise ValueError("accepted parent IDs must be unique")
        self._operation_locks: dict[
            tuple[asyncio.AbstractEventLoop, str], asyncio.Lock
        ] = {}

    @classmethod
    def from_config(
        cls,
        config: ExtractionRunConfig,
        *,
        audit_sink: AuditSinkProtocol,
        engines: EngineRegistry | None = None,
        fault_injector: Any | None = None,
    ) -> PDFExtractionService:
        """Build a service from a serializable run config."""

        return cls(
            accepted_parents=config.accepted_parents,
            workspace_bindings=config.workspace_bindings,
            producer=config.producer,
            audit_sink=audit_sink,
            engines=engines,
            fault_injector=fault_injector,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self, requests: Sequence[ExtractionRequest]
    ) -> ExtractionBatchOutcome:
        """Run one same-workspace/run extraction batch without lying about it."""

        if not requests:
            raise ValueError("at least one extraction request is required")
        snapshot = tuple(request.model_copy(deep=True) for request in requests)
        try:
            prepared = self._preflight(snapshot)
        except (ExtractionPreflightError, UnsupportedExtractionEngine) as error:
            if isinstance(error, UnsupportedExtractionEngine):
                error = ExtractionPreflightError(error.code, error.message)
            return self._preflight_failure(snapshot, error)
        except (ExtractionCommitError, CandidateContractError, ValueError) as error:
            return self._preflight_failure(
                snapshot,
                ExtractionPreflightError(
                    getattr(error, "code", "PREFLIGHT_INVALID"),
                    getattr(error, "message", str(error)),
                ),
            )
        first = prepared[0]
        lock = await self._operation_lock(first.request.workspace_id)
        async with lock:
            with self._extraction_publication_lock(first.workspace_root):
                try:
                    replay, prior, carried = self._find_run_replay(
                        first.workspace_root,
                        first.request.run_id,
                        first.idempotency_key,
                    )
                except ExtractionPreflightError as error:
                    return self._preflight_failure(snapshot, error)
                if replay is not None and carried:
                    # Section 6.7(9) successor over a *byte-bearing* commit: a
                    # sibling is re-driven because its repair is observable, so
                    # the published records are carried rather than re-extracted
                    # and the run may mint a new ``EXT-`` id under this key.
                    try:
                        self._verify_replay_inputs(replay[0], prepared)
                        self.verify_manifest(replay[0], verify_bytes=True)
                        return await self._extract_locked(
                            prepared,
                            prior_outcomes=prior,
                            carried=carried,
                            prior_replay=replay,
                        )
                    except ExtractionPreflightError as error:
                        return self._preflight_failure(snapshot, error)
                    except ExtractionCommitError as error:
                        return self._preflight_failure(
                            snapshot, self._published_commit_rejection(replay[0], error)
                        )
                if replay is not None:
                    try:
                        return await self._replay_outcome(replay, prepared)
                    except ExtractionPreflightError as error:
                        return self._preflight_failure(snapshot, error)
                    except ExtractionCommitError as error:
                        # A published sidecar whose bound source bytes or
                        # committed extracted bodies no longer verify is a
                        # fail-closed replay rejection, not an exception that
                        # escapes the public API (E2-NEG-017 / E2-NEG-040).  This
                        # mirrors E1's REPLAY_VERIFICATION_FAILED precedent
                        # (acquisition.py:600-607).
                        return self._preflight_failure(
                            snapshot,
                            self._published_commit_rejection(replay[0], error),
                        )
                return await self._extract_locked(prepared, prior_outcomes=prior)

    @staticmethod
    def _published_commit_rejection(
        manifest: ExtractionManifest, error: ExtractionCommitError
    ) -> ExtractionPreflightError:
        """Map a published commit's failed byte/checksum verification.

        The published sidecar is the commit marker, so a body, bound source, or
        lineage that no longer verifies is a fail-closed replay rejection rather
        than an exception that escapes the public API (``E2-NEG-017`` /
        ``E2-NEG-040``).  This mirrors E1's ``REPLAY_VERIFICATION_FAILED``
        precedent (``acquisition.py:600-607``).
        """

        return ExtractionPreflightError(
            "REPLAY_VERIFICATION_FAILED",
            "A matching extraction sidecar failed byte, checksum, or lineage "
            "verification.",
            manifest_id=manifest.manifest_id,
            manifest_checksum=manifest.artifact_checksum,
            failure_code=error.code,
        )

    def verify_manifest(
        self, manifest: ExtractionManifest, *, verify_bytes: bool
    ) -> None:
        """Verify a published sidecar exactly as the commit path constructed it."""

        raw = manifest.model_dump(mode="json")
        payload = dict(raw)
        payload.pop("manifest_payload_fingerprint", None)
        payload["artifact_checksum"] = None
        if canonical_fingerprint(payload) != manifest.manifest_payload_fingerprint:
            raise ExtractionCommitError(
                "MANIFEST_CHECKSUM_MISMATCH",
                "The extraction manifest payload fingerprint is invalid.",
            )
        checksum_payload = dict(raw)
        checksum_payload["artifact_checksum"] = None
        if canonical_fingerprint(checksum_payload) != manifest.artifact_checksum:
            raise ExtractionCommitError(
                "MANIFEST_CHECKSUM_MISMATCH",
                "The extraction manifest artifact checksum is invalid.",
            )
        expected_id = deterministic_extraction_manifest_id(
            schema_version=manifest.schema_version,
            workspace_id=manifest.workspace_id,
            run_id=manifest.run_id,
            acquisition_manifest_ref=manifest.acquisition_manifest_ref.model_dump(
                mode="json"
            ),
            extraction_records=self._stable_extraction_records(
                manifest.item_outcomes, manifest.records
            ),
            algorithm_version=manifest.manifest_identity_algorithm_version,
        )
        if expected_id != manifest.manifest_id:
            raise ExtractionCommitError(
                "MANIFEST_ID_MISMATCH", "The deterministic manifest ID is invalid."
            )
        expected_relative = (
            f"{SIDECAR_STORAGE_PREFIX}/{manifest.run_id}/{manifest.manifest_id}.json"
        )
        if manifest.run_id not in expected_relative:  # pragma: no cover - defensive
            raise ExtractionCommitError(
                "MANIFEST_PATH_MISMATCH", "The manifest path is not identity addressed."
            )
        if (
            canonical_fingerprint(
                {
                    "acquisition_manifest_ref": manifest.acquisition_manifest_ref.model_dump(
                        mode="json"
                    ),
                    "screening_decisions_ref": manifest.screening_decisions_ref.model_dump(
                        mode="json"
                    ),
                }
            )
            != manifest.parent_lineage_sha256
        ):
            raise ExtractionCommitError(
                "PARENT_LINEAGE_MISMATCH", "The parent lineage fingerprint is invalid."
            )
        for record in manifest.records:
            self.verify_document_identity(manifest, record)
        if not verify_bytes:
            return
        binding = self.workspace_bindings.get(manifest.workspace_id)
        if binding is None:
            raise ExtractionCommitError(
                "WORKSPACE_BINDING_MISSING",
                "The manifest workspace has no accepted canonical root binding.",
            )
        root = binding.canonical_root
        for record in manifest.records:
            try:
                self._verify_committed_file(root, record)
                source = self._safe_workspace_path(
                    root.resolve(strict=True),
                    record.source_workspace_relative_path,
                    create=False,
                )
            except (ExtractionCommitError, OSError, ValueError) as error:
                raise ExtractionCommitError(
                    "REUSED_CONTENT_INVALID",
                    "A manifest-bound extracted artifact is missing or changed.",
                ) from error
            if (
                not source.is_file()
                or source.stat().st_size != record.byte_length
                or self._hash_file(source) != record.source_sha256
            ):
                raise ExtractionCommitError(
                    "REUSED_CONTENT_INVALID",
                    "A manifest-bound source artifact is missing or changed.",
                )

    def verify_document_identity(
        self, manifest: ExtractionManifest, record: ExtractedDocumentRecord
    ) -> None:
        """Recompute the E1 document identity and require exact equality.

        E2 reuses the E1 identity and never re-derives a competing one, so a
        mismatch is an identity failure rather than an opportunity to re-mint.
        """

        expected = deterministic_document_id(
            study_id=record.study_id,
            source_hash=record.source_sha256,
            workspace_id=manifest.workspace_id,
            algorithm_version=record.document_identity_algorithm_version,
        )
        if expected != record.document_id:
            raise ExtractionCommitError(
                "BLOCKED_DOCUMENT_IDENTITY",
                "The E2 record document identity does not match the E1 identity.",
                document_id=record.document_id,
            )

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def _preflight(
        self, requests: Sequence[ExtractionRequest]
    ) -> list[_PreparedExtraction]:
        """Validate every request before a single byte of output is produced."""

        first = requests[0]
        batch_fields = (
            "workspace_id",
            "run_id",
            "protocol_fingerprint",
            "corpus_fingerprint",
            "acquisition_manifest_id",
            "acquisition_manifest_sha256",
            "acquisition_manifest_path",
            "requested_engine",
            "allow_fallback",
            "storage_prefix",
            "grobid_url",
            "page_range",
        )
        seen_keys: set[str] = set()
        studies: set[str] = set()
        prepared: list[_PreparedExtraction] = []
        for request in requests:
            for name in batch_fields:
                if getattr(request, name) != getattr(first, name):
                    raise ExtractionPreflightError(
                        "BATCH_SCOPE_MISMATCH",
                        "An extraction batch must share one workspace, run, engine, "
                        "and acquisition manifest.",
                        field=name,
                    )
            if request.screening_decisions.artifact_id != (
                first.screening_decisions.artifact_id
            ):
                raise ExtractionPreflightError(
                    "BATCH_SCOPE_MISMATCH",
                    "An extraction batch must share one screening parent.",
                )
            if request.usability_profile != first.usability_profile:
                raise ExtractionPreflightError(
                    "BATCH_SCOPE_MISMATCH",
                    "An extraction batch must share one versioned usability profile.",
                )
            if request.study_id in studies:
                raise ExtractionPreflightError(
                    "DUPLICATE_REQUEST",
                    "The batch names the same study twice.",
                    study_id=request.study_id,
                )
            studies.add(request.study_id)
            # An engine token outside the declared registry is a structural
            # rejection with no fallback (E2-013 / E2-NEG-010).
            self.engines.get(request.requested_engine)
            if request.allow_fallback:
                for token in self.fallback_order:
                    self.engines.get(token)
            item = self._prepare(request)
            if item.idempotency_key in seen_keys:
                raise ExtractionPreflightError(
                    "DUPLICATE_REQUEST",
                    "The batch contains two requests with the same document set.",
                    study_id=request.study_id,
                )
            seen_keys.add(item.idempotency_key)
            prepared.append(item)
        return prepared

    def _prepare(self, request: ExtractionRequest) -> _PreparedExtraction:
        binding = self.workspace_bindings.get(request.workspace_id)
        if binding is None:
            raise ExtractionPreflightError(
                "WORKSPACE_BINDING_MISSING",
                "The request workspace has no accepted canonical root binding.",
            )
        if request.workspace_root != binding.canonical_root:
            raise ExtractionPreflightError(
                "WORKSPACE_BINDING_MISMATCH",
                "The request workspace root does not match its accepted binding.",
            )
        try:
            workspace_root = binding.canonical_root.resolve(strict=True)
        except OSError as error:
            raise ExtractionPreflightError(
                "WORKSPACE_ROOT_UNAVAILABLE",
                "The canonical workspace root could not be resolved.",
            ) from error
        screening = self._verify_screening_parent(request, workspace_root)
        manifest, manifest_path = self._load_acquisition_manifest(
            request, workspace_root
        )
        documents = self._select_documents(request, manifest)
        self._anchor_storage_prefix(request, workspace_root)
        acquisition_ref = self._acquisition_reference(manifest)
        return _PreparedExtraction(
            request=request,
            screening=screening,
            workspace_root=workspace_root,
            manifest=manifest,
            manifest_path=manifest_path,
            documents=documents,
            acquisition_ref=acquisition_ref,
            idempotency_key=compute_extraction_idempotency_key(
                workspace_id=request.workspace_id,
                run_id=request.run_id,
                acquisition_manifest_ref=acquisition_ref,
                documents=[
                    (
                        record.document_id,
                        record.source_sha256,
                        request.requested_engine.upper(),
                    )
                    for record in documents
                ],
            ),
        )

    def _anchor_storage_prefix(
        self, request: ExtractionRequest, workspace_root: Path
    ) -> None:
        """Verify the output prefix can be anchored, before any engine runs.

        Section 7.3(1) lists path containment among the preflight verifications
        and states that "no extraction and no output are produced before this
        succeeds".  Anchoring the prefix here is therefore a *preflight* refusal,
        not a commit-time failure: the request is invalid on its face, so no
        engine reads verified bytes and no output is attempted.

        The check is the E1 anchor rule, reached through the shared
        :meth:`_safe_workspace_path` primitive rather than a second copy of it,
        and it runs with ``create=False`` so a valid prefix is not created as a
        side effect of being validated.  :meth:`extract` already maps a
        containment ``ExtractionCommitError`` raised here onto
        :meth:`_preflight_failure`, which reports ``FAILED`` with
        ``manifest_reference=None``, no sidecar, and no audit event -- so the
        refusal never occupies the idempotency key space and is never replayed.
        """

        try:
            self._safe_workspace_path(
                workspace_root, request.storage_prefix, create=False
            )
        except ExtractionCommitError as error:
            raise ExtractionPreflightError(
                error.code, error.message, **error.details
            ) from error

    def _verify_screening_parent(
        self, request: ExtractionRequest, workspace_root: Path
    ) -> AcceptedParentBinding:
        """Verify the accepted screening parent: registry, file, and fingerprint."""

        declared = request.screening_decisions
        parent = self._parent_by_id.get(declared.artifact_id)
        if parent is None:
            raise ExtractionPreflightError(
                "PARENT_NOT_ACCEPTED",
                "The screening parent is not present in the accepted parent registry.",
                artifact_id=declared.artifact_id,
            )
        if (
            parent.artifact_type != SCREENING_DECISIONS_ARTIFACT_TYPE
            or parent.sha256 != declared.sha256
            or parent.workspace_relative_path != declared.workspace_relative_path
        ):
            raise ExtractionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The screening parent declaration does not match the accepted binding.",
            )
        # The screening parent is an *upstream* artifact: its run_id is the
        # screening run, not this extraction run, so only the namespace and the
        # study lineage must agree.  E2-NEG-004's run_id rule binds the
        # acquisition manifest (`_load_acquisition_manifest`), not this
        # cross-stage parent.  E2-NEG-025/036 still fail closed on workspace.
        if (
            parent.workspace_id != request.workspace_id
            or parent.protocol_fingerprint != request.protocol_fingerprint
            or parent.corpus_fingerprint != request.corpus_fingerprint
        ):
            raise ExtractionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The screening parent belongs to a different workspace or lineage.",
            )
        created_at = (parent.payload or {}).get("created_at")
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            # The candidate projects this value; refuse early rather than
            # discovering a missing created_at after committing extracted bytes.
            raise ExtractionPreflightError(
                "SCREENING_PARENT_CREATED_AT_MISSING",
                "The accepted screening parent does not carry an RFC3339 created_at.",
            )
        path = self._safe_workspace_path(
            workspace_root, parent.workspace_relative_path, create=False
        )
        if not path.is_file():
            raise ExtractionPreflightError(
                "PARENT_FILE_MISSING",
                "The accepted screening parent file is not present in the workspace.",
                path=parent.workspace_relative_path,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ExtractionPreflightError(
                "PARENT_FILE_UNREADABLE",
                "The accepted screening parent file could not be read as JSON.",
                path=parent.workspace_relative_path,
            ) from error
        # The accepted hash is the canonical-JSON fingerprint, never the raw byte
        # digest: re-serialization must not be able to fail or forge the lineage.
        digest = canonical_fingerprint(payload)
        if digest != parent.sha256:
            raise ExtractionPreflightError(
                "PARENT_FILE_MISMATCH",
                "The accepted screening parent file does not match its checksum.",
                path=parent.workspace_relative_path,
            )
        if parent.payload is not None and digest != canonical_fingerprint(
            parent.payload
        ):
            raise ExtractionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The accepted screening parent file does not match the binding payload.",
            )
        if not isinstance(payload, dict) or (
            payload.get("artifact_id") != parent.artifact_id
        ):
            raise ExtractionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The accepted screening parent file does not carry the bound artifact_id.",
            )
        return parent

    def _load_acquisition_manifest(
        self, request: ExtractionRequest, workspace_root: Path
    ) -> tuple[AcquiredDocumentManifest, Path]:
        """Load and fully verify the E1 manifest the request names."""

        path = self._safe_workspace_path(
            workspace_root, request.acquisition_manifest_path, create=False
        )
        if not path.is_file():
            raise ExtractionPreflightError(
                "ACQUISITION_MANIFEST_MISSING",
                "The referenced E1 acquisition manifest is not present.",
                path=request.acquisition_manifest_path,
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ExtractionPreflightError(
                "ACQUISITION_MANIFEST_UNREADABLE",
                "The referenced E1 acquisition manifest could not be read.",
            ) from error
        try:
            # Reject a non-JSON manifest with a precise diagnostic before the
            # schema validator reports an opaque parse failure.
            json.loads(raw)
        except ValueError as error:
            raise ExtractionPreflightError(
                "ACQUISITION_MANIFEST_INVALID",
                "The E1 acquisition manifest is not valid JSON.",
            ) from error
        try:
            manifest = AcquiredDocumentManifest.model_validate_json(raw)
        except ValueError as error:
            raise ExtractionPreflightError(
                "ACQUISITION_MANIFEST_INVALID",
                "The E1 acquisition manifest does not satisfy its own schema.",
            ) from error
        # The declared checksum is E1's *null-excluded artifact_checksum*, not a
        # raw-file digest.  Recomputing it here would duplicate E1's authority, so
        # E2 only checks that the request declares the same value E1 committed;
        # `verify_manifest` below is what actually recomputes it (E2-NEG-003).
        if manifest.artifact_checksum != request.acquisition_manifest_sha256:
            raise ExtractionPreflightError(
                "ACQUISITION_MANIFEST_CHECKSUM_MISMATCH",
                "The E1 acquisition manifest does not match the declared checksum.",
            )
        expected_name = f"{request.acquisition_manifest_id}.json"
        if (
            manifest.manifest_id != request.acquisition_manifest_id
            or PurePosixPath(request.acquisition_manifest_path).name != expected_name
            or request.acquisition_manifest_path
            != manifest.e2_reference.acquisition_manifest_path
        ):
            raise ExtractionPreflightError(
                "ACQUISITION_REFERENCE_MISMATCH",
                "The E1 acquisition reference does not target its own identity path.",
            )
        # E2-NEG-004: the declared location must be exactly the E1 identity path
        # under *E1's* run, and that run is the acquisition run -- not E2's own
        # extraction run.  E2 mirrors the convention; it does not reuse the run.
        if (
            manifest.e2_reference.acquisition_manifest_path
            != f"{ACQUISITION_STORAGE_PREFIX}/{manifest.run_id}/{expected_name}"
        ):
            raise ExtractionPreflightError(
                "ACQUISITION_REFERENCE_MISMATCH",
                "The E1 acquisition manifest is not at its own identity path.",
            )
        if (
            manifest.workspace_id != request.workspace_id
            or manifest.protocol_fingerprint != request.protocol_fingerprint
            or manifest.corpus_fingerprint != request.corpus_fingerprint
        ):
            raise ExtractionPreflightError(
                "ACQUISITION_CONTEXT_MISMATCH",
                "The E1 acquisition manifest belongs to a different workspace or lineage.",
            )
        try:
            # E1 verifies its own identity, checksum, lineage and (optionally) the
            # bound PDF bytes.  E2 restates none of that.
            self._acquisition.verify_manifest(
                manifest, verify_bytes=self.verify_source_bytes
            )
        except AcquisitionCommitError as error:
            raise ExtractionPreflightError(
                "ACQUISITION_MANIFEST_UNVERIFIED",
                "The E1 acquisition manifest failed canonical verification.",
                detail=error.code,
            ) from error
        return manifest, path

    def _select_documents(
        self, request: ExtractionRequest, manifest: AcquiredDocumentManifest
    ) -> tuple[AcquiredDocumentRecord, ...]:
        """Select this request's E1 records, enforcing study containment."""

        selected = [
            record for record in manifest.records if record.study_id == request.study_id
        ]
        if request.document_ids is not None:
            wanted = set(request.document_ids)
            selected = [record for record in selected if record.document_id in wanted]
            missing = wanted - {record.document_id for record in selected}
            if missing:
                raise ExtractionPreflightError(
                    "DOCUMENT_NOT_IN_ACQUISITION_MANIFEST",
                    "A requested document is not an accepted E1 record for this study.",
                    document_ids=sorted(missing),
                )
        if not selected:
            raise ExtractionPreflightError(
                "NO_DOCUMENTS_SELECTED",
                "The request selects no acquired document for this study.",
                study_id=request.study_id,
            )
        return tuple(
            sorted(selected, key=lambda record: (record.study_id, record.document_id))
        )

    @staticmethod
    def _acquisition_reference(manifest: AcquiredDocumentManifest) -> ManifestReference:
        return ManifestReference(
            manifest_id=manifest.manifest_id,
            manifest_type=manifest.manifest_type,
            workspace_relative_path=(manifest.e2_reference.acquisition_manifest_path),
            artifact_checksum=manifest.artifact_checksum,
        )

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def _find_run_replay(
        self, workspace_root: Path, run_id: str, expected_key: str
    ) -> tuple[
        tuple[ExtractionManifest, Path] | None,
        dict[str, tuple[str, ExtractionItemOutcome]],
        dict[str, tuple[ExtractedDocumentRecord, ExtractionItemOutcome]],
    ]:
        """Locate the published sidecars that bind this exact request.

        The replay lookup is keyed by the **recomputed deterministic ``EXT-`` id**,
        not by the request-side idempotency key alone (packet E2 section 7.4).
        The two identities are not redundant: ``idempotency_key`` covers the
        request-side payload (workspace, run, acquisition reference, document set,
        requested engine), while the ``EXT-`` id covers the normalized extraction
        records and therefore their outcomes.

        A prior commit that bound *no* bytes is the section 6.7(9) **successor**
        case: a retry may now succeed, so it must mint a new ``EXT-`` id and must
        never be reported as ``REUSED``.

        A byte-bearing prior commit is *not* decided by its record count, because
        section 7.4 is explicit that a run whose key matches but whose ``EXT-`` id
        differs is a new commit rather than a replay.  A mixed commit therefore
        holds both kinds of row, and the rows are decided one at a time:

        * a document the prior commit holds bytes for is **final**: its record is
          *carried* -- adopted verbatim after the published commit re-verifies --
          and no engine runs for it again;
        * a document the prior commit merely *determined* is re-driven only when
          the engine environment observably changed
          (:meth:`_engine_environment_changed`), and a repair that commits bytes
          supersedes its prior row by reference (section 6.7(9)).

        A carried document is only carried when at least one sibling is re-driven,
        so the third element of the returned triple is non-empty exactly when this
        call is asking for the successor path.  Afterwards the run publishes a
        successor only if the re-drive recovered a document, which is what moves
        the prospective ``EXT-`` id; an unchanged id stays the ``REUSED`` replay of
        section 7.4.

        A changed non-volatile payload under an existing key remains
        ``IDEMPOTENCY_CONFLICT``, and two unsuperseded byte-bearing commits for
        the same key are ambiguous and also fail closed.
        """

        manifest_root = workspace_root / SIDECAR_STORAGE_PREFIX / run_id
        prior: dict[str, tuple[str, ExtractionItemOutcome]] = {}
        if not manifest_root.exists():
            return None, prior, {}
        matches: list[tuple[ExtractionManifest, Path]] = []
        try:
            canonical_root = manifest_root.resolve(strict=True)
        except OSError as error:
            raise ExtractionPreflightError(
                "SIDECAR_DIRECTORY_INVALID",
                "The extraction sidecar directory could not be resolved.",
            ) from error
        for path in sorted(manifest_root.glob("EXT-*.json")):
            if path.is_symlink():
                raise ExtractionPreflightError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "An extraction sidecar candidate cannot be a symlink.",
                )
            try:
                path.resolve(strict=True).relative_to(canonical_root)
                manifest = ExtractionManifest.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                self.verify_manifest(manifest, verify_bytes=False)
            except (OSError, ValueError, ExtractionCommitError) as error:
                raise ExtractionPreflightError(
                    "SIDECAR_CORRUPT",
                    "An extraction sidecar for this run failed canonical verification.",
                    path=path.name,
                ) from error
            if manifest.run_id != run_id:
                continue
            if manifest.idempotency_key != expected_key:
                continue
            # Recompute the deterministic EXT- id from the sidecar's own
            # normalized records: this is the replay key.  A published sidecar
            # that no longer recomputes its own id is not a replay candidate.
            recomputed_id = deterministic_extraction_manifest_id(
                schema_version=manifest.schema_version,
                workspace_id=manifest.workspace_id,
                run_id=manifest.run_id,
                acquisition_manifest_ref=manifest.acquisition_manifest_ref.model_dump(
                    mode="json"
                ),
                extraction_records=self._stable_extraction_records(
                    manifest.item_outcomes, manifest.records
                ),
                algorithm_version=manifest.manifest_identity_algorithm_version,
            )
            if recomputed_id != manifest.manifest_id:  # pragma: no cover - defensive
                raise ExtractionPreflightError(
                    "SIDECAR_CORRUPT",
                    "An extraction sidecar does not recompute its own EXT- id.",
                    path=path.name,
                )
            prior.update(
                {
                    document: (manifest.manifest_id, outcome)
                    for document, outcome in self._superseded_outcomes(manifest).items()
                }
            )
            if not manifest.records:
                # Nothing to carry and nothing to replay: this commit anchored no
                # ``EXT-`` identity, so a retry may mint a new one under the same
                # key and its prior outcomes are returned for section 6.7(9).
                continue
            matches.append((manifest, path))
        if not matches:
            return None, prior, {}
        # A section 6.7(9) successor chain leaves several same-key commits behind.
        # The authoritative one is the tip: the commit no other same-key sidecar
        # supersedes by reference.  More than one tip means the chain is
        # ambiguous, which is a fail-closed conflict rather than a guess.
        superseded = {
            outcome.prior_outcome_manifest_id
            for manifest, _ in matches
            for outcome in manifest.item_outcomes
            if outcome.prior_outcome_manifest_id is not None
        }
        tips = [match for match in matches if match[0].manifest_id not in superseded]
        if len(tips) != 1:
            raise ExtractionPreflightError(
                "IDEMPOTENCY_CONFLICT",
                "The run contains more than one unsuperseded extraction sidecar.",
                manifest_ids=sorted(match[0].manifest_id for match in tips),
            )
        replay = tips[0]
        return replay, prior, self._carried_records(replay[0])

    def _carried_records(
        self, manifest: ExtractionManifest
    ) -> dict[str, tuple[ExtractedDocumentRecord, ExtractionItemOutcome]]:
        """Return the records a repair rerun adopts instead of re-extracting.

        A byte-bearing commit is final (section 7.5: a valid existing output is
        never replaced by a different identity), so its documents are carried:
        the record and its current row are adopted verbatim, which keeps the
        document's ``extracted_sha256``/``extracted_file_sha256`` describing the
        bytes that are actually on disk and keeps exactly one byte-bearing record
        per document (section 6.7(9)).

        Adoption is only offered when a sibling is re-driven, because that is the
        only way the prospective ``EXT-`` id can move: with nothing re-driven the
        run is the exact replay section 7.4 reports as ``REUSED``, and re-running
        an engine to rediscover that would break the rule that a verified replay
        runs no engine.  Only the two *determined* failures are re-driven, and only
        under :meth:`_engine_environment_changed`: ``EXTRACTION_CANCELLED`` is not
        retried, and a ``NEEDS_REVIEW`` row is not a determined failure at all.

        That comparison is recorded-against-current rather than run-against-run,
        because a published commit is frozen.  So when a re-drive recovers nothing
        the commit is reported unchanged, the record of the failing environment
        never clears, and the next rerun retries that one document again: a bounded
        retry of a single determined document per rerun, never a silent replay and
        never a second commit.  That is the same retry semantic ``E2-NEG-018c``
        pins for a commit that owns no bytes at all.
        """

        current = {
            outcome.document_id: outcome
            for outcome in manifest.item_outcomes
            if not outcome.is_superseded
        }
        retryable = [
            document
            for document, outcome in current.items()
            if outcome is not None
            and outcome.extraction_status in SUPERSEDABLE_DETERMINED_STATUSES
        ]
        if not any(
            self._engine_environment_changed(current[document])
            for document in retryable
        ):
            return {}
        return {
            record.document_id: (record, current[record.document_id])
            for record in manifest.records
            if current.get(record.document_id) is not None
        }

    def _engine_environment_changed(self, prior: ExtractionItemOutcome) -> bool:
        """Report whether the engine environment differs from a prior failure.

        Section 7.4 forbids re-running an engine to rediscover an exact replay,
        and the prospective ``EXT-`` id cannot be known without running one, so
        the repair has to be *observable* first.  It is, for the two repairs the
        failure modes describe:

        * an engine that could not run at all (``ENGINE_NOT_INSTALLED``,
          ``ENGINE_UNAVAILABLE``, ``ENGINE_LICENSE_MISSING``) either resolves now
          or does not -- ``resolve()`` is a cheap, side-effect-free probe, so a
          provider that came back up is detected;
        * an engine that did run resolves to a different ``version()`` now, so an
          upgraded or swapped library is detected.

        An environment change that is invisible to both probes -- the same
        adapter, the same version, the same availability -- leaves the published
        commit the current truth, which is what section 7.4's replay means.
        """

        recorded: dict[str, str] = {}
        for attempt in prior.attempts:
            recorded[attempt.engine] = attempt.engine_version
        unavailable = {
            step.engine
            for step in prior.fallback_chain
            if step.reason in AVAILABILITY_FALLBACK_REASONS
        }
        if not recorded:
            # No attempt ever ran against the verified bytes, so there is no
            # recorded environment to compare: a retry is the only way to know.
            return True
        for engine_name, version in sorted(recorded.items()):
            try:
                adapter = self.engines.get(engine_name)
            except UnsupportedExtractionEngine:
                return True
            try:
                adapter.resolve()
            except Exception:  # noqa: BLE001 - a probe never breaks the decision
                if engine_name not in unavailable:
                    # An engine that ran before cannot run now.
                    return True
                continue
            if engine_name in unavailable:
                return True
            if self._engine_version(adapter) != version:
                return True
        return False

    @staticmethod
    def _superseded_outcomes(
        manifest: ExtractionManifest,
    ) -> dict[str, ExtractionItemOutcome]:
        """Return this sidecar's non-byte-bearing outcomes, keyed by document.

        Only a document that owns no bytes in this sidecar can be superseded: a
        byte-bearing record is final and is *carried* into a successor commit,
        while a row that owns no bytes is a determination a retry may replace.

        Rows that are *already* superseded are excluded: they are retained prior
        history inside this sidecar, and treating them as a fresh pending failure
        would make an exact rerun of a successor commit again instead of
        reporting ``REUSED``.
        """

        byte_bearing = {record.document_id for record in manifest.records}
        return {
            outcome.document_id: outcome
            for outcome in manifest.item_outcomes
            if outcome.document_id is not None
            and outcome.document_id not in byte_bearing
            and not outcome.is_superseded
        }

    async def _replay_outcome(
        self,
        replay: tuple[ExtractionManifest, Path],
        prepared: Sequence[_PreparedExtraction],
    ) -> ExtractionBatchOutcome:
        """Report the published commit again without re-running an engine."""

        manifest, path = replay
        first = prepared[0]
        if (
            manifest.workspace_id != first.request.workspace_id
            or manifest.run_id != first.request.run_id
            or manifest.idempotency_key != first.idempotency_key
        ):
            raise ExtractionPreflightError(
                "IDEMPOTENCY_CONFLICT",
                "The run already contains a sidecar for a different semantic request.",
                manifest_id=manifest.manifest_id,
            )
        if manifest.acquisition_manifest_ref.manifest_id != (
            first.request.acquisition_manifest_id
        ):
            raise ExtractionPreflightError(
                "IDEMPOTENCY_CONFLICT",
                "The published sidecar descends from a different acquisition manifest.",
            )
        self._verify_replay_inputs(manifest, prepared)
        # Fail closed when the published sidecar's bound bytes or extracted bodies
        # no longer verify (E2-NEG-017 / E2-NEG-040).
        self.verify_manifest(manifest, verify_bytes=True)
        # A replay re-reports the published commit, never a fresh extraction.  The
        # row's ``extraction_status`` becomes ``REUSED``; its ``content_status``
        # stays the one recorded on the *committed record* it re-reports, because
        # section 6.7 rule 6 derives the frozen content status from the recorded
        # degradation reasons -- re-projecting onto REUSED would silently upgrade
        # a degraded (PARTIAL) commit to VALID while its ``degradation_reasons``
        # survived on the same row.  The authoritative committed record, which the
        # candidate is built from, is untouched, so the artifact payload stays
        # byte-identical.
        committed_status = {
            record.document_id: record.content_status for record in manifest.records
        }
        outcomes = [
            item.model_copy(
                update={
                    "extraction_status": ExtractionStatus.REUSED,
                    "content_status": committed_status.get(
                        item.document_id, item.content_status
                    ),
                }
            )
            if item.extraction_status in BYTE_BEARING_STATUSES
            else item
            for item in manifest.item_outcomes
        ]
        candidate = self._build_candidate(manifest, first, outcomes)
        audit_error = await self._append_audit(manifest, path, candidate)
        errors = list(manifest.operation.errors)
        status = manifest.operation.status
        if audit_error is not None:
            errors.append(audit_error)
            if manifest.records:
                status = OperationStatus.PARTIAL
        return self._batch_outcome(
            manifest,
            path,
            outcomes,
            status=status,
            candidate=candidate,
            extra_errors=[audit_error] if audit_error is not None else [],
        )

    def _verify_replay_inputs(
        self,
        manifest: ExtractionManifest,
        prepared: Sequence[_PreparedExtraction],
    ) -> None:
        """Reject a persisted key that no longer names the same immutable inputs."""

        by_document = {record.document_id: record for record in manifest.records}
        for item in prepared:
            request = item.request
            if any(
                record.requested_engine != request.requested_engine.upper()
                for record in manifest.records
            ):
                raise ExtractionPreflightError(
                    "IDEMPOTENCY_CONFLICT",
                    "A persisted extraction key no longer names the same engine.",
                    study_id=request.study_id,
                )
            for source in item.documents:
                published = by_document.get(source.document_id)
                if published is None:
                    continue
                if (
                    published.source_sha256 != source.source_sha256
                    or published.study_id != source.study_id
                    or published.acquisition_manifest_sha256
                    != manifest.acquisition_manifest_ref.artifact_checksum
                ):
                    raise ExtractionPreflightError(
                        "IDEMPOTENCY_CONFLICT",
                        "A persisted extraction record no longer names the same "
                        "source bytes.",
                        document_id=source.document_id,
                    )
                self._verify_replay_request_shape(manifest, request, source)

    def _verify_replay_request_shape(
        self,
        manifest: ExtractionManifest,
        request: ExtractionRequest,
        source: AcquiredDocumentRecord,
    ) -> None:
        """Refuse a replay whose non-keyed, non-volatile inputs changed.

        Packet 7.4 fixes the idempotency key to the workspace, run, acquisition
        reference, and the ``{document_id, source_sha256, requested_engine}``
        document set.  The page range and the provider endpoint are extraction
        inputs that change the work and its bytes but are deliberately *not* in
        that key, so a rerun that changes them still lands on the same key.  Such
        a rerun is a changed non-volatile payload under an existing key: it is an
        ``IDEMPOTENCY_CONFLICT``, never a silent reuse of bytes that were
        extracted under different settings (E2-NEG-018).
        """

        published = next(
            (
                outcome
                for outcome in manifest.item_outcomes
                if outcome.study_id == source.study_id
                and outcome.document_id == source.document_id
            ),
            None,
        )
        if published is None:
            return
        current = self._request_shape(request)
        # A row with no recorded attempts committed no bytes, so there is nothing
        # a replay could silently reuse under a different request shape: such a
        # row needs no request-shape check.
        for attempt in published.attempts:
            if any(
                attempt.request_shape.get(name) != current.get(name)
                for name in NON_KEYED_REQUEST_KEYS
            ):
                raise ExtractionPreflightError(
                    "IDEMPOTENCY_CONFLICT",
                    "A persisted extraction key no longer names the same page "
                    "range or provider endpoint.",
                    document_id=source.document_id,
                )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    async def _extract_locked(
        self,
        prepared: Sequence[_PreparedExtraction],
        prior_outcomes: Mapping[str, tuple[str, ExtractionItemOutcome]] | None = None,
        carried: Mapping[str, tuple[ExtractedDocumentRecord, ExtractionItemOutcome]]
        | None = None,
        prior_replay: tuple[ExtractionManifest, Path] | None = None,
    ) -> ExtractionBatchOutcome:
        first = prepared[0]
        staged: list[_StagedExtraction] = []
        for index, item in enumerate(prepared):
            for position, source in enumerate(item.documents):
                adopted = None if carried is None else carried.get(source.document_id)
                if adopted is not None:
                    # Section 6.7(9): this document's commit is final and was just
                    # re-verified against its own bytes, so it is carried into the
                    # successor commit instead of running an engine a second time.
                    staged.append(
                        _StagedExtraction(record=adopted[0], outcome=adopted[1])
                    )
                    continue
                try:
                    entries = self._extract_one_document(item, source)
                except ExtractionCommitError as error:
                    # A commit failure is an item-level, fail-closed outcome, not
                    # an exception that escapes the public API (E1's per-item
                    # `_failed_item` precedent).  Section 7.5: a valid existing
                    # output is never removed by a failed refresh, so the
                    # content-addressed bytes that are already published stay.
                    entries = [
                        _StagedExtraction(
                            record=None,
                            outcome=self._commit_failure_outcome(item, source, error),
                        )
                    ]
                staged.extend(entries)
                if not any(
                    entry.outcome.extraction_status is ExtractionStatus.CANCELLED
                    for entry in entries
                ):
                    continue
                # Section 6.1/7.3: once the caller cancelled, the rest of the
                # batch is accounted for as cancelled rather than silently
                # dropped -- and none of it commits a record.
                for later in prepared[index:]:
                    for pending in later.documents:
                        if pending is source:
                            continue
                        already = any(
                            staged_entry.outcome.document_id == pending.document_id
                            for staged_entry in staged
                        )
                        if not already:
                            staged.append(
                                _StagedExtraction(
                                    record=None,
                                    outcome=self._cancelled_outcome(later, pending),
                                )
                            )
                break
            else:
                continue
            break
        records = [entry.record for entry in staged if entry.record is not None]
        outcomes = [entry.outcome for entry in staged]
        if prior_outcomes:
            # Section 6.7(9): the superseded outcome stays visible in this
            # sidecar's item_outcomes, referenced by the prior EXT- id, so the
            # recovery is auditable instead of a silent overwrite.
            outcomes = self._carry_superseded_outcomes(outcomes, prior_outcomes)
        if self._is_refused_batch(outcomes):
            # Section 7.3(1): the request was refused before any extraction
            # attempt, so the batch is refused rather than determined.  Returning
            # here mints no ``EXT-`` id, writes no sidecar, and appends no audit
            # event, so the refusal cannot occupy the idempotency key space or be
            # replayed by a later run (section 7.4).  This is deliberately *not*
            # the all-engine-failure shape: a determined failure still publishes
            # its fail-closed zero-byte sidecar, because "we ran and nothing was
            # usable" and "we refused to run" are different facts.
            return self._refused_batch_outcome(first, outcomes)
        status = self._operation_status(outcomes)
        manifest = self._build_manifest(prepared, records, outcomes, status)
        if prior_replay is not None:
            # Section 7.4: the replay lookup is keyed by the recomputed ``EXT-`` id,
            # so this run is a new commit only when that id moved.  It moved only
            # when the repair recovered a document: a superseded row exists exactly
            # when a document that owned no bytes now owns a record (6.7(9)), so a
            # re-drive that merely re-recorded a failure leaves every document's
            # state as published and must not mint an unlinked second sidecar --
            # nothing would supersede either commit and every later replay of this
            # key would be ambiguous.  Both ways out report the published commit,
            # with no second sidecar and no second audit event.
            recovered = any(outcome.is_superseded for outcome in outcomes)
            if manifest.manifest_id == prior_replay[0].manifest_id or not recovered:
                return await self._replay_outcome(prior_replay, prepared)
        sidecar_relative = f"{SIDECAR_STORAGE_PREFIX}/{first.request.run_id}/{manifest.manifest_id}.json"
        sidecar_path = self._prepared_file_path(first.workspace_root, sidecar_relative)
        published = self._publish_sidecar_locked(manifest, sidecar_path)
        candidate = self._build_candidate(published, first, outcomes)
        audit_error = await self._append_audit(published, sidecar_path, candidate)
        final_status = published.operation.status
        if audit_error is not None and published.records:
            # The sidecar is the commit marker; a missing audit event is a
            # recoverable partial, never a silent success.
            final_status = OperationStatus.PARTIAL
        return self._batch_outcome(
            published,
            sidecar_path,
            outcomes,
            status=final_status,
            candidate=candidate,
            extra_errors=[audit_error] if audit_error is not None else [],
        )

    @staticmethod
    def _carry_superseded_outcomes(
        outcomes: list[ExtractionItemOutcome],
        prior: Mapping[str, tuple[str, ExtractionItemOutcome]],
    ) -> list[ExtractionItemOutcome]:
        """Append the prior determined outcomes that this run superseded.

        The superseded rows are appended verbatim except for the two fields that
        make the supersession auditable: ``prior_outcome_manifest_id`` names the
        ``EXT-`` id of the sidecar that previously held the outcome, and
        ``prior_outcome_status`` names the status it held there.  They are
        non-byte-bearing rows for a document that now owns a byte-bearing record,
        which is the only successor shape section 6.7(9) allows.
        """

        carried = list(outcomes)
        current = {
            outcome.document_id
            for outcome in carried
            if outcome.extraction_status in BYTE_BEARING_STATUSES
        }
        for document_id, (prior_manifest_id, prior_outcome) in sorted(prior.items()):
            if document_id not in current or prior_outcome.extraction_status in (
                BYTE_BEARING_STATUSES
            ):
                continue
            carried.append(
                prior_outcome.model_copy(
                    update={
                        "prior_outcome_manifest_id": prior_manifest_id,
                        "prior_outcome_status": prior_outcome.extraction_status,
                    }
                )
            )
        return carried

    @staticmethod
    def _is_refused_batch(outcomes: Sequence[ExtractionItemOutcome]) -> bool:
        """Report whether every requested document was refused, not determined.

        A refusal-class outcome is a request-level rejection: the containment
        rule of section 7.3(1) refused the destination, so the batch carries no
        determination about the documents and must publish nothing.  One refusal
        is enough to make the batch invalid as a whole, so this is an ``all()``
        over the *current* rows: a retained superseded row (6.7(9)) is sidecar
        history, and a sibling that committed or merely failed keeps the batch a
        determined one, which still commits (a mixed batch records its refused
        sibling inside the committed sidecar, section 7.3(4)/(5)).
        """

        current = [outcome for outcome in outcomes if not outcome.is_superseded]
        return bool(current) and all(
            outcome.extraction_status is ExtractionStatus.FAILED
            and outcome.error is not None
            and outcome.error.code in REFUSAL_ERROR_CODES
            for outcome in current
        )

    def _refused_batch_outcome(
        self,
        first: _PreparedExtraction,
        outcomes: Sequence[ExtractionItemOutcome],
    ) -> ExtractionBatchOutcome:
        """Report a wholly refused batch with no sidecar, candidate, or audit.

        The refusal keeps the per-document FAILED outcomes and the structured
        error exactly as the commit path produced them -- the caller learns
        *which* document was refused and *why* -- but the envelope carries no
        ``manifest_reference``: nothing was published, so there is no
        ``EXT-`` identity, no artifact checksum, and no idempotency key for a
        later run to replay.
        """

        current = [outcome for outcome in outcomes if not outcome.is_superseded]
        errors: list[StructuredError] = []
        for outcome in current:
            if outcome.error is not None and outcome.error not in errors:
                errors.append(outcome.error)
        if not errors:  # pragma: no cover - a refused row always carries its code
            errors.append(
                StructuredError(
                    code="BATCH_FAILED",
                    message="Every requested document was refused before extraction.",
                )
            )
        return ExtractionBatchOutcome(
            run_id=first.request.run_id,
            status=OperationStatus.FAILED,
            data=ExtractionOperationData(
                manifest_reference=None,
                item_outcomes=list(current),
                committed_count=0,
                requested_count=len(current),
                candidate=None,
            ),
            errors=errors,
            provenance={
                "refused": True,
                "refusal_codes": sorted(
                    {outcome.error.code for outcome in current if outcome.error}
                ),
                "contract_acceptance": "not_performed_by_kit",
            },
        )

    def _extract_one(self, prepared: _PreparedExtraction) -> list[_StagedExtraction]:
        """Extract every selected document of one request, accounting for each."""

        results: list[_StagedExtraction] = []
        for source in prepared.documents:
            results.extend(self._extract_one_document(prepared, source))
        return results

    def _extract_one_document(
        self, prepared: _PreparedExtraction, source: AcquiredDocumentRecord
    ) -> list[_StagedExtraction]:
        """Extract one document, or convert a cancellation into a truthful row.

        ``asyncio.CancelledError`` inherits from ``BaseException``, so it is not
        swallowed by the engine-failure handling above: a cancelled document
        returns a ``CANCELLED`` outcome with no record and no candidate entry
        (section 6.1 -- a cancelled item is never authoritative), and the caller
        marks the remaining documents of the batch cancelled as well.  This
        mirrors E1's acquisition cancellation semantics
        (``acquisition.py:1589-1597``).
        """

        try:
            self._inject(ExtractionFault.ENGINE, source.document_id)
            data = self._read_verified_source(prepared, source)
            chain = self._run_chain(prepared, source, data)
        except asyncio.CancelledError:
            return [
                _StagedExtraction(
                    record=None, outcome=self._cancelled_outcome(prepared, source)
                )
            ]
        evaluation = self._evaluate(prepared, chain)
        if evaluation.status not in BYTE_BEARING_STATUSES:
            return [
                _StagedExtraction(
                    record=None,
                    outcome=self._determined_outcome(
                        prepared, source, chain, evaluation
                    ),
                )
            ]
        committed, promoted_new = self._commit_bytes(
            prepared, source, chain, evaluation
        )
        record = self._extracted_record(prepared, source, chain, evaluation, committed)
        return [
            _StagedExtraction(
                record=record,
                outcome=self._committed_outcome(
                    prepared, source, chain, evaluation, committed
                ),
                promoted_new=promoted_new,
            )
        ]

    def _commit_failure_outcome(
        self,
        prepared: _PreparedExtraction,
        source: AcquiredDocumentRecord,
        error: ExtractionCommitError,
    ) -> ExtractionItemOutcome:
        """Account for a document whose commit failed before any record existed.

        The identity-addressed path is the storage contract (7.5), so a second
        extraction of the same document that produces different bytes cannot
        occupy it.  The batch reports that as an explicit, per-item failure with
        the code the commit path raised; it never overwrites the published body
        and never invents a record.
        """

        return ExtractionItemOutcome(
            study_id=source.study_id,
            document_id=source.document_id,
            extraction_status=ExtractionStatus.FAILED,
            requested_engine=prepared.request.requested_engine.upper(),
            content_status=DocumentContentStatus.FAILED,
            # The failure is decided by a deterministic rule over the storage
            # contract, not by the engine that produced the candidate bytes, so
            # the method records exactly that.
            extraction_method=ExtractionMethod.DETERMINISTIC_RULE,
            source_sha256=source.source_sha256,
            acquisition_manifest_id=prepared.request.acquisition_manifest_id,
            acquisition_manifest_sha256=prepared.request.acquisition_manifest_sha256,
            stage=ExtractionStage.PROMOTION,
            error=StructuredError(
                code=error.code,
                message=error.message,
                retryable=False,
                details={"document_id": source.document_id},
            ),
        )

    def _cancelled_outcome(
        self, prepared: _PreparedExtraction, source: AcquiredDocumentRecord
    ) -> ExtractionItemOutcome:
        """Account for a document the caller cancelled before any commit."""

        return ExtractionItemOutcome(
            study_id=source.study_id,
            document_id=source.document_id,
            extraction_status=ExtractionStatus.CANCELLED,
            requested_engine=prepared.request.requested_engine.upper(),
            content_status=None,
            # A cancelled item emits no record, so the frozen method never
            # applies; the outcome keeps the source identity it would have had.
            extraction_method=None,
            source_sha256=source.source_sha256,
            acquisition_manifest_id=prepared.request.acquisition_manifest_id,
            acquisition_manifest_sha256=prepared.request.acquisition_manifest_sha256,
            stage=ExtractionStage.ENGINE,
            error=StructuredError(
                code=REASON_EXTRACTION_CANCELLED,
                message="Extraction was cancelled before content commit.",
                retryable=True,
                details={"document_id": source.document_id},
            ),
        )

    def _read_verified_source(
        self, prepared: _PreparedExtraction, record: AcquiredDocumentRecord
    ) -> bytes:
        """Re-verify the E1 source bytes before any engine sees them."""

        path = self._safe_workspace_path(
            prepared.workspace_root, record.workspace_relative_path, create=False
        )
        if not path.is_file():
            raise ExtractionCommitError(
                "SOURCE_BYTES_MISSING",
                "The E1 source bytes are no longer present in the workspace.",
                document_id=record.document_id,
            )
        if path.stat().st_size != record.byte_length:
            raise ExtractionCommitError(
                "SOURCE_BYTES_MISMATCH",
                "The E1 source bytes no longer match the recorded byte length.",
                document_id=record.document_id,
            )
        if self._hash_file(path) != record.source_sha256:
            raise ExtractionCommitError(
                "SOURCE_BYTES_MISMATCH",
                "The E1 source bytes no longer match the recorded checksum.",
                document_id=record.document_id,
            )
        return path.read_bytes()

    def _run_chain(
        self,
        prepared: _PreparedExtraction,
        record: AcquiredDocumentRecord,
        data: bytes,
    ) -> _ChainResult:
        """Run the ordered engine chain over verified bytes, recording attempts."""

        request = prepared.request
        try:
            _, chain = engine_chain(
                self.engines,
                request.requested_engine,
                allow_fallback=request.allow_fallback,
                fallback_order=self.fallback_order,
            )
        except UnsupportedExtractionEngine as error:
            raise ExtractionPreflightError(error.code, error.message) from error
        attempts: list[ExtractionAttempt] = []
        fallback: list[FallbackStep] = []
        for ordinal, adapter in enumerate(chain, start=1):
            engine_name = adapter.name.value
            version = self._engine_version(adapter)
            output_format = self._attempt_format(adapter)
            try:
                adapter.resolve()
                result = adapter.extract(
                    data,
                    grobid_url=request.grobid_url,
                    page_range=request.page_range,
                )
            except EngineFailure as failure:
                attempts.append(
                    ExtractionAttempt(
                        ordinal=ordinal,
                        engine=engine_name,
                        engine_version=version,
                        output_format=output_format,
                        request_shape=self._request_shape(request),
                        result=AttemptResult.ERROR,
                        diagnostic_code=failure.code,
                        diagnostic_message=failure.message or failure.code,
                        attempted_at=utc_now(),
                    )
                )
                if failure.reason is FallbackReason.ENGINE_LICENSE_MISSING:
                    raise ExtractionCommitError(
                        "ENGINE_LICENSE_MISSING",
                        "The effective engine requires a license that is not present.",
                        engine=engine_name,
                    ) from failure
                fallback.append(
                    FallbackStep(
                        engine=engine_name,
                        engine_version=version,
                        reason=failure.reason,
                        detail=(failure.message or failure.code)[:500],
                    )
                )
                continue
            except Exception as error:  # noqa: BLE001 - a chain step never escapes
                message = sanitize_diagnostic(str(error))
                attempts.append(
                    ExtractionAttempt(
                        ordinal=ordinal,
                        engine=engine_name,
                        engine_version=version,
                        output_format=output_format,
                        request_shape=self._request_shape(request),
                        result=AttemptResult.ERROR,
                        diagnostic_code="ENGINE_EXCEPTION",
                        diagnostic_message=message,
                        attempted_at=utc_now(),
                    )
                )
                fallback.append(
                    FallbackStep(
                        engine=engine_name,
                        engine_version=version,
                        reason=FallbackReason.ENGINE_ERROR,
                        detail=message[:500],
                    )
                )
                continue
            self._inject(ExtractionFault.VALIDATION, record.document_id)
            measured = measure_extracted_body(result.text)
            produced_text = measured.character_count > 0 and not is_legacy_stub(
                measured.body
            )
            attempts.append(
                ExtractionAttempt(
                    ordinal=ordinal,
                    engine=engine_name,
                    engine_version=version,
                    output_format=result.output_format,
                    request_shape=dict(result.request_shape)
                    or self._request_shape(request),
                    result=(
                        AttemptResult.TEXT_EXTRACTED
                        if produced_text
                        else AttemptResult.NO_TEXT
                    ),
                    effective=produced_text,
                    page_count=result.page_count,
                    character_count=measured.character_count,
                    text_layer_present=result.text_layer_present,
                    diagnostic_code=(
                        None
                        if produced_text
                        else bound_diagnostic_code(REASON_UNUSABLE_OUTPUT)
                    ),
                    diagnostic_message=(
                        None if produced_text else "the engine produced no usable text"
                    ),
                    attempted_at=utc_now(),
                )
            )
            if produced_text:
                return _ChainResult(
                    attempts=tuple(attempts),
                    fallback=tuple(fallback),
                    text=measured.body,
                    effective_engine=engine_name,
                    effective_version=version,
                    output_format=result.output_format,
                    page_count=result.page_count,
                    character_count=measured.character_count,
                    text_layer_present=result.text_layer_present,
                    engine_degradation=tuple(result.degradation_reasons),
                    diagnostics=(),
                )
            # The engine ran and produced nothing usable: record the substitution
            # and try the next engine in the chain.
            fallback.append(
                FallbackStep(
                    engine=engine_name,
                    engine_version=version,
                    reason=FallbackReason.ENGINE_OUTPUT_UNUSABLE,
                    detail="the engine produced no usable text",
                )
            )
        return self._exhausted_chain(request, tuple(attempts), tuple(fallback), chain)

    def _exhausted_chain(
        self,
        request: ExtractionRequest,
        attempts: tuple[ExtractionAttempt, ...],
        fallback: tuple[FallbackStep, ...],
        chain: Sequence[ExtractionEngineAdapter],
    ) -> _ChainResult:
        """Describe a chain that ran to completion without usable text."""

        last = attempts[-1] if attempts else None
        saw_missing_layer = any(
            attempt.text_layer_present is False for attempt in attempts
        )
        code = (
            REASON_NO_TEXT_LAYER
            if saw_missing_layer
            else (REASON_UNUSABLE_OUTPUT if attempts else REASON_CHAIN_EXHAUSTED)
        )
        return _ChainResult(
            attempts=attempts,
            fallback=fallback,
            text="",
            effective_engine=(
                last.engine if last else request.requested_engine.upper()
            ),
            effective_version=(last.engine_version if last else ENGINE_VERSION_UNKNOWN),
            output_format=(
                last.output_format if last else self._attempt_format(chain[-1])
            ),
            page_count=last.page_count if last and last.page_count else 0,
            character_count=0,
            text_layer_present=False if saw_missing_layer else None,
            engine_degradation=(),
            diagnostics=(code,),
        )

    def _engine_version(self, adapter: ExtractionEngineAdapter) -> str:
        """Record the runtime version, or an explicit ``unknown`` marker.

        A version that cannot be determined is never omitted: the model layer
        requires a bounded diagnostic alongside the marker, so the attempt
        remains explainable after the fact.
        """

        try:
            version = str(adapter.version()).strip()
        except Exception:  # noqa: BLE001 - a version probe must not break the chain
            return ENGINE_VERSION_UNKNOWN
        return version or ENGINE_VERSION_UNKNOWN

    @staticmethod
    def _attempt_format(adapter: ExtractionEngineAdapter) -> ExtractionOutputFormat:
        declared = getattr(adapter, "output_format", ExtractionOutputFormat.MARKDOWN)
        return (
            ExtractionOutputFormat.TEI_XML
            if declared is ExtractionOutputFormat.TEI_XML
            else ExtractionOutputFormat.MARKDOWN
        )

    @staticmethod
    def _request_shape(request: ExtractionRequest) -> dict[str, Any]:
        """Record the request/response shape that matters for an attempt."""

        shape: dict[str, Any] = {
            "requested_engine": request.requested_engine.upper(),
        }
        if request.grobid_url:
            # Any credential in the endpoint URL is redacted before storage.
            shape["grobid_url"] = sanitize_diagnostic(request.grobid_url, limit=200)
        if request.page_range:
            shape["page_range"] = request.page_range
        return shape

    def _evaluate(
        self, prepared: _PreparedExtraction, chain: _ChainResult
    ) -> _Evaluation:
        """Apply the ordered section 6.7(5) rule and decide the item's truth."""

        profile = prepared.request.usability_profile
        if not chain.text:
            code = chain.diagnostics[0] if chain.diagnostics else REASON_UNUSABLE_OUTPUT
            content_status = (
                DocumentContentStatus.NEEDS_OCR
                if code == REASON_NO_TEXT_LAYER
                else DocumentContentStatus.FAILED
            )
            return _Evaluation(
                status=(
                    ExtractionStatus.NO_TEXT_LAYER
                    if content_status is DocumentContentStatus.NEEDS_OCR
                    else ExtractionStatus.EXTRACTION_FAILED
                ),
                content_status=content_status,
                text="",
                output_format=chain.output_format,
                page_count=chain.page_count,
                character_count=0,
                effective_engine=chain.effective_engine,
                effective_version=chain.effective_version,
                degradation_reasons=tuple(step.reason.value for step in chain.fallback),
                diagnostic_code=bound_diagnostic_code(code),
                diagnostic_message=(
                    "no engine in the chain produced usable extracted text"
                ),
            )
        if is_legacy_stub(chain.text):
            # The legacy parse-failure stub is never content (E2-NEG-013).
            return self._rejected(
                chain,
                REASON_STUB_OUTPUT,
                "the engine emitted the legacy parse-failure stub, which is not content",
            )
        if chain.character_count < profile.minimum_character_count:
            return self._rejected(
                chain,
                REASON_BELOW_THRESHOLD,
                "the extracted body is below the recorded usefulness threshold",
            )
        degradation = list(dict.fromkeys(chain.engine_degradation))
        if chain.fallback:
            degradation.append(REASON_ENGINE_SUBSTITUTED)
        if not degradation:
            return _Evaluation(
                status=ExtractionStatus.EXTRACTED,
                content_status=DocumentContentStatus.VALID,
                text=chain.text,
                output_format=chain.output_format,
                page_count=chain.page_count,
                character_count=chain.character_count,
                effective_engine=chain.effective_engine,
                effective_version=chain.effective_version,
                degradation_reasons=(),
                diagnostic_code=None,
                diagnostic_message=None,
            )
        return _Evaluation(
            status=ExtractionStatus.PARTIAL,
            content_status=DocumentContentStatus.PARTIAL,
            text=chain.text,
            output_format=chain.output_format,
            page_count=chain.page_count,
            character_count=chain.character_count,
            effective_engine=chain.effective_engine,
            effective_version=chain.effective_version,
            degradation_reasons=tuple(dict.fromkeys(degradation)),
            diagnostic_code=bound_diagnostic_code(REASON_PAGE_DEGRADED)
            if REASON_PAGE_DEGRADED in degradation
            else None,
            diagnostic_message=(
                "the extraction degraded and the committed status is PARTIAL"
            ),
        )

    @staticmethod
    def _rejected(chain: _ChainResult, code: str, message: str) -> _Evaluation:
        return _Evaluation(
            status=ExtractionStatus.EXTRACTION_FAILED,
            content_status=DocumentContentStatus.FAILED,
            text="",
            output_format=chain.output_format,
            page_count=chain.page_count,
            character_count=chain.character_count,
            effective_engine=chain.effective_engine,
            effective_version=chain.effective_version,
            degradation_reasons=(),
            diagnostic_code=bound_diagnostic_code(code),
            diagnostic_message=message,
        )

    def _commit_bytes(
        self,
        prepared: _PreparedExtraction,
        source: AcquiredDocumentRecord,
        chain: _ChainResult,
        evaluation: _Evaluation,
    ) -> tuple[CommittedFile, bool]:
        """Compose, stage, validate, and atomically promote the extracted file."""

        request = prepared.request
        committed = self._compose(prepared, source, evaluation)
        relative = self._extracted_relative_path(
            request, source, evaluation.output_format
        )
        # Only the *parent directory* may be created.  Anchoring the file path
        # itself with create=True would materialize a zero-length directory
        # entry in place of the file, and the promotion below would then collide
        # with it instead of publishing the validated bytes.
        final = self._prepared_file_path(prepared.workspace_root, relative)
        coalesced = self._coalesce_existing_extraction(
            prepared, relative=relative, committed=committed
        )
        if coalesced is not None:
            return coalesced, False
        self._write_commit_intent(
            final,
            {
                "schema_version": COMMIT_INTENT_SCHEMA_VERSION,
                "document_id": source.document_id,
                "study_id": source.study_id,
                "workspace_id": request.workspace_id,
                "run_id": request.run_id,
                "workspace_relative_path": relative,
                "idempotency_key": prepared.idempotency_key,
                "extracted_file_sha256": committed.file_sha256,
            },
        )
        temporary = self._new_temporary(final.parent, prefix=f".{final.name}.")
        try:
            with temporary.open("wb") as stream:
                stream.write(committed.data)
                stream.flush()
                os.fsync(stream.fileno())
            # Validate the *staged* bytes, not the not-yet-existing final path:
            # a promoted file must never be the first place a bad checksum is
            # noticed.
            self._verify_committed_file(
                prepared.workspace_root,
                source,
                relative=relative,
                expected=committed,
                candidate_path=temporary,
            )
            self._inject(ExtractionFault.CONTENT_MOVE, source.document_id)
            promotion = self._promote(temporary, final, committed.file_sha256)
        except (OSError, FrontmatterError, ExtractionCommitError):
            self._cleanup_temporary(temporary)
            raise
        self._discard_commit_intent(final)
        if promotion.temporary_leftover is not None:
            _LOGGER.warning(
                "extracted staging litter remained: %s",
                promotion.temporary_leftover.name,
            )
        return committed, promotion.promoted_new

    def _coalesce_existing_extraction(
        self,
        prepared: _PreparedExtraction,
        *,
        relative: str,
        committed: CommittedFile,
    ) -> CommittedFile | None:
        """Adopt the file already committed when only provenance differs.

        Packet E2 section 6.6 keeps ``extracted_at`` in the bound frontmatter and
        labels it "provenance only; never in the identity payload", so two runs
        over the same document with the same requested engine produce the same
        body -- and therefore the same ``extracted_sha256`` -- while their file
        bytes differ only by the clock.  Section 7.5 requires concurrent
        duplicate logical inputs to "deterministically coalesce to exactly one
        committed extraction" and forbids removing a valid existing output, so
        such a re-extraction must adopt the bytes on disk instead of colliding
        with them.  Returning ``None`` leaves the ordinary paths in charge: an
        identical file is still promoted as ``promoted_new=False`` and a genuine
        content difference is still a ``CONTENT_COLLISION`` (``E2-NEG-017``).

        The comparison is deliberately exact.  Every frontmatter key is compared
        and the only tolerated difference is a provenance-only one, so a
        different engine, a different status, or a different body never
        coalesces; a symlinked or unparsable destination is never adopted.  The
        returned :class:`CommittedFile` describes the bytes actually on disk, so
        the record's ``extracted_file_sha256`` stays truthful, and the body
        digest is recomputed from the file rather than trusted from its
        frontmatter.
        """

        final = self._prepared_file_path(prepared.workspace_root, relative)
        if final.is_symlink() or not final.is_file():
            return None
        try:
            data = final.read_bytes()
        except OSError:
            return None
        if data == committed.data:
            return None
        try:
            existing_values, existing_body = parse_bound_frontmatter(data)
            fresh_values, _ = parse_bound_frontmatter(committed.data)
        except FrontmatterError:
            return None
        differing = {
            key
            for key in set(existing_values) | set(fresh_values)
            if str(existing_values.get(key)) != str(fresh_values.get(key))
        }
        if not differing or not differing <= PROVENANCE_ONLY_FRONTMATTER_KEYS:
            return None
        body_sha256 = sha256_bytes(existing_body.encode("utf-8"))
        if body_sha256 != str(existing_values.get("extracted_sha256")):
            return None
        if body_sha256 != committed.extracted_sha256:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - parse_bound_frontmatter raised
            return None
        return CommittedFile(
            text=text,
            data=data,
            extracted_sha256=body_sha256,
            file_sha256=sha256_bytes(data),
        )

    def _compose(
        self,
        prepared: _PreparedExtraction,
        source: AcquiredDocumentRecord,
        evaluation: _Evaluation,
    ) -> CommittedFile:
        """Build the bound frontmatter and the exact committed bytes."""

        request = prepared.request
        metadata = request.metadata
        doi = source.normalized_doi
        if metadata.get("doi") and doi and str(metadata["doi"]) != doi:
            raise ExtractionPreflightError(
                "METADATA_CONFLICT",
                "Caller-supplied metadata disagrees with the accepted E1 record.",
                document_id=source.document_id,
            )
        frontmatter = build_frontmatter_values(
            document_id=source.document_id,
            study_id=source.study_id,
            source_sha256=source.source_sha256,
            acquisition_manifest_id=prepared.request.acquisition_manifest_id,
            acquisition_manifest_sha256=prepared.request.acquisition_manifest_sha256,
            extraction_engine=evaluation.effective_engine,
            extraction_engine_version=evaluation.effective_version,
            extraction_requested_engine=request.requested_engine.upper(),
            extraction_status=evaluation.status.value,
            content_status=evaluation.content_status.value,
            workspace_id=request.workspace_id,
            doi=doi or (str(metadata["doi"]) if metadata.get("doi") else None),
            title=metadata.get("title"),
            authors=metadata.get("authors"),
            year=metadata.get("year"),
            extracted_at=utc_now(),
        )
        return compose_extracted_file(body=evaluation.text, frontmatter=frontmatter)

    @staticmethod
    def _extracted_relative_path(
        request: ExtractionRequest,
        record: AcquiredDocumentRecord,
        output_format: ExtractionOutputFormat,
    ) -> str:
        """Return the identity-addressed output path for one document.

        Never ``{stem}.md``: two studies that share a title would collide, and
        nothing would tie the output back to an identity.
        """

        return (
            f"{request.storage_prefix}/{record.document_id}"
            f"{EXTRACTED_SUFFIXES[output_format]}"
        )

    # ------------------------------------------------------------------
    # Records and outcomes
    # ------------------------------------------------------------------

    def _extracted_record(
        self,
        prepared: _PreparedExtraction,
        source: AcquiredDocumentRecord,
        chain: _ChainResult,
        evaluation: _Evaluation,
        committed: CommittedFile,
    ) -> ExtractedDocumentRecord:
        relative = self._extracted_relative_path(
            prepared.request, source, evaluation.output_format
        )
        return ExtractedDocumentRecord(
            document_id=source.document_id,
            document_identity_algorithm_version=source.document_identity_algorithm_version,
            study_id=source.study_id,
            acquisition_manifest_id=prepared.request.acquisition_manifest_id,
            acquisition_manifest_sha256=prepared.request.acquisition_manifest_sha256,
            acquisition_manifest_path=prepared.request.acquisition_manifest_path,
            source_sha256=source.source_sha256,
            byte_length=source.byte_length,
            media_type=source.media_type,
            source_workspace_relative_path=source.workspace_relative_path,
            extraction_status=evaluation.status,
            content_status=evaluation.content_status,
            extraction_method=self._method_for(evaluation.effective_engine),
            requested_engine=prepared.request.requested_engine.upper(),
            requested_engine_version=chain.attempts[0].engine_version,
            effective_engine=evaluation.effective_engine,
            effective_engine_version=evaluation.effective_version,
            fallback_chain=list(chain.fallback),
            degradation_reasons=list(evaluation.degradation_reasons),
            attempts=list(chain.attempts),
            page_count=evaluation.page_count,
            character_count=evaluation.character_count,
            extracted_sha256=committed.extracted_sha256,
            extracted_file_sha256=committed.file_sha256,
            extracted_path=relative,
            extraction_output_format=evaluation.output_format,
            access_status=source.access_status,
            acquisition_method=source.acquisition_method,
            acquisition_attempts=list(source.attempts),
            access_assertion=source.access_assertion,
            selected_source=source.selected_source,
            selected_source_url=source.selected_source_url,
            normalized_doi=source.normalized_doi,
        )

    @staticmethod
    def _method_for(engine: str) -> ExtractionMethod:
        """Map an engine to the frozen extraction method it yields."""

        return extraction_method_for_engine(engine)

    def _committed_outcome(
        self,
        prepared: _PreparedExtraction,
        source: AcquiredDocumentRecord,
        chain: _ChainResult,
        evaluation: _Evaluation,
        committed: CommittedFile,
    ) -> ExtractionItemOutcome:
        relative = self._extracted_relative_path(
            prepared.request, source, evaluation.output_format
        )
        return ExtractionItemOutcome(
            study_id=source.study_id,
            document_id=source.document_id,
            extraction_status=evaluation.status,
            requested_engine=prepared.request.requested_engine.upper(),
            requested_engine_version=chain.attempts[0].engine_version,
            effective_engine=evaluation.effective_engine,
            effective_engine_version=evaluation.effective_version,
            content_status=evaluation.content_status,
            extraction_method=self._method_for(evaluation.effective_engine),
            page_count=evaluation.page_count,
            character_count=evaluation.character_count,
            source_sha256=source.source_sha256,
            acquisition_manifest_id=prepared.request.acquisition_manifest_id,
            acquisition_manifest_sha256=prepared.request.acquisition_manifest_sha256,
            extracted_path=relative,
            extracted_sha256=committed.extracted_sha256,
            extracted_file_sha256=committed.file_sha256,
            attempts=list(chain.attempts),
            fallback_chain=list(chain.fallback),
            degradation_reasons=list(evaluation.degradation_reasons),
        )

    def _determined_outcome(
        self,
        prepared: _PreparedExtraction,
        source: AcquiredDocumentRecord,
        chain: _ChainResult,
        evaluation: _Evaluation,
    ) -> ExtractionItemOutcome:
        return ExtractionItemOutcome(
            study_id=source.study_id,
            document_id=source.document_id,
            extraction_status=evaluation.status,
            requested_engine=prepared.request.requested_engine.upper(),
            requested_engine_version=chain.attempts[0].engine_version
            if chain.attempts
            else None,
            effective_engine=evaluation.effective_engine,
            effective_engine_version=evaluation.effective_version,
            content_status=evaluation.content_status,
            # The frozen DocumentRecord requires extraction_method on every
            # record, so a determined outcome states how it was reached instead
            # of leaving the field null.  The value is derived from the attempts
            # that actually ran: a provider engine gives EXTERNAL_PROVIDER, a
            # structural no-engine failure gives DETERMINISTIC_RULE.
            extraction_method=determined_outcome_method(chain.attempts),
            page_count=evaluation.page_count,
            character_count=evaluation.character_count,
            source_sha256=source.source_sha256,
            acquisition_manifest_id=prepared.request.acquisition_manifest_id,
            acquisition_manifest_sha256=prepared.request.acquisition_manifest_sha256,
            attempts=list(chain.attempts),
            fallback_chain=list(chain.fallback),
            degradation_reasons=list(evaluation.degradation_reasons),
            stage=ExtractionStage.VALIDATION,
            warning=StructuredError(
                code=evaluation.diagnostic_code or REASON_UNUSABLE_OUTPUT,
                message=(
                    evaluation.diagnostic_message
                    or "no engine produced usable extracted text"
                ),
                details={
                    "document_id": source.document_id,
                    "minimum_character_count": (
                        prepared.request.usability_profile.minimum_character_count
                    ),
                },
            ),
        )

    # ------------------------------------------------------------------
    # Manifest, candidate, envelope
    # ------------------------------------------------------------------

    def _build_manifest(
        self,
        prepared: Sequence[_PreparedExtraction],
        records: Sequence[ExtractedDocumentRecord],
        outcomes: Sequence[ExtractionItemOutcome],
        status: OperationStatus,
    ) -> ExtractionManifest:
        first = prepared[0].request
        acquisition_ref = prepared[0].acquisition_ref
        screening = prepared[0].screening
        screening_ref = ArtifactReference(
            artifact_id=screening.artifact_id,
            path=screening.workspace_relative_path,
            sha256=screening.sha256,
        )
        errors = [outcome.error for outcome in outcomes if outcome.error is not None]
        warnings = [
            outcome.warning for outcome in outcomes if outcome.warning is not None
        ]
        if status is OperationStatus.PARTIAL and not errors and not warnings:
            errors = [
                StructuredError(
                    code="BATCH_PARTIAL",
                    message="At least one requested document did not commit.",
                )
            ]
        if status is OperationStatus.FAILED and not errors:
            errors = [
                StructuredError(
                    code="BATCH_FAILED",
                    message="No requested document produced usable extracted text.",
                )
            ]
        manifest_id = deterministic_extraction_manifest_id(
            schema_version=EXTRACTION_MANIFEST_SCHEMA_VERSION,
            workspace_id=first.workspace_id,
            run_id=first.run_id,
            acquisition_manifest_ref=acquisition_ref.model_dump(mode="json"),
            extraction_records=self._stable_extraction_records(outcomes, records),
            algorithm_version="v1",
        )
        payload: dict[str, Any] = {
            "schema_version": EXTRACTION_MANIFEST_SCHEMA_VERSION,
            "manifest_type": EXTRACTION_MANIFEST_TYPE,
            "manifest_id": manifest_id,
            "manifest_identity_algorithm_version": "v1",
            "workspace_id": first.workspace_id,
            "run_id": first.run_id,
            "protocol_fingerprint": first.protocol_fingerprint,
            "corpus_fingerprint": first.corpus_fingerprint,
            "acquisition_manifest_ref": acquisition_ref.model_dump(mode="json"),
            "screening_decisions_ref": screening_ref.model_dump(mode="json"),
            "parent_lineage_sha256": canonical_fingerprint(
                {
                    "acquisition_manifest_ref": acquisition_ref.model_dump(mode="json"),
                    "screening_decisions_ref": screening_ref.model_dump(mode="json"),
                }
            ),
            "producer": self.producer.model_dump(mode="json"),
            "usability_profile": first.usability_profile.model_dump(mode="json"),
            "records": sorted(
                [record.model_dump(mode="json") for record in records],
                key=lambda record: (record["study_id"], record["document_id"]),
            ),
            "item_outcomes": sorted(
                [outcome.model_dump(mode="json") for outcome in outcomes],
                key=lambda outcome: (outcome["study_id"], outcome["document_id"] or ""),
            ),
            "idempotency_key": prepared[0].idempotency_key,
            "committed_at": utc_now(),
            "operation": ManifestOperation(
                status=status, errors=errors, warnings=warnings
            ).model_dump(mode="json"),
        }
        # The stable payload fingerprint covers the complete sidecar with the
        # self-referential checksum slot explicitly null, exactly as E1 does.
        payload["artifact_checksum"] = None
        payload["manifest_payload_fingerprint"] = canonical_fingerprint(payload)
        payload["artifact_checksum"] = canonical_fingerprint(payload)
        return ExtractionManifest.model_validate(payload)

    @staticmethod
    def _stable_extraction_records(
        outcomes: Sequence[ExtractionItemOutcome],
        records: Sequence[ExtractedDocumentRecord],
    ) -> list[dict[str, Any]]:
        """Project the volatile-free identity view of one batch (section 6.4).

        Timestamps (``committed_at``, ``attempted_at``), attempt ordinals, and
        response headers stay in the sidecar provenance but never reach the
        ``EXT-`` identity, and document/attempt order is normalized so input order
        cannot move the id.
        """

        by_document = {record.document_id: record for record in records}
        stable: list[dict[str, Any]] = []
        for outcome in outcomes:
            record = by_document.get(outcome.document_id or "")
            stable.append(
                {
                    "acquisition_manifest_id": outcome.acquisition_manifest_id,
                    "acquisition_manifest_sha256": outcome.acquisition_manifest_sha256,
                    "character_count": outcome.character_count,
                    "content_status": (
                        outcome.content_status.value
                        if outcome.content_status is not None
                        else None
                    ),
                    "degradation_reasons": sorted(set(outcome.degradation_reasons)),
                    "document_id": outcome.document_id,
                    "effective_engine": outcome.effective_engine,
                    "effective_engine_version": outcome.effective_engine_version,
                    "extracted_file_sha256": (
                        record.extracted_file_sha256 if record is not None else None
                    ),
                    "extracted_path": outcome.extracted_path,
                    "extracted_sha256": (
                        record.extracted_sha256 if record is not None else None
                    ),
                    "extraction_method": (
                        outcome.extraction_method.value
                        if outcome.extraction_method is not None
                        else None
                    ),
                    "extraction_output_format": (
                        record.extraction_output_format.value
                        if record is not None
                        else None
                    ),
                    "extraction_status": outcome.extraction_status.value,
                    "fallback_chain": [
                        {
                            "engine": step.engine,
                            "engine_version": step.engine_version,
                            "reason": step.reason.value,
                        }
                        for step in outcome.fallback_chain
                    ],
                    "page_count": outcome.page_count,
                    "prior_outcome_manifest_id": outcome.prior_outcome_manifest_id,
                    "prior_outcome_status": (
                        outcome.prior_outcome_status.value
                        if outcome.prior_outcome_status is not None
                        else None
                    ),
                    "requested_engine": outcome.requested_engine,
                    "source_sha256": outcome.source_sha256,
                    "study_id": outcome.study_id,
                }
            )
        return sorted(
            stable, key=lambda item: (item["study_id"], item["document_id"] or "")
        )

    @staticmethod
    def _operation_status(outcomes: Sequence[ExtractionItemOutcome]) -> OperationStatus:
        """Map the batch onto the canonical ``OperationStatus`` vocabulary.

        A retained superseded row (section 6.7(9)) is sidecar history, not a
        requested document, so it is excluded from the counts: a single-document
        retry that now succeeds is a full ``SUCCESS`` of the one current item, and
        reporting ``PARTIAL`` for it would misreport the batch.
        """

        current = [outcome for outcome in outcomes if not outcome.is_superseded]
        committed = sum(
            outcome.extraction_status in BYTE_BEARING_STATUSES for outcome in current
        )
        if committed == len(current):
            return OperationStatus.SUCCESS
        if committed:
            return OperationStatus.PARTIAL
        if any(
            outcome.extraction_status is ExtractionStatus.CANCELLED
            for outcome in current
        ):
            return OperationStatus.CANCELLED
        return OperationStatus.FAILED

    def _build_candidate(
        self,
        manifest: ExtractionManifest,
        prepared: _PreparedExtraction,
        outcomes: Sequence[ExtractionItemOutcome],
    ) -> DocumentManifestCandidate | None:
        """Build the deterministic candidate, or nothing when no bytes committed.

        A run with no byte-bearing record constructs no Contract candidate: the
        frozen model requires ``min_length=1`` documents, and the fail-closed
        sidecar is the authoritative outcome.
        """

        by_document = {record.document_id: record for record in manifest.records}
        committed_records: list[ExtractedDocumentRecord] = []
        non_committed: list[ArtifactRecordProjection] = []
        for outcome in outcomes:
            if outcome.is_superseded:
                # A retained prior outcome is sidecar history, not a second
                # candidate document: the document's candidate record is the one
                # its current outcome produced.
                continue
            record = by_document.get(outcome.document_id or "")
            if (
                record is not None
                and outcome.extraction_status in BYTE_BEARING_STATUSES
            ):
                committed_records.append(record)
            elif outcome.extraction_status in PRE_COMMIT_STATUSES:
                # Section 6.1: CANCELLED / pre-commit FAILED emit no record at
                # all -- not a FAILED DocumentRecord.  The failure taxonomy that
                # does reach the candidate is DETERMINED_FAILURE_STATUSES only.
                continue
            elif outcome.document_id is not None and outcome.source_sha256:
                non_committed.append(ArtifactRecordProjection.from_outcome(outcome))
        # A run in which *no* document produced a byte-bearing record constructs
        # no candidate at all: `DocumentManifestData.documents` has
        # min_length=1, and an empty list is a frozen-model violation rather than
        # a lenient encoding of "nothing worked" (E2-NEG-041).  Otherwise the
        # manifest is a complete account of the batch -- failed and OCR-pending
        # documents included, so grounded evidence is never silently dropped.
        if not committed_records:
            return None
        return build_document_manifest_candidate(
            workspace_id=manifest.workspace_id,
            run_id=manifest.run_id,
            protocol_fingerprint=manifest.protocol_fingerprint,
            corpus_fingerprint=manifest.corpus_fingerprint,
            screening_parent=prepared.screening,
            producer=self.producer,
            records=committed_records,
            non_committed=non_committed,
        )

    def _batch_outcome(
        self,
        manifest: ExtractionManifest,
        manifest_path: Path,
        outcomes: Sequence[ExtractionItemOutcome],
        *,
        status: OperationStatus,
        candidate: DocumentManifestCandidate | None,
        extra_errors: Sequence[StructuredError] = (),
    ) -> ExtractionBatchOutcome:
        # The envelope reports the batch's *current* truth.  A superseded row
        # (section 6.7(9)) is retained in the sidecar's ``item_outcomes`` for
        # auditability, but handing it back to a caller as if it were the
        # document's current state -- and counting it as a requested document --
        # would make a recovered single-document batch look like a partial one.
        current = [outcome for outcome in outcomes if not outcome.is_superseded]
        committed = sum(
            outcome.extraction_status in BYTE_BEARING_STATUSES for outcome in current
        )
        root = self.workspace_bindings[manifest.workspace_id].canonical_root
        # A FAILED/CANCELLED envelope must not be silent: carry the batch-level
        # diagnostic the sidecar recorded, plus every per-item error, so a caller
        # never has to re-derive *why* nothing committed.
        errors = list(extra_errors)
        # A non-SUCCESS envelope must never be silent (the envelope validator
        # rejects a FAILED/CANCELLED/PARTIAL envelope that explains nothing).
        # PARTIAL belongs here too: a mixed batch commits some documents and
        # determines others, so the sidecar records the per-item diagnostics while
        # the envelope carries neither the batch-level error nor the per-item
        # errors/warnings -- the caller was told a committed subset without being
        # told *why*.  Warnings are lifted as well as errors because a determined
        # failure is routinely reported as a warning (an unusable body, a missing
        # text layer) and that warning is the only explanation the row carries.
        if status in {
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.PARTIAL,
        }:
            for error in manifest.operation.errors:
                if error not in errors:
                    errors.append(error)
            for outcome in current:
                for diagnostic in (outcome.error, outcome.warning):
                    if diagnostic is not None and diagnostic not in errors:
                        errors.append(diagnostic)
        if not errors and status in {OperationStatus.FAILED, OperationStatus.CANCELLED}:
            errors.append(
                StructuredError(
                    code="BATCH_FAILED",
                    message="No requested document produced usable extracted text.",
                )
            )
        return ExtractionBatchOutcome(
            run_id=manifest.run_id,
            status=status,
            data=ExtractionOperationData(
                manifest_reference=ExtractionManifestReference(
                    manifest_id=manifest.manifest_id,
                    workspace_relative_path=self._relative_path(root, manifest_path),
                    artifact_checksum=manifest.artifact_checksum,
                ),
                item_outcomes=current,
                committed_count=committed,
                requested_count=len(current),
                candidate=candidate,
            ),
            errors=errors,
            provenance={
                "manifest_checksum": manifest.artifact_checksum,
                "manifest_id": manifest.manifest_id,
                "idempotency_key": manifest.idempotency_key,
                "usability_profile": manifest.usability_profile.model_dump(mode="json"),
                "acquisition_manifest_id": manifest.acquisition_manifest_ref.manifest_id,
                "contract_acceptance": "not_performed_by_kit",
            },
        )

    def _preflight_failure(
        self,
        requests: Sequence[ExtractionRequest],
        error: ExtractionPreflightError,
    ) -> ExtractionBatchOutcome:
        """Report a fail-closed rejection with no sidecar and no candidate."""

        first = requests[0]
        outcomes = [
            ExtractionItemOutcome(
                study_id=request.study_id,
                document_id=None,
                extraction_status=ExtractionStatus.FAILED,
                requested_engine=request.requested_engine.upper(),
                requested_engine_version=None,
                content_status=DocumentContentStatus.FAILED,
                # No engine ran: a preflight rejection is determined entirely by
                # a deterministic rule over the request and its lineage, so the
                # method records exactly that instead of asserting a model or
                # provider involvement that never happened.
                extraction_method=ExtractionMethod.DETERMINISTIC_RULE,
                source_sha256=None,
                acquisition_manifest_id=request.acquisition_manifest_id,
                acquisition_manifest_sha256=request.acquisition_manifest_sha256,
                stage=ExtractionStage.PREFLIGHT,
                error=error.as_error(),
            )
            for request in requests
        ]
        return ExtractionBatchOutcome(
            run_id=first.run_id,
            status=OperationStatus.FAILED,
            data=ExtractionOperationData(
                manifest_reference=None,
                item_outcomes=outcomes,
                committed_count=0,
                requested_count=len(outcomes),
                candidate=None,
            ),
            errors=[error.as_error()],
            provenance={"preflight_code": error.code},
        )

    # ------------------------------------------------------------------
    # Sidecar publication
    # ------------------------------------------------------------------

    def _publish_sidecar_locked(
        self, manifest: ExtractionManifest, destination: Path
    ) -> ExtractionManifest:
        """Publish the sidecar with atomic no-replace semantics.

        The sidecar is the commit marker for extraction state, so a concurrent
        writer must never be able to make this operation replace a different
        valid sidecar at the same identity path.  An already-published valid
        sidecar for this identity is returned unchanged (deterministic coalesce).
        """

        lock_path = destination.with_suffix(SIDECAR_LOCK_SUFFIX)
        descriptor = self._acquire_lock_file(lock_path, timeout=30.0)
        temporary: Path | None = None
        try:
            if destination.exists():
                existing = self._read_published(destination)
                if existing is not None:
                    return existing
                raise ExtractionCommitError(
                    "SIDECAR_CONFLICT",
                    "The sidecar identity is already bound to invalid bytes.",
                )
            self._inject(ExtractionFault.SIDECAR_REPLACE, manifest.manifest_id)
            temporary = self._new_temporary(
                destination.parent, prefix=f".{manifest.manifest_id}."
            )
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(
                    manifest.model_dump(mode="json"),
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                if destination.is_symlink():
                    raise ExtractionCommitError(
                        "PATH_OUTSIDE_WORKSPACE",
                        "The sidecar destination cannot be a symlink.",
                    ) from error
                existing = self._read_published(destination)
                if existing is not None:
                    return existing
                raise ExtractionCommitError(
                    "SIDECAR_CONFLICT",
                    "The sidecar identity is already bound to invalid bytes.",
                ) from error
            return manifest
        except ExtractionCommitError:
            raise
        except OSError as error:
            raise ExtractionCommitError(
                "ATOMIC_COMMIT_FAILED",
                "The extraction sidecar could not be committed atomically.",
            ) from error
        finally:
            self._cleanup_temporary(temporary)
            os.close(descriptor)
            self._cleanup_temporary(lock_path)

    def _read_published(self, path: Path) -> ExtractionManifest | None:
        """Return the valid sidecar already bound to *path*, if any."""

        try:
            existing = ExtractionManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            self.verify_manifest(existing, verify_bytes=False)
        except (OSError, ValueError, ExtractionCommitError):
            return None
        return existing

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    async def _append_audit(
        self,
        manifest: ExtractionManifest,
        manifest_path: Path,
        candidate: DocumentManifestCandidate | None,
    ) -> StructuredError | None:
        try:
            self._inject(ExtractionFault.AUDIT_APPEND, manifest.manifest_id)
            if await self.audit_sink.has_event(
                manifest.manifest_id, manifest.idempotency_key
            ):
                return None
            await self.audit_sink.append_once(
                self._audit_event(manifest, manifest_path, candidate)
            )
        except asyncio.CancelledError:
            return StructuredError(
                code="AUDIT_APPEND_CANCELLED",
                message=(
                    "The sidecar committed, but audit append was cancelled and is "
                    "recoverable."
                ),
                retryable=True,
                details={"error_type": "CancelledError"},
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return StructuredError(
                code="AUDIT_APPEND_FAILED",
                message=(
                    "The sidecar committed, but its canonical audit event is "
                    "recoverable."
                ),
                retryable=True,
                details={"error_type": type(error).__name__},
            )
        return None

    def _audit_event(
        self,
        manifest: ExtractionManifest,
        manifest_path: Path,
        candidate: DocumentManifestCandidate | None,
    ) -> dict[str, Any]:
        """Build the one canonical workspace-manager event for this commit.

        The event records the real kit identity, the ``EXT-`` id and checksum, the
        acquisition manifest identity and checksum, the recorded usability profile,
        and the candidate artifact id.  It never claims Contract acceptance:
        acceptance evidence comes from a harness ``accept_artifact`` call, not from
        this kit.
        """

        root = self.workspace_bindings[manifest.workspace_id].canonical_root
        sidecar = self._relative_path(root, manifest_path)
        return {
            "action": EXTRACTION_AUDIT_ACTION,
            "agent_or_tool": (
                f"{self.producer.package}/{self.producer.version}@{self.producer.commit}"
            ),
            "description": "Committed deterministic PDF text extraction sidecar.",
            "inputs": [
                manifest.acquisition_manifest_ref.manifest_id
                + "@"
                + manifest.acquisition_manifest_ref.artifact_checksum,
                manifest.screening_decisions_ref.artifact_id
                + "@"
                + manifest.screening_decisions_ref.sha256,
            ],
            "outputs": [sidecar],
            "parameters": {
                "acquisition_manifest_id": manifest.acquisition_manifest_ref.manifest_id,
                "acquisition_manifest_sha256": (
                    manifest.acquisition_manifest_ref.artifact_checksum
                ),
                "artifact_checksum": manifest.artifact_checksum,
                "contract_acceptance": "not_performed_by_kit",
                "contract_candidate_artifact_id": (
                    None if candidate is None else candidate.artifact_id
                ),
                "idempotency_key": manifest.idempotency_key,
                "manifest_id": manifest.manifest_id,
                "manifest_path": sidecar,
                "manifest_type": manifest.manifest_type,
                "protocol_fingerprint": manifest.protocol_fingerprint,
                "corpus_fingerprint": manifest.corpus_fingerprint,
                "usability_profile": manifest.usability_profile.model_dump(mode="json"),
            },
            "metrics": {
                "committed_documents": len(manifest.records),
                "requested_documents": len(manifest.item_outcomes),
            },
            "status": manifest.operation.status,
        }

    # ------------------------------------------------------------------
    # Filesystem primitives, delegated to the E1 implementation
    # ------------------------------------------------------------------

    def _safe_workspace_path(self, root: Path, relative: str, *, create: bool) -> Path:
        try:
            return self._acquisition._safe_workspace_path(root, relative, create=create)
        except (AcquisitionPreflightError, OSError, ValueError) as error:
            # E1 refuses an escaping or non-portable path.  A bare ``ValueError``
            # from the portable-path rule is containment evidence too, not a
            # crash: when such a path reaches the commit path without having
            # passed the request model -- a hand-built request, a legacy
            # adapter, a field the model gained later -- it becomes the same
            # structured per-item commit failure, so the batch reports FAILED
            # with a candidate of None instead of an unstructured exception
            # escaping the public API.
            raise ExtractionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "A workspace path component cannot be safely anchored.",
                path=relative,
            ) from error

    def _prepared_file_path(self, root: Path, relative: str) -> Path:
        """Anchor a *file* destination, creating only its parent directory."""

        parent = str(PurePosixPath(relative).parent)
        self._safe_workspace_path(root, parent, create=True)
        return self._safe_workspace_path(root, relative, create=False)

    def _new_temporary(self, directory: Path, *, prefix: str) -> Path:
        return self._acquisition._new_temporary(directory, prefix=prefix)

    def _cleanup_temporary(self, path: Path | None) -> StructuredError | None:
        return self._acquisition._cleanup_temporary(path)

    def _hash_file(self, path: Path) -> str:
        return self._acquisition._hash_file(path)

    def _acquire_lock_file(self, lock_path: Path, *, timeout: float) -> int:
        return self._acquisition._acquire_lock_file(lock_path, timeout=timeout)

    def _relative_path(self, root: Path, path: Path) -> str:
        return self._acquisition._relative_path(root, path)

    @contextmanager
    def _extraction_publication_lock(self, workspace_root: Path) -> Iterator[None]:
        """Serialize sidecar publication and owned-orphan cleanup per workspace."""

        root = self._safe_workspace_path(
            workspace_root, SIDECAR_STORAGE_PREFIX, create=True
        )
        descriptor = self._acquire_lock_file(root / ".publication.lock", timeout=30.0)
        try:
            yield
        finally:
            os.close(descriptor)
            self._cleanup_temporary(root / ".publication.lock")

    async def _operation_lock(self, key: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        identity = (loop, key)
        if identity not in self._operation_locks:
            self._operation_locks[identity] = asyncio.Lock()
        return self._operation_locks[identity]

    def _promote(
        self, temporary_path: Path, final_path: Path, expected_hash: str
    ) -> Any:
        """Publish validated bytes at the identity-addressed path atomically."""

        try:
            return self._acquisition._promote(temporary_path, final_path, expected_hash)
        except AcquisitionCommitError as error:
            raise ExtractionCommitError(error.code, error.message) from error

    def _write_commit_intent(
        self, final_path: Path, payload: Mapping[str, Any]
    ) -> None:
        try:
            self._acquisition._write_commit_intent(final_path, payload)
        except AcquisitionCommitError as error:
            raise ExtractionCommitError(error.code, error.message) from error

    def _discard_commit_intent(self, final_path: Path) -> None:
        self._acquisition._discard_commit_intent(final_path)

    def _verify_committed_file(
        self,
        root: Path,
        record: ExtractedDocumentRecord | AcquiredDocumentRecord,
        *,
        relative: str | None = None,
        expected: CommittedFile | None = None,
        candidate_path: Path | None = None,
    ) -> None:
        """Verify an extracted file against its recorded bindings.

        ``candidate_path`` verifies a *staged* file before it is promoted, so a
        file that never reached its final path is still validated (and rejected)
        on the way in rather than discovered missing afterwards.
        """

        target = relative or getattr(record, "extracted_path", None)
        if not target:
            raise ExtractionCommitError(
                "EXTRACTED_PATH_MISSING", "The record carries no extracted path."
            )
        path = candidate_path or self._safe_workspace_path(root, target, create=False)
        if not path.is_file():
            raise ExtractionCommitError(
                "EXTRACTED_CONTENT_MISSING",
                "The committed extracted file is missing.",
                path=target,
            )
        data = path.read_bytes()
        file_sha256 = (
            expected.file_sha256
            if expected
            else getattr(record, "extracted_file_sha256", None)
        )
        body_sha256 = (
            expected.extracted_sha256
            if expected
            else getattr(record, "extracted_sha256", None)
        )
        if not file_sha256 or not body_sha256:
            raise ExtractionCommitError(
                "EXTRACTED_CHECKSUM_MISSING",
                "The record carries no extracted checksums.",
            )
        if expected is not None and data != expected.data:
            raise ExtractionCommitError(
                "EXTRACTED_CONTENT_MISMATCH",
                "The staged extracted file no longer holds the validated bytes.",
            )
        if file_sha256 != self._hash_file(path):
            raise ExtractionCommitError(
                "EXTRACTED_FILE_MUTATED",
                "The committed extracted file no longer matches its checksum.",
            )
        try:
            verify_bound_frontmatter(
                data,
                document_id=record.document_id,
                source_sha256=record.source_sha256,
                # An E1 `AcquiredDocumentRecord` carries no manifest binding, so
                # only assert what the record actually holds.  Replay and verify
                # paths pass an E2 record, which always carries both.
                acquisition_manifest_sha256=getattr(
                    record, "acquisition_manifest_sha256", None
                ),
                acquisition_manifest_id=getattr(
                    record, "acquisition_manifest_id", None
                ),
                extracted_sha256=body_sha256,
                file_sha256=file_sha256,
            )
        except FrontmatterError as error:
            raise ExtractionCommitError(error.code, error.message) from error

    def _inject(self, point: ExtractionFault, identity: str) -> None:
        if self.fault_injector is None:
            return
        self.fault_injector(point, identity)


__all__ = [
    "EXTRACTION_AUDIT_ACTION",
    "REFUSAL_ERROR_CODES",
    "ExtractionCommitError",
    "ExtractionFault",
    "ExtractionPreflightError",
    "InMemoryAuditSink",
    "PDFExtractionService",
    "WorkspaceManagerCliAuditSink",
]
