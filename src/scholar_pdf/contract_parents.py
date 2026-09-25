"""Local structural gate for the two frozen Contract v1 parent shapes E1 accepts.

Packet E1 requires the acquired-document boundary to fail closed on an accepted
parent that is not a real ``corpus_snapshot`` or ``screening_decisions`` payload
*before* any parent file is read, any transport runs, and any output is
published.  ``AcceptedParentBinding.payload`` is deliberately a plain mapping, so
without this module a duck-typed parent only had to carry a ``schema_version``
and a ``data`` key to reach the transport.

The kit must not import the harness package and must not vendor the frozen
Contract v1 models, generated schemas, or identifier registry (packet E1 forbids
those paths).  This module is therefore an independent, read-only restatement of
the *required* fields of ``CorpusSnapshotArtifact`` and
``ScreeningDecisionsArtifact`` — it is not a second definition of the contract
and it mints nothing.  It checks shape only: presence, type, identifier prefix,
hash form, UTC timestamp, enum membership, and within-payload uniqueness.  It
deliberately does **not** recompute ``corpus_fingerprint`` or compare the
screening envelope against its binding; those semantic cross-checks stay in
``PDFAcquisitionService._verify_study_lineage`` so they keep their own
diagnostic codes.  Unknown minor-version fields are tolerated because the frozen
``ContractModel`` base is forward compatible.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

CONTRACT_V1_MAJOR_VERSION = "1"

# Mirrors the frozen ``IdentifierKind`` prefix table (corpus/study/source-record/
# screening-decision/artifact/workspace/run) and its opaque-suffix rule.
_IDENTIFIER_PREFIXES: dict[str, tuple[str, ...]] = {
    "artifact_id": ("ART-",),
    "corpus_id": ("COR-",),
    "run_id": ("RUN-",),
    "screening_decision_id": ("SCR-",),
    "source_record_id": ("REC-",),
    "study_id": ("STU-", "SCI-"),
    "workspace_id": ("WSP-",),
}

_OPAQUE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_METHOD_PROVENANCE = frozenset(
    {
        "COMPOSED",
        "DETERMINISTIC_RULE",
        "EXTERNAL_PROVIDER",
        "HEURISTIC",
        "HUMAN",
        "LLM",
    }
)
_SCREENING_DECISION_VALUES = frozenset({"CONFLICT", "EXCLUDE", "INCLUDE", "MAYBE"})

_ENVELOPE_SCALAR_FIELDS = {
    "artifact_type": str,
    "created_at": str,
    "producer": Mapping,
    "workspace_id": str,
    "run_id": str,
    "protocol_fingerprint": str,
    "corpus_fingerprint": str,
    "inputs": Sequence,
    "data": Mapping,
}


class ParentStructureError(ValueError):
    """An accepted parent payload is not a frozen Contract v1 parent shape."""

    def __init__(self, pointer: str, reason: str) -> None:
        super().__init__(f"{pointer}: {reason}")
        self.pointer = pointer
        self.reason = reason


def validate_parent_structure(artifact_type: str, payload: Any) -> None:
    """Raise :class:`ParentStructureError` unless *payload* is a valid v1 parent.

    *artifact_type* must be ``corpus_snapshot`` or ``screening_decisions``; any
    other value is reported rather than silently coerced.
    """

    root = f"{artifact_type}"
    if artifact_type == "corpus_snapshot":
        _validate_envelope(payload, expected_type="corpus_snapshot", root=root)
        _validate_corpus_data(payload["data"], root=f"{root}.data")
    elif artifact_type == "screening_decisions":
        _validate_envelope(payload, expected_type="screening_decisions", root=root)
        _validate_screening_data(payload["data"], root=f"{root}.data")
    else:
        raise ParentStructureError(
            root, f"unsupported accepted parent artifact_type {artifact_type!r}"
        )


def _fail(pointer: str, reason: str) -> None:
    raise ParentStructureError(pointer, reason)


def _as_mapping(value: Any, pointer: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(pointer, f"must be an object, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            _fail(pointer, "object keys must be strings")
    return value


def _as_sequence(value: Any, pointer: str, *, min_length: int = 0) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail(pointer, f"must be an array, got {type(value).__name__}")
    if len(value) < min_length:
        _fail(pointer, f"must contain at least {min_length} item(s)")
    return value


def _require(value: Any, pointer: str) -> Any:
    """Reject an absent member.  ``None`` is never a valid frozen value."""

    if value is None:
        _fail(pointer, "is required")
    return value


def _as_str(
    value: Any, pointer: str, *, min_length: int = 0, max_length: int | None = None
) -> str:
    _require(value, pointer)
    if not isinstance(value, str):
        _fail(pointer, f"must be a string, got {type(value).__name__}")
    if len(value) < min_length:
        _fail(pointer, f"must be at least {min_length} character(s)")
    if max_length is not None and len(value) > max_length:
        _fail(pointer, f"must be at most {max_length} character(s)")
    return value


def _as_identifier(value: Any, kind: str, pointer: str) -> str:
    text = _as_str(_require(value, pointer), pointer)
    prefixes = _IDENTIFIER_PREFIXES[kind]
    prefix = next(
        (candidate for candidate in prefixes if text.startswith(candidate)), None
    )
    if prefix is None:
        _fail(pointer, f"{kind} must start with one of: {', '.join(prefixes)}")
    suffix = text[len(prefix) :]
    if not suffix or _OPAQUE_SUFFIX_RE.fullmatch(suffix) is None:
        _fail(pointer, f"{kind} has an invalid opaque suffix")
    return text


def _as_sha256(value: Any, pointer: str) -> str:
    text = _as_str(_require(value, pointer), pointer)
    if _SHA256_RE.fullmatch(text) is None:
        _fail(pointer, "must be sha256:<64 lowercase hex characters>")
    return text


def _as_enum(value: Any, allowed: frozenset[str], pointer: str) -> str:
    text = _as_str(_require(value, pointer), pointer)
    if text not in allowed:
        _fail(pointer, f"must be one of: {', '.join(sorted(allowed))}")
    return text


def _as_utc_timestamp(value: Any, pointer: str) -> str:
    text = _as_str(_require(value, pointer), pointer)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail(pointer, "must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(pointer, "must carry an explicit UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        _fail(pointer, "must be UTC")
    return text


def _as_int_in_range(value: Any, pointer: str, low: int, high: int) -> int:
    _require(value, pointer)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(pointer, f"must be an integer, got {type(value).__name__}")
    if not low <= value <= high:
        _fail(pointer, f"must be between {low} and {high}")
    return value


def _validate_envelope(payload: Any, *, expected_type: str, root: str) -> None:
    """Validate the shared frozen ``ArtifactEnvelope`` members."""

    envelope = _as_mapping(payload, root)
    schema_version = _as_str(envelope.get("schema_version"), f"{root}.schema_version")
    match = _VERSION_RE.fullmatch(schema_version)
    if match is None or int(match.group(1)) != int(CONTRACT_V1_MAJOR_VERSION):
        _fail(
            f"{root}.schema_version",
            f"unsupported contract schema_version {schema_version!r}",
        )
    if envelope.get("artifact_type") != expected_type:
        _fail(f"{root}.artifact_type", f"must be {expected_type!r}")
    for field, expected in _ENVELOPE_SCALAR_FIELDS.items():
        if expected is str and field != "artifact_type":
            _as_str(envelope.get(field), f"{root}.{field}")
    _as_identifier(envelope.get("artifact_id"), "artifact_id", f"{root}.artifact_id")
    _as_utc_timestamp(envelope.get("created_at"), f"{root}.created_at")
    _as_identifier(envelope.get("workspace_id"), "workspace_id", f"{root}.workspace_id")
    _as_identifier(envelope.get("run_id"), "run_id", f"{root}.run_id")
    _as_sha256(envelope.get("protocol_fingerprint"), f"{root}.protocol_fingerprint")
    _as_sha256(envelope.get("corpus_fingerprint"), f"{root}.corpus_fingerprint")
    _validate_producer(envelope.get("producer"), f"{root}.producer")
    _validate_input_artifacts(envelope.get("inputs"), f"{root}.inputs")


def _validate_producer(value: Any, pointer: str) -> None:
    producer = _as_mapping(value, pointer)
    for field in ("package", "version", "commit"):
        _as_str(producer.get(field), f"{pointer}.{field}", min_length=1)


def _validate_input_artifacts(value: Any, pointer: str) -> None:
    for index, item in enumerate(_as_sequence(value, pointer)):
        item_pointer = f"{pointer}[{index}]"
        artifact = _as_mapping(item, item_pointer)
        _as_identifier(
            artifact.get("artifact_id"), "artifact_id", f"{item_pointer}.artifact_id"
        )
        _as_sha256(artifact.get("sha256"), f"{item_pointer}.sha256")


def _validate_corpus_data(value: Any, root: str) -> None:
    data = _as_mapping(value, root)
    _as_identifier(data.get("corpus_id"), "corpus_id", f"{root}.corpus_id")
    _as_str(
        data.get("identity_algorithm_version"),
        f"{root}.identity_algorithm_version",
        min_length=1,
    )
    studies_pointer = f"{root}.studies"
    studies = _as_sequence(data.get("studies"), studies_pointer, min_length=1)
    record_to_study = _as_mapping(
        data.get("record_to_study"), f"{root}.record_to_study"
    )
    seen_study_ids: set[str] = set()
    declared_records: dict[str, str] = {}
    for index, study in enumerate(studies):
        study_id = _validate_corpus_study(study, f"{studies_pointer}[{index}]")
        if study_id in seen_study_ids:
            _fail(
                f"{studies_pointer}[{index}].study_id",
                "corpus study IDs must be unique",
            )
        seen_study_ids.add(study_id)
        for record_id in _as_sequence(
            study.get("source_record_ids"),
            f"{studies_pointer}[{index}].source_record_ids",
            min_length=1,
        ):
            declared_records[record_id] = study_id
    for record_id, study_id in record_to_study.items():
        _as_identifier(record_id, "source_record_id", f"{root}.record_to_study key")
        _as_identifier(study_id, "study_id", f"{root}.record_to_study[{record_id!r}]")
    _validate_record_to_study(record_to_study, declared_records, seen_study_ids, root)


def _validate_record_to_study(
    record_to_study: Mapping[str, Any],
    declared_records: Mapping[str, str],
    study_ids: set[str],
    root: str,
) -> None:
    """Mirror the frozen ``identity_map_is_total_and_unambiguous`` invariant.

    The identity graph must be total in both directions and consistent, so a
    study can never be resolved to a record that belongs to another study, and a
    declared record can never be missing from (or undeclared in) the map.
    """

    pointer = f"{root}.record_to_study"
    missing = sorted(set(declared_records) - set(record_to_study))
    if missing:
        _fail(pointer, f"record_to_study must be total; missing={missing}")
    unknown = sorted(set(record_to_study) - set(declared_records))
    if unknown:
        _fail(pointer, f"record_to_study must not declare unknown records={unknown}")
    unknown_studies = sorted(set(record_to_study.values()) - study_ids)
    if unknown_studies:
        _fail(
            pointer,
            f"record_to_study references unknown study IDs: {unknown_studies}",
        )
    for record_id, study_id in sorted(declared_records.items()):
        if record_to_study[record_id] != study_id:
            _fail(
                f"{pointer}[{record_id!r}]",
                "record_to_study must map a source record to its declaring study",
            )


def _validate_corpus_study(value: Any, pointer: str) -> str:
    study = _as_mapping(value, pointer)
    study_id = _as_identifier(study.get("study_id"), "study_id", f"{pointer}.study_id")

    source_record_ids: list[str] = []
    records_pointer = f"{pointer}.source_record_ids"
    for index, record_id in enumerate(
        _as_sequence(study.get("source_record_ids"), records_pointer, min_length=1)
    ):
        source_record_ids.append(
            _as_identifier(record_id, "source_record_id", f"{records_pointer}[{index}]")
        )
    if len(source_record_ids) != len(set(source_record_ids)):
        _fail(records_pointer, "source record IDs must be unique within a study")

    # ``alias_ids`` is a bare ``list[str]`` in the frozen ``CorpusStudy``: the
    # contract constrains neither its prefix nor its membership, so it is
    # validated as strings only.  Inventing a prefix rule here would reject
    # parents the frozen contract accepts.
    for index, alias_id in enumerate(
        _as_sequence(study.get("alias_ids", []), f"{pointer}.alias_ids")
    ):
        _as_str(alias_id, f"{pointer}.alias_ids[{index}]")

    external_ids = _as_mapping(study.get("external_ids", {}), f"{pointer}.external_ids")
    for provider in external_ids:
        _as_str(provider, f"{pointer}.external_ids provider", min_length=1)
        values_pointer = f"{pointer}.external_ids[{provider!r}]"
        values = _as_sequence(external_ids[provider], values_pointer, min_length=1)
        for index, identifier in enumerate(values):
            _as_str(identifier, f"{values_pointer}[{index}]", min_length=1)
        if len(values) != len(set(values)):
            _fail(values_pointer, f"external IDs for {provider!r} must be unique")

    _as_str(study.get("title"), f"{pointer}.title", min_length=1)
    if study.get("publication_year") is not None:
        _as_int_in_range(
            study.get("publication_year"), f"{pointer}.publication_year", 1000, 9999
        )
    return study_id


def _validate_screening_data(value: Any, root: str) -> None:
    data = _as_mapping(value, root)
    _validate_screening_binding(data.get("binding"), f"{root}.binding")
    _as_str(data.get("batch_id"), f"{root}.batch_id", min_length=1)
    decisions_pointer = f"{root}.decisions"
    decisions = _as_sequence(data.get("decisions"), decisions_pointer, min_length=1)
    seen_decision_ids: set[str] = set()
    for index, decision in enumerate(decisions):
        decision_id = _validate_screening_decision(
            decision, f"{decisions_pointer}[{index}]"
        )
        if decision_id in seen_decision_ids:
            _fail(
                f"{decisions_pointer}[{index}].decision_id",
                "screening decision IDs must be unique",
            )
        seen_decision_ids.add(decision_id)


def _validate_screening_binding(value: Any, pointer: str) -> None:
    binding = _as_mapping(value, pointer)
    _as_identifier(
        binding.get("screening_run_id"), "run_id", f"{pointer}.screening_run_id"
    )
    _as_identifier(
        binding.get("preparation_run_id"), "run_id", f"{pointer}.preparation_run_id"
    )
    _as_sha256(binding.get("protocol_fingerprint"), f"{pointer}.protocol_fingerprint")
    _as_sha256(binding.get("corpus_fingerprint"), f"{pointer}.corpus_fingerprint")
    _as_sha256(
        binding.get("dedup_configuration_hash"), f"{pointer}.dedup_configuration_hash"
    )
    _as_str(
        binding.get("criteria_renderer_version"),
        f"{pointer}.criteria_renderer_version",
        min_length=1,
    )


def _validate_screening_decision(value: Any, pointer: str) -> str:
    decision = _as_mapping(value, pointer)
    decision_id = _as_identifier(
        decision.get("decision_id"), "screening_decision_id", f"{pointer}.decision_id"
    )
    _as_identifier(decision.get("study_id"), "study_id", f"{pointer}.study_id")
    _as_str(decision.get("screener_id"), f"{pointer}.screener_id", min_length=1)
    _as_enum(decision.get("method"), _METHOD_PROVENANCE, f"{pointer}.method")
    _as_enum(
        decision.get("decision"), _SCREENING_DECISION_VALUES, f"{pointer}.decision"
    )
    _as_str(decision.get("reason"), f"{pointer}.reason", min_length=1)
    _as_utc_timestamp(decision.get("decided_at"), f"{pointer}.decided_at")
    for optional in ("model_id", "prompt_version"):
        if decision.get(optional) is not None:
            # The frozen ``ScreeningDecision`` declares these as bare
            # ``str | None = None``: a non-empty value is not a contract
            # requirement, so an empty string is a valid frozen artifact and
            # must not fail closed here.  Same reasoning as ``alias_ids``.
            _as_str(decision.get(optional), f"{pointer}.{optional}")
    for index, parent_id in enumerate(
        _as_sequence(
            decision.get("parent_decision_ids", []), f"{pointer}.parent_decision_ids"
        )
    ):
        _as_identifier(
            parent_id,
            "screening_decision_id",
            f"{pointer}.parent_decision_ids[{index}]",
        )
    return decision_id
