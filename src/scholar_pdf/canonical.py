"""Canonical JSON and deterministic identities owned by the PDF acquisition boundary.

This module is a *reimplementation* of the frozen Contract v1 canonicalization
and identifier formulas, not a byte-for-byte adapter: the kit deliberately does
not import the harness package, so the algorithms are restated here and kept
algorithm-identical (the harness conformance suite pins the two against the
shared golden payloads).  Acquisition manifests are kit-owned artifacts, while
the identifier formula remains compatible with the harness registry.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOI_PREFIX = re.compile(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)


def _pointer(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    escaped = (part.replace("~", "~0").replace("/", "~1") for part in path)
    return "/" + "/".join(escaped)


def _normalize_json(
    value: Any,
    *,
    path: tuple[str, ...],
    set_like_arrays: frozenset[str],
) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects NaN and infinity")
        if value == 0:
            return 0
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {
            key: _normalize_json(
                child,
                path=(*path, key),
                set_like_arrays=set_like_arrays,
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        normalized = [
            _normalize_json(
                child,
                path=(*path, str(index)),
                set_like_arrays=set_like_arrays,
            )
            for index, child in enumerate(value)
        ]
        if _pointer(path) in set_like_arrays:
            normalized.sort(
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        return normalized
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonical_json_bytes(value: Any, *, set_like_arrays: Iterable[str] = ()) -> bytes:
    """Return UTF-8 canonical JSON with recursively sorted object keys."""

    normalized = _normalize_json(
        value,
        path=(),
        set_like_arrays=frozenset(set_like_arrays),
    )
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_fingerprint(value: Any, *, set_like_arrays: Iterable[str] = ()) -> str:
    """Return ``sha256:<lowercase hex>`` over canonical JSON bytes."""

    digest = hashlib.sha256(
        canonical_json_bytes(value, set_like_arrays=set_like_arrays)
    ).hexdigest()
    return f"sha256:{digest}"


def normalize_doi(value: str) -> str:
    """Normalize a DOI exactly as Contract v1 does for comparisons."""

    if not isinstance(value, str):
        raise TypeError("DOI must be a string")
    normalized = value.strip()
    while True:
        stripped = _DOI_PREFIX.sub("", normalized, count=1).strip()
        if stripped == normalized:
            break
        normalized = stripped
    if not normalized:
        raise ValueError("DOI is empty after normalization")
    return normalized.lower()


def corpus_snapshot_fingerprint(value: Any) -> str:
    """Fingerprint corpus identity with Contract v1 graph normalization."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise TypeError("corpus snapshot data must be a mapping or Pydantic model")
    studies = value.get("studies")
    record_to_study = value.get("record_to_study")
    if not isinstance(studies, list) or not isinstance(record_to_study, Mapping):
        raise TypeError("corpus snapshot requires studies and record_to_study")

    normalized_studies: list[dict[str, Any]] = []
    for study in studies:
        if not isinstance(study, Mapping):
            raise TypeError("each corpus study must be a mapping")
        external_ids = study.get("external_ids", {})
        if not isinstance(external_ids, Mapping):
            raise TypeError("study external_ids must be a mapping")
        normalized_studies.append(
            {
                **study,
                "source_record_ids": sorted(study.get("source_record_ids", [])),
                "alias_ids": sorted(study.get("alias_ids", [])),
                "external_ids": {
                    provider: sorted(provider_ids)
                    for provider, provider_ids in external_ids.items()
                },
            }
        )
    normalized_studies.sort(key=lambda study: study["study_id"])
    normalized = {
        **value,
        "studies": normalized_studies,
        "record_to_study": dict(record_to_study),
    }
    return canonical_fingerprint(normalized)


def source_sha256(data: bytes) -> str:
    """Hash the exact validated PDF bytes."""

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def document_identity_payload(
    *,
    study_id: str,
    source_hash: str,
    workspace_id: str,
    algorithm_version: str = "v1",
) -> dict[str, Any]:
    """Build the exact Contract v1 document-identity payload."""

    if algorithm_version != "v1":
        raise ValueError("unsupported document identity algorithm version")
    if _SHA256_RE.fullmatch(source_hash) is None:
        raise ValueError("source_hash must be a canonical sha256 digest")
    return {
        "algorithm_version": algorithm_version,
        "input": {
            "media_type": "application/pdf",
            "source_sha256": source_hash,
            "study_id": study_id,
        },
        "kind": "document",
        "workspace_namespace": workspace_id,
    }


