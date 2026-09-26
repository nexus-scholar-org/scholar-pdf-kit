"""Deterministic frozen-Contract ``document_manifest`` candidate construction.

Packet E2 section 6.5 is narrow on purpose.  This module builds the candidate
*payload*; it never accepts it:

* the kit imports no harness module and holds no Contract registry, so the frozen
  ``DocumentManifestArtifact`` model is restated here as a strict local shape
  check (mirroring E1's ``contract_parents.py``) rather than imported;
* ``artifact_id`` is a pure function of the payload with the id slot removed, so
  the same inputs always reproduce the same identity and a changed payload is a
  different artifact instead of a silent overwrite of an existing one;
* ``contract_acceptance`` is fixed to ``not_performed_by_kit``.  Only a bounded
  harness adapter calling ``accept_artifact`` can make such an artifact
  authoritative, and this kit must never label its own candidate as accepted;
* ``inputs`` is exactly the accepted ``ScreeningDecisionsArtifact`` reference
  (``artifact_id`` + ``sha256``) and nothing else, matching the frozen acceptance
  gate that requires a ``screening_decisions`` parent;
* ``created_at`` is derived from the accepted screening parent's immutable
  ``created_at`` rather than from the wall clock, so the candidate -- unlike the
  sidecar's provenance ``committed_at`` -- is reproducible for identical inputs.
"""

from __future__ import annotations

from typing import Any

from .acquisition_models import (
    AcceptedParentBinding,
    ProducerProvenance,
)
from .canonical import canonical_fingerprint
from .extraction_models import (
    BYTE_BEARING_STATUSES,
    CONTRACT_ARTIFACT_TYPE,
    CONTRACT_VERSION,
    DOCUMENT_MANIFEST_REQUIRED_STATUSES,
    SCREENING_DECISIONS_ARTIFACT_TYPE,
    ArtifactRecordProjection,
    DocumentManifestCandidate,
    ExtractedDocumentRecord,
    ExtractionMethod,
    project_content_status,
)

#: The frozen envelope keys (models.py:195-198) E2 reproduces exactly.
ENVELOPE_KEYS = (
    "schema_version",
    "artifact_type",
    "artifact_id",
    "created_at",
    "producer",
    "workspace_id",
    "run_id",
    "protocol_fingerprint",
    "corpus_fingerprint",
    "inputs",
    "data",
)

#: The frozen ``DocumentRecord`` keys (models.py:472-510).
DOCUMENT_RECORD_KEYS = (
    "document_id",
    "study_id",
    "source_hash",
    "content_status",
    "extracted_path",
    "extraction_method",
)

#: Only ``extracted_path`` is conditional in the frozen ``DocumentRecord``
#: (models.py:503-510): it is present for ``VALID``/``PARTIAL`` and absent for
#: ``FAILED``/``NEEDS_OCR``.  ``extraction_method`` is required on *every* record,
#: so a failed or no-text-layer document still records how its outcome was
#: reached.  Treating the method as optional here made the kit validator accept a
#: payload the frozen model rejects with ``Field required``.
OPTIONAL_DOCUMENT_RECORD_KEYS = ("extracted_path",)

#: The frozen ``extraction_method`` vocabulary (``MethodProvenance``).
DOCUMENT_RECORD_METHODS = frozenset(method.value for method in ExtractionMethod)

_ARTIFACT_ID_PREFIX = "ART-"
_ALGORITHM_VERSION = "v1"


