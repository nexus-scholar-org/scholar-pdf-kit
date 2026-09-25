"""Deterministic, parent-bound PDF acquisition service for Contract v1 lineage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

import aiohttp
from pypdf.errors import PdfReadError

from .acquisition_models import (
    MANIFEST_SCHEMA_VERSION,
    PDF_MEDIA_TYPE,
    AcceptedParentBinding,
    AccessStatus,
    AcquiredDocumentManifest,
    AcquiredDocumentRecord,
    AcquisitionAttempt,
    AcquisitionBatchOutcome,
    AcquisitionItemOutcome,
    AcquisitionOperationData,
    AcquisitionRequest,
    AcquisitionRunConfig,
    AcquisitionSourceKind,
    AcquisitionStage,
    AcquisitionStatus,
    E2EmbeddedReference,
    ManifestOperation,
    ManifestReference,
    MethodProvenance,
    OperationStatus,
    ParentArtifactRef,
    ParentArtifactRefs,
    ProducerProvenance,
    StructuredError,
    ValidationResult,
    WorkspaceRootBinding,
    validate_portable_relative_path,
)
from .canonical import (
    canonical_fingerprint,
    corpus_snapshot_fingerprint,
    deterministic_acquisition_manifest_id,
    deterministic_document_id,
)
from .contract_parents import ParentStructureError, validate_parent_structure
from .publisher_patterns import rewrite_via_proxy
from .validator import MIN_PDF_SIZE_BYTES, is_valid_pdf

_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_DEFAULT_RETRIES = 2
_LOGGER = logging.getLogger(__name__)
# Durability marker written next to a promoted document *before* the manifest
# is published.  Its presence is the only evidence that distinguishes an orphan
# left by a killed acquisition process from an unmanaged file that a human (or
# another tool) dropped on the content-addressed path by hand.
COMMIT_INTENT_SUFFIX = ".commit-intent.json"
COMMIT_INTENT_SCHEMA_VERSION = "pdf-acquisition-commit-intent-v1"
_TEMPORARY_CLEANUP_ATTEMPTS = 3
_SAFE_PROVIDER_KEYS = {
    "best_oa_location",
    "doi",
    "is_oa",
    "landing_page_url",
    "license",
    "oa_status",
    "open_access",
    "primary_location",
    "publisher",
    "pdf_url",
    "restricted",
    "source",
    "url_for_pdf",
    "version",
}


class AcquisitionFault(StrEnum):
    DOWNLOAD = "download"
    VALIDATION = "validation"
    CONTENT_MOVE = "content_move"
    MANIFEST_REPLACE = "manifest_replace"
    AUDIT_APPEND = "audit_append"


class AcquisitionPreflightError(Exception):
    """A fail-closed request rejection that occurs before output I/O."""

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


class AcquisitionCommitError(Exception):
    """A committed-state or atomic-publication failure with bounded detail."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        transport_result: Any | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.transport_result = transport_result

    def as_error(self) -> StructuredError:
        return StructuredError(
            code=self.code,
            message=self.message,
            retryable=self.code == "NETWORK_ERROR",
            details=self.details,
        )


@dataclass(frozen=True)
class TransportResult:
    http_status: int | None = None
    resolved_url: str | None = None
    observed_media_type: str | None = None
    retryable: bool = False


class AcquisitionTransport(Protocol):
    """Minimal fake-friendly transport owned by the acquisition service."""

    owns_resources: bool

    async def download(
        self,
        *,
        url: str,
        destination: Path,
        forward_proxy_url: str | None,
        max_bytes: int,
    ) -> TransportResult: ...

    async def close(self) -> None: ...


class AiohttpAcquisitionTransport:
    """Streaming aiohttp transport; a new session is owned when requested."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_seconds: float = 60.0,
        user_agent: str = "scholar-pdf-kit/0.1 (+https://github.com/nexus-scholar-org)",
    ) -> None:
        self._session = session
        self._owns_resources = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._user_agent = user_agent

    @property
    def owns_resources(self) -> bool:
        return self._owns_resources

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def download(
        self,
        *,
        url: str,
        destination: Path,
        forward_proxy_url: str | None,
        max_bytes: int,
    ) -> TransportResult:
        session = self._get_session()
        timeout = self._timeout
        headers = {"Accept": "application/pdf", "User-Agent": self._user_agent}
        async with session.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            proxy=forward_proxy_url or None,
        ) as response:
            if response.status in {401, 403, 404, 410}:
                return TransportResult(
                    http_status=response.status,
                    resolved_url=str(response.url),
                    observed_media_type=response.headers.get("Content-Type"),
                )
            if response.status == 429 or response.status >= 500:
                return TransportResult(
                    http_status=response.status,
                    resolved_url=str(response.url),
                    observed_media_type=response.headers.get("Content-Type"),
                    retryable=True,
                )
            if 400 <= response.status < 500:
                return TransportResult(
                    http_status=response.status,
                    resolved_url=str(response.url),
                    observed_media_type=response.headers.get("Content-Type"),
                )
            response.raise_for_status()
            byte_count = 0
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise AcquisitionCommitError(
                            "CONTENT_SIZE_EXCEEDED",
                            "Downloaded content exceeded the configured size limit.",
                        )
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            return TransportResult(
                http_status=response.status,
                resolved_url=str(response.url),
                observed_media_type=response.headers.get("Content-Type"),
            )

    async def close(self) -> None:
        if self._owns_resources and self._session is not None:
            await self._session.close()
            self._session = None


class AuditSink(Protocol):
    """Caller-provided canonical workspace-manager adapter."""

    async def has_event(self, manifest_id: str, idempotency_key: str) -> bool: ...

    async def append_once(self, event: Mapping[str, Any]) -> bool: ...


class InMemoryAuditSink:
    """Deterministic audit adapter for API tests and embedding."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._keys: set[tuple[str, str]] = set()

    async def has_event(self, manifest_id: str, idempotency_key: str) -> bool:
        return (manifest_id, idempotency_key) in self._keys

    async def append_once(self, event: Mapping[str, Any]) -> bool:
        parameters = event.get("parameters", {})
        key = (
            str(parameters.get("manifest_id")),
            str(parameters.get("idempotency_key")),
        )
        if key in self._keys:
            return False
        self._keys.add(key)
        self.events.append(dict(event))
        return True