def deterministic_document_id(
    *,
    study_id: str,
    source_hash: str,
    workspace_id: str,
    algorithm_version: str = "v1",
) -> str:
    """Mint the harness-compatible ``DOC-*`` identity for validated bytes."""

    if algorithm_version != "v1":
        raise ValueError("unsupported document identity algorithm version")
    payload = document_identity_payload(
        study_id=study_id,
        source_hash=source_hash,
        workspace_id=workspace_id,
        algorithm_version=algorithm_version,
    )
    suffix = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]
    return f"DOC-{suffix}"


def deterministic_acquisition_manifest_id(
    *,
    schema_version: str,
    workspace_id: str,
    run_id: str,
    parent_refs: Mapping[str, Any],
    acquisition_records: list[Mapping[str, Any]],
    algorithm_version: str = "v1",
) -> str:
    """Mint ``ACQ-*`` over normalized set-like acquisition identities."""

    if algorithm_version != "v1":
        raise ValueError("unsupported acquisition manifest identity algorithm version")
    normalized = sorted(
        (dict(record) for record in acquisition_records),
        key=lambda record: (
            str(record.get("study_id", "")),
            str(record.get("document_id") or ""),
        ),
    )
    # The v1 handoff defines the manifest identity payload without an
    # algorithm-version member.  The algorithm is recorded separately on the
    # manifest and is validated here so a future version gets an explicit
    # migration rather than silently changing the v1 formula.
    payload = {
        "acquisition_records": normalized,
        "parent_refs": dict(parent_refs),
        "run_id": run_id,
        "schema_version": schema_version,
        "workspace_id": workspace_id,
    }
    suffix = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]
    return f"ACQ-{suffix}"


def extraction_identity_payload(
    *,
    schema_version: str,
    workspace_id: str,
    run_id: str,
    acquisition_manifest_ref: Mapping[str, Any],
    extraction_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the E2 ``EXT-`` identity payload over normalized extraction records.

    The records are supplied already reduced to their non-volatile projection by
    the extraction service (see ``PDFExtractionService._stable_extraction_records``),
    so timestamps, attempt ordinals, and retry counters cannot reach the identity
    even if a caller hands this helper a richer record.  Ordering is normalized
    here as well, so document input order and attempt order never move the id.
    """

    if schema_version != "pdf-extraction-manifest-v1":
        raise ValueError("unsupported extraction manifest identity schema version")
    normalized = sorted(
        (dict(record) for record in extraction_records),
        key=lambda record: (
            str(record.get("study_id", "")),
            str(record.get("document_id") or ""),
        ),
    )
    return {
        "acquisition_manifest_ref": dict(acquisition_manifest_ref),
        "extraction_records": normalized,
        "run_id": run_id,
        "schema_version": schema_version,
        "workspace_id": workspace_id,
    }


def deterministic_extraction_manifest_id(
    *,
    schema_version: str,
    workspace_id: str,
    run_id: str,
    acquisition_manifest_ref: Mapping[str, Any],
    extraction_records: Iterable[Mapping[str, Any]],
    algorithm_version: str = "v1",
) -> str:
    """Mint ``EXT-*`` over the normalized set-like extraction identities.

    Mirrors :func:`deterministic_acquisition_manifest_id` so the E2 sidecar has the
    same shape of deterministic identity as the E1 manifest it descends from, and
    so the two identities stay distinguishable by prefix while sharing one
    formula style.
    """

    if algorithm_version != "v1":
        raise ValueError("unsupported extraction manifest identity algorithm version")
    payload = extraction_identity_payload(
        schema_version=schema_version,
        workspace_id=workspace_id,
        run_id=run_id,
        acquisition_manifest_ref=acquisition_manifest_ref,
        extraction_records=extraction_records,
    )
    suffix = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]
    return f"EXT-{suffix}"