class CandidateContractError(ValueError):
    """The candidate payload does not satisfy the frozen Contract v1 shape."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise CandidateContractError(code, message)


def _reject_unknown(payload: dict[str, Any], keys: tuple[str, ...], label: str) -> None:
    unexpected = sorted(set(payload) - set(keys))
    _require(
        not unexpected,
        "CANDIDATE_UNKNOWN_FIELD",
        f"{label} carries fields the frozen model does not declare: {unexpected}",
    )


def _require_optional_str(value: Any, label: str) -> None:
    _require(
        value is None or isinstance(value, str),
        "CANDIDATE_FIELD_TYPE",
        f"{label} must be a string or absent",
    )


def find_screening_parent(
    accepted_parents: list[AcceptedParentBinding],
) -> AcceptedParentBinding:
    """Return the single accepted ``screening_decisions`` parent binding.

    A missing or ambiguous screening parent is a hard rejection: the candidate's
    only declared input is that reference, so guessing is not available.
    """

    matches = [
        parent
        for parent in accepted_parents
        if parent.artifact_type == SCREENING_DECISIONS_ARTIFACT_TYPE
    ]
    _require(
        len(matches) == 1,
        "SCREENING_PARENT_REQUIRED",
        "the extraction run must carry exactly one accepted screening_decisions parent",
    )
    return matches[0]


def document_record(
    record: ExtractedDocumentRecord,
) -> dict[str, Any]:
    """Project one committed E2 record onto the frozen ``DocumentRecord`` shape.

    Every value is derived from the E1 record or from the extraction result; no
    field is invented, and a non-byte-bearing record is never projected here
    (the caller supplies it from the item outcome instead).
    """

    content_status = project_content_status(record.extraction_status)
    if content_status not in DOCUMENT_MANIFEST_REQUIRED_STATUSES:
        raise CandidateContractError(
            "CANDIDATE_RECORD_STATUS",
            "only a byte-bearing VALID/PARTIAL record may be projected",
        )
    return {
        "document_id": record.document_id,
        "study_id": record.study_id,
        "source_hash": record.source_sha256,
        "content_status": content_status.value,
        "extracted_path": record.extracted_path,
        "extraction_method": record.extraction_method.value,
    }


def failed_document_record(
    projection: ArtifactRecordProjection,
) -> dict[str, Any]:
    """Build the truthful ``FAILED``/``NEEDS_OCR`` frozen record for a failure.

    ``extracted_path`` is *absent* rather than null: the frozen model requires a
    path only for ``VALID``/``PARTIAL``, and inventing one for a failed document
    is the fabrication ``E2-NEG-014`` names.

    ``extraction_method`` is emitted on every record, including this one, because
    the frozen model requires it unconditionally.  Its value comes from the
    projection, which derives it from the attempts that actually ran
    (``determined_outcome_method``), so a failed document states how its outcome
    was reached instead of being silently stripped of provenance.
    """

    payload: dict[str, Any] = {
        "document_id": projection.document_id,
        "study_id": projection.study_id,
        "source_hash": projection.source_sha256,
        "content_status": projection.content_status,
        "extraction_method": projection.extraction_method.value,
    }
    if projection.content_status not in {"FAILED", "NEEDS_OCR"}:
        raise CandidateContractError(
            "CANDIDATE_RECORD_STATUS",
            "a non-byte-bearing document must project FAILED or NEEDS_OCR",
        )
    return payload


def build_document_manifest_candidate(
    *,
    workspace_id: str,
    run_id: str,
    protocol_fingerprint: str,
    corpus_fingerprint: str,
    screening_parent: AcceptedParentBinding,
    producer: ProducerProvenance,
    records: list[ExtractedDocumentRecord],
    non_committed: list[ArtifactRecordProjection],
) -> DocumentManifestCandidate:
    """Construct the deterministic, non-authoritative ``document_manifest`` candidate.

    The candidate set is *every* document whose engine chain ran against verified
    bytes -- successes and determined failures alike -- so the manifest is a
    complete account of the batch instead of a quiet selection of the successes
    (packet E2 section 6.5).  The caller must pass no records for an
    all-failure run; this builder refuses to emit an empty ``documents`` list
    because ``min_length=1`` is a frozen-model property, not a lenient encoding.
    """

    _require(
        producer.package == "scholar-pdf-kit",
        "CANDIDATE_PRODUCER_INVALID",
        "the candidate producer package must be scholar-pdf-kit",
    )
    _require(
        screening_parent.artifact_type == SCREENING_DECISIONS_ARTIFACT_TYPE,
        "SCREENING_PARENT_REQUIRED",
        "the candidate declares only a screening_decisions parent",
    )
    _require(
        screening_parent.workspace_id == workspace_id,
        "CANDIDATE_WORKSPACE_MISMATCH",
        "the accepted screening parent belongs to a different workspace",
    )
    created_at = _screening_created_at(screening_parent)

    documents: list[dict[str, Any]] = []
    for record in records:
        documents.append(document_record(record))
    for projection in non_committed:
        documents.append(failed_document_record(projection))
    documents.sort(key=lambda document: (document["study_id"], document["document_id"]))

    _require(
        documents,
        "CANDIDATE_EMPTY_DOCUMENT_SET",
        "a document_manifest candidate requires at least one determined document",
    )
    identities = [(item["study_id"], item["document_id"]) for item in documents]
    _require(
        len(identities) == len(set(identities)),
        "CANDIDATE_DUPLICATE_DOCUMENT",
        "the candidate set must contain one record per document",
    )
    for document in documents:
        _require(
            document["content_status"] in {"VALID", "PARTIAL", "FAILED", "NEEDS_OCR"},
            "CANDIDATE_RECORD_STATUS",
            "content_status is outside the frozen content-status enum",
        )
        # extraction_method is required on EVERY record, so it is checked before
        # the path check and never relaxed for a failed document.
        _require(
            isinstance(document.get("extraction_method"), str)
            and document["extraction_method"] in DOCUMENT_RECORD_METHODS,
            "CANDIDATE_RECORD_METHOD",
            "every candidate document requires a frozen extraction_method",
        )
        if document["content_status"] in {"FAILED", "NEEDS_OCR"}:
            _require(
                "extracted_path" not in document,
                "CANDIDATE_FAILED_RECORD_PATH",
                "a FAILED/NEEDS_OCR document must omit extracted_path",
            )
        else:
            _require(
                bool(document.get("extracted_path")),
                "CANDIDATE_VALID_RECORD_PATH",
                "a VALID/PARTIAL document must carry its extracted_path",
            )

    payload: dict[str, Any] = {
        # The frozen Contract v1 envelope version is a kit constant, not a
        # property of the E1 binding: the accepted screening parent is a
        # workspace artifact, not a Contract v1 envelope.
        "schema_version": CONTRACT_VERSION,
        "artifact_type": CONTRACT_ARTIFACT_TYPE,
        "created_at": created_at,
        "producer": producer.model_dump(mode="json"),
        "workspace_id": workspace_id,
        "run_id": run_id,
        "protocol_fingerprint": protocol_fingerprint,
        "corpus_fingerprint": corpus_fingerprint,
        "inputs": [
            {
                "artifact_id": screening_parent.artifact_id,
                "sha256": screening_parent.sha256,
            }
        ],
        "data": {"documents": documents},
    }
    # The identity is derived *without* the id slot, then inserted, so a changed
    # payload can never collide with an existing artifact identity.
    identity_payload = dict(payload)
    identity_payload["artifact_id"] = None
    digest = canonical_fingerprint(identity_payload)
    payload["artifact_id"] = (
        f"{_ARTIFACT_ID_PREFIX}{digest.removeprefix('sha256:')[:32]}"
    )
    candidate = DocumentManifestCandidate(
        artifact_id=payload["artifact_id"],
        payload=payload,
        payload_sha256=canonical_fingerprint(payload),
    )
    validate_document_manifest_candidate(candidate, screening_parent=screening_parent)
    return candidate


def _screening_created_at(parent: AcceptedParentBinding) -> str:
    """Project the accepted parent's immutable ``created_at`` onto the candidate.

    The E1 acquisition manifest carries no ``committed_at``; the accepted
    screening parent is the direct Contract parent and the only immutable
    wall-clock value in the lineage, so it anchors the candidate timestamp.  Using
    the clock here would make an identical rerun produce a different artifact
    identity, which is precisely what the deterministic-identity rule forbids.
    """

    payload = parent.payload or {}
    created_at = payload.get("created_at")
    _require(
        isinstance(created_at, str) and created_at.strip(),
        "SCREENING_PARENT_CREATED_AT_MISSING",
        "the accepted screening parent does not carry a created_at timestamp",
    )
    _require(
        created_at.endswith("Z"),
        "SCREENING_PARENT_CREATED_AT_MALFORMED",
        "the accepted screening parent created_at must be an RFC3339 UTC instant",
    )
    return created_at


def validate_document_manifest_candidate(
    candidate: DocumentManifestCandidate,
    *,
    screening_parent: AcceptedParentBinding,
) -> None:
    """Strictly re-validate a candidate against the frozen shape and its parent.

    This is the kit-local stand-in for the harness acceptance gate.  It never
    approves anything: it only refuses to hand back a candidate that the frozen
    model would reject, so a rejection surfaces here as a structured failure
    instead of a bad artifact reaching a caller.
    """

    payload = candidate.payload
    _require(
        isinstance(payload, dict),
        "CANDIDATE_PAYLOAD_INVALID",
        "the candidate payload must be a mapping",
    )
    _reject_unknown(payload, ENVELOPE_KEYS, "the candidate envelope")
    _require(
        payload.get("artifact_type") == CONTRACT_ARTIFACT_TYPE,
        "CANDIDATE_ARTIFACT_TYPE",
        "the candidate artifact_type must be document_manifest",
    )
    _require(
        candidate.artifact_id == payload.get("artifact_id"),
        "CANDIDATE_ID_MISMATCH",
        "the candidate artifact_id must equal the payload artifact_id",
    )
    _require(
        str(payload.get("artifact_id", "")).startswith(_ARTIFACT_ID_PREFIX),
        "CANDIDATE_ID_INVALID",
        "the candidate artifact_id must use the registered ART- prefix",
    )
    _require(
        isinstance(payload.get("schema_version"), str),
        "CANDIDATE_SCHEMA_VERSION",
        "the candidate schema_version must be a string",
    )
    for key in (
        "created_at",
        "workspace_id",
        "run_id",
        "protocol_fingerprint",
        "corpus_fingerprint",
    ):
        _require(
            isinstance(payload.get(key), str) and bool(payload.get(key)),
            "CANDIDATE_FIELD_MISSING",
            f"the candidate envelope requires a {key}",
        )
    producer = payload.get("producer")
    _require(
        isinstance(producer, dict) and producer.get("package") == "scholar-pdf-kit",
        "CANDIDATE_PRODUCER_INVALID",
        "the candidate producer package must be scholar-pdf-kit",
    )
    inputs = payload.get("inputs")
    _require(
        isinstance(inputs, list) and len(inputs) == 1,
        "CANDIDATE_INPUTS_INVALID",
        "the candidate must declare exactly one input",
    )
    declared = inputs[0]
    _require(
        isinstance(declared, dict),
        "CANDIDATE_INPUTS_INVALID",
        "the candidate input must be a mapping",
    )
    _reject_unknown(declared, ("artifact_id", "sha256"), "the candidate input")
    _require(
        declared.get("artifact_id") == screening_parent.artifact_id
        and declared.get("sha256") == screening_parent.sha256,
        "CANDIDATE_PARENT_MISMATCH",
        "the candidate input must be exactly the accepted screening reference",
    )
    _require(
        payload.get("workspace_id") == screening_parent.workspace_id,
        "CANDIDATE_WORKSPACE_MISMATCH",
        "the candidate workspace must equal the accepted parent workspace",
    )
    _require(
        payload.get("protocol_fingerprint") == screening_parent.protocol_fingerprint
        and payload.get("corpus_fingerprint") == screening_parent.corpus_fingerprint,
        "CANDIDATE_FINGERPRINT_MISMATCH",
        "the candidate fingerprints must equal the accepted parent fingerprints",
    )
    data = payload.get("data")
    _require(
        isinstance(data, dict),
        "CANDIDATE_DATA_INVALID",
        "the candidate data must be a mapping",
    )
    _reject_unknown(data, ("documents",), "the candidate data")
    documents = data.get("documents")
    _require(
        isinstance(documents, list) and bool(documents),
        "CANDIDATE_EMPTY_DOCUMENT_SET",
        "DocumentManifestData.documents requires at least one document",
    )
    seen: set[tuple[str, str]] = set()
    for document in documents:
        _require(
            isinstance(document, dict),
            "CANDIDATE_DOCUMENT_INVALID",
            "each candidate document must be a mapping",
        )
        _reject_unknown(document, DOCUMENT_RECORD_KEYS, "a candidate document")
        for key in (
            "document_id",
            "study_id",
            "source_hash",
            "content_status",
            "extraction_method",
        ):
            _require(
                isinstance(document.get(key), str) and bool(document.get(key)),
                "CANDIDATE_DOCUMENT_INVALID",
                f"a candidate document requires a {key}",
            )
        _require(
            document["extraction_method"] in DOCUMENT_RECORD_METHODS,
            "CANDIDATE_RECORD_METHOD",
            "extraction_method must be a frozen MethodProvenance value",
        )
        for key in OPTIONAL_DOCUMENT_RECORD_KEYS:
            _require_optional_str(document.get(key), f"a candidate document {key}")
        status = document["content_status"]
        _require(
            status in {"VALID", "PARTIAL", "FAILED", "NEEDS_OCR"},
            "CANDIDATE_RECORD_STATUS",
            "content_status is outside the frozen content-status enum",
        )
        identity = (document["study_id"], document["document_id"])
        _require(
            identity not in seen,
            "CANDIDATE_DUPLICATE_DOCUMENT",
            "the candidate set must contain one record per document",
        )
        seen.add(identity)
        if status in {"VALID", "PARTIAL"}:
            _require(
                bool(document.get("extracted_path")),
                "CANDIDATE_VALID_RECORD_PATH",
                "a VALID/PARTIAL document requires extracted_path",
            )
        else:
            # The frozen model makes only extracted_path conditional: a
            # FAILED/NEEDS_OCR record must omit the path but keeps its method.
            _require(
                document.get("extracted_path") is None,
                "CANDIDATE_FAILED_RECORD_PATH",
                "a FAILED/NEEDS_OCR document must omit extracted_path",
            )
    _require(
        candidate.payload_sha256 == canonical_fingerprint(payload),
        "CANDIDATE_CHECKSUM_MISMATCH",
        "the candidate payload checksum does not match the payload",
    )


def byte_bearing_documents(
    records: list[ExtractedDocumentRecord],
) -> list[ExtractedDocumentRecord]:
    """Return the committed subset of *records* in deterministic order."""

    committed = [
        record
        for record in records
        if record.extraction_status in BYTE_BEARING_STATUSES
    ]
    return sorted(committed, key=lambda record: (record.study_id, record.document_id))