class WorkspaceManagerCliAuditSink:
    """Invoke the canonical workspace-manager logger under an idempotency lock."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        logger_path: Path,
        python_executable: str | None = None,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.workspace_root = workspace_root
        self.logger_path = logger_path
        self.python_executable = python_executable or os.sys.executable
        self.lock_timeout_seconds = lock_timeout_seconds
        self._lock_path = workspace_root / "audit" / ".pdf-acquisition.lock"

    def _safe_workspace_path(self, relative: str) -> Path:
        root = self.workspace_root.resolve(strict=True)
        candidate = self.workspace_root.joinpath(*PurePosixPath(relative).parts)
        cursor = root
        for part in PurePosixPath(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise OSError("workspace audit path component cannot be a symlink")
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as error:
            raise OSError("workspace audit path escapes the canonical root") from error
        return candidate

    def _event_exists(self, manifest_id: str, idempotency_key: str) -> bool:
        journal = self._safe_workspace_path("audit/journal.jsonl")
        if not journal.exists():
            return False
        with journal.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                parameters = event.get("parameters", {})
                if (
                    parameters.get("manifest_id") == manifest_id
                    and parameters.get("idempotency_key") == idempotency_key
                ):
                    return True
        return False

    async def has_event(self, manifest_id: str, idempotency_key: str) -> bool:
        return self._event_exists(manifest_id, idempotency_key)

    async def _acquire_lock(self) -> Any:
        audit_dir = self._safe_workspace_path("audit")
        audit_dir.mkdir(parents=True, exist_ok=True)
        lock_path = audit_dir / ".pdf-acquisition.lock"
        self._safe_workspace_path("audit/.pdf-acquisition.lock")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.lock_timeout_seconds
        while True:
            try:
                if lock_path.is_symlink():
                    raise OSError("workspace audit lock cannot be a symlink")
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
                # Hold the OS lock for the whole critical section.  Without it a
                # concurrent process could treat this fresh lock as a dead
                # owner's leftover and unlink it.
                if not PDFAcquisitionService._lock_descriptor(descriptor):
                    os.close(descriptor)
                    raise OSError("workspace audit lock could not be locked")
                self._lock_path = lock_path
                return os.fdopen(descriptor, "wb")
            except FileExistsError:
                # The OS byte-range lock is released when the owner dies, so a
                # lock left by a killed logger is reclaimed instead of waiting
                # for the full timeout.
                if PDFAcquisitionService._reclaim_stale_lock(lock_path):
                    continue
                if loop.time() >= deadline:
                    raise TimeoutError("timed out waiting for the workspace audit lock")
                await asyncio.sleep(0.05)

    async def append_once(self, event: Mapping[str, Any]) -> bool:
        parameters = event.get("parameters", {})
        manifest_id = str(parameters.get("manifest_id"))
        idempotency_key = str(parameters.get("idempotency_key"))
        lock = await self._acquire_lock()
        try:
            if self._event_exists(manifest_id, idempotency_key):
                return False
            program = (
                "import importlib.util,json,sys;"
                "spec=importlib.util.spec_from_file_location('workspace_manager_log_event',sys.argv[1]);"
                "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
                "spec=json.loads(sys.argv[3]);"
                "module.log_project_event(sys.argv[2],spec['action'],spec['agent_or_tool'],"
                "spec['description'],spec.get('inputs'),spec.get('outputs'),"
                "spec.get('parameters'),spec.get('metrics'),spec.get('status','SUCCESS'))"
            )
            process = await asyncio.create_subprocess_exec(
                self.python_executable,
                "-c",
                program,
                str(self.logger_path),
                str(self.workspace_root),
                json.dumps(dict(event), sort_keys=True),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communication = asyncio.create_task(process.communicate())
            try:
                _, stderr = await asyncio.shield(communication)
            except asyncio.CancelledError:
                # Keep the exclusive audit lock until the logger subprocess has
                # either appended the event or exited.  A cancellation after the
                # manifest commit is surfaced as a recoverable audit error.
                await communication
                raise
            if process.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip()
                raise OSError(f"workspace-manager logger failed: {message[:300]}")
            return True
        finally:
            lock.close()
            self._lock_path.unlink(missing_ok=True)


FaultInjector = Callable[[AcquisitionFault, str], None]


@dataclass(frozen=True)
class ValidationReport:
    profile: str
    profile_version: str
    valid: bool
    media_type: str = "application/pdf"
    byte_length: int = 0
    source_sha256: str | None = None
    structural: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "profile": self.profile,
            "profile_version": self.profile_version,
            "source_sha256": self.source_sha256,
            "structural": self.structural,
            "valid": self.valid,
        }


@dataclass(frozen=True)
class _PreparedRequest:
    request: AcquisitionRequest
    corpus: AcceptedParentBinding
    screening: AcceptedParentBinding
    workspace_root: Path
    idempotency_key: str
    selected_source: str | None


@dataclass(frozen=True)
class _StagedDocument:
    record: AcquiredDocumentRecord | None
    outcome: AcquisitionItemOutcome
    temporary_path: Path | None
    promoted_new: bool = False


@dataclass(frozen=True)
class _Promotion:
    """Result of promoting staged bytes to their content-addressed path."""

    promoted_new: bool
    temporary_leftover: Path | None


class PDFAcquisitionService:
    """Own the E1 API while delegating transport and audit to injected adapters."""

    def __init__(
        self,
        *,
        accepted_parents: Sequence[AcceptedParentBinding],
        workspace_bindings: Mapping[str, WorkspaceRootBinding],
        producer: ProducerProvenance,
        audit_sink: AuditSink,
        transport: AcquisitionTransport | None = None,
        fault_injector: FaultInjector | None = None,
        minimum_pdf_bytes: int = MIN_PDF_SIZE_BYTES,
        maximum_pdf_bytes: int = _DEFAULT_MAX_BYTES,
        max_attempts: int = _DEFAULT_RETRIES,
    ) -> None:
        if minimum_pdf_bytes <= 0 or maximum_pdf_bytes < minimum_pdf_bytes:
            raise ValueError("PDF size bounds are invalid")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.accepted_parents = tuple(
            parent.model_copy(deep=True) for parent in accepted_parents
        )
        self.workspace_bindings = {
            workspace_id: binding.model_copy(deep=True)
            for workspace_id, binding in workspace_bindings.items()
        }
        self.producer = producer.model_copy(deep=True)
        self.audit_sink = audit_sink
        self.transport = transport
        self.fault_injector = fault_injector
        self.minimum_pdf_bytes = minimum_pdf_bytes
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.max_attempts = max_attempts
        self._parent_by_id = {
            parent.artifact_id: parent for parent in self.accepted_parents
        }
        if len(self._parent_by_id) != len(self.accepted_parents):
            raise ValueError("accepted parent IDs must be unique")
        # Serialize equivalent batches within one service instance.  The
        # filesystem manifest lock remains the cross-process safety boundary.
        self._operation_locks: dict[
            tuple[asyncio.AbstractEventLoop, str], asyncio.Lock
        ] = {}

    @classmethod
    def from_config(
        cls,
        config: AcquisitionRunConfig,
        *,
        audit_sink: AuditSink,
        transport: AcquisitionTransport | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> PDFAcquisitionService:
        return cls(
            accepted_parents=config.accepted_parents,
            workspace_bindings=config.workspace_bindings,
            producer=config.producer,
            audit_sink=audit_sink,
            transport=transport,
            fault_injector=fault_injector,
        )

    @staticmethod
    async def _close_owned_transport(transport: AcquisitionTransport) -> None:
        """Finish resource cleanup even when the acquisition task is cancelled."""

        close_task = asyncio.create_task(transport.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            try:
                await close_task
            except Exception:
                _LOGGER.exception("Owned PDF acquisition transport cleanup failed.")
            raise
        except Exception:
            # Resource cleanup must not replace a committed or fail-closed
            # acquisition outcome.
            _LOGGER.exception("Owned PDF acquisition transport cleanup failed.")

    async def acquire(
        self, requests: Sequence[AcquisitionRequest]
    ) -> AcquisitionBatchOutcome:
        """Run one same-workspace/run batch without treating failures as success."""

        if not requests:
            raise ValueError("at least one acquisition request is required")
        request_snapshot = tuple(request.model_copy(deep=True) for request in requests)
        transport = self.transport or AiohttpAcquisitionTransport()
        owns_transport = bool(
            getattr(transport, "owns_resources", self.transport is None)
        )
        try:
            try:
                prepared = self._preflight(request_snapshot)
            except AcquisitionPreflightError as error:
                return self._preflight_failure(request_snapshot, error)

            operation_key = self._batch_idempotency_key(prepared)
            operation_lock = await self._operation_lock(operation_key)
            await operation_lock.acquire()
            try:
                return await self._acquire_locked(prepared, transport)
            finally:
                operation_lock.release()
        finally:
            if owns_transport:
                await self._close_owned_transport(transport)

    async def _acquire_locked(
        self,
        prepared: Sequence[_PreparedRequest],
        transport: AcquisitionTransport,
    ) -> AcquisitionBatchOutcome:
        """Run a preflighted batch while holding the per-service batch lock."""

        first = prepared[0]
        try:
            manifest_root = self._safe_workspace_path(
                first.workspace_root,
                f"literature/acquisition/{first.request.run_id}",
                create=False,
            )
        except AcquisitionPreflightError as error:
            return self._preflight_failure([item.request for item in prepared], error)
        batch_key = self._batch_idempotency_key(prepared)
        try:
            replay = self._find_run_replay(
                manifest_root, first.request.run_id, batch_key
            )
            if replay is not None:
                return await self._replay_outcome(replay, prepared)
        except AcquisitionPreflightError as error:
            return self._preflight_failure([item.request for item in prepared], error)
        except AcquisitionCommitError:
            return self._preflight_failure(
                [item.request for item in prepared],
                AcquisitionPreflightError(
                    "REPLAY_VERIFICATION_FAILED",
                    "A matching acquisition manifest failed byte or lineage verification.",
                ),
            )

        staged: list[_StagedDocument] = []
        try:
            for index, item in enumerate(prepared):
                staged_item = await self._acquire_one(item, transport)
                staged.append(staged_item)
                if (
                    staged_item.outcome.acquisition_status
                    is AcquisitionStatus.CANCELLED
                ):
                    for remaining in prepared[index + 1 :]:
                        staged.append(
                            self._failed_item(
                                remaining,
                                AcquisitionStatus.CANCELLED,
                                "ACQUISITION_CANCELLED",
                                "Acquisition was cancelled before content commit.",
                                [],
                            )
                        )
                    break
        except asyncio.CancelledError:
            for item in staged:
                self._cleanup_temporary(item.temporary_path)
            raise

        outcomes = [item.outcome for item in staged]
        if outcomes and all(
            item.acquisition_status is AcquisitionStatus.REUSED for item in outcomes
        ):
            try:
                replay = self._find_run_replay(
                    manifest_root, first.request.run_id, batch_key
                )
                if replay is not None:
                    return await self._replay_outcome(replay, prepared)
            except AcquisitionPreflightError as error:
                return self._preflight_failure(
                    [item.request for item in prepared], error
                )
            except AcquisitionCommitError:
                return self._preflight_failure(
                    [item.request for item in prepared],
                    AcquisitionPreflightError(
                        "REPLAY_VERIFICATION_FAILED",
                        "A matching acquisition manifest failed byte or lineage verification.",
                    ),
                )

        records = [item.record for item in staged if item.record is not None]
        status = self._operation_status(outcomes)
        manifest = self._build_manifest(prepared, records, outcomes, status)
        manifest_path = self._manifest_path(manifest_root, manifest.manifest_id)
        try:
            published_manifest = self._publish_manifest(manifest, manifest_path)
            if published_manifest.model_dump(mode="json") != manifest.model_dump(
                mode="json"
            ):
                # A concurrent writer won the identity path with equivalent
                # stable records.  Report the committed bytes as reused and
                # use the persisted manifest/provenance for audit/output.
                manifest = published_manifest
                outcomes = [
                    item.model_copy(
                        update={"acquisition_status": AcquisitionStatus.REUSED}
                    )
                    if item.document_id is not None
                    else item
                    for item in outcomes
                ]
                status = self._operation_status(outcomes)
        except (
            AcquisitionCommitError,
            OSError,
            RuntimeError,
            TimeoutError,
        ) as error:
            notes = self._release_staged(staged)
            for item in staged:
                if item.promoted_new and item.record is not None:
                    anomaly = self._remove_owned_orphan(
                        first.workspace_root,
                        item.record.workspace_relative_path,
                        document_id=item.record.document_id,
                        expected_sha256=item.record.source_sha256,
                    )
                    if anomaly is not None:
                        notes.append(anomaly.message)
            commit_error = (
                error
                if isinstance(error, AcquisitionCommitError)
                else AcquisitionCommitError(
                    "ATOMIC_COMMIT_FAILED",
                    "The acquisition manifest could not be committed.",
                )
            )
            return self._commit_failure_outcome(
                prepared,
                outcomes,
                commit_error.as_error(),
                extra_warnings=notes,
            )
        except BaseException:
            # Cancellation or an unexpected error: still release every staged
            # temporary before propagating, then let the caller decide.
            self._release_staged(staged)
            raise

        # The manifest is now the authoritative commit marker, so the crash
        # recovery markers for its documents are no longer needed.
        for item in staged:
            if item.record is not None:
                self._discard_commit_intent(
                    self._safe_workspace_path(
                        first.workspace_root,
                        item.record.workspace_relative_path,
                        create=False,
                    )
                )
        notes = self._release_staged(staged)

        audit_error = await self._append_audit(manifest, manifest_path)
        reported_status = status
        if audit_error is not None and manifest.records:
            reported_status = OperationStatus.PARTIAL
        if notes and reported_status is OperationStatus.SUCCESS:
            reported_status = OperationStatus.PARTIAL
        return self._batch_outcome(
            manifest,
            manifest_path,
            outcomes,
            status=reported_status,
            extra_errors=[audit_error] if audit_error else [],
            extra_warnings=notes,
        )

    async def _operation_lock(self, key: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock_key = (loop, key)
        lock = self._operation_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[lock_key] = lock
        return lock

    @staticmethod
    def _batch_idempotency_key(prepared: Sequence[_PreparedRequest]) -> str:
        first = prepared[0]
        return canonical_fingerprint(
            {
                "request_keys": sorted(item.idempotency_key for item in prepared),
                "run_id": first.request.run_id,
                "schema_version": first.request.schema_version,
                "workspace_id": first.request.workspace_id,
            }
        )

    def _preflight(
        self, requests: Sequence[AcquisitionRequest]
    ) -> list[_PreparedRequest]:
        first = requests[0]
        if any(
            request.workspace_id != first.workspace_id or request.run_id != first.run_id
            for request in requests
        ):
            raise AcquisitionPreflightError(
                "BATCH_SCOPE_MISMATCH",
                "One acquisition batch must use one workspace and run.",
            )
        parent_binding = (
            first.protocol_fingerprint,
            first.corpus_fingerprint,
            first.inputs.corpus_snapshot.artifact_id,
            first.inputs.corpus_snapshot.sha256,
            first.inputs.screening_decisions.artifact_id,
            first.inputs.screening_decisions.sha256,
        )
        if any(
            (
                request.protocol_fingerprint,
                request.corpus_fingerprint,
                request.inputs.corpus_snapshot.artifact_id,
                request.inputs.corpus_snapshot.sha256,
                request.inputs.screening_decisions.artifact_id,
                request.inputs.screening_decisions.sha256,
            )
            != parent_binding
            for request in requests
        ):
            raise AcquisitionPreflightError(
                "BATCH_SCOPE_MISMATCH",
                "Every item in one acquisition batch must bind the same accepted parents.",
            )
        # A batch may contain duplicate copies of one logical request (for
        # example, a duplicate DOI/roster row).  Coalesce identical immutable
        # requests before any staging; conflicting source bindings for the
        # same accepted study remain an explicit preflight conflict.
        unique_requests: list[AcquisitionRequest] = []
        by_study: dict[str, AcquisitionRequest] = {}
        for request in requests:
            prior = by_study.get(request.study_id)
            if prior is None:
                by_study[request.study_id] = request
                unique_requests.append(request)
                continue
            if prior.model_dump(mode="json") != request.model_dump(mode="json"):
                raise AcquisitionPreflightError(
                    "DUPLICATE_STUDY_CONFLICT",
                    "Duplicate requests for one study carry different source bindings.",
                    study_id=request.study_id,
                )
        requests = unique_requests

        prepared: list[_PreparedRequest] = []
        for request in requests:
            binding = self.workspace_bindings.get(request.workspace_id)
            if binding is None or binding.workspace_id != request.workspace_id:
                raise AcquisitionPreflightError(
                    "WORKSPACE_BINDING_MISSING",
                    "The request workspace has no accepted canonical root binding.",
                )
            if (
                not binding.canonical_root.is_absolute()
                or not binding.canonical_root.is_dir()
            ):
                raise AcquisitionPreflightError(
                    "WORKSPACE_ROOT_INVALID",
                    "The accepted workspace root must be an existing absolute directory.",
                )
            workspace_root = binding.canonical_root.resolve(strict=True)
            request_root = request.workspace_root.resolve(strict=False)
            if request_root != workspace_root:
                raise AcquisitionPreflightError(
                    "WORKSPACE_BINDING_MISMATCH",
                    "The request root does not match the accepted workspace binding.",
                )
            expected_binding = canonical_fingerprint(
                {
                    "algorithm_version": binding.binding_algorithm_version,
                    "canonical_root": str(workspace_root),
                    "workspace_id": binding.workspace_id,
                }
            )
            if expected_binding != binding.binding_fingerprint:
                raise AcquisitionPreflightError(
                    "WORKSPACE_BINDING_MISMATCH",
                    "The canonical workspace root binding fingerprint is stale.",
                )

            corpus = self._verify_parent(
                request.inputs.corpus_snapshot, request, "corpus_snapshot"
            )
            screening = self._verify_parent(
                request.inputs.screening_decisions, request, "screening_decisions"
            )
            self._verify_study_lineage(request, corpus, screening)
            self._validate_transport_configuration(request)
            selected_source = self._selected_source(request, workspace_root)
            self._verify_destination_prefix(request.storage_prefix, workspace_root)
            if (
                request.source_mode.value == "USER_PATH"
                and request.source_path is not None
            ):
                source = request.source_path.resolve(strict=False)
                try:
                    source.relative_to(workspace_root)
                    internal_source = True
                except ValueError:
                    internal_source = False
                if not internal_source and not request.allow_external_source:
                    raise AcquisitionPreflightError(
                        "EXTERNAL_SOURCE_NOT_PERMITTED",
                        "An external USER_PATH requires explicit read-only permission.",
                    )
                if internal_source:
                    source_relative = source.relative_to(workspace_root)
                    if (
                        source_relative.parent == Path(request.storage_prefix)
                        and source.suffix.lower() == ".pdf"
                        and source.name.startswith("DOC-")
                    ):
                        try:
                            source_digest = self._hash_file(source)
                        except OSError:
                            source_digest = None
                        if source_digest is not None:
                            expected_destination = deterministic_document_id(
                                study_id=request.study_id,
                                source_hash=source_digest,
                                workspace_id=request.workspace_id,
                            )
                            if source.name == f"{expected_destination}.pdf":
                                raise AcquisitionPreflightError(
                                    "SOURCE_EQUALS_DESTINATION",
                                    "A USER_PATH source cannot also be its acquisition destination.",
                                )
                    selected_source = source_relative.as_posix()
                else:
                    label = request.external_source_label
                    digest = hashlib.sha256(
                        str(source).encode("utf-8", errors="surrogatepass")
                    ).hexdigest()[:12]
                    selected_source = (
                        f"external:{label}-{digest}"
                        if label is not None
                        else f"external:{digest}"
                    )
            idempotency_key = self._idempotency_key(request, selected_source)
            prepared.append(
                _PreparedRequest(
                    request=request,
                    corpus=corpus,
                    screening=screening,
                    workspace_root=workspace_root,
                    idempotency_key=idempotency_key,
                    selected_source=selected_source,
                )
            )

        key_payload = canonical_fingerprint(
            {
                "request_keys": sorted(item.idempotency_key for item in prepared),
                "run_id": first.run_id,
                "schema_version": first.schema_version,
                "workspace_id": first.workspace_id,
            }
        )
        if key_payload == "sha256:" + "0" * 64:
            raise AcquisitionPreflightError(
                "IDEMPOTENCY_KEY_INVALID", "Invalid batch key."
            )
        return prepared

    def _verify_parent(
        self,
        parent_input: Any,
        request: AcquisitionRequest,
        expected_type: str,
    ) -> AcceptedParentBinding:
        parent = self._parent_by_id.get(parent_input.artifact_id)
        if parent is None:
            raise AcquisitionPreflightError(
                "PARENT_ARTIFACT_MISSING",
                "A required accepted parent is not registered.",
                artifact_id=parent_input.artifact_id,
            )
        expected = {
            "artifact_type": expected_type,
            "sha256": parent_input.sha256,
            "workspace_relative_path": parent_input.workspace_relative_path,
            "workspace_id": request.workspace_id,
            "protocol_fingerprint": request.protocol_fingerprint,
            "corpus_fingerprint": request.corpus_fingerprint,
        }
        actual = {
            "artifact_type": parent.artifact_type,
            "sha256": parent.sha256,
            "workspace_relative_path": parent.workspace_relative_path,
            "workspace_id": parent.workspace_id,
            "protocol_fingerprint": parent.protocol_fingerprint,
            "corpus_fingerprint": parent.corpus_fingerprint,
        }
        if actual != expected:
            raise AcquisitionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The request parent binding is stale or cross-workspace.",
                artifact_id=parent.artifact_id,
            )
        if canonical_fingerprint(parent.payload) != parent.sha256:
            raise AcquisitionPreflightError(
                "PARENT_HASH_MISMATCH",
                "The accepted parent payload hash does not match its registry entry.",
                artifact_id=parent.artifact_id,
            )
        if parent.payload.get("schema_version") != "1.0.0":
            raise AcquisitionPreflightError(
                "PARENT_SCHEMA_UNSUPPORTED",
                "The accepted parent is not Contract v1.",
                artifact_id=parent.artifact_id,
            )
        # Structural conformance to the frozen Contract v1 parent shape is
        # checked before the parent file is read and long before any transport
        # or output I/O, so a duck-typed registry entry cannot reach download.
        self._validate_parent_structure(parent)
        self._verify_parent_file(parent, request.workspace_root.resolve(strict=True))
        return parent

    @staticmethod
    def _validate_parent_structure(parent: AcceptedParentBinding) -> None:
        """Fail closed unless the accepted parent is a real Contract v1 artifact."""

        try:
            validate_parent_structure(parent.artifact_type, parent.payload)
        except ParentStructureError as error:
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID",
                "The accepted parent payload is not a Contract v1 parent shape.",
                artifact_id=parent.artifact_id,
                pointer=error.pointer,
                reason=error.reason,
            ) from error

    def _verify_parent_file(
        self, parent: AcceptedParentBinding, workspace_root: Path
    ) -> None:
        """Read and verify the exact accepted parent JSON named by the binding."""

        try:
            path = self._safe_workspace_path(
                workspace_root,
                parent.workspace_relative_path,
                create=False,
            )
            if not path.is_file():
                raise AcquisitionPreflightError(
                    "PARENT_ARTIFACT_MISSING",
                    "The accepted parent artifact file is missing.",
                    artifact_id=parent.artifact_id,
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
        except AcquisitionPreflightError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise AcquisitionPreflightError(
                "PARENT_ARTIFACT_INVALID",
                "The accepted parent artifact file could not be read as JSON.",
                artifact_id=parent.artifact_id,
            ) from error
        if canonical_fingerprint(payload) != parent.sha256:
            raise AcquisitionPreflightError(
                "PARENT_HASH_MISMATCH",
                "The accepted parent file does not match its registry hash.",
                artifact_id=parent.artifact_id,
            )
        if canonical_fingerprint(payload) != canonical_fingerprint(parent.payload):
            raise AcquisitionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The accepted parent file does not match the supplied binding payload.",
                artifact_id=parent.artifact_id,
            )

    def _verify_study_lineage(
        self,
        request: AcquisitionRequest,
        corpus: AcceptedParentBinding,
        screening: AcceptedParentBinding,
    ) -> None:
        corpus_data = corpus.payload.get("data")
        screening_data = screening.payload.get("data")
        if not isinstance(corpus_data, Mapping) or not isinstance(
            screening_data, Mapping
        ):
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID", "Accepted parent data payloads are invalid."
            )
        studies = corpus_data.get("studies")
        if not isinstance(studies, list):
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID", "The corpus snapshot has no studies list."
            )
        matching = [
            study
            for study in studies
            if isinstance(study, Mapping) and study.get("study_id") == request.study_id
        ]
        if not matching:
            raise AcquisitionPreflightError(
                "STUDY_NOT_IN_CORPUS",
                "The requested study is absent from the accepted corpus snapshot.",
                study_id=request.study_id,
            )
        if len(matching) != 1:
            raise AcquisitionPreflightError(
                "STUDY_IDENTITY_AMBIGUOUS",
                "The accepted corpus contains duplicate requested study identities.",
                study_id=request.study_id,
            )
        if corpus.payload.get("corpus_fingerprint") != request.corpus_fingerprint:
            raise AcquisitionPreflightError(
                "CORPUS_FINGERPRINT_MISMATCH",
                "The accepted corpus fingerprint does not match the request.",
            )
        try:
            recomputed_corpus = corpus_snapshot_fingerprint(corpus_data)
        except (TypeError, ValueError) as error:
            raise AcquisitionPreflightError(
                "CORPUS_FINGERPRINT_MISMATCH",
                "The accepted corpus identity graph is invalid.",
            ) from error
        if recomputed_corpus != request.corpus_fingerprint:
            raise AcquisitionPreflightError(
                "CORPUS_FINGERPRINT_MISMATCH",
                "The accepted corpus identity graph fingerprint is stale.",
            )

        binding = screening_data.get("binding")
        if not isinstance(binding, Mapping):
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID", "Screening decisions have no lineage binding."
            )
        expected_binding = {
            "protocol_fingerprint": request.protocol_fingerprint,
            "corpus_fingerprint": request.corpus_fingerprint,
        }
        if any(binding.get(key) != value for key, value in expected_binding.items()):
            raise AcquisitionPreflightError(
                "SCREENING_BINDING_MISMATCH",
                "Screening decisions do not bind the requested protocol and corpus.",
            )
        if binding.get("screening_run_id") != screening.run_id:
            raise AcquisitionPreflightError(
                "SCREENING_BINDING_MISMATCH",
                "Screening decisions have a stale run binding.",
            )
        inputs = screening.payload.get("inputs")
        if not isinstance(inputs, list):
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID",
                "Screening decisions have no parent-input list.",
            )
        corpus_inputs = [
            item
            for item in inputs
            if isinstance(item, Mapping)
            and item.get("artifact_type", "corpus_snapshot") == "corpus_snapshot"
        ]
        if len(corpus_inputs) != 1 or any(
            item.get("artifact_id") != corpus.artifact_id
            or item.get("sha256") != corpus.sha256
            for item in corpus_inputs
        ):
            raise AcquisitionPreflightError(
                "PARENT_LINEAGE_MISMATCH",
                "Screening decisions do not reference the exact accepted corpus snapshot.",
            )
        decisions = screening_data.get("decisions")
        if not isinstance(decisions, list):
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID", "Screening decisions have no decisions list."
            )
        values = {
            decision.get("decision")
            for decision in decisions
            if isinstance(decision, Mapping)
            and decision.get("study_id") == request.study_id
        }
        if "INCLUDE" not in values:
            raise AcquisitionPreflightError(
                "STUDY_NOT_INCLUDED",
                "The requested study is not included by accepted screening decisions.",
                study_id=request.study_id,
            )
        if "EXCLUDE" in values or "CONFLICT" in values:
            raise AcquisitionPreflightError(
                "SCREENING_DECISION_CONFLICT",
                "The accepted screening lineage contains an unresolved study decision.",
                study_id=request.study_id,
            )
        external_ids = matching[0].get("external_ids", {})
        raw_corpus_dois = (
            external_ids.get("doi", []) if isinstance(external_ids, Mapping) else []
        )
        if isinstance(raw_corpus_dois, str):
            raw_corpus_dois = [raw_corpus_dois]
        if not isinstance(raw_corpus_dois, list):
            raise AcquisitionPreflightError(
                "PARENT_PAYLOAD_INVALID",
                "Corpus DOI identifiers must be a list or string.",
                study_id=request.study_id,
            )
        corpus_dois = {
            self._safe_doi(value)
            for value in raw_corpus_dois
            if self._safe_doi(value) is not None
        }
        for supplied_doi in (request.doi, request.source_doi):
            if supplied_doi is not None and supplied_doi not in corpus_dois:
                raise AcquisitionPreflightError(
                    "STUDY_DOI_MISMATCH",
                    "A supplied DOI is not present in the accepted corpus metadata.",
                    study_id=request.study_id,
                )

    @staticmethod
    def _safe_doi(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            from .canonical import normalize_doi

            return normalize_doi(value)
        except (TypeError, ValueError):
            return None

    def _validate_transport_configuration(self, request: AcquisitionRequest) -> None:
        values = [
            value
            for value in (
                request.requested_source,
                request.selected_source_url,
                request.institutional_gateway_url,
                request.forward_proxy_url,
            )
            if value is not None
        ]
        for value in values:
            try:
                parsed = urlsplit(value)
                parsed_port = parsed.port
            except ValueError as error:
                raise AcquisitionPreflightError(
                    "SOURCE_URL_INVALID",
                    "Acquisition source and transport URLs must be valid HTTP(S) URLs.",
                ) from error
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise AcquisitionPreflightError(
                    "SOURCE_URL_INVALID",
                    "Acquisition source and transport URLs must be HTTP(S) URLs.",
                )
            if parsed.username is not None or parsed.password is not None:
                raise AcquisitionPreflightError(
                    "SOURCE_CREDENTIALS_FORBIDDEN",
                    "URL credentials must never be supplied to acquisition.",
                )
            if parsed_port is not None and not (1 <= parsed_port <= 65535):
                raise AcquisitionPreflightError(
                    "SOURCE_URL_INVALID",
                    "Transport URL port is outside the valid range.",
                )
        if (
            request.institutional_gateway_url is not None
            and request.forward_proxy_url is not None
            and request.institutional_gateway_url == request.forward_proxy_url
        ):
            raise AcquisitionPreflightError(
                "GATEWAY_PROXY_CONFLATION",
                "An institutional gateway and transport forward proxy must be distinct.",
            )
        if request.source_mode.value == "USER_PATH" and (
            request.institutional_gateway_url is not None
            or request.forward_proxy_url is not None
        ):
            raise AcquisitionPreflightError(
                "TRANSPORT_CONFIG_NOT_APPLICABLE",
                "Gateway and forward-proxy settings are only valid for discovery.",
            )
        if (
            request.source_mode.value != "USER_PATH"
            and request.selected_source_url is not None
            and urlsplit(request.selected_source_url).scheme not in {"http", "https"}
        ):
            raise AcquisitionPreflightError(
                "SOURCE_URL_INVALID", "A selected discovery source must be HTTP(S)."
            )

    def _selected_source(
        self, request: AcquisitionRequest, workspace_root: Path
    ) -> str | None:
        if request.source_mode.value == "USER_PATH":
            return None
        source = request.selected_source_url or request.requested_source
        if source is None:
            return None
        return self._redact_source(source)

    @staticmethod
    def _requested_source(prepared: _PreparedRequest) -> str | None:
        request = prepared.request
        if request.source_path is not None:
            return prepared.selected_source
        if request.requested_source is not None:
            return PDFAcquisitionService._redact_source(request.requested_source)
        return prepared.selected_source

    @classmethod
    def _request_source_label(cls, request: AcquisitionRequest) -> str | None:
        if request.source_path is not None:
            try:
                relative = request.source_path.resolve(strict=False).relative_to(
                    request.workspace_root.resolve(strict=False)
                )
            except ValueError:
                return cls._redact_external_path(request.source_path)
            return relative.as_posix()
        if request.requested_source is not None:
            return cls._redact_source(request.requested_source)
        if request.selected_source_url is not None:
            return cls._redact_source(request.selected_source_url)
        return None

    def _verify_destination_prefix(self, prefix: str, workspace_root: Path) -> None:
        self._safe_workspace_path(workspace_root, prefix, create=False)
        prefix_path = PurePosixPath(prefix)
        for parent in self.accepted_parents:
            parent_path = PurePosixPath(parent.workspace_relative_path)
            if parent_path == prefix_path or parent_path.is_relative_to(prefix_path):
                raise AcquisitionPreflightError(
                    "PATH_COLLISION",
                    "The acquisition storage prefix overlaps a frozen parent artifact.",
                    path=prefix,
                )

    def _idempotency_key(
        self, request: AcquisitionRequest, selected_source: str | None
    ) -> str:
        proposed_path = f"{request.storage_prefix}/pending-{request.study_id}.pdf"
        source_payload = {
            "expected_media_type": "application/pdf",
            "normalized_doi": request.doi,
            "requested_source": (
                self._redact_source(request.requested_source)
                if request.requested_source is not None
                else None
            ),
            "selected_source": selected_source,
            "source_kind": request.source_mode.value,
            "validation_profile": request.validation_profile,
            "validation_profile_version": request.validation_profile_version,
            "workspace_relative_final_path": proposed_path,
        }
        return canonical_fingerprint(
            {
                "schema_version": request.schema_version,
                "source_payload_hash": canonical_fingerprint(source_payload),
                "run_id": request.run_id,
                "study_id": request.study_id,
                "workspace_id": request.workspace_id,
            }
        )

    def _safe_workspace_path(
        self, workspace_root: Path, relative: str, *, create: bool
    ) -> Path:
        validate_portable_relative_path(relative)
        canonical_root = workspace_root.resolve(strict=True)
        parts = PurePosixPath(relative).parts

        def reject_symlink_components() -> None:
            cursor = canonical_root
            for part in parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise AcquisitionPreflightError(
                        "PATH_OUTSIDE_WORKSPACE",
                        "A workspace path component cannot be a symlink.",
                        path=relative,
                    )

        # Resolve the lexical path as well as its target.  Returning the
        # resolved target is useful for containment checks, but callers must
        # never mistake a symlink for an ordinary final path.
        reject_symlink_components()
        candidate = canonical_root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(canonical_root)
        except (OSError, ValueError) as error:
            raise AcquisitionPreflightError(
                "PATH_OUTSIDE_WORKSPACE",
                "A destination resolves outside the canonical workspace root.",
                path=relative,
            ) from error
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
            reject_symlink_components()
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(canonical_root)
            except ValueError as error:
                raise AcquisitionPreflightError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "A destination resolves outside the canonical workspace root.",
                    path=relative,
                ) from error
        return resolved

    def _verify_manifest_file_path(
        self, manifest: AcquiredDocumentManifest, path: Path, root: Path
    ) -> None:
        expected_name = f"{manifest.manifest_id}.json"
        expected_relative = f"{manifest.run_id}/{expected_name}"
        try:
            actual_relative = path.relative_to(root).as_posix()
        except ValueError as error:
            raise AcquisitionPreflightError(
                "MANIFEST_CORRUPT",
                "A manifest candidate is outside the acquisition run directory.",
            ) from error
        if path.name != expected_name or actual_relative != expected_relative:
            raise AcquisitionPreflightError(
                "MANIFEST_CORRUPT",
                "A manifest candidate does not match its embedded identity and run.",
            )

    def _find_run_replay(
        self, manifest_root: Path, run_id: str, expected_key: str
    ) -> tuple[AcquiredDocumentManifest, Path] | None:
        if not manifest_root.exists():
            return None
        replay: tuple[AcquiredDocumentManifest, Path] | None = None
        try:
            canonical_root = manifest_root.resolve(strict=True)
        except OSError as error:
            raise AcquisitionPreflightError(
                "MANIFEST_DIRECTORY_INVALID",
                "The acquisition manifest directory could not be resolved.",
            ) from error
        for path in sorted(manifest_root.glob("ACQ-*.json")):
            if path.is_symlink():
                raise AcquisitionPreflightError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "A manifest candidate cannot be a symlink.",
                )
            try:
                path.resolve(strict=True).relative_to(canonical_root)
                manifest = AcquiredDocumentManifest.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                self._verify_manifest_file_path(manifest, path, canonical_root.parent)
                self.verify_manifest(manifest, verify_bytes=False)
            except (OSError, ValueError, AcquisitionCommitError) as error:
                raise AcquisitionPreflightError(
                    "MANIFEST_CORRUPT",
                    "A manifest for this run failed canonical verification.",
                    path=path.name,
                ) from error
            if manifest.run_id == run_id:
                if manifest.idempotency_key != expected_key:
                    raise AcquisitionPreflightError(
                        "IDEMPOTENCY_CONFLICT",
                        "The run already contains a manifest for a different semantic request.",
                        manifest_id=manifest.manifest_id,
                    )
                if replay is not None:
                    raise AcquisitionPreflightError(
                        "IDEMPOTENCY_CONFLICT",
                        "The run contains more than one matching acquisition manifest.",
                        manifest_id=manifest.manifest_id,
                    )
                replay = (manifest, path)
        return replay

    def _verify_replay_inputs(
        self,
        manifest: AcquiredDocumentManifest,
        prepared: Sequence[_PreparedRequest],
    ) -> None:
        """Reject a persisted key that no longer names the same immutable inputs."""

        outcomes = {outcome.study_id: outcome for outcome in manifest.item_outcomes}
        records = {record.study_id: record for record in manifest.records}
        for item in prepared:
            request = item.request
            outcome = outcomes.get(request.study_id)
            if outcome is None or any(
                (
                    outcome.source_kind is not request.source_kind,
                    outcome.requested_source != self._requested_source(item),
                    outcome.normalized_doi != request.doi,
                    outcome.validation_profile != request.validation_profile,
                    outcome.validation_profile_version
                    != request.validation_profile_version,
                )
            ):
                raise AcquisitionPreflightError(
                    "IDEMPOTENCY_CONFLICT",
                    "A persisted acquisition key no longer names the same source payload.",
                    study_id=request.study_id,
                )
            if request.source_path is None or outcome.document_id is None:
                continue
            record = records.get(request.study_id)
            if record is None or record.document_id != outcome.document_id:
                raise AcquisitionCommitError(
                    "REPLAY_SOURCE_MISMATCH",
                    "A replayed USER_PATH has no matching committed source record.",
                )
            try:
                source_hash = self._hash_file(request.source_path)
                source_length = request.source_path.stat().st_size
            except OSError as error:
                raise AcquisitionCommitError(
                    "REPLAY_SOURCE_MISMATCH",
                    "A replayed USER_PATH is no longer readable.",
                ) from error
            if (
                source_hash != record.source_sha256
                or source_length != record.byte_length
            ):
                raise AcquisitionCommitError(
                    "REPLAY_SOURCE_MISMATCH",
                    "A replayed USER_PATH no longer has the bytes bound by the manifest.",
                )

    async def _replay_outcome(
        self,
        replay: tuple[AcquiredDocumentManifest, Path],
        prepared: Sequence[_PreparedRequest],
    ) -> AcquisitionBatchOutcome:
        manifest, path = replay
        expected_key = canonical_fingerprint(
            {
                "request_keys": sorted(item.idempotency_key for item in prepared),
                "run_id": prepared[0].request.run_id,
                "schema_version": prepared[0].request.schema_version,
                "workspace_id": prepared[0].request.workspace_id,
            }
        )
        if manifest.idempotency_key != expected_key:
            raise AcquisitionPreflightError(
                "IDEMPOTENCY_CONFLICT",
                "The run already contains a manifest for a different semantic request.",
                manifest_id=manifest.manifest_id,
            )
        if (
            manifest.workspace_id != prepared[0].request.workspace_id
            or manifest.run_id != prepared[0].request.run_id
        ):
            raise AcquisitionPreflightError(
                "WORKSPACE_BINDING_MISMATCH",
                "The replay manifest belongs to a different workspace or run.",
            )
        self._verify_manifest_parents(manifest, prepared)
        self._verify_replay_inputs(manifest, prepared)
        self.verify_manifest(manifest, verify_bytes=True)
        outcomes = [
            item.model_copy(update={"acquisition_status": AcquisitionStatus.REUSED})
            if item.document_id is not None
            else item
            for item in manifest.item_outcomes
        ]
        audit_error = await self._append_audit(manifest, path)
        status = (
            OperationStatus.PARTIAL
            if audit_error is not None and manifest.records
            else manifest.operation.status
        )
        errors = list(manifest.operation.errors)
        if audit_error is not None:
            errors.append(audit_error)
        return self._batch_outcome(
            manifest,
            path,
            outcomes,
            status=status,
            extra_errors=[audit_error] if audit_error is not None else [],
            operation_errors=errors,
        )

    async def _acquire_one(
        self, prepared: _PreparedRequest, transport: AcquisitionTransport
    ) -> _StagedDocument:
        request = prepared.request
        if prepared.selected_source is None:
            return self._unresolved(prepared)

        content_dir = self._safe_workspace_path(
            prepared.workspace_root, request.storage_prefix, create=True
        )
        temporary_path: Path | None = None
        final_path: Path | None = None
        final_relative: str | None = None
        promoted_new = False
        attempts: list[AcquisitionAttempt] = []
        try:
            for ordinal in range(1, self.max_attempts + 1):
                temporary_path = self._new_temporary(content_dir)
                try:
                    self._inject(AcquisitionFault.DOWNLOAD, request.study_id)
                    transport_result = await self._stage_source(
                        prepared, transport, temporary_path
                    )
                    attempts.append(
                        self._attempt(
                            prepared,
                            ordinal=ordinal,
                            result=AcquisitionStatus.ACQUIRED,
                            transport=transport_result,
                        )
                    )
                    break
                except asyncio.CancelledError:
                    return self._failed_item(
                        prepared,
                        AcquisitionStatus.CANCELLED,
                        "ACQUISITION_CANCELLED",
                        "Acquisition was cancelled before content commit.",
                        attempts,
                        temporary=self._release(temporary_path),
                    )
                except _NotFound as error:
                    attempts.append(
                        self._attempt(
                            prepared,
                            ordinal=ordinal,
                            result=AcquisitionStatus.NOT_FOUND,
                            transport=error.result,
                        )
                    )
                    return self._failed_item(
                        prepared,
                        AcquisitionStatus.NOT_FOUND,
                        "SOURCE_NOT_FOUND",
                        "The selected source was authoritatively absent.",
                        attempts,
                        temporary=self._release(temporary_path),
                    )
                except AcquisitionCommitError as error:
                    error_status = self._commit_error_status(error.code)
                    attempts.append(
                        self._attempt(
                            prepared,
                            ordinal=ordinal,
                            result=error_status,
                            transport=getattr(error, "transport_result", None),
                            diagnostic_code=error.code,
                            diagnostic_message=error.message,
                        )
                    )
                    if error.code != "NETWORK_ERROR":
                        return self._failed_item(
                            prepared,
                            error_status,
                            error.code,
                            error.message,
                            attempts,
                            temporary=self._release(temporary_path),
                        )
                    if ordinal == self.max_attempts:
                        return self._failed_item(
                            prepared,
                            AcquisitionStatus.NETWORK_FAILED,
                            error.code,
                            error.message,
                            attempts,
                            temporary=self._release(temporary_path),
                        )
                except RuntimeError as error:
                    attempts.append(
                        self._attempt(
                            prepared,
                            ordinal=ordinal,
                            result=AcquisitionStatus.FAILED,
                            diagnostic_code="INTERNAL_ERROR",
                            diagnostic_message=type(error).__name__,
                        )
                    )
                    return self._failed_item(
                        prepared,
                        AcquisitionStatus.FAILED,
                        "INTERNAL_ERROR",
                        "The transport or injected operation failed internally.",
                        attempts,
                        temporary=self._release(temporary_path),
                    )
                except (
                    ConnectionError,
                    aiohttp.ClientError,
                    TimeoutError,
                ) as error:
                    attempts.append(
                        self._attempt(
                            prepared,
                            ordinal=ordinal,
                            result=AcquisitionStatus.NETWORK_FAILED,
                            diagnostic_code="NETWORK_ERROR",
                            diagnostic_message=type(error).__name__,
                        )
                    )
                    if ordinal == self.max_attempts:
                        return self._failed_item(
                            prepared,
                            AcquisitionStatus.NETWORK_FAILED,
                            "NETWORK_ERROR",
                            "Transport failed before a response sufficient to validate content.",
                            attempts,
                            temporary=self._release(temporary_path),
                        )
            else:
                return self._failed_item(
                    prepared,
                    AcquisitionStatus.NETWORK_FAILED,
                    "NETWORK_ERROR",
                    "Transport retries were exhausted.",
                    attempts,
                    temporary=self._release(temporary_path),
                )

            if temporary_path is None:
                raise AcquisitionCommitError(
                    "INTERNAL_ERROR", "No staged content was produced."
                )
            try:
                self._inject(AcquisitionFault.VALIDATION, request.study_id)
                validation = self._validate_staged(temporary_path, request)
            except (
                AcquisitionPreflightError,
                OSError,
                RuntimeError,
                ValueError,
                PdfReadError,
            ) as error:
                if isinstance(error, AcquisitionPreflightError):
                    return self._failed_item(
                        prepared,
                        AcquisitionStatus.IDENTITY_MISMATCH
                        if error.code == "IDENTITY_MISMATCH"
                        else AcquisitionStatus.INVALID_CONTENT,
                        error.code,
                        error.message,
                        attempts,
                        temporary=self._release(temporary_path),
                    )
                return self._failed_item(
                    prepared,
                    AcquisitionStatus.INVALID_CONTENT,
                    "INVALID_PDF",
                    "Staged content failed the named PDF validation profile.",
                    attempts,
                    temporary=self._release(temporary_path),
                )

            if not validation.valid or validation.source_sha256 is None:
                return self._failed_item(
                    prepared,
                    AcquisitionStatus.INVALID_CONTENT,
                    "INVALID_PDF",
                    "Staged content failed the named PDF validation profile.",
                    attempts,
                    temporary=self._release(temporary_path),
                )
            if (
                request.doi is not None
                and request.source_doi is not None
                and request.doi != request.source_doi
            ):
                return self._failed_item(
                    prepared,
                    AcquisitionStatus.IDENTITY_MISMATCH,
                    "IDENTITY_MISMATCH",
                    "Requested and source DOI identities conflict.",
                    attempts,
                    temporary=self._release(temporary_path),
                )

            document_id = deterministic_document_id(
                study_id=request.study_id,
                source_hash=validation.source_sha256,
                workspace_id=request.workspace_id,
            )
            final_relative = f"{request.storage_prefix}/{document_id}.pdf"
            final_path = self._safe_workspace_path(
                prepared.workspace_root, final_relative, create=False
            )
            if request.source_path is not None:
                source_resolved = request.source_path.resolve(strict=False)
                if source_resolved == final_path.resolve(strict=False):
                    return self._failed_item(
                        prepared,
                        AcquisitionStatus.FAILED,
                        "SOURCE_EQUALS_DESTINATION",
                        "A USER_PATH source cannot also be its acquisition destination.",
                        attempts,
                        temporary=self._release(temporary_path),
                    )
            reuse_outcome = self._try_exact_reuse(
                prepared, document_id, validation, final_relative, final_path
            )
            if reuse_outcome is not None:
                return _StagedDocument(
                    reuse_outcome.record,
                    reuse_outcome.outcome,
                    self._release(temporary_path),
                )

            # Durable ownership evidence must exist before the inode is
            # published: a process killed between promotion and manifest
            # replacement leaves an orphan that only this idempotency key may
            # recover.
            self._write_commit_intent(
                final_path,
                {
                    "schema_version": COMMIT_INTENT_SCHEMA_VERSION,
                    "byte_length": validation.byte_length,
                    "document_id": document_id,
                    "idempotency_key": prepared.idempotency_key,
                    "source_sha256": validation.source_sha256,
                    "study_id": request.study_id,
                    "workspace_relative_path": final_relative,
                },
            )
            self._inject(AcquisitionFault.CONTENT_MOVE, request.study_id)
            promotion = self._promote(
                temporary_path, final_path, validation.source_sha256
            )
            promoted_new = promotion.promoted_new
            temporary_path = promotion.temporary_leftover
            record = self._record(
                prepared,
                document_id=document_id,
                validation=validation,
                final_relative=final_relative,
                attempts=attempts,
                status=AcquisitionStatus.ACQUIRED,
            )
            warning = self._title_warning(request)
            outcome = self._committed_outcome(
                prepared,
                record,
                attempts,
                warning=warning,
            )
            return _StagedDocument(record, outcome, temporary_path, promoted_new)
        except asyncio.CancelledError:
            leftover = self._release(temporary_path)
            if leftover is not None:
                _LOGGER.warning(
                    "PDF acquisition left a temporary file after cancellation: %s",
                    leftover,
                )
            if promoted_new and final_relative is not None:
                self._remove_owned_orphan(
                    prepared.workspace_root,
                    final_relative,
                    document_id=document_id,
                    expected_sha256=validation.source_sha256,
                )
            raise
        except AcquisitionPreflightError as error:
            return self._failed_item(
                prepared,
                AcquisitionStatus.IDENTITY_MISMATCH
                if error.code == "IDENTITY_MISMATCH"
                else AcquisitionStatus.FAILED,
                error.code,
                error.message,
                attempts,
                temporary=self._release(temporary_path),
            )
        except AcquisitionCommitError as error:
            return self._failed_item(
                prepared,
                self._commit_error_status(error.code),
                error.code,
                error.message,
                attempts,
                temporary=self._release(temporary_path),
            )
        except (OSError, RuntimeError, ValueError):
            return self._failed_item(
                prepared,
                AcquisitionStatus.FAILED,
                "ACQUISITION_FAILED",
                "Acquisition failed before commit.",
                attempts,
                temporary=self._release(temporary_path),
            )

    async def _stage_source(
        self,
        prepared: _PreparedRequest,
        transport: AcquisitionTransport,
        temporary_path: Path,
    ) -> TransportResult:
        request = prepared.request
        if request.source_path is not None:
            try:
                with (
                    request.source_path.open("rb") as source,
                    temporary_path.open("wb") as target,
                ):
                    total = 0
                    while chunk := source.read(64 * 1024):
                        total += len(chunk)
                        if total > self.maximum_pdf_bytes:
                            raise AcquisitionCommitError(
                                "CONTENT_SIZE_EXCEEDED",
                                "USER_PATH content exceeded the configured size limit.",
                            )
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            except FileNotFoundError as error:
                raise _NotFound(TransportResult()) from error
            return TransportResult(resolved_url=None)
        source_url = request.selected_source_url or request.requested_source
        if source_url is None:
            raise _NotFound(TransportResult())
        transport_url = source_url
        if request.institutional_gateway_url is not None:
            try:
                transport_url = rewrite_via_proxy(
                    source_url,
                    request.institutional_gateway_url,
                    style="auto",
                )
            except (TypeError, ValueError) as error:
                raise AcquisitionCommitError(
                    "GATEWAY_REWRITE_FAILED",
                    "The institutional gateway could not construct a source URL.",
                ) from error
        result = await transport.download(
            url=transport_url,
            destination=temporary_path,
            forward_proxy_url=request.forward_proxy_url,
            max_bytes=self.maximum_pdf_bytes,
        )
        if result.http_status in {401, 403}:
            raise AcquisitionCommitError(
                "SOURCE_ACCESS_RESTRICTED",
                "The selected source denied access; no legal-copy determination was made.",
                transport_result=result,
            )
        if result.http_status in {404, 410}:
            raise _NotFound(result)
        if (
            result.retryable
            or result.http_status == 429
            or (result.http_status is not None and result.http_status >= 500)
        ):
            raise AcquisitionCommitError(
                "NETWORK_ERROR",
                "The transport reported a retryable provider failure.",
                transport_result=result,
            )
        if result.http_status is not None and 400 <= result.http_status < 500:
            raise AcquisitionCommitError(
                "HTTP_CLIENT_ERROR",
                "The selected source rejected the request without a legal-copy result.",
                transport_result=result,
            )
        return result

    def _validate_staged(
        self, path: Path, request: AcquisitionRequest
    ) -> ValidationReport:
        size = path.stat().st_size
        if size < self.minimum_pdf_bytes or size > self.maximum_pdf_bytes:
            return ValidationReport(
                request.validation_profile,
                request.validation_profile_version,
                False,
                byte_length=size,
            )
        if request.validation_profile != "strict-pdf":
            raise AcquisitionPreflightError(
                "VALIDATION_PROFILE_UNSUPPORTED",
                "The requested validation profile is not supported.",
            )
        if not is_valid_pdf(path):
            return ValidationReport(
                request.validation_profile,
                request.validation_profile_version,
                False,
                byte_length=size,
            )
        structural = self._structural_validation(path)
        if not structural:
            return ValidationReport(
                request.validation_profile,
                request.validation_profile_version,
                False,
                byte_length=size,
            )
        digest = self._hash_file(path)
        return ValidationReport(
            request.validation_profile,
            request.validation_profile_version,
            True,
            byte_length=size,
            source_sha256=digest,
            structural=True,
        )

    @staticmethod
    def _structural_validation(path: Path) -> bool:
        try:
            from pypdf import PdfReader

            with path.open("rb") as stream:
                reader = PdfReader(stream, strict=True)
                if reader.is_encrypted:
                    return False
                return len(reader.pages) > 0
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, PdfReadError):
            return False

    def _try_exact_reuse(
        self,
        prepared: _PreparedRequest,
        document_id: str,
        validation: ValidationReport,
        final_relative: str,
        final_path: Path,
    ) -> _StagedDocument | None:
        request = prepared.request
        candidate = self._record(
            prepared,
            document_id=document_id,
            validation=validation,
            final_relative=final_relative,
            attempts=[
                AcquisitionAttempt(
                    ordinal=1,
                    source_kind=request.source_kind,
                    requested_source=(
                        self._requested_source(prepared) or "workspace:user-path"
                    ),
                    result=AcquisitionStatus.REUSED,
                    resolved_source=(prepared.selected_source or "workspace:user-path"),
                    observed_media_type=PDF_MEDIA_TYPE,
                )
            ],
            status=AcquisitionStatus.REUSED,
        )
        found_identity = False
        manifest_root = self._safe_workspace_path(
            prepared.workspace_root,
            "literature/acquisition",
            create=False,
        )
        for manifest_path in self._manifest_paths(prepared.workspace_root):
            try:
                manifest = AcquiredDocumentManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
                self._verify_manifest_file_path(manifest, manifest_path, manifest_root)
                self.verify_manifest(manifest, verify_bytes=False)
            except (OSError, ValueError, AcquisitionCommitError) as error:
                raise AcquisitionCommitError(
                    "REUSE_VERIFICATION_FAILED",
                    "An existing acquisition manifest failed reuse verification.",
                ) from error

            matching_records = [
                record
                for record in manifest.records
                if record.document_id == candidate.document_id
                or record.workspace_relative_path == candidate.workspace_relative_path
            ]
            if not matching_records:
                continue
            if manifest.workspace_id != request.workspace_id:
                raise AcquisitionCommitError(
                    "REUSE_WORKSPACE_MISMATCH",
                    "Existing content identity is bound to another workspace.",
                )
            try:
                self._verify_manifest_parents(manifest, [prepared])
            except AcquisitionPreflightError as error:
                raise AcquisitionCommitError(
                    "REUSE_BINDING_MISMATCH",
                    "Existing content identity is bound to different accepted parents.",
                ) from error

            for record in matching_records:
                found_identity = True
                if not self._same_reuse_binding(candidate, record):
                    raise AcquisitionCommitError(
                        "REUSE_BINDING_MISMATCH",
                        "Existing content identity has a different complete reuse binding.",
                    )
                self.verify_manifest(manifest, verify_bytes=True)
                persisted_attempts = record.attempts
                persisted = record.model_copy(
                    update={"acquisition_status": AcquisitionStatus.REUSED}
                )
                return _StagedDocument(
                    persisted,
                    self._committed_outcome(
                        prepared,
                        persisted,
                        persisted_attempts,
                        warning=self._title_warning(request),
                    ),
                    None,
                )
        if found_identity:
            raise AcquisitionCommitError(
                "UNMANAGED_CONTENT",
                "Existing content has no complete manifest-bound reuse record.",
            )
        if final_path.exists():
            recovered = self._recover_promoted_orphan(
                prepared, document_id, validation, final_relative, final_path
            )
            if recovered is not None:
                return recovered
            raise AcquisitionCommitError(
                "UNMANAGED_CONTENT",
                "Existing content has no complete manifest-bound reuse record.",
            )
        return None

    def _recover_promoted_orphan(
        self,
        prepared: _PreparedRequest,
        document_id: str,
        validation: ValidationReport,
        final_relative: str,
        final_path: Path,
    ) -> _StagedDocument | None:
        """Adopt an orphan this idempotency key promoted before it died.

        A durable commit-intent marker written before promotion is the only
        evidence that separates a crash orphan from an unmanaged file that merely
        happens to sit at the content-addressed path.  Without that marker the
        path stays unusable, so filename matching alone never becomes a cache hit.

        The recovered bytes were staged and validated by this very attempt, so
        the record is returned as an owned acquisition and becomes authoritative
        only once the manifest is published.
        """

        marker = self._read_commit_intent(
            final_path, prepared, document_id, final_relative
        )
        if marker is None:
            return None
        if (
            marker.get("source_sha256") != validation.source_sha256
            or marker.get("byte_length") != validation.byte_length
        ):
            raise AcquisitionCommitError(
                "REPLAY_VERIFICATION_FAILED",
                "A promoted orphan no longer holds the bytes recorded for it.",
            )
        try:
            if (
                final_path.stat().st_size != validation.byte_length
                or self._hash_file(final_path) != validation.source_sha256
            ):
                raise AcquisitionCommitError(
                    "REPLAY_VERIFICATION_FAILED",
                    "A promoted orphan no longer holds the bytes recorded for it.",
                )
        except OSError as error:
            raise AcquisitionCommitError(
                "REPLAY_VERIFICATION_FAILED",
                "A promoted orphan could not be verified for recovery.",
            ) from error
        record = self._record(
            prepared,
            document_id=document_id,
            validation=validation,
            final_relative=final_relative,
            attempts=[
                AcquisitionAttempt(
                    ordinal=1,
                    source_kind=prepared.request.source_kind,
                    requested_source=(
                        self._requested_source(prepared) or "workspace:user-path"
                    ),
                    result=AcquisitionStatus.ACQUIRED,
                    resolved_source=(prepared.selected_source or "workspace:user-path"),
                    observed_media_type=PDF_MEDIA_TYPE,
                )
            ],
            status=AcquisitionStatus.ACQUIRED,
        )
        return _StagedDocument(
            record,
            self._committed_outcome(
                prepared,
                record,
                record.attempts,
                warning=self._title_warning(prepared.request),
            ),
            None,
        )

    @staticmethod
    def _same_reuse_binding(
        candidate: AcquiredDocumentRecord,
        existing: AcquiredDocumentRecord,
    ) -> bool:
        fields = (
            "document_id",
            "document_identity_algorithm_version",
            "study_id",
            "source_kind",
            "source_sha256",
            "byte_length",
            "media_type",
            "workspace_relative_path",
            "normalized_doi",
            "validation_profile",
            "validation_profile_version",
            "selected_source",
            "selected_source_url",
            "access_status",
            "access_assertion",
            "acquisition_method",
        )
        return (
            all(
                getattr(candidate, field) == getattr(existing, field)
                for field in fields
            )
            and existing.workspace_relative_path == candidate.workspace_relative_path
        )

    def _promote(
        self, temporary_path: Path, final_path: Path, expected_hash: str
    ) -> _Promotion:
        """Publish validated bytes at their content-addressed final path.

        ``temporary_leftover`` is non-``None`` when the temporary could not be
        removed, so the caller keeps the reference and reports the litter
        instead of pretending the staging file is gone.
        """

        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.is_symlink():
            raise AcquisitionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "A content destination cannot be a symlink.",
            )
        if final_path.exists():
            try:
                existing_hash = self._hash_file(final_path)
            except OSError as error:
                raise AcquisitionCommitError(
                    "FILESYSTEM_ERROR",
                    "The existing content path could not be verified.",
                ) from error
            if existing_hash != expected_hash:
                raise AcquisitionCommitError(
                    "CONTENT_COLLISION",
                    "A content-addressed path contains different bytes.",
                )
            return _Promotion(False, self._release(temporary_path))
        linked = False
        try:
            # A hard link publishes a complete, already-validated inode without
            # replacing a concurrent writer's final path.  The manifest remains
            # the authoritative commit marker.
            os.link(temporary_path, final_path)
            linked = True
            # The final link is already valid, so a transient unlink failure
            # cannot damage committed content; the leftover is only litter and
            # is retried and surfaced by the caller.
            return _Promotion(True, self._release(temporary_path))
        except FileExistsError:
            try:
                existing_hash = self._hash_file(final_path)
            except OSError as error:
                raise AcquisitionCommitError(
                    "FILESYSTEM_ERROR",
                    "The concurrent content path could not be verified.",
                ) from error
            if existing_hash != expected_hash:
                raise AcquisitionCommitError(
                    "CONTENT_COLLISION",
                    "A content-addressed path contains different bytes.",
                )
            return _Promotion(False, self._release(temporary_path))
        except OSError as error:
            if linked:
                try:
                    final_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise AcquisitionCommitError(
                "ATOMIC_COMMIT_FAILED",
                "Validated content could not be promoted atomically.",
            ) from error

    def _record(
        self,
        prepared: _PreparedRequest,
        *,
        document_id: str,
        validation: ValidationReport,
        final_relative: str,
        attempts: list[AcquisitionAttempt],
        status: AcquisitionStatus,
    ) -> AcquiredDocumentRecord:
        request = prepared.request
        if request.source_path is None and prepared.selected_source is None:
            raise AcquisitionCommitError(
                "SOURCE_UNRESOLVED",
                "A committed network record requires a selected source.",
            )
        return AcquiredDocumentRecord(
            document_id=document_id,
            document_identity_algorithm_version="v1",
            study_id=request.study_id,
            source_kind=request.source_kind,
            source_sha256=validation.source_sha256,
            byte_length=validation.byte_length,
            media_type="application/pdf",
            workspace_relative_path=final_relative,
            acquisition_status=status,
            access_status=request.access_status,
            access_assertion=request.access_assertion,
            selected_source_url=(
                self._redact_source(request.selected_source_url)
                if request.selected_source_url is not None
                else (
                    self._redact_source(prepared.selected_source)
                    if request.source_path is None
                    and prepared.selected_source is not None
                    else None
                )
            ),
            selected_source=prepared.selected_source or "external:read-only-input",
            normalized_doi=request.doi,
            validation_profile=request.validation_profile,
            validation_profile_version=request.validation_profile_version,
            acquisition_method=(
                MethodProvenance.HUMAN
                if request.source_kind is AcquisitionSourceKind.USER_PATH
                else MethodProvenance.EXTERNAL_PROVIDER
            ),
            attempts=attempts,
        )

    def _attempt(
        self,
        prepared: _PreparedRequest,
        *,
        ordinal: int,
        result: AcquisitionStatus,
        transport: TransportResult | None = None,
        diagnostic_code: str | None = None,
        diagnostic_message: str | None = None,
    ) -> AcquisitionAttempt:
        request = prepared.request
        return AcquisitionAttempt(
            ordinal=ordinal,
            source_kind=request.source_kind,
            requested_source=self._requested_source(prepared) or "workspace:user-path",
            result=result,
            resolved_source=(
                self._redact_source(transport.resolved_url)
                if transport is not None and transport.resolved_url is not None
                else None
            ),
            gateway_used=request.institutional_gateway_url is not None,
            forward_proxy_used=request.forward_proxy_url is not None,
            observed_media_type=(
                transport.observed_media_type if transport is not None else None
            ),
            http_status=transport.http_status if transport is not None else None,
            diagnostic_code=diagnostic_code,
            diagnostic_message=diagnostic_message,
            provider_evidence=self._safe_evidence(request.provider_evidence),
        )

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    @classmethod
    def _safe_evidence(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        def safe_item(key: str, item: Any) -> Any:
            lowered = key.lower()
            if any(
                marker in lowered
                for marker in ("token", "secret", "password", "cookie", "authorization")
            ):
                return "[redacted]"
            if isinstance(item, str):
                return cls._redact_source(item) if "://" in item else item[:1000]
            if isinstance(item, (int, float, bool, type(None))):
                return item
            if isinstance(item, list):
                return [safe_item(key, child) for child in item[:100]]
            if isinstance(item, Mapping):
                return {
                    str(child_key): safe_item(str(child_key), child)
                    for child_key, child_key_value in sorted(item.items())
                    if str(child_key) in _SAFE_PROVIDER_KEYS
                    for child in (child_key_value,)
                }
            return str(item)[:1000]

        return {
            key: safe_item(key, value[key])
            for key in sorted(value)
            if key in _SAFE_PROVIDER_KEYS
        }

    @staticmethod
    def _redact_source(value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return "[redacted-source]"
        if parsed.scheme not in {"http", "https"}:
            return f"{parsed.scheme}:[redacted]"
        hostname = parsed.hostname or "[redacted]"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
        segments = []
        for segment in parsed.path.split("/"):
            lowered = segment.lower()
            if any(
                marker in lowered
                for marker in ("token", "secret", "password", "apikey", "access_key")
            ):
                segments.append("[redacted]")
            else:
                segments.append(segment)
        return urlunsplit(
            SplitResult(parsed.scheme, netloc, "/".join(segments), "", "")
        )

    def _title_warning(self, request: AcquisitionRequest) -> StructuredError | None:
        if request.title_similarity is None or request.title_similarity >= 1.0:
            return None
        return StructuredError(
            code="TITLE_SIMILARITY_REVIEW",
            message="Title similarity is provenance only and does not establish identity.",
            details={"title_similarity": request.title_similarity},
        )

    @staticmethod
    def _validation_result(record: AcquiredDocumentRecord) -> ValidationResult:
        return ValidationResult(
            profile=record.validation_profile,
            profile_version=record.validation_profile_version,
            valid=True,
            media_type=record.media_type,
            byte_length=record.byte_length,
            source_sha256=record.source_sha256,
            structural=True,
        )

    def _committed_outcome(
        self,
        prepared: _PreparedRequest,
        record: AcquiredDocumentRecord,
        attempts: list[AcquisitionAttempt],
        *,
        warning: StructuredError | None = None,
    ) -> AcquisitionItemOutcome:
        request = prepared.request
        return AcquisitionItemOutcome(
            study_id=request.study_id,
            source_kind=request.source_kind,
            acquisition_status=record.acquisition_status,
            requested_source=self._requested_source(prepared),
            normalized_doi=record.normalized_doi,
            validation_profile=record.validation_profile,
            validation_profile_version=record.validation_profile_version,
            document_id=record.document_id,
            selected_source=record.selected_source,
            selected_source_url=record.selected_source_url,
            workspace_relative_path=record.workspace_relative_path,
            access_status=record.access_status,
            attempts=attempts,
            stage=AcquisitionStage.COMPLETE,
            validation_result=self._validation_result(record),
            source_sha256=record.source_sha256,
            warning=warning,
        )

    def _unresolved(self, prepared: _PreparedRequest) -> _StagedDocument:
        request = prepared.request
        error = StructuredError(
            code="UNRESOLVED_NO_LEGAL_OA_COPY_FOUND",
            message="No legal OA source was resolved; this is not a paywall determination.",
        )
        return _StagedDocument(
            None,
            AcquisitionItemOutcome(
                study_id=request.study_id,
                source_kind=request.source_kind,
                acquisition_status=AcquisitionStatus.UNRESOLVED,
                requested_source=self._requested_source(prepared),
                normalized_doi=request.doi,
                validation_profile=request.validation_profile,
                validation_profile_version=request.validation_profile_version,
                selected_source=None,
                selected_source_url=None,
                access_status=request.access_status,
                attempts=[],
                stage=AcquisitionStage.TRANSPORT,
                error=error,
            ),
            None,
        )

    @staticmethod
    def _commit_error_status(code: str) -> AcquisitionStatus:
        if code == "NETWORK_ERROR":
            return AcquisitionStatus.NETWORK_FAILED
        if code == "CONTENT_SIZE_EXCEEDED":
            return AcquisitionStatus.INVALID_CONTENT
        return AcquisitionStatus.FAILED

    @staticmethod
    def _failure_stage(status: AcquisitionStatus, code: str) -> AcquisitionStage:
        if status in {
            AcquisitionStatus.INVALID_CONTENT,
            AcquisitionStatus.IDENTITY_MISMATCH,
        }:
            return AcquisitionStage.VALIDATION
        if code in {"SOURCE_EQUALS_DESTINATION", "UNMANAGED_CONTENT"}:
            return AcquisitionStage.PROMOTION
        if status is AcquisitionStatus.CANCELLED:
            return AcquisitionStage.TRANSPORT
        if status in {
            AcquisitionStatus.NETWORK_FAILED,
            AcquisitionStatus.NOT_FOUND,
            AcquisitionStatus.FAILED,
        }:
            return AcquisitionStage.TRANSPORT
        return AcquisitionStage.COMPLETE

    def _failed_item(
        self,
        prepared: _PreparedRequest,
        status: AcquisitionStatus,
        code: str,
        message: str,
        attempts: list[AcquisitionAttempt],
        *,
        temporary: Path | None = None,
    ) -> _StagedDocument:
        request = prepared.request
        access_status = request.access_status
        if access_status is not AccessStatus.RESTRICTED_CONFIRMED and status in {
            AcquisitionStatus.FAILED,
            AcquisitionStatus.NETWORK_FAILED,
            AcquisitionStatus.NOT_FOUND,
        }:
            access_status = AccessStatus.UNRESOLVED
        return _StagedDocument(
            None,
            AcquisitionItemOutcome(
                study_id=request.study_id,
                source_kind=request.source_kind,
                acquisition_status=status,
                requested_source=self._requested_source(prepared),
                normalized_doi=request.doi,
                validation_profile=request.validation_profile,
                validation_profile_version=request.validation_profile_version,
                selected_source=prepared.selected_source,
                selected_source_url=(
                    self._redact_source(request.selected_source_url)
                    if request.selected_source_url is not None
                    else (
                        self._redact_source(prepared.selected_source)
                        if request.source_path is None
                        and prepared.selected_source is not None
                        else None
                    )
                ),
                access_status=access_status,
                attempts=attempts,
                stage=self._failure_stage(status, code),
                error=StructuredError(
                    code=code,
                    message=message,
                    retryable=code == "NETWORK_ERROR",
                ),
            ),
            temporary,
        )

    @staticmethod
    def _operation_status(
        outcomes: Sequence[AcquisitionItemOutcome],
    ) -> OperationStatus:
        committed = sum(
            outcome.acquisition_status
            in {AcquisitionStatus.ACQUIRED, AcquisitionStatus.REUSED}
            for outcome in outcomes
        )
        if committed == len(outcomes):
            return OperationStatus.SUCCESS
        if committed:
            return OperationStatus.PARTIAL
        if all(
            outcome.acquisition_status is AcquisitionStatus.CANCELLED
            for outcome in outcomes
        ):
            return OperationStatus.CANCELLED
        return OperationStatus.FAILED

    def _parent_refs(self, prepared: Sequence[_PreparedRequest]) -> ParentArtifactRefs:
        first = prepared[0]
        return ParentArtifactRefs(
            corpus_snapshot=self._parent_ref(first.corpus),
            screening_decisions=self._parent_ref(first.screening),
        )

    @staticmethod
    def _parent_ref(parent: AcceptedParentBinding) -> ParentArtifactRef:
        return ParentArtifactRef(
            artifact_id=parent.artifact_id,
            artifact_type=parent.artifact_type,
            sha256=parent.sha256,
            workspace_id=parent.workspace_id,
            protocol_fingerprint=parent.protocol_fingerprint,
            corpus_fingerprint=parent.corpus_fingerprint,
        )

    def _build_manifest(
        self,
        prepared: Sequence[_PreparedRequest],
        records: list[AcquiredDocumentRecord],
        outcomes: list[AcquisitionItemOutcome],
        status: OperationStatus,
    ) -> AcquiredDocumentManifest:
        parent_refs = self._parent_refs(prepared)
        errors = [outcome.error for outcome in outcomes if outcome.error is not None]
        warnings = [
            outcome.warning for outcome in outcomes if outcome.warning is not None
        ]
        if status is OperationStatus.PARTIAL and not errors and not warnings:
            errors = [
                StructuredError(
                    code="BATCH_PARTIAL",
                    message="At least one requested item did not commit.",
                )
            ]
        operation = ManifestOperation(
            status=status,
            errors=errors,
            warnings=warnings,
        )
        stable_records = self._stable_acquisition_records(outcomes, records)
        manifest_id = deterministic_acquisition_manifest_id(
            schema_version=MANIFEST_SCHEMA_VERSION,
            workspace_id=prepared[0].request.workspace_id,
            run_id=prepared[0].request.run_id,
            parent_refs=parent_refs.model_dump(mode="json"),
            acquisition_records=stable_records,
            algorithm_version="v1",
        )
        idempotency_key = canonical_fingerprint(
            {
                "request_keys": sorted(item.idempotency_key for item in prepared),
                "run_id": prepared[0].request.run_id,
                "schema_version": prepared[0].request.schema_version,
                "workspace_id": prepared[0].request.workspace_id,
            }
        )
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "manifest_type": "pdf_acquisition_manifest",
            "manifest_id": manifest_id,
            "manifest_identity_algorithm_version": "v1",
            "workspace_id": prepared[0].request.workspace_id,
            "run_id": prepared[0].request.run_id,
            "protocol_fingerprint": prepared[0].request.protocol_fingerprint,
            "corpus_fingerprint": prepared[0].request.corpus_fingerprint,
            "parent_refs": parent_refs.model_dump(mode="json"),
            "parent_lineage_sha256": canonical_fingerprint(
                parent_refs.model_dump(mode="json")
            ),
            "producer": self.producer.model_dump(mode="json"),
            "records": sorted(
                [record.model_dump(mode="json") for record in records],
                key=lambda record: (record["study_id"], record["document_id"]),
            ),
            "item_outcomes": sorted(
                [outcome.model_dump(mode="json") for outcome in outcomes],
                key=lambda outcome: outcome["study_id"],
            ),
            "idempotency_key": idempotency_key,
            "e2_reference": E2EmbeddedReference(
                acquisition_manifest_id=manifest_id,
                acquisition_manifest_path=(
                    f"literature/acquisition/{prepared[0].request.run_id}/"
                    f"{manifest_id}.json"
                ),
            ).model_dump(mode="json"),
            "operation": operation.model_dump(mode="json"),
        }
        # The stable payload fingerprint covers the complete manifest shape
        # with the self-referential checksum slot explicitly null.  The
        # verifier uses the same construction, while the final artifact
        # checksum below covers this fingerprint as well.
        payload["artifact_checksum"] = None
        payload_fingerprint = canonical_fingerprint(payload)
        payload["manifest_payload_fingerprint"] = payload_fingerprint
        artifact_checksum = canonical_fingerprint(payload)
        payload["artifact_checksum"] = artifact_checksum
        return AcquiredDocumentManifest.model_validate(payload)

    @staticmethod
    def _stable_acquisition_records(
        outcomes: Sequence[AcquisitionItemOutcome],
        records: Sequence[AcquiredDocumentRecord],
    ) -> list[dict[str, Any]]:
        by_document = {record.document_id: record for record in records}
        stable: list[dict[str, Any]] = []
        for outcome in outcomes:
            record = by_document.get(outcome.document_id or "")
            stable.append(
                {
                    "access_status": outcome.access_status.value,
                    "document_identity_algorithm_version": (
                        record.document_identity_algorithm_version
                        if record is not None
                        else "v1"
                    ),
                    "acquisition_status": (
                        "COMMITTED"
                        if outcome.document_id is not None
                        else outcome.acquisition_status.value
                    ),
                    "document_id": outcome.document_id,
                    "media_type": record.media_type if record is not None else None,
                    "normalized_doi": outcome.normalized_doi,
                    "requested_source": outcome.requested_source,
                    "selected_source": outcome.selected_source,
                    "selected_source_url": outcome.selected_source_url,
                    "source_kind": outcome.source_kind.value,
                    "source_sha256": record.source_sha256
                    if record is not None
                    else None,
                    "byte_length": record.byte_length if record is not None else None,
                    "study_id": outcome.study_id,
                    "validation_profile": outcome.validation_profile,
                    "validation_profile_version": outcome.validation_profile_version,
                    "workspace_relative_path": outcome.workspace_relative_path,
                }
            )
        return sorted(
            stable, key=lambda item: (item["study_id"], item["document_id"] or "")
        )

    def _manifest_path(self, manifest_root: Path, manifest_id: str) -> Path:
        return manifest_root / f"{manifest_id}.json"

    def _manifest_paths(self, workspace_root: Path) -> list[Path]:
        try:
            root = self._safe_workspace_path(
                workspace_root,
                "literature/acquisition",
                create=False,
            )
        except AcquisitionPreflightError as error:
            raise AcquisitionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "The acquisition manifest directory is not safely anchored.",
            ) from error
        if not root.exists():
            return []
        try:
            canonical_root = root.resolve(strict=True)
            candidates: list[Path] = []
            for run_path in sorted(root.iterdir()):
                if run_path.is_symlink():
                    raise AcquisitionCommitError(
                        "PATH_OUTSIDE_WORKSPACE",
                        "An acquisition run directory cannot be a symlink.",
                    )
                if not run_path.is_dir():
                    continue
                run_relative = run_path.relative_to(workspace_root).as_posix()
                safe_run_path = self._safe_workspace_path(
                    workspace_root,
                    run_relative,
                    create=False,
                )
                try:
                    safe_run_path.resolve(strict=True).relative_to(canonical_root)
                except (OSError, ValueError) as error:
                    raise AcquisitionCommitError(
                        "PATH_OUTSIDE_WORKSPACE",
                        "An acquisition run directory resolves outside the workspace.",
                    ) from error
                for path in sorted(run_path.glob("ACQ-*.json")):
                    if path.is_symlink():
                        raise AcquisitionCommitError(
                            "PATH_OUTSIDE_WORKSPACE",
                            "A manifest candidate cannot be a symlink.",
                        )
                    candidate_relative = path.relative_to(workspace_root).as_posix()
                    safe_path = self._safe_workspace_path(
                        workspace_root,
                        candidate_relative,
                        create=False,
                    )
                    try:
                        resolved = safe_path.resolve(strict=True)
                        resolved.relative_to(canonical_root)
                    except (OSError, ValueError) as error:
                        raise AcquisitionCommitError(
                            "PATH_OUTSIDE_WORKSPACE",
                            "A manifest candidate resolves outside the workspace.",
                        ) from error
                    if not resolved.is_file():
                        raise AcquisitionCommitError(
                            "MANIFEST_DIRECTORY_INVALID",
                            "A manifest candidate is not a regular file.",
                        )
                    candidates.append(path)
            return candidates
        except AcquisitionCommitError:
            raise
        except (OSError, ValueError) as error:
            raise AcquisitionCommitError(
                "MANIFEST_DIRECTORY_INVALID",
                "The acquisition manifest directory could not be inspected.",
            ) from error

    def _ensure_manifest_parent(
        self, destination: Path, *, manifest: AcquiredDocumentManifest
    ) -> Path:
        binding = self.workspace_bindings.get(manifest.workspace_id)
        if binding is None:
            raise AcquisitionCommitError(
                "WORKSPACE_BINDING_MISSING",
                "The manifest workspace has no accepted canonical root binding.",
            )
        try:
            workspace_root = binding.canonical_root.resolve(strict=True)
            expected_relative = (
                f"literature/acquisition/{manifest.run_id}/{manifest.manifest_id}.json"
            )
            expected_destination = self._safe_workspace_path(
                workspace_root,
                expected_relative,
                create=False,
            )
            if destination.resolve(strict=False) != expected_destination.resolve(
                strict=False
            ):
                raise AcquisitionCommitError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "The manifest destination does not match its embedded identity.",
                )
            parent_relative = PurePosixPath(expected_relative).parent.as_posix()
            expected_parent = self._safe_workspace_path(
                workspace_root,
                parent_relative,
                create=True,
            )
            if destination.parent.resolve(strict=False) != expected_parent.resolve(
                strict=False
            ):
                raise AcquisitionCommitError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "The manifest parent does not match the accepted workspace.",
                )
            return expected_destination
        except AcquisitionCommitError:
            raise
        except AcquisitionPreflightError as error:
            raise AcquisitionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "The manifest parent is not safely anchored to the workspace.",
            ) from error
        except (OSError, ValueError) as error:
            raise AcquisitionCommitError(
                "MANIFEST_DIRECTORY_INVALID",
                "The manifest parent could not be resolved.",
            ) from error

    def _publish_manifest(
        self, manifest: AcquiredDocumentManifest, destination: Path
    ) -> AcquiredDocumentManifest:
        self.verify_manifest(manifest, verify_bytes=False)
        destination = self._ensure_manifest_parent(
            destination,
            manifest=manifest,
        )
        if destination.is_symlink():
            raise AcquisitionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "The manifest destination cannot be a symlink.",
            )
        try:
            workspace_root = self.workspace_bindings[
                manifest.workspace_id
            ].canonical_root.resolve(strict=True)
        except (KeyError, OSError) as error:
            raise AcquisitionCommitError(
                "WORKSPACE_BINDING_MISSING",
                "The manifest workspace has no resolvable canonical root binding.",
            ) from error
        try:
            safe_destination = self._safe_workspace_path(
                workspace_root,
                f"literature/acquisition/{manifest.run_id}/{manifest.manifest_id}.json",
                create=False,
            )
        except AcquisitionPreflightError as error:
            raise AcquisitionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "The manifest destination is not safely anchored to the workspace.",
            ) from error
        if destination != safe_destination:
            raise AcquisitionCommitError(
                "PATH_OUTSIDE_WORKSPACE",
                "The manifest destination is not the canonical workspace path.",
            )
        # Publication and owned-orphan cleanup share one workspace lock so a
        # cleanup can never race a publication that is binding the same bytes.
        with self._publication_lock(workspace_root):
            return self._publish_manifest_locked(manifest, destination)

    def _publish_manifest_locked(
        self, manifest: AcquiredDocumentManifest, destination: Path
    ) -> AcquiredDocumentManifest:
        # A lock file left behind by a killed process is recovered through the
        # OS byte-range lock, so process death cannot stall a later commit.
        lock_path = destination.with_suffix(".json.lock")
        descriptor = self._acquire_lock_file(lock_path, timeout=30.0)

        temporary: Path | None = None
        try:
            if destination.exists():
                try:
                    existing = AcquiredDocumentManifest.model_validate_json(
                        destination.read_text(encoding="utf-8")
                    )
                    self.verify_manifest(existing, verify_bytes=True)
                except (KeyError, OSError, ValueError, AcquisitionCommitError) as error:
                    raise AcquisitionCommitError(
                        "MANIFEST_CONFLICT",
                        "The manifest identity is already bound to invalid bytes.",
                    ) from error
                if existing.model_dump(mode="json") != manifest.model_dump(mode="json"):
                    same_logical_commit = (
                        existing.idempotency_key == manifest.idempotency_key
                        and self._stable_acquisition_records(
                            existing.item_outcomes, existing.records
                        )
                        == self._stable_acquisition_records(
                            manifest.item_outcomes, manifest.records
                        )
                    )
                    if not same_logical_commit:
                        raise AcquisitionCommitError(
                            "MANIFEST_CONFLICT",
                            "The manifest identity is already bound to different content.",
                        )
                return existing

            self._inject(AcquisitionFault.MANIFEST_REPLACE, manifest.manifest_id)
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
                # Publish with an atomic no-replace link.  A concurrent writer
                # can never make this operation overwrite a different valid
                # manifest at the same identity path.
                os.link(temporary, destination)
            except FileExistsError as error:
                if destination.is_symlink():
                    raise AcquisitionCommitError(
                        "PATH_OUTSIDE_WORKSPACE",
                        "The manifest destination cannot be a symlink.",
                    ) from error
                try:
                    existing = AcquiredDocumentManifest.model_validate_json(
                        destination.read_text(encoding="utf-8")
                    )
                    self.verify_manifest(existing, verify_bytes=True)
                except (OSError, ValueError, AcquisitionCommitError) as conflict:
                    raise AcquisitionCommitError(
                        "MANIFEST_CONFLICT",
                        "The manifest identity is already bound to invalid bytes.",
                    ) from conflict
                same_logical_commit = (
                    existing.idempotency_key == manifest.idempotency_key
                    and self._stable_acquisition_records(
                        existing.item_outcomes, existing.records
                    )
                    == self._stable_acquisition_records(
                        manifest.item_outcomes, manifest.records
                    )
                )
                if not same_logical_commit:
                    raise AcquisitionCommitError(
                        "MANIFEST_CONFLICT",
                        "The manifest identity is already bound to different content.",
                    ) from error
                return existing
            return manifest
        except AcquisitionCommitError:
            raise
        except OSError as error:
            raise AcquisitionCommitError(
                "ATOMIC_COMMIT_FAILED",
                "The acquisition manifest could not be committed atomically.",
            ) from error
        finally:
            self._cleanup_temporary(temporary)
            if descriptor is not None:
                os.close(descriptor)
            self._cleanup_temporary(lock_path)

    def verify_manifest(
        self, manifest: AcquiredDocumentManifest, *, verify_bytes: bool
    ) -> None:
        raw = manifest.model_dump(mode="json")
        payload = dict(raw)
        payload.pop("manifest_payload_fingerprint", None)
        payload["artifact_checksum"] = None
        if canonical_fingerprint(payload) != manifest.manifest_payload_fingerprint:
            raise AcquisitionCommitError(
                "MANIFEST_CHECKSUM_MISMATCH",
                "The stable acquisition manifest payload fingerprint is invalid.",
            )
        checksum_payload = dict(raw)
        checksum_payload["artifact_checksum"] = None
        if canonical_fingerprint(checksum_payload) != manifest.artifact_checksum:
            raise AcquisitionCommitError(
                "MANIFEST_CHECKSUM_MISMATCH",
                "The acquisition manifest artifact checksum is invalid.",
            )
        stable_records = self._stable_acquisition_records(
            manifest.item_outcomes, manifest.records
        )
        expected_id = deterministic_acquisition_manifest_id(
            schema_version=manifest.schema_version,
            workspace_id=manifest.workspace_id,
            run_id=manifest.run_id,
            parent_refs=manifest.parent_refs.model_dump(mode="json"),
            acquisition_records=stable_records,
            algorithm_version=manifest.manifest_identity_algorithm_version,
        )
        if expected_id != manifest.manifest_id:
            raise AcquisitionCommitError(
                "MANIFEST_ID_MISMATCH", "The deterministic manifest ID is invalid."
            )
        expected_manifest_relative = (
            f"literature/acquisition/{manifest.run_id}/{manifest.manifest_id}.json"
        )
        if (
            manifest.e2_reference.acquisition_manifest_path
            != expected_manifest_relative
        ):
            raise AcquisitionCommitError(
                "E2_REFERENCE_MISMATCH",
                "The embedded E2 reference does not target this manifest path.",
            )
        if (
            canonical_fingerprint(manifest.parent_refs.model_dump(mode="json"))
            != manifest.parent_lineage_sha256
        ):
            raise AcquisitionCommitError(
                "PARENT_LINEAGE_MISMATCH", "The parent lineage fingerprint is invalid."
            )
        for record in manifest.records:
            expected_document = deterministic_document_id(
                study_id=record.study_id,
                source_hash=record.source_sha256,
                workspace_id=manifest.workspace_id,
                algorithm_version=record.document_identity_algorithm_version,
            )
            if expected_document != record.document_id:
                raise AcquisitionCommitError(
                    "DOCUMENT_ID_MISMATCH", "A record document ID is invalid."
                )
            if verify_bytes:
                binding = self.workspace_bindings.get(manifest.workspace_id)
                if binding is None:
                    raise AcquisitionCommitError(
                        "WORKSPACE_BINDING_MISSING",
                        "The manifest workspace has no accepted canonical root binding.",
                    )
                try:
                    root = binding.canonical_root
                    path = self._safe_workspace_path(
                        root.resolve(strict=True),
                        record.workspace_relative_path,
                        create=False,
                    )
                    if (
                        not path.is_file()
                        or path.stat().st_size != record.byte_length
                        or self._hash_file(path) != record.source_sha256
                    ):
                        raise AcquisitionCommitError(
                            "REUSED_CONTENT_INVALID",
                            "A manifest-bound content artifact is missing or changed.",
                        )
                    if not is_valid_pdf(path) or not self._structural_validation(path):
                        raise AcquisitionCommitError(
                            "REUSED_CONTENT_INVALID",
                            "A manifest-bound content artifact failed structural validation.",
                        )
                except AcquisitionCommitError:
                    raise
                except (AcquisitionPreflightError, OSError, ValueError) as error:
                    raise AcquisitionCommitError(
                        "REUSED_CONTENT_INVALID",
                        "A manifest-bound content artifact could not be verified.",
                    ) from error

    def _verify_manifest_parents(
        self,
        manifest: AcquiredDocumentManifest,
        prepared: Sequence[_PreparedRequest],
    ) -> None:
        first = prepared[0].request
        expected = self._parent_refs(prepared)
        if (
            manifest.workspace_id != first.workspace_id
            or manifest.protocol_fingerprint != first.protocol_fingerprint
            or manifest.corpus_fingerprint != first.corpus_fingerprint
            or manifest.parent_refs != expected
        ):
            raise AcquisitionPreflightError(
                "PARENT_BINDING_MISMATCH",
                "The manifest does not reference the current accepted workspace/run lineage.",
            )

    async def _append_audit(
        self, manifest: AcquiredDocumentManifest, manifest_path: Path
    ) -> StructuredError | None:
        try:
            self._inject(AcquisitionFault.AUDIT_APPEND, manifest.manifest_id)
            if await self.audit_sink.has_event(
                manifest.manifest_id, manifest.idempotency_key
            ):
                return None
            await self.audit_sink.append_once(
                self._audit_event(manifest, manifest_path)
            )
        except asyncio.CancelledError:
            return StructuredError(
                code="AUDIT_APPEND_CANCELLED",
                message=(
                    "The manifest committed, but audit append was cancelled and is "
                    "recoverable."
                ),
                retryable=True,
                details={"error_type": "CancelledError"},
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return StructuredError(
                code="AUDIT_APPEND_FAILED",
                message="The manifest committed, but its canonical audit event is recoverable.",
                retryable=True,
                details={"error_type": type(error).__name__},
            )
        return None

    def _audit_event(
        self, manifest: AcquiredDocumentManifest, manifest_path: Path
    ) -> dict[str, Any]:
        return {
            "action": "PDF_DISCOVERY_DOWNLOAD",
            "agent_or_tool": (
                f"{self.producer.package}/{self.producer.version}"
                f"@{self.producer.commit}"
            ),
            "description": "Committed deterministic PDF acquisition manifest.",
            "inputs": [
                f"{manifest.parent_refs.corpus_snapshot.artifact_id}@{manifest.parent_refs.corpus_snapshot.sha256}",
                f"{manifest.parent_refs.screening_decisions.artifact_id}@{manifest.parent_refs.screening_decisions.sha256}",
            ],
            "outputs": [
                self._relative_path(
                    self.workspace_bindings[manifest.workspace_id].canonical_root,
                    manifest_path,
                )
            ],
            "parameters": {
                "artifact_checksum": manifest.artifact_checksum,
                "idempotency_key": manifest.idempotency_key,
                "manifest_id": manifest.manifest_id,
                "manifest_path": self._relative_path(
                    self.workspace_bindings[manifest.workspace_id].canonical_root,
                    manifest_path,
                ),
                "manifest_type": manifest.manifest_type,
                "protocol_fingerprint": manifest.protocol_fingerprint,
                "corpus_fingerprint": manifest.corpus_fingerprint,
            },
            "metrics": {
                "committed_documents": len(manifest.records),
                "requested_documents": len(manifest.item_outcomes),
            },
            "status": manifest.operation.status,
        }

    def _batch_outcome(
        self,
        manifest: AcquiredDocumentManifest,
        manifest_path: Path,
        outcomes: Sequence[AcquisitionItemOutcome],
        *,
        status: OperationStatus,
        extra_errors: list[StructuredError] | None = None,
        operation_errors: list[StructuredError] | None = None,
        extra_warnings: Sequence[str] | None = None,
    ) -> AcquisitionBatchOutcome:
        errors = operation_errors
        if errors is None:
            errors = list(manifest.operation.errors)
        if extra_errors:
            errors.extend(extra_errors)
        warnings = [error.message for error in manifest.operation.warnings]
        if extra_warnings:
            warnings.extend(extra_warnings)
        return AcquisitionBatchOutcome(
            run_id=manifest.run_id,
            status=status,
            stage=AcquisitionStage.COMPLETE,
            data=AcquisitionOperationData(
                manifest_reference=ManifestReference(
                    manifest_id=manifest.manifest_id,
                    workspace_relative_path=self._relative_path(
                        self.workspace_bindings[manifest.workspace_id].canonical_root,
                        manifest_path,
                    ),
                    artifact_checksum=manifest.artifact_checksum,
                ),
                item_outcomes=list(outcomes),
                committed_count=sum(
                    outcome.document_id is not None for outcome in outcomes
                ),
                requested_count=len(outcomes),
            ),
            # The E1 manifest is kit-owned and is deliberately not a registered
            # Contract v1 artifact.  Its reference is carried in ``data`` so
            # callers do not accidentally feed an ACQ-* identity to frozen
            # artifact registries.
            artifacts=[],
            errors=errors,
            warnings=warnings,
            provenance={
                "manifest_payload_fingerprint": manifest.manifest_payload_fingerprint,
                "producer": manifest.producer.model_dump(mode="json"),
            },
        )

    def _commit_failure_outcome(
        self,
        prepared: Sequence[_PreparedRequest],
        outcomes: Sequence[AcquisitionItemOutcome],
        error: StructuredError,
        extra_warnings: Sequence[str] | None = None,
    ) -> AcquisitionBatchOutcome:
        first = prepared[0].request
        failed = [
            AcquisitionItemOutcome(
                study_id=item.study_id,
                source_kind=item.source_kind,
                acquisition_status=AcquisitionStatus.FAILED,
                requested_source=item.requested_source,
                normalized_doi=item.normalized_doi,
                validation_profile=item.validation_profile,
                validation_profile_version=item.validation_profile_version,
                selected_source=item.selected_source,
                selected_source_url=item.selected_source_url,
                access_status=item.access_status,
                attempts=item.attempts,
                stage=AcquisitionStage.PROMOTION,
                error=error,
            )
            for item in outcomes
        ]
        return AcquisitionBatchOutcome(
            run_id=first.run_id,
            status=OperationStatus.FAILED,
            stage=AcquisitionStage.PROMOTION,
            data=AcquisitionOperationData(
                item_outcomes=failed,
                committed_count=0,
                requested_count=len(prepared),
            ),
            errors=[error],
            warnings=list(extra_warnings or []),
            provenance={"atomic_commit": "failed", "preflight_passed": True},
        )

    def _preflight_failure(
        self, requests: Sequence[AcquisitionRequest], error: AcquisitionPreflightError
    ) -> AcquisitionBatchOutcome:
        first = requests[0]
        item_status = (
            AcquisitionStatus.IDENTITY_MISMATCH
            if error.code == "STUDY_DOI_MISMATCH"
            else AcquisitionStatus.FAILED
        )
        outcomes = [
            AcquisitionItemOutcome(
                study_id=request.study_id,
                source_kind=request.source_kind,
                acquisition_status=item_status,
                requested_source=self._request_source_label(request),
                normalized_doi=request.doi,
                validation_profile=request.validation_profile,
                validation_profile_version=request.validation_profile_version,
                access_status=request.access_status,
                stage=AcquisitionStage.PREFLIGHT,
                error=error.as_error(),
            )
            for request in requests
        ]
        return AcquisitionBatchOutcome(
            run_id=first.run_id,
            status=OperationStatus.FAILED,
            stage=AcquisitionStage.PREFLIGHT,
            data=AcquisitionOperationData(
                item_outcomes=outcomes,
                committed_count=0,
                requested_count=len(requests),
            ),
            errors=[error.as_error()],
            provenance={"preflight_failed": True},
        )

    @staticmethod
    def _relative_path(root: Path, path: Path) -> str:
        return (
            path.resolve(strict=False).relative_to(root.resolve(strict=True)).as_posix()
        )

    @staticmethod
    def _new_temporary(directory: Path, *, prefix: str = ".acquisition-") -> Path:
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _cleanup_temporary(path: Path | None) -> StructuredError | None:
        """Remove a temporary path, retrying transient handle errors.

        Returns a structured diagnostic when the path could not be removed.
        Swallowing that failure would leave non-authoritative bytes in the
        workspace with no evidence, so the caller must surface it.
        """

        if path is None:
            return None
        last_error: OSError | None = None
        for attempt in range(_TEMPORARY_CLEANUP_ATTEMPTS):
            try:
                path.unlink(missing_ok=True)
                return None
            except OSError as error:
                last_error = error
                if attempt + 1 < _TEMPORARY_CLEANUP_ATTEMPTS:
                    time.sleep(0.02 * (attempt + 1))
        _LOGGER.warning(
            "PDF acquisition could not remove temporary path %s: %s",
            path,
            last_error,
        )
        return StructuredError(
            code="TEMPORARY_CLEANUP_FAILED",
            message=(
                "A temporary acquisition file could not be removed and may remain "
                "in the workspace as non-authoritative litter."
            ),
            retryable=True,
            details={"error_type": type(last_error).__name__},
        )

    @classmethod
    def _release(cls, path: Path | None) -> Path | None:
        """Return *path* only when it survived cleanup and must be retried later."""

        if path is not None and cls._cleanup_temporary(path) is not None:
            return path
        return None

    def _release_staged(self, staged: Sequence[_StagedDocument]) -> list[str]:
        """Clean every retained temporary and return surfaced cleanup warnings.

        The warning keeps the structured diagnostic code so a caller can
        recognise the condition instead of having to match prose.
        """

        notes: list[str] = []
        for item in staged:
            diagnostic = self._cleanup_temporary(item.temporary_path)
            if diagnostic is not None:
                notes.append(f"{diagnostic.code}: {diagnostic.message}")
        return notes

    # ------------------------------------------------------------------
    # Crash-safe commit locking
    # ------------------------------------------------------------------

    @staticmethod
    def _lock_descriptor(descriptor: int) -> bool:
        """Take the exclusive byte-range lock for *descriptor* without blocking.

        The lock must be held for as long as the caller owns the lock file: it is
        the only thing that distinguishes a live owner from a leftover created by
        a killed process, because the operating system releases it on process
        death.
        """

        try:
            if os.fstat(descriptor).st_size == 0:
                # ``msvcrt.locking`` needs at least one lockable byte.
                os.write(descriptor, b"0")
            if os.name == "nt":  # pragma: no cover - platform branch
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - platform branch
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    @staticmethod
    def _reclaim_stale_lock(lock_path: Path) -> bool:
        """Remove ``lock_path`` when its owner died without releasing the lock."""

        try:
            descriptor = os.open(lock_path, os.O_RDWR)
        except OSError:
            return False
        try:
            reclaimed = PDFAcquisitionService._lock_descriptor(descriptor)
        except OSError:
            return False
        finally:
            os.close(descriptor)
        if not reclaimed:
            return False
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    @staticmethod
    def _acquire_lock_file(lock_path: Path, *, timeout: float) -> int:
        """Create ``lock_path`` exclusively, recovering a dead owner's leftover.

        The returned descriptor holds the OS lock, so the caller must keep it
        open for the whole critical section.
        """

        deadline = time.monotonic() + timeout
        while True:
            if lock_path.is_symlink():
                raise AcquisitionCommitError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "A commit lock cannot be a symlink.",
                )
            try:
                descriptor = os.open(
                    lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                )
            except FileExistsError:
                if PDFAcquisitionService._reclaim_stale_lock(lock_path):
                    # The previous owner is gone; retry immediately rather than
                    # stalling for the full lock timeout.
                    continue
                if time.monotonic() >= deadline:
                    raise AcquisitionCommitError(
                        "ATOMIC_COMMIT_TIMEOUT",
                        "A concurrent commit did not release its lock.",
                    )
                time.sleep(0.02)
                continue
            except OSError as error:
                raise AcquisitionCommitError(
                    "FILESYSTEM_ERROR",
                    "The commit lock could not be acquired.",
                ) from error
            # We created the file, so nobody else can hold the lock yet.  Taking
            # it now is what stops a concurrent process from concluding that this
            # brand-new lock belongs to a dead owner and unlinking it.
            if PDFAcquisitionService._lock_descriptor(descriptor):
                return descriptor
            os.close(descriptor)
            if time.monotonic() >= deadline:
                raise AcquisitionCommitError(
                    "ATOMIC_COMMIT_TIMEOUT",
                    "A concurrent commit did not release its lock.",
                )
            time.sleep(0.02)

    @contextmanager
    def _publication_lock(self, workspace_root: Path) -> Iterator[None]:
        """Serialize manifest publication and orphan removal in one workspace.

        Publication and owned-orphan cleanup must not interleave: a cleanup that
        scanned the manifest directory before a concurrent publication would
        then delete content that publication has just bound.
        """

        try:
            root = self._safe_workspace_path(
                workspace_root, "literature/acquisition", create=True
            )
        except AcquisitionPreflightError as error:
            raise AcquisitionCommitError(
                "MANIFEST_DIRECTORY_INVALID",
                "The acquisition publication directory is not safely anchored.",
            ) from error
        descriptor = self._acquire_lock_file(root / ".publication.lock", timeout=30.0)
        try:
            yield
        finally:
            os.close(descriptor)
            self._cleanup_temporary(root / ".publication.lock")

    # ------------------------------------------------------------------
    # Durable commit intent (crash recovery ownership evidence)
    # ------------------------------------------------------------------

    def _commit_intent_path(self, final_path: Path) -> Path:
        return final_path.with_name(final_path.name + COMMIT_INTENT_SUFFIX)

    def _write_commit_intent(
        self, final_path: Path, payload: Mapping[str, Any]
    ) -> None:
        """Persist ownership evidence for a document about to be promoted.

        The marker is published with an atomic no-replace link so a killed
        process leaves either a complete marker or none.  It is written *before*
        promotion: an orphan without a marker is unmanaged content and must stay
        unusable, while an orphan with a marker is recoverable under the exact
        idempotency key that created it.
        """

        marker_path = self._commit_intent_path(final_path)
        temporary = self._new_temporary(
            final_path.parent, prefix=f".{final_path.name}."
        )
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, marker_path)
            except FileExistsError:
                # A previous attempt already recorded intent for this
                # content-addressed identity; keep the first durable claim.
                pass
        except OSError as error:
            raise AcquisitionCommitError(
                "ATOMIC_COMMIT_FAILED",
                "The acquisition commit intent could not be recorded.",
            ) from error
        finally:
            self._cleanup_temporary(temporary)

    def _discard_commit_intent(self, final_path: Path) -> None:
        """Drop the recovery marker once a manifest authoritatively binds the path."""

        self._cleanup_temporary(self._commit_intent_path(final_path))

    def _read_commit_intent(
        self,
        final_path: Path,
        prepared: _PreparedRequest,
        document_id: str,
        final_relative: str,
    ) -> dict[str, Any] | None:
        """Return the intent marker that proves this attempt owns the orphan."""

        marker_path = self._commit_intent_path(final_path)
        if marker_path.is_symlink() or not marker_path.is_file():
            return None
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise AcquisitionCommitError(
                "UNMANAGED_CONTENT",
                "An existing commit-intent marker is unreadable.",
            ) from error
        if not isinstance(marker, Mapping):
            raise AcquisitionCommitError(
                "UNMANAGED_CONTENT",
                "An existing commit-intent marker is not an object.",
            )
        if marker.get("schema_version") != COMMIT_INTENT_SCHEMA_VERSION:
            return None
        if (
            marker.get("idempotency_key") != prepared.idempotency_key
            or marker.get("document_id") != document_id
            or marker.get("study_id") != prepared.request.study_id
            or marker.get("workspace_relative_path") != final_relative
        ):
            # A marker written for a different semantic request never authorizes
            # adoption of this orphan.
            return None
        return dict(marker)

    def _remove_owned_orphan(
        self,
        workspace_root: Path,
        relative: str,
        *,
        document_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> StructuredError | None:
        """Remove an orphan this attempt promoted without touching bound content.

        Removal is gated on this attempt's ownership identity, so a concurrent
        manifest that already binds the path is preserved.  An unverifiable
        workspace manifest is reported as an anomaly instead of silently
        becoming a reason to keep the artifact.
        """

        anomalies: list[StructuredError] = []
        try:
            path = self._safe_workspace_path(workspace_root, relative, create=False)
            if not path.exists():
                return None
            if document_id is not None and path.name != f"{document_id}.pdf":
                anomalies.append(
                    StructuredError(
                        code="ORPHAN_OWNERSHIP_MISMATCH",
                        message=(
                            "An orphan path did not match the document identity "
                            "promoted by this attempt."
                        ),
                    )
                )
            if expected_sha256 is not None:
                try:
                    if self._hash_file(path) != expected_sha256:
                        anomalies.append(
                            StructuredError(
                                code="ORPHAN_OWNERSHIP_MISMATCH",
                                message=(
                                    "An orphan path no longer holds the bytes this "
                                    "attempt promoted."
                                ),
                            )
                        )
                except OSError as error:
                    anomalies.append(
                        StructuredError(
                            code="ORPHAN_OWNERSHIP_MISMATCH",
                            message="An orphan path could not be read for ownership.",
                            details={"error_type": type(error).__name__},
                        )
                    )
            if anomalies:
                return self._orphan_anomaly(anomalies)

            with self._publication_lock(workspace_root):
                # A concurrent process may have committed the same identity after
                # this operation promoted its inode.  Never remove shared content
                # that is already bound by a valid manifest.
                for manifest_path in self._manifest_paths(workspace_root):
                    try:
                        manifest = AcquiredDocumentManifest.model_validate_json(
                            manifest_path.read_text(encoding="utf-8")
                        )
                        self.verify_manifest(manifest, verify_bytes=False)
                    except (OSError, ValueError, AcquisitionCommitError) as error:
                        # Report the anomaly but keep going: this attempt's
                        # ownership evidence is independent of an unrelated
                        # unreadable manifest, and the orphan is still litter.
                        anomalies.append(
                            StructuredError(
                                code="ORPHAN_MANIFEST_UNVERIFIABLE",
                                message=(
                                    "A workspace manifest could not be verified "
                                    "while removing an owned orphan."
                                ),
                                details={
                                    "error_type": type(error).__name__,
                                    "path": manifest_path.name,
                                },
                            )
                        )
                        continue
                    if any(
                        record.workspace_relative_path == relative
                        for record in manifest.records
                    ):
                        self._discard_commit_intent(path)
                        return self._orphan_anomaly(anomalies)
                path.unlink(missing_ok=True)
                self._discard_commit_intent(path)
        except (AcquisitionPreflightError, AcquisitionCommitError, OSError) as error:
            anomalies.append(
                StructuredError(
                    code="ORPHAN_CLEANUP_FAILED",
                    message="An owned orphan could not be removed from the workspace.",
                    details={"error_type": type(error).__name__},
                )
            )
        return self._orphan_anomaly(anomalies)

    @staticmethod
    def _orphan_anomaly(
        anomalies: Sequence[StructuredError],
    ) -> StructuredError | None:
        if not anomalies:
            return None
        for anomaly in anomalies:
            _LOGGER.warning("PDF acquisition orphan cleanup: %s", anomaly.message)
        return StructuredError(
            code=anomalies[0].code,
            message=anomalies[0].message,
            retryable=anomalies[0].retryable,
            details={
                "anomaly_codes": sorted({anomaly.code for anomaly in anomalies}),
                "anomaly_count": len(anomalies),
            },
        )

    def _inject(self, point: AcquisitionFault, identity: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point, identity)

    @staticmethod
    def _redact_external_path(path: Path) -> str:
        return f"external:{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"


class _NotFound(Exception):
    def __init__(self, result: TransportResult) -> None:
        super().__init__("selected source was not found")
        self.result = result
