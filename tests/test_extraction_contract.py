"""WP01-E2 extraction contract tests (offline, hermetic, local fixtures only).

Every test name carries a mandated ID from
``docs/architecture/wp01_packet_e2_extracted_text_handoff.md`` section 10, so a
reviewer can trace each assertion to the packet.  One ID may be covered by more
than one test when the packet names several limbs for it (for example
``E2-NEG-029`` names three fault points); the docstring names the limb.

Fixture rules observed throughout:

* no network, no provider, no licensed model -- ``FakeEngine``/``FakeTransport``
  or the real local PyMuPDF engine over a committed local PDF;
* E1 is executed for real, so E2 only ever consumes a genuinely committed
  ``ACQ-`` manifest bound to verified bytes;
* nothing here imports the harness package or accepts a Contract artifact: the
  kit's candidate stays non-authoritative by construction, and the harness
  acceptance gate plus the frozen registry conformance are Stage 3 obligations
  (see the coverage note in the repair report).

Stage-1 batch shape, recorded here so no test implies otherwise: an E2 batch must
share one acquisition manifest, one requested engine, and one screening parent, and
one ``ACQ-`` manifest can hold several staged documents, so a single batch is the
unit that produces a sidecar.  A *mixed* batch is producible at Stage 1 and is
pinned by real tests rather than by assumption: ``test_e2_neg_014_mixed_batch``
commits two documents from one ``ACQ-`` manifest so the batch is PARTIAL, and
``test_e2_neg_014_mixed_set`` exercises the mixed-set behaviour where the
packet's other blocker lives, the candidate builder/validator, over three
genuinely committed outcomes (one VALID, one provider-attempt FAILED, one
NEEDS_OCR) assembled by the test rather than by a batch.  Whether every document
in a batch commits is an outcome, not a shape, so both a one-record sidecar and a
two-record one are real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_acquisition_contract import PROTOCOL, make_workspace, pdf_bytes
from typer.testing import CliRunner

from scholar_pdf import cli
from scholar_pdf.acquisition import InMemoryAuditSink, PDFAcquisitionService
from scholar_pdf.acquisition_models import (
    AcceptedParentBinding,
    AccessAssertion,
    AccessStatus,
    AcquiredDocumentManifest,
    AcquiredDocumentRecord,
    AcquisitionRequest,
    AcquisitionSourceKind,
    OperationStatus,
    ProducerProvenance,
    SourceMode,
    WorkspaceRootBinding,
)
from scholar_pdf.canonical import (
    canonical_fingerprint,
    corpus_snapshot_fingerprint,
    deterministic_document_id,
)
from scholar_pdf.cli import app
from scholar_pdf.contract_candidate import (
    DOCUMENT_RECORD_KEYS,
    OPTIONAL_DOCUMENT_RECORD_KEYS,
    CandidateContractError,
    build_document_manifest_candidate,
    validate_document_manifest_candidate,
)
from scholar_pdf.extraction import (
    ExtractionCommitError,
    PDFExtractionService,
)
from scholar_pdf.extraction_engines import (
    EngineExtractionResult,
    EngineFailure,
    EngineRegistry,
    FakeEngine,
    default_engine_registry,
)
from scholar_pdf.extraction_models import (
    CONTRACT_ACCEPTANCE_NOT_PERFORMED,
    DEFAULT_MINIMUM_CHARACTER_COUNT,
    ArtifactRecordProjection,
    DocumentContentStatus,
    DocumentManifestCandidate,
    ExtractedDocumentRecord,
    ExtractionBatchOutcome,
    ExtractionEngine,
    ExtractionManifest,
    ExtractionOutputFormat,
    ExtractionRequest,
    ExtractionRunConfig,
    ExtractionStage,
    ExtractionStatus,
    FallbackReason,
)
from scholar_pdf.frontmatter import (
    measure_extracted_body,
    parse_bound_frontmatter,
)

REAL_PDF_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_text_layer.pdf"
REAL_PDF_MARKER = "SENTINEL_REAL_TEXT_LAYER_MARKER"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class ExtractionFixture:
    root: Path
    parents: list[AcceptedParentBinding]
    binding: WorkspaceRootBinding
    producer: ProducerProvenance
    acquisition: AcquiredDocumentManifest
    acquisition_path: Path
    audit: InMemoryAuditSink
    document: int | str = 0

    def record(self, document: int | str | None = None) -> AcquiredDocumentRecord:
        selector = self.document if document is None else document
        if isinstance(selector, int):
            return self.acquisition.records[selector]
        for record in self.acquisition.records:
            if record.document_id == selector:
                return record
        raise AssertionError(f"no acquired record for {selector!r}")

    def request(self, **updates: Any) -> ExtractionRequest:
        record = self.record()
        values: dict[str, Any] = {
            "workspace_id": "WSP-test",
            "workspace_root": self.root,
            "run_id": "RUN-extraction",
            "study_id": record.study_id,
            "protocol_fingerprint": PROTOCOL,
            "corpus_fingerprint": self.parents[0].corpus_fingerprint,
            "screening_decisions": {
                "artifact_id": "ART-screening",
                "artifact_type": "screening_decisions",
                "sha256": self.parents[1].sha256,
                "workspace_relative_path": "literature/screening.json",
            },
            "acquisition_manifest_id": self.acquisition.manifest_id,
            "acquisition_manifest_sha256": self.acquisition.artifact_checksum,
            "acquisition_manifest_path": (
                f"literature/acquisition/{self.acquisition.run_id}/"
                f"{self.acquisition.manifest_id}.json"
            ),
            "document_ids": [record.document_id],
        }
        values.update(updates)
        return ExtractionRequest.model_validate(values)

    def service(self, registry: EngineRegistry | None = None, **kwargs: Any):
        return PDFExtractionService(
            accepted_parents=self.parents,
            workspace_bindings={"WSP-test": self.binding},
            producer=self.producer,
            audit_sink=kwargs.pop("audit_sink", InMemoryAuditSink()),
            engines=registry,
            **kwargs,
        )

    def manifest_path(self) -> Path:
        return self.root / "literature" / "extraction" / "RUN-extraction"

    def sidecar(self, outcome: Any) -> ExtractionManifest:
        reference = outcome.data.manifest_reference
        assert reference is not None
        return ExtractionManifest.model_validate_json(
            (self.root / reference.workspace_relative_path).read_text(encoding="utf-8")
        )

    def sidecars(self) -> list[Path]:
        return sorted(self.manifest_path().glob("*.json"))

    def extracted(self, record: AcquiredDocumentRecord | None = None) -> Path:
        chosen = self.record() if record is None else record
        return self.root / "extracted" / f"{chosen.document_id}.md"


def acquired_fixture(tmp_path: Path) -> ExtractionFixture:
    """Run E1 for real so E2 consumes a genuine committed manifest."""

    workspace = make_workspace(tmp_path)
    audit = InMemoryAuditSink()
    acquisition_service = PDFAcquisitionService(
        accepted_parents=workspace.parents,
        workspace_bindings={"WSP-test": workspace.binding},
        producer=workspace.producer,
        audit_sink=audit,
    )
    outcome = asyncio.run(acquisition_service.acquire([workspace.request()]))
    assert outcome.status is OperationStatus.SUCCESS
    reference = outcome.data.manifest_reference
    assert reference is not None
    manifest_path = workspace.root / reference.workspace_relative_path
    acquisition = AcquiredDocumentManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    return ExtractionFixture(
        root=workspace.root,
        parents=workspace.parents,
        binding=workspace.binding,
        producer=workspace.producer,
        acquisition=acquisition,
        acquisition_path=manifest_path,
        audit=audit,
    )


@dataclass
class StudyWorkspace:
    """A workspace over N studies covered by one screening artifact."""

    workspace_id: str
    root: Path
    parents: list[AcceptedParentBinding]
    binding: WorkspaceRootBinding
    producer: ProducerProvenance
    study_ids: list[str] = field(default_factory=list)

    def acquisition_request(self, study_id: str, source: Path) -> AcquisitionRequest:
        return AcquisitionRequest.model_validate(
            {
                "workspace_id": self.workspace_id,
                "workspace_root": self.root,
                "run_id": "RUN-acquisition",
                "study_id": study_id,
                "protocol_fingerprint": PROTOCOL,
                "corpus_fingerprint": self.parents[0].corpus_fingerprint,
                "inputs": {
                    "corpus_snapshot": {
                        "artifact_id": self.parents[0].artifact_id,
                        "artifact_type": "corpus_snapshot",
                        "sha256": self.parents[0].sha256,
                        "workspace_relative_path": self.parents[
                            0
                        ].workspace_relative_path,
                    },
                    "screening_decisions": {
                        "artifact_id": self.parents[1].artifact_id,
                        "artifact_type": "screening_decisions",
                        "sha256": self.parents[1].sha256,
                        "workspace_relative_path": self.parents[
                            1
                        ].workspace_relative_path,
                    },
                },
                "source_mode": SourceMode.USER_PATH,
                "source_path": source,
                "source_kind": AcquisitionSourceKind.USER_PATH,
                "access_status": AccessStatus.USER_PROVIDED,
                "access_assertion": AccessAssertion(
                    supplied_by="researcher@example.org",
                    permission_basis="Researcher supplied an authorized local copy.",
                ),
                "doi": f"10.1000/{study_id.lower()}",
            }
        )

    def extraction_request(
        self,
        study_id: str,
        manifest: AcquiredDocumentManifest,
        record: AcquiredDocumentRecord,
        **updates: Any,
    ) -> ExtractionRequest:
        values: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "workspace_root": self.root,
            "run_id": "RUN-extraction",
            "study_id": study_id,
            "protocol_fingerprint": PROTOCOL,
            "corpus_fingerprint": self.parents[0].corpus_fingerprint,
            "screening_decisions": {
                "artifact_id": self.parents[1].artifact_id,
                "artifact_type": "screening_decisions",
                "sha256": self.parents[1].sha256,
                "workspace_relative_path": self.parents[1].workspace_relative_path,
            },
            "acquisition_manifest_id": manifest.manifest_id,
            "acquisition_manifest_sha256": manifest.artifact_checksum,
            "acquisition_manifest_path": manifest.e2_reference.acquisition_manifest_path,
            "document_ids": [record.document_id],
        }
        values.update(updates)
        return ExtractionRequest.model_validate(values)

    def service(
        self, registry: EngineRegistry | None = None, **kwargs: Any
    ) -> PDFExtractionService:
        return PDFExtractionService(
            accepted_parents=self.parents,
            workspace_bindings={self.workspace_id: self.binding},
            producer=self.producer,
            audit_sink=kwargs.pop("audit_sink", InMemoryAuditSink()),
            engines=registry,
            **kwargs,
        )


def make_study_workspace(
    tmp_path: Path,
    study_ids: list[str],
    *,
    workspace_id: str = "WSP-test",
) -> StudyWorkspace:
    """Build a workspace whose screening artifact covers every named study.

    E1 refuses two conflicting source bindings for one study, so a batch of
    several documents is expressed as several studies over one corpus-level
    screening artifact -- the shape a real screening run produces.
    """

    root = tmp_path / "workspace"
    (root / "inbox").mkdir(parents=True)
    corpus_data = {
        "corpus_id": f"COR-{workspace_id}",
        "identity_algorithm_version": "v1",
        "studies": [
            {
                "study_id": study_id,
                "source_record_ids": [f"REC-{study_id}"],
                "alias_ids": [],
                "external_ids": {"doi": [f"10.1000/{study_id.lower()}"]},
                "title": f"Fixture study {study_id}",
                "publication_year": 2024,
            }
            for study_id in study_ids
        ],
        "record_to_study": {f"REC-{study_id}": study_id for study_id in study_ids},
    }
    corpus_fingerprint = corpus_snapshot_fingerprint(corpus_data)
    producer_payload = {"package": "fixture", "version": "1", "commit": "abcdef1"}
    corpus_payload = {
        "schema_version": "1.0.0",
        "artifact_id": "ART-corpus",
        "artifact_type": "corpus_snapshot",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_id": workspace_id,
        "run_id": "RUN-screening",
        "protocol_fingerprint": PROTOCOL,
        "corpus_fingerprint": corpus_fingerprint,
        "inputs": [],
        "data": corpus_data,
        "producer": producer_payload,
    }
    screening_payload = {
        "schema_version": "1.0.0",
        "artifact_id": "ART-screening",
        "artifact_type": "screening_decisions",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_id": workspace_id,
        "run_id": "RUN-screening",
        "protocol_fingerprint": PROTOCOL,
        "corpus_fingerprint": corpus_fingerprint,
        "inputs": [
            {
                "artifact_id": "ART-corpus",
                "sha256": canonical_fingerprint(corpus_payload),
            }
        ],
        "data": {
            "binding": {
                "screening_run_id": "RUN-screening",
                "protocol_fingerprint": PROTOCOL,
                "corpus_fingerprint": corpus_fingerprint,
                "criteria_renderer_version": "1",
                "dedup_configuration_hash": "sha256:" + "2" * 64,
                "preparation_run_id": "RUN-preparation",
            },
            "batch_id": "BATCH-one",
            "decisions": [
                {
                    "decision_id": f"SCR-{study_id}",
                    "study_id": study_id,
                    "screener_id": "human:fixture",
                    "method": "HUMAN",
                    "decision": "INCLUDE",
                    "reason": "Included by the fixture.",
                    "decided_at": "2026-01-01T00:00:00Z",
                    "parent_decision_ids": [],
                }
                for study_id in study_ids
            ],
        },
        "producer": producer_payload,
    }
    parents = [
        AcceptedParentBinding(
            artifact_id="ART-corpus",
            artifact_type="corpus_snapshot",
            sha256=canonical_fingerprint(corpus_payload),
            workspace_relative_path="literature/corpus.json",
            workspace_id=workspace_id,
            run_id="RUN-screening",
            protocol_fingerprint=PROTOCOL,
            corpus_fingerprint=corpus_fingerprint,
            payload=corpus_payload,
        ),
        AcceptedParentBinding(
            artifact_id="ART-screening",
            artifact_type="screening_decisions",
            sha256=canonical_fingerprint(screening_payload),
            workspace_relative_path="literature/screening.json",
            workspace_id=workspace_id,
            run_id="RUN-screening",
            protocol_fingerprint=PROTOCOL,
            corpus_fingerprint=corpus_fingerprint,
            payload=screening_payload,
        ),
    ]
    literature = root / "literature"
    literature.mkdir()
    for parent in parents:
        (literature / Path(parent.workspace_relative_path).name).write_text(
            json.dumps(parent.payload, sort_keys=True), encoding="utf-8"
        )
    binding = WorkspaceRootBinding(
        workspace_id=workspace_id,
        canonical_root=root,
        binding_fingerprint=canonical_fingerprint(
            {
                "algorithm_version": "v1",
                "canonical_root": str(root.resolve()),
                "workspace_id": workspace_id,
            }
        ),
    )
    return StudyWorkspace(
        workspace_id=workspace_id,
        root=root,
        parents=parents,
        binding=binding,
        producer=ProducerProvenance(
            package="scholar-pdf-kit", version="0.1.0", commit="abcdef1"
        ),
        study_ids=list(study_ids),
    )


def acquire_studies(
    workspace: StudyWorkspace, sources: dict[str, bytes]
) -> dict[str, tuple[AcquiredDocumentManifest, AcquiredDocumentRecord]]:
    """Run E1 for real over several studies and index the committed records."""

    requests: list[AcquisitionRequest] = []
    for study_id, payload in sources.items():
        source = workspace.root / "inbox" / f"{study_id}.pdf"
        source.write_bytes(payload)
        requests.append(workspace.acquisition_request(study_id, source))
    service = PDFAcquisitionService(
        accepted_parents=workspace.parents,
        workspace_bindings={workspace.workspace_id: workspace.binding},
        producer=workspace.producer,
        audit_sink=InMemoryAuditSink(),
    )
    outcome = asyncio.run(service.acquire(requests))
    assert outcome.status is OperationStatus.SUCCESS, outcome.errors
    indexed: dict[str, tuple[AcquiredDocumentManifest, AcquiredDocumentRecord]] = {}
    for path in sorted(
        (workspace.root / "literature" / "acquisition").rglob("ACQ-*.json")
    ):
        manifest = AcquiredDocumentManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        for record in manifest.records:
            indexed[record.study_id] = (manifest, record)
    assert set(indexed) == set(sources)
    return indexed


def usable_engine(markdown: str | None = None) -> FakeEngine:
    """A scripted engine returning real text: no network, no licensed model."""

    body = markdown if markdown is not None else ("Real findings follow. " * 40)
    return FakeEngine(
        ExtractionEngine.PYMUPDF,
        version_value="1.2.3",
        result=EngineExtractionResult(
            text=body,
            output_format=ExtractionOutputFormat.MARKDOWN,
            page_count=3,
            text_layer_present=True,
        ),
    )


def no_text_layer_engine() -> FakeEngine:
    """A scripted engine that reports a page without an extractable text layer."""

    return FakeEngine(
        ExtractionEngine.PYMUPDF,
        version_value="1.2.3",
        result=EngineExtractionResult(
            text="",
            output_format=ExtractionOutputFormat.MARKDOWN,
            page_count=1,
            text_layer_present=False,
        ),
    )


def failing_engine(
    engine: ExtractionEngine = ExtractionEngine.PYMUPDF, *, message: str = "boom"
) -> FakeEngine:
    """A scripted engine that always fails, for fallback/failure tests."""

    return FakeEngine(
        engine,
        version_value="1.0.0",
        failure=EngineFailure(FallbackReason.ENGINE_ERROR, "ENGINE_ERROR", message),
    )


def unresolvable_engine(engine: ExtractionEngine) -> FakeEngine:
    """A declared engine that cannot run here: ``resolve`` raises structurally."""

    return FakeEngine(
        engine,
        version_value="1.0.0",
        resolve_failure=EngineFailure(
            FallbackReason.ENGINE_NOT_INSTALLED,
            "ENGINE_NOT_INSTALLED",
            "the engine is not installed in this environment",
        ),
    )


def provider_registry(*, fallback_to: ExtractionEngine | None = None) -> EngineRegistry:
    """A registry whose GROBID provider is unreachable and never substitutes."""

    engines = {
        ExtractionEngine.GROBID: failing_engine(
            ExtractionEngine.GROBID, message="connection refused"
        )
    }
    if fallback_to is not None:
        engines[fallback_to] = usable_engine()
    return EngineRegistry(engines)


def write_logger_script(path: Path) -> None:
    """A minimal stand-in for the workspace-manager audit logger."""

    path.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "def log_project_event(slug, action, agent, description, inputs=None, "
        "outputs=None, parameters=None, metrics=None, status='SUCCESS'):\n"
        "    root = Path(sys.argv[2])\n"
        "    (root / 'audit').mkdir(parents=True, exist_ok=True)\n"
        "    event = {'action': action, 'agent_or_tool': agent, "
        "'description': description, 'inputs': inputs or [], "
        "'outputs': outputs or [], 'parameters': parameters or {}, "
        "'metrics': metrics or {}, 'status': status}\n"
        "    with (root / 'audit' / 'journal.jsonl').open('a', "
        "encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(event, sort_keys=True) + '\\n')\n",
        encoding="utf-8",
    )


def _record_fields(record: ExtractedDocumentRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


# ---------------------------------------------------------------------------
# E2-POS-001 -- the authoritative happy path
# ---------------------------------------------------------------------------


def test_e2_pos_001_screened_pdf_commits_authoritative_outputs(
    tmp_path: Path,
) -> None:
    """An accepted E1 record plus usable text commits bytes, sidecar, candidate."""

    fixture = acquired_fixture(tmp_path)
    audit = InMemoryAuditSink()
    request = fixture.request()
    engine = usable_engine()
    service = fixture.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: engine}), audit_sink=audit
    )

    outcome = asyncio.run(service.extract([request]))

    assert outcome.status is OperationStatus.SUCCESS
    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.EXTRACTED
    assert item.content_status is DocumentContentStatus.VALID
    assert item.extraction_method is not None
    assert item.effective_engine == ExtractionEngine.PYMUPDF.value
    assert item.effective_engine_version == "1.2.3"
    assert outcome.data.committed_count == 1
    assert outcome.data.requested_count == 1

    # Authoritative, identity-addressed output path.
    document_id = fixture.record().document_id
    extracted = fixture.root / "extracted" / f"{document_id}.md"
    assert extracted.is_file()
    assert not (fixture.root / "extracted" / f"{document_id}.md.part").exists()

    values, body = parse_bound_frontmatter(extracted.read_bytes())
    # Packet 6.6: the identity keys are never dropped from bound frontmatter.
    assert values["document_id"] == document_id
    assert values["study_id"] == "STU-one"
    assert values["source_sha256"] == fixture.record().source_sha256
    assert values["acquisition_manifest_id"] == fixture.acquisition.manifest_id
    assert values["acquisition_manifest_sha256"] == (
        fixture.acquisition.artifact_checksum
    )
    assert values["extraction_engine"] == ExtractionEngine.PYMUPDF.value
    assert values["extraction_status"] == ExtractionStatus.EXTRACTED.value
    assert values["content_status"] == DocumentContentStatus.VALID.value
    assert values["workspace_id"] == "WSP-test"
    assert (
        measure_extracted_body(body).character_count >= DEFAULT_MINIMUM_CHARACTER_COUNT
    )

    # The candidate is a deterministic, explicitly unaccepted projection.
    candidate = outcome.data.candidate
    assert isinstance(candidate, DocumentManifestCandidate)
    assert candidate.contract_acceptance == CONTRACT_ACCEPTANCE_NOT_PERFORMED
    documents = candidate.payload["data"]["documents"]
    assert len(documents) == 1
    assert documents[0]["document_id"] == document_id
    assert documents[0]["content_status"] == DocumentContentStatus.VALID.value
    assert documents[0]["extracted_path"] == f"extracted/{document_id}.md"
    # The kit never claims acceptance and never names an accepted artifact.
    assert "artifact_reference" not in candidate.payload
    assert audit.events, "a commit must append an audit event"


def test_e2_pos_001_real_text_layer_pdf_extracts_with_the_default_engine(
    tmp_path: Path,
) -> None:
    """Second limb: a real local PDF with a real text layer, real engine, no fake.

    The scripted-engine limb above proves the contract plumbing; this limb proves
    the declared local engine actually reads a text layer and that the recorded
    character/page counts come from the document, not from a fixture literal.
    """

    payload = REAL_PDF_FIXTURE.read_bytes()
    assert len(payload) > 10_000, "the real fixture must be a realistic PDF"
    workspace = make_study_workspace(tmp_path, ["STU-real"])
    acquired = acquire_studies(workspace, {"STU-real": payload})
    manifest, record = acquired["STU-real"]
    request = workspace.extraction_request("STU-real", manifest, record)

    outcome = asyncio.run(
        workspace.service(default_engine_registry()).extract([request])
    )

    assert outcome.status is OperationStatus.SUCCESS
    item = outcome.data.item_outcomes[0]
    assert item.content_status is DocumentContentStatus.VALID
    assert item.effective_engine == ExtractionEngine.PYMUPDF.value
    assert item.page_count == 1
    assert item.character_count and item.character_count >= 1000
    extracted = workspace.root / "extracted" / f"{record.document_id}.md"
    text = extracted.read_text(encoding="utf-8")
    assert REAL_PDF_MARKER in text, "the real text layer must reach the output"
    values, _body = parse_bound_frontmatter(extracted.read_bytes())
    assert values["extraction_engine"] == ExtractionEngine.PYMUPDF.value
    assert values["source_sha256"] == record.source_sha256


# ---------------------------------------------------------------------------
# E2-POS-003 -- a reason-recorded fallback
# ---------------------------------------------------------------------------


def test_e2_pos_003_unavailable_requested_engine_records_the_fallback_reason(
    tmp_path: Path,
) -> None:
    """The requested engine cannot run; the substitution and its reason are kept."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry(
        {
            ExtractionEngine.DOCLING: failing_engine(ExtractionEngine.DOCLING),
            ExtractionEngine.PYMUPDF: usable_engine(),
        }
    )
    request = fixture.request(
        requested_engine=ExtractionEngine.DOCLING, allow_fallback=True
    )
    outcome = asyncio.run(fixture.service(registry).extract([request]))

    item = outcome.data.item_outcomes[0]
    # A degraded chain is PARTIAL, never VALID: the packet requires a reason for
    # PARTIAL and reserves VALID for a clean, threshold-meeting extraction.
    assert item.content_status is DocumentContentStatus.PARTIAL
    assert item.extraction_status is ExtractionStatus.PARTIAL
    assert item.effective_engine == ExtractionEngine.PYMUPDF.value
    assert item.fallback_chain, "the failed engine must be recorded"
    step = item.fallback_chain[0]
    assert step.engine == ExtractionEngine.DOCLING.value
    assert step.reason is FallbackReason.ENGINE_ERROR
    assert item.attempts, "every attempted engine must be recorded"
    assert len(item.attempts) == 2, "both attempts are recorded, in order"
    assert [attempt.engine for attempt in item.attempts] == [
        ExtractionEngine.DOCLING.value,
        ExtractionEngine.PYMUPDF.value,
    ]
    # A PARTIAL candidate is still unaccepted and still carries its reason.
    candidate = outcome.data.candidate
    assert isinstance(candidate, DocumentManifestCandidate)
    document = candidate.payload["data"]["documents"][0]
    assert document["content_status"] == DocumentContentStatus.PARTIAL.value
    assert document["extraction_method"] in {
        "HUMAN",
        "DETERMINISTIC_RULE",
        "HEURISTIC",
        "LLM",
        "EXTERNAL_PROVIDER",
        "COMPOSED",
    }
    assert document["extracted_path"] == f"extracted/{fixture.record().document_id}.md"


# ---------------------------------------------------------------------------
# E2-NEG-001/002/003/004 -- the acquisition parent must be present and unchanged
# ---------------------------------------------------------------------------


def test_e2_neg_001_missing_acquisition_manifest_is_rejected_before_any_engine_call(
    tmp_path: Path,
) -> None:
    """No ``ACQ-`` manifest means no engine call, no output, no candidate."""

    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    fixture.acquisition_path.unlink()

    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [fixture.request()]
        )
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    assert engine.calls == [], "a rejected lineage must not reach an engine"
    assert not (fixture.root / "extracted").exists() or not list(
        (fixture.root / "extracted").glob("*.md")
    )
    item = outcome.data.item_outcomes[0]
    assert item.stage.value == "PREFLIGHT"
    assert item.error is not None
    assert item.error.code in {
        "ACQUISITION_MANIFEST_MISSING",
        "ACQUISITION_MANIFEST_UNREADABLE",
        "ACQUISITION_MANIFEST_INVALID",
    }


def test_e2_neg_002_changed_parent_payload_is_rejected(tmp_path: Path) -> None:
    """The accepted screening payload is re-read; a changed parent fails closed."""

    fixture = acquired_fixture(tmp_path)
    screening_path = fixture.root / "literature" / "screening.json"
    payload = json.loads(screening_path.read_text(encoding="utf-8"))
    payload["data"]["decisions"][0]["decision"] = "EXCLUDE"
    screening_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    assert not list((fixture.root / "extracted").glob("*.md"))
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {"PARENT_FILE_MISMATCH", "PARENT_NOT_ACCEPTED"}


def test_e2_neg_003_manifest_checksum_mutation_is_never_trusted(
    tmp_path: Path,
) -> None:
    """Neither a changed field nor a rewritten stored digest is trusted."""

    fixture = acquired_fixture(tmp_path)
    original = json.loads(fixture.acquisition_path.read_text(encoding="utf-8"))
    mutated = json.loads(json.dumps(original))
    # A schema-valid, semantically inert field: only the null-excluded canonical
    # digest can notice it.
    mutated["producer"]["version"] = "0.0.0-forged"
    assert AcquiredDocumentManifest.model_validate(mutated).producer.version == (
        "0.0.0-forged"
    )

    # Limb 1: a changed field no longer matches the stored null-excluded digest.
    fixture.acquisition_path.write_text(json.dumps(mutated), encoding="utf-8")
    first = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    assert first.status is OperationStatus.FAILED
    assert first.data.manifest_reference is None
    assert first.data.item_outcomes[0].error is not None
    assert first.data.item_outcomes[0].error.code in {
        "ACQUISITION_MANIFEST_CHECKSUM_MISMATCH",
        "MANIFEST_CHECKSUM_MISMATCH",
        "ACQUISITION_MANIFEST_UNVERIFIED",
    }

    # Limb 2: rewriting the stored digest alone does not help either.
    forged = json.loads(json.dumps(mutated))
    forged["artifact_checksum"] = "sha256:" + "9" * 64
    fixture.acquisition_path.write_text(json.dumps(forged), encoding="utf-8")
    second = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    assert second.status is OperationStatus.FAILED
    assert second.data.manifest_reference is None
    assert second.data.item_outcomes[0].error is not None
    assert second.data.item_outcomes[0].error.code != "BATCH_FAILED"
    # The original bytes are what the request declared; the engine never ran.
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_004_cross_workspace_acquisition_manifest_is_rejected(
    tmp_path: Path,
) -> None:
    """A manifest whose namespace is not the bound workspace cannot mint identity.

    The foreign manifest is published *inside* this workspace on purpose: the
    path is legal, so only the ``workspace_id``/binding disagreement can catch
    it.  Minting an identity in the wrong namespace is the failure mode the
    packet names.
    """

    fixture = acquired_fixture(tmp_path)
    foreign = make_study_workspace(
        tmp_path / "foreign", ["STU-one"], workspace_id="WSP-other"
    )
    acquired = acquire_studies(foreign, {"STU-one": pdf_bytes()})
    manifest, record = acquired["STU-one"]
    assert manifest.workspace_id == "WSP-other"
    in_root = (
        fixture.root
        / "literature"
        / "acquisition"
        / "RUN-acquisition"
        / f"{manifest.manifest_id}.json"
    )
    in_root.parent.mkdir(parents=True, exist_ok=True)
    in_root.write_text(
        (foreign.root / manifest.e2_reference.acquisition_manifest_path).read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract(
            [
                fixture.request(
                    acquisition_manifest_id=manifest.manifest_id,
                    acquisition_manifest_sha256=manifest.artifact_checksum,
                    acquisition_manifest_path=in_root.relative_to(
                        fixture.root
                    ).as_posix(),
                    document_ids=[record.document_id],
                )
            ]
        )
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    assert not list((fixture.root / "extracted").glob("*.md"))
    assert not list((fixture.root / "literature" / "extraction").rglob("EXT-*.json"))
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {
        "ACQUISITION_CONTEXT_MISMATCH",
        "ACQUISITION_REFERENCE_MISMATCH",
        "WORKSPACE_BINDING_MISMATCH",
        "SOURCE_BYTES_MISSING",
        "PARENT_BINDING_MISMATCH",
    }
    assert record.document_id.startswith("DOC-")


# ---------------------------------------------------------------------------
# E2-NEG-005/006/007/008 -- document identity and committed bytes
# ---------------------------------------------------------------------------


def test_e2_neg_005_requested_document_absent_from_manifest_is_rejected(
    tmp_path: Path,
) -> None:
    """E2 never invents a document E1 did not commit for the study."""

    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    request = fixture.request(document_ids=["DOC-" + "0" * 32])

    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [request]
        )
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    assert engine.calls == []
    assert outcome.errors
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code == "DOCUMENT_NOT_IN_ACQUISITION_MANIFEST"


def test_e2_neg_006_document_identity_disagreement_is_rejected(
    tmp_path: Path,
) -> None:
    """A ``document_id`` that does not recompute from its own inputs is rejected."""

    fixture = acquired_fixture(tmp_path)
    payload = json.loads(fixture.acquisition_path.read_text(encoding="utf-8"))
    record = payload["records"][0]
    recomputed = deterministic_document_id(
        study_id=record["study_id"],
        source_hash=record["source_sha256"],
        workspace_id=payload["workspace_id"],
        algorithm_version=record["document_identity_algorithm_version"],
    )
    assert recomputed == record["document_id"]
    record["document_id"] = "DOC-" + "1" * 32
    # The manifest cannot even satisfy its own schema with a disagreeing id.
    with pytest.raises(ValueError):
        AcquiredDocumentManifest.model_validate(payload)
    fixture.acquisition_path.write_text(json.dumps(payload), encoding="utf-8")

    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {
        "ACQUISITION_MANIFEST_INVALID",
        "BLOCKED_DOCUMENT_IDENTITY",
        "MANIFEST_ID_MISMATCH",
    }
    # The recomputed value is never substituted silently: no output is produced.
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_007_changed_source_bytes_at_the_committed_path_are_rejected(
    tmp_path: Path,
) -> None:
    """A one-byte mutation and a truncation are both lineage failures."""

    for mutation in ("one_byte", "truncate"):
        fixture = acquired_fixture(tmp_path / mutation)
        engine = usable_engine()
        source = fixture.root / fixture.record().workspace_relative_path
        payload = bytearray(source.read_bytes())
        if mutation == "one_byte":
            payload[len(payload) // 2] = (payload[len(payload) // 2] + 1) % 256
        else:
            payload = payload[: len(payload) // 2]
        source.write_bytes(bytes(payload))

        outcome = asyncio.run(
            fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
                [fixture.request()]
            )
        )

        assert outcome.status is OperationStatus.FAILED
        assert outcome.data.manifest_reference is None
        assert engine.calls == [], "changed bytes must not reach an engine"
        item = outcome.data.item_outcomes[0]
        assert item.error is not None
        assert item.error.code in {
            "SOURCE_BYTES_MISMATCH",
            "SOURCE_BYTES_MISSING",
            "ACQUISITION_MANIFEST_UNVERIFIED",
        }
        assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_008_missing_or_irregular_source_is_rejected(tmp_path: Path) -> None:
    """A directory or an empty file is not a source.

    The symlinked-source limb lives in its own test so this one always reports a
    result: a skip inside the loop would otherwise mark the directory and
    empty-file assertions as unexecuted.
    """

    def _directory(source: Path, root: Path) -> None:
        source.mkdir(parents=True)

    def _empty_file(source: Path, root: Path) -> None:
        source.write_bytes(b"")

    for kind, mutate in (
        ("directory", _directory),
        ("empty_file", _empty_file),
    ):
        fixture = acquired_fixture(tmp_path / kind)
        engine = usable_engine()
        source = fixture.root / fixture.record().workspace_relative_path
        source.unlink()
        mutate(source, fixture.root)

        outcome = asyncio.run(
            fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
                [fixture.request()]
            )
        )

        assert outcome.status is OperationStatus.FAILED, kind
        assert outcome.data.manifest_reference is None, kind
        assert engine.calls == [], kind
        item = outcome.data.item_outcomes[0]
        assert item.error is not None, kind
        assert item.error.code in {
            "SOURCE_BYTES_MISSING",
            "SOURCE_BYTES_MISMATCH",
            "PATH_OUTSIDE_WORKSPACE",
            "ACQUISITION_MANIFEST_UNVERIFIED",
        }, kind
        assert not list((fixture.root / "extracted").glob("*.md")), kind


def test_e2_neg_008b_symlinked_source_outside_the_workspace_is_rejected(
    tmp_path: Path,
) -> None:
    """A source that resolves outside the workspace is rejected, not followed."""

    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    source = fixture.root / fixture.record().workspace_relative_path
    source.unlink()
    outside = fixture.root.parent / "outside.pdf"
    outside.write_bytes(pdf_bytes("z"))
    try:
        os.symlink(outside, source)
    except OSError as error:  # pragma: no cover - platform capability
        pytest.skip(f"symlink creation is unavailable: {error}")

    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [fixture.request()]
        )
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert engine.calls == []
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {
        "PATH_OUTSIDE_WORKSPACE",
        "ACQUISITION_MANIFEST_UNVERIFIED",
    }
    assert not list((fixture.root / "extracted").glob("*.md"))


# ---------------------------------------------------------------------------
# E2-NEG-010/011/012/038 -- engine selection and fallback discipline
# ---------------------------------------------------------------------------


def test_e2_neg_010_unavailable_engine_records_a_reason_or_a_structured_failure(
    tmp_path: Path,
) -> None:
    """An engine that cannot resolve is recorded, never silently substituted."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry(
        {
            ExtractionEngine.DOCLING: unresolvable_engine(ExtractionEngine.DOCLING),
            ExtractionEngine.PYMUPDF: usable_engine(),
        }
    )
    request = fixture.request(
        requested_engine=ExtractionEngine.DOCLING, allow_fallback=True
    )
    outcome = asyncio.run(fixture.service(registry).extract([request]))

    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.PARTIAL
    assert item.effective_engine == ExtractionEngine.PYMUPDF.value
    assert item.fallback_chain, "an unreachable engine needs a reason entry"
    step = item.fallback_chain[0]
    assert step.engine == ExtractionEngine.DOCLING.value
    assert step.reason is FallbackReason.ENGINE_NOT_INSTALLED
    assert item.attempts[0].engine == ExtractionEngine.DOCLING.value

    # Without a fallback allowance the same engine is a structured failure.  A
    # *fresh* workspace is required: the idempotency key is the request-side
    # semantic key of packet 7.4 (workspace, run, acquisition ref, document set,
    # requested engine) and deliberately excludes the fallback policy, so the
    # second call against the same workspace is an exact replay of the degraded
    # commit -- covered by E2-NEG-018 below, not a fresh strict run.
    strict_fixture = acquired_fixture(tmp_path / "strict")
    strict = asyncio.run(
        strict_fixture.service(registry).extract(
            [
                strict_fixture.request(
                    requested_engine=ExtractionEngine.DOCLING, allow_fallback=False
                )
            ]
        )
    )
    strict_item = strict.data.item_outcomes[0]
    # No engine in the chain ran, so the item is a *determined* failure: it is
    # committed and visible with an explicit warning, and it mints no candidate
    # and no extracted bytes.  It is emphatically not a silent success.
    assert strict.status is OperationStatus.FAILED
    assert strict_item.extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert strict_item.content_status is DocumentContentStatus.FAILED
    # The engine that *determined* the outcome is named, but no attempt claims to
    # be effective: nothing produced text, so nothing is reported as extracted.
    assert strict_item.effective_engine == ExtractionEngine.DOCLING.value
    assert strict_item.effective_engine == strict_item.requested_engine
    assert all(not attempt.effective for attempt in strict_item.attempts)
    assert strict_item.extraction_method is not None
    assert strict_item.error is None
    assert strict_item.warning is not None
    assert strict_item.warning.code == "ENGINE_OUTPUT_UNUSABLE"
    # The underlying unavailability reason is never lost: it stays on the
    # attempt, which is the only place a non-substituting chain records it.
    assert len(strict_item.attempts) == 1
    assert strict_item.attempts[0].diagnostic_code == "ENGINE_NOT_INSTALLED"
    # The reason is recorded as a chain entry naming the *requested* engine; no
    # substitution happened, so the effective engine is still the requested one.
    assert len(strict_item.fallback_chain) == 1
    assert strict_item.fallback_chain[0].engine == ExtractionEngine.DOCLING.value
    assert strict_item.fallback_chain[0].reason is FallbackReason.ENGINE_NOT_INSTALLED
    assert strict.data.candidate is None
    assert len(strict_fixture.sidecars()) == 1, "a determined failure is still a commit"
    assert not list((strict_fixture.root / "extracted").glob("*.md"))


def test_e2_neg_011_unrecorded_fallback_substitution_is_rejected(
    tmp_path: Path,
) -> None:
    """``effective != requested`` with an empty reason chain is not a record."""

    fixture = acquired_fixture(tmp_path)
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    record = fixture.sidecar(outcome).records[0]
    payload = _record_fields(record)

    unrecorded = {**payload, "effective_engine": ExtractionEngine.DOCLING.value}
    unrecorded["attempts"] = [
        *payload["attempts"],
        {
            **payload["attempts"][0],
            "ordinal": 2,
            "engine": ExtractionEngine.DOCLING.value,
            "effective": True,
        },
    ]
    payload["attempts"][0]["effective"] = False
    with pytest.raises(ValueError, match="reason-recorded fallback chain"):
        ExtractedDocumentRecord.model_validate(unrecorded)


def test_e2_neg_012_fallback_chain_order_must_match_recorded_attempts(
    tmp_path: Path,
) -> None:
    """A chain whose order contradicts the attempts is rejected."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry(
        {
            ExtractionEngine.DOCLING: failing_engine(ExtractionEngine.DOCLING),
            ExtractionEngine.PYMUPDF: usable_engine(),
        }
    )
    outcome = asyncio.run(
        fixture.service(registry).extract(
            [
                fixture.request(
                    requested_engine=ExtractionEngine.DOCLING, allow_fallback=True
                )
            ]
        )
    )
    record = fixture.sidecar(outcome).records[0]
    payload = _record_fields(record)
    assert len(payload["fallback_chain"]) == 1

    reordered = dict(payload)
    reordered["fallback_chain"] = [
        {**payload["fallback_chain"][0], "engine": ExtractionEngine.PYMUPDF.value}
    ]
    with pytest.raises(ValueError, match="fallback chain order"):
        ExtractedDocumentRecord.model_validate(reordered)

    # The reverse direction is equally rejected: a step the attempts never ran.
    fabricated = dict(payload)
    fabricated["fallback_chain"] = [
        *payload["fallback_chain"],
        {
            "engine": ExtractionEngine.DOCLING.value,
            "engine_version": "1.0.0",
            "reason": FallbackReason.ENGINE_ERROR.value,
        },
    ]
    with pytest.raises(ValueError, match="fallback chain order"):
        ExtractedDocumentRecord.model_validate(fabricated)


def test_e2_neg_038_unknown_engine_name_is_a_structured_error(
    tmp_path: Path,
) -> None:
    """A typo is a structured rejection, never a silent default."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    for requested in ("pymupf", "GROBId", "doclin"):
        outcome = asyncio.run(
            fixture.service(registry).extract(
                [fixture.request(requested_engine=requested, allow_fallback=False)]
            )
        )
        assert outcome.status is OperationStatus.FAILED
        assert outcome.data.manifest_reference is None
        item = outcome.data.item_outcomes[0]
        assert item.extraction_status is ExtractionStatus.FAILED
        assert item.error is not None
        assert item.error.code == "UNSUPPORTED_ENGINE"
        assert not list((fixture.root / "extracted").glob("*.md"))

    # A token that is not even an engine token is refused at construction, so a
    # typo can never reach the registry as a default.
    with pytest.raises(ValidationError, match="lowercase engine token"):
        fixture.request(requested_engine="not-an-engine")


# ---------------------------------------------------------------------------
# E2-NEG-013/014/015/016/039 -- truthful content status
# ---------------------------------------------------------------------------


def test_e2_neg_013_stub_output_is_never_valid(tmp_path: Path) -> None:
    """The legacy parse-failure stub is never content, at any length."""

    fixture = acquired_fixture(tmp_path)
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry(
                {
                    ExtractionEngine.PYMUPDF: usable_engine(
                        "Extracted content from paper.pdf"
                    )
                }
            )
        ).extract([fixture.request()])
    )
    assert outcome.status is OperationStatus.FAILED
    item = outcome.data.item_outcomes[0]
    assert item.content_status is not DocumentContentStatus.VALID
    assert item.content_status is DocumentContentStatus.FAILED
    # No bytes are published, so no Contract candidate exists (E2-NEG-041).
    assert item.extracted_path is None
    assert outcome.data.candidate is None
    assert outcome.errors, "a failed envelope must carry a diagnostic"
    assert FallbackReason.ENGINE_OUTPUT_UNUSABLE.value in item.degradation_reasons


def test_e2_neg_015_valid_requires_text_above_the_recorded_threshold(
    tmp_path: Path,
) -> None:
    """Empty, whitespace-only, frontmatter-only, and stub bodies are not VALID."""

    cases = {
        "empty": "",
        "whitespace": "   \n\t\n  ",
        "frontmatter_only": "---\ndocument_id: DOC-x\n---\n",
        "short": "too short to be content",
    }
    for name, body in cases.items():
        fixture = acquired_fixture(tmp_path / name)
        outcome = asyncio.run(
            fixture.service(
                EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine(body)})
            ).extract([fixture.request()])
        )
        item = outcome.data.item_outcomes[0]
        assert item.content_status is not DocumentContentStatus.VALID, name
        assert item.extraction_status in {
            ExtractionStatus.EXTRACTION_FAILED,
            ExtractionStatus.NO_TEXT_LAYER,
        }, name
        assert item.extracted_path is None, name
        assert item.warning is not None, name
        assert not list((fixture.root / "extracted").glob("*.md")), name

    # A heading plus filler is above the floor, so it is publishable -- but the
    # stub prefix alone is what the previous test rejects.  Both thresholds are
    # the recorded ones, never a test-local constant.
    fixture = acquired_fixture(tmp_path / "above_floor")
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry(
                {
                    ExtractionEngine.PYMUPDF: usable_engine(
                        "# paper.pdf\n\n" + ("placeholder " * 60)
                    )
                }
            )
        ).extract([fixture.request()])
    )
    item = outcome.data.item_outcomes[0]
    assert item.content_status is DocumentContentStatus.VALID
    assert (
        measure_extracted_body(
            parse_bound_frontmatter(fixture.extracted().read_bytes())[1]
        ).character_count
        >= DEFAULT_MINIMUM_CHARACTER_COUNT
    )


def test_e2_neg_016_partial_requires_an_explanation(tmp_path: Path) -> None:
    """A ``PARTIAL`` record without any degradation reason is rejected."""

    fixture = acquired_fixture(tmp_path)
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    record = fixture.sidecar(outcome).records[0]
    payload = _record_fields(record)
    # A clean single-engine run is the minimal self-consistent byte-bearing
    # record: requested == effective, one effective text-extracting attempt, and
    # no fallback chain, no degradation reasons, no attempt diagnostics.
    assert payload["content_status"] == DocumentContentStatus.VALID.value
    assert payload["fallback_chain"] == []
    assert payload["degradation_reasons"] == []
    assert not any(attempt["diagnostic_code"] for attempt in payload["attempts"])

    unexplained = {
        **payload,
        "extraction_status": ExtractionStatus.PARTIAL.value,
        "content_status": DocumentContentStatus.PARTIAL.value,
    }
    with pytest.raises(ValueError, match="PARTIAL content requires"):
        ExtractedDocumentRecord.model_validate(unexplained)

    # Positive control: the same record is accepted the moment a reason exists,
    # so the rejection above is about the missing explanation and nothing else.
    for reason in (
        {"degradation_reasons": ["ENGINE_SUBSTITUTED"]},
        {"attempts": [{**payload["attempts"][0], "diagnostic_code": "PAGES_PARTIAL"}]},
    ):
        ExtractedDocumentRecord.model_validate(
            {
                **unexplained,
                **reason,
            }
        )


def test_e2_neg_016b_real_fallback_run_is_partial_with_a_reason(tmp_path: Path) -> None:
    """The real degraded path produces exactly the record shape the rule wants."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry(
        {
            ExtractionEngine.DOCLING: failing_engine(ExtractionEngine.DOCLING),
            ExtractionEngine.PYMUPDF: usable_engine(),
        }
    )
    outcome = asyncio.run(
        fixture.service(registry).extract(
            [
                fixture.request(
                    requested_engine=ExtractionEngine.DOCLING, allow_fallback=True
                )
            ]
        )
    )
    payload = _record_fields(fixture.sidecar(outcome).records[0])
    assert payload["content_status"] == DocumentContentStatus.PARTIAL.value
    assert payload["extraction_status"] == ExtractionStatus.PARTIAL.value
    assert payload["effective_engine"] == ExtractionEngine.PYMUPDF.value
    assert payload["degradation_reasons"]
    assert payload["fallback_chain"]
    assert payload["fallback_chain"][0]["engine"] == ExtractionEngine.DOCLING.value
    # It validates as published: the real run is not a schema near-miss.
    ExtractedDocumentRecord.model_validate(payload)


def test_e2_neg_030_replayed_degraded_commit_reports_partial_not_valid(
    tmp_path: Path,
) -> None:
    """A replayed degraded commit is ``REUSED``/``PARTIAL``, never ``VALID``.

    Covers the E2-NEG-030 limb of E2-NEG-016 (PARTIAL always carries a reason):
    the run above is real, not a fixture shortcut, so replaying it is the
    strongest available check that the ``REUSED`` projection preserves the
    recorded degradation.  Section 6.7 rule 6 derives ``content_status`` from the
    recorded reasons, and section 6.1 reserves the byte-bearing statuses, so the
    committed record, the candidate document, and the sidecar all stay ``PARTIAL``
    while the sidecar still verifies against its own bytes.
    """

    fixture = acquired_fixture(tmp_path)
    audit = InMemoryAuditSink()
    service = fixture.service(
        EngineRegistry(
            {
                ExtractionEngine.DOCLING: failing_engine(ExtractionEngine.DOCLING),
                ExtractionEngine.PYMUPDF: usable_engine(),
            }
        ),
        audit_sink=audit,
    )
    request = fixture.request(
        requested_engine=ExtractionEngine.DOCLING, allow_fallback=True
    )

    first = asyncio.run(service.extract([request]))
    assert first.status is OperationStatus.SUCCESS
    committed = first.data.item_outcomes[0]
    assert committed.content_status is DocumentContentStatus.PARTIAL

    second = asyncio.run(service.extract([request]))
    assert second.status is OperationStatus.SUCCESS
    assert second.data.committed_count == 1
    row = second.data.item_outcomes[0]
    assert row.extraction_status is ExtractionStatus.REUSED
    assert row.content_status is DocumentContentStatus.PARTIAL
    assert row.degradation_reasons == ["ENGINE_SUBSTITUTED"]
    assert row.extracted_path == committed.extracted_path

    manifest = fixture.sidecar(second)
    record = manifest.records[0]
    assert record.extraction_status is ExtractionStatus.PARTIAL
    assert record.content_status is DocumentContentStatus.PARTIAL
    assert record.effective_engine == ExtractionEngine.PYMUPDF.value
    assert record.degradation_reasons == ["ENGINE_SUBSTITUTED"]

    candidate = second.data.candidate
    assert isinstance(candidate, DocumentManifestCandidate)
    assert candidate.payload["data"]["documents"][0]["content_status"] == (
        DocumentContentStatus.PARTIAL.value
    )
    assert candidate.payload_sha256 == first.data.candidate.payload_sha256
    service.verify_manifest(manifest, verify_bytes=True)
    assert len(audit.events) == 1, "a replay is not a new decision"


def test_e2_neg_039_needs_ocr_requires_a_reason_and_carries_no_path(
    tmp_path: Path,
) -> None:
    """``NEEDS_OCR`` is a recorded detection, never a path and never evidence."""

    fixture = acquired_fixture(tmp_path)
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: no_text_layer_engine()})
        ).extract([fixture.request()])
    )
    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.NO_TEXT_LAYER
    assert item.content_status is DocumentContentStatus.NEEDS_OCR
    assert item.warning is not None
    assert item.warning.code == "NO_EXTRACTABLE_TEXT_LAYER"
    assert item.extracted_path is None
    assert item.extraction_method is not None
    assert outcome.data.candidate is None, "no bytes means no candidate"
    manifest = fixture.sidecar(outcome)
    assert manifest.records == []
    assert manifest.item_outcomes[0].attempts[0].text_layer_present is False
    # Nothing on disk may be cited as extracted evidence for this document.
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_014_mixed_set_keeps_a_truthful_method_and_invents_no_path(
    tmp_path: Path,
) -> None:
    """The mixed-set blocker: three real outcomes, one candidate, no fabrication.

    Limb A -- a VALID sibling, a FAILED sibling that attempted a provider, and a
    NEEDS_OCR sibling that attempted no provider all appear in one candidate set;
    the two non-byte-bearing rows keep a frozen ``extraction_method`` and omit
    ``extracted_path`` entirely.

    Limb B -- a FAILED/NEEDS_OCR row that carries a path is rejected, and the
    projection type cannot even express one.
    """

    workspace = make_study_workspace(tmp_path, ["STU-valid", "STU-fail", "STU-ocr"])
    sources = {
        "STU-valid": pdf_bytes("V"),
        "STU-fail": pdf_bytes("F"),
        "STU-ocr": pdf_bytes("O"),
    }
    acquired = acquire_studies(workspace, sources)

    class MarkerEngine(FakeEngine):
        """Dispatches on the fixture's own PDF metadata, so it stays hermetic."""

        def extract(
            self,
            data: bytes,
            *,
            grobid_url: str | None = None,
            page_range: str | None = None,
        ) -> EngineExtractionResult:
            self.calls.append({"byte_length": len(data)})
            marker = "F" if b"F" * 64 in data else "O" if b"O" * 64 in data else "V"
            if marker == "F":
                raise EngineFailure(
                    FallbackReason.ENGINE_ERROR, "ENGINE_ERROR", "fixture failure"
                )
            return EngineExtractionResult(
                text="" if marker == "O" else ("Real findings follow. " * 40),
                output_format=ExtractionOutputFormat.MARKDOWN,
                page_count=1,
                text_layer_present=marker != "O",
            )

    registry = EngineRegistry(
        {
            ExtractionEngine.PYMUPDF: MarkerEngine(
                ExtractionEngine.PYMUPDF, version_value="9.9.9"
            ),
            ExtractionEngine.GROBID: failing_engine(
                ExtractionEngine.GROBID, message="connection refused"
            ),
        }
    )

    rows: dict[str, Any] = {}
    records: list[ExtractedDocumentRecord] = []
    for index, (study_id, engine) in enumerate(
        (
            ("STU-valid", ExtractionEngine.PYMUPDF),
            ("STU-fail", ExtractionEngine.GROBID),
            ("STU-ocr", ExtractionEngine.PYMUPDF),
        )
    ):
        manifest, record = acquired[study_id]
        updates: dict[str, Any] = {
            "run_id": f"RUN-extraction-{index}",
            "requested_engine": engine.value,
            "allow_fallback": False,
        }
        if engine is ExtractionEngine.GROBID:
            updates["grobid_url"] = "http://127.0.0.1:8070/api/processFulltextDocument"
        outcome = asyncio.run(
            workspace.service(registry).extract(
                [workspace.extraction_request(study_id, manifest, record, **updates)]
            )
        )
        rows[study_id] = outcome.data.item_outcomes[0]
        records.extend(fixture_sidecar(workspace, outcome).records)

    assert rows["STU-valid"].content_status is DocumentContentStatus.VALID
    assert rows["STU-valid"].extracted_path is not None
    assert rows["STU-fail"].content_status is DocumentContentStatus.FAILED
    assert rows["STU-fail"].extracted_path is None
    assert rows["STU-ocr"].content_status is DocumentContentStatus.NEEDS_OCR
    assert rows["STU-ocr"].extracted_path is None
    # A provider attempt is recorded for the failed row, so the method is
    # EXTERNAL_PROVIDER; the NEEDS_OCR row never reached a provider, so its
    # determination is a deterministic rule.
    assert rows["STU-fail"].extraction_method == "EXTERNAL_PROVIDER"
    assert rows["STU-ocr"].extraction_method == "DETERMINISTIC_RULE"
    assert len(records) == 1, "only the VALID row owns bytes"

    projections = [
        ArtifactRecordProjection.from_outcome(rows["STU-fail"]),
        ArtifactRecordProjection.from_outcome(rows["STU-ocr"]),
    ]
    screening_parent = workspace.parents[1]
    candidate = build_document_manifest_candidate(
        workspace_id=workspace.workspace_id,
        run_id="RUN-extraction-mixed",
        protocol_fingerprint=PROTOCOL,
        corpus_fingerprint=workspace.parents[0].corpus_fingerprint,
        screening_parent=screening_parent,
        producer=workspace.producer,
        records=records,
        non_committed=projections,
    )
    documents = {
        entry["document_id"]: entry for entry in candidate.payload["data"]["documents"]
    }
    assert len(documents) == 3, "a failed sibling is never suppressed"
    for document in documents.values():
        assert document["extraction_method"] in {
            "HUMAN",
            "DETERMINISTIC_RULE",
            "HEURISTIC",
            "LLM",
            "EXTERNAL_PROVIDER",
            "COMPOSED",
        }
    failed = documents[rows["STU-fail"].document_id]
    ocr = documents[rows["STU-ocr"].document_id]
    assert failed["content_status"] == DocumentContentStatus.FAILED.value
    assert ocr["content_status"] == DocumentContentStatus.NEEDS_OCR.value
    assert "extracted_path" not in failed
    assert "extracted_path" not in ocr
    # Only the optional path key is ever absent; identity and method stay.
    assert set(failed) == set(DOCUMENT_RECORD_KEYS) - set(OPTIONAL_DOCUMENT_RECORD_KEYS)

    # Limb B: the fabricated path is rejected in both available spellings.
    forged = json.loads(json.dumps(candidate.payload))
    for document in forged["data"]["documents"]:
        if document["content_status"] in {"FAILED", "NEEDS_OCR"}:
            document["extracted_path"] = "extracted/fabricated.md"
    with pytest.raises(CandidateContractError) as excinfo:
        validate_document_manifest_candidate(
            DocumentManifestCandidate(
                artifact_id=forged["artifact_id"],
                payload=forged,
                payload_sha256=canonical_fingerprint(forged),
            ),
            screening_parent=screening_parent,
        )
    assert "must omit extracted_path" in str(excinfo.value)
    # And the projection type has no field with which to claim a path at all.
    assert "extracted_path" not in ArtifactRecordProjection.model_fields
    # A byte-bearing record may not be minted for a determined failure: the real
    # committed record is relabelled FAILED while still claiming an
    # ``extracted_path``, which is the exact forgery a permissive kit would emit.
    merged = {
        **records[0].model_dump(mode="json"),
        "extraction_status": ExtractionStatus.EXTRACTION_FAILED.value,
        "content_status": DocumentContentStatus.FAILED.value,
    }
    assert merged["extraction_status"] == ExtractionStatus.EXTRACTION_FAILED.value
    assert merged["content_status"] == DocumentContentStatus.FAILED.value
    assert "extracted_path" in merged
    with pytest.raises(ValueError, match="only EXTRACTED/PARTIAL/REUSED records"):
        ExtractedDocumentRecord.model_validate(merged)


def fixture_sidecar(workspace: StudyWorkspace, outcome: Any) -> ExtractionManifest:
    """Read back the sidecar a committed outcome published."""

    reference = outcome.data.manifest_reference
    assert reference is not None, "this fixture requires a committed sidecar"
    return ExtractionManifest.model_validate_json(
        (workspace.root / reference.workspace_relative_path).read_text(encoding="utf-8")
    )


def test_e2_neg_014_mixed_batch_partially_commits_and_replays_without_re_extracting(
    tmp_path: Path,
) -> None:
    """A mixed batch is a PARTIAL envelope that explains itself, and replays clean.

    Covers the E2-NEG-014 mixed-batch limb, E2-NEG-030 (structured outcomes), and
    the E2-010 / E2-POS-002 exact-rerun rule.  One ``extract`` call over two
    studies committed by a *single* E1 manifest: ``STU-a`` produces usable text,
    ``STU-b`` fails deterministically.  The batch is therefore ``PARTIAL``, the
    envelope must carry the failed sibling's diagnostic (its row warning is the
    only explanation the row owns), the candidate must keep both documents, and
    exactly one sidecar and one audit event may exist.  The identical rerun must
    reuse that published commit without touching an engine again.
    """

    workspace = make_study_workspace(tmp_path, ["STU-a", "STU-b"])
    acquired = acquire_studies(
        workspace, {"STU-a": pdf_bytes("A"), "STU-b": pdf_bytes("B")}
    )
    manifest_a, record_a = acquired["STU-a"]
    manifest_b, record_b = acquired["STU-b"]
    # One E1 commit holding both records is what makes the mixed batch legal: a
    # batch shares one acquisition manifest, one run, and one requested engine.
    assert manifest_a.manifest_id == manifest_b.manifest_id

    class MixedEngine(FakeEngine):
        """Commits STU-a and fails STU-b, dispatching on the fixture's own bytes."""

        def extract(
            self,
            data: bytes,
            *,
            grobid_url: str | None = None,
            page_range: str | None = None,
        ) -> EngineExtractionResult:
            self.calls.append({"byte_length": len(data)})
            if b"B" * 64 in data:
                raise EngineFailure(
                    FallbackReason.ENGINE_ERROR,
                    "ENGINE_ERROR",
                    "the fixture engine cannot read this document",
                )
            return EngineExtractionResult(
                text="Real findings follow. " * 40,
                output_format=ExtractionOutputFormat.MARKDOWN,
                page_count=1,
                text_layer_present=True,
            )

    engine = MixedEngine(ExtractionEngine.PYMUPDF, version_value="7.7.7")
    audit = InMemoryAuditSink()
    service = workspace.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: engine}),
        audit_sink=audit,
        fallback_order=("pymupdf",),
    )
    requests = [
        workspace.extraction_request(
            "STU-a", manifest_a, record_a, document_ids=[record_a.document_id]
        ),
        workspace.extraction_request(
            "STU-b", manifest_b, record_b, document_ids=[record_b.document_id]
        ),
    ]

    first = asyncio.run(service.extract(requests))

    # A mixed batch is PARTIAL, never a silent success and never a crash: the
    # envelope reports the committed subset *and* the diagnostic that explains it.
    assert first.status is OperationStatus.PARTIAL
    assert first.data.committed_count == 1
    assert first.data.requested_count == 2
    rows = {row.study_id: row for row in first.data.item_outcomes}
    assert set(rows) == {"STU-a", "STU-b"}, "a failed sibling is never suppressed"
    assert rows["STU-a"].extraction_status is ExtractionStatus.EXTRACTED
    assert rows["STU-a"].content_status is DocumentContentStatus.VALID
    failed = rows["STU-b"]
    assert failed.extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert failed.content_status is DocumentContentStatus.FAILED
    assert failed.extracted_path is None
    assert failed.warning is not None
    assert failed.warning.code == "ENGINE_OUTPUT_UNUSABLE"
    # The envelope explains the degradation; the service never returns an outcome
    # its own envelope validator would reject after the sidecar was committed.
    assert any(error.code == "ENGINE_OUTPUT_UNUSABLE" for error in first.errors), [
        error.code for error in first.errors
    ]

    # The candidate is a complete account of the batch: the FAILED document is
    # present and carries no fabricated path.
    candidate = first.data.candidate
    assert isinstance(candidate, DocumentManifestCandidate)
    documents = {
        entry["document_id"]: entry for entry in candidate.payload["data"]["documents"]
    }
    assert set(documents) == {record_a.document_id, record_b.document_id}
    assert documents[record_a.document_id]["content_status"] == (
        DocumentContentStatus.VALID.value
    )
    assert documents[record_a.document_id]["extracted_path"] == (
        f"extracted/{record_a.document_id}.md"
    )
    assert documents[record_b.document_id]["content_status"] == (
        DocumentContentStatus.FAILED.value
    )
    assert "extracted_path" not in documents[record_b.document_id]

    # Exactly one sidecar, one audit event, and a sidecar that round-trips.
    sidecars = sorted(
        (workspace.root / "literature" / "extraction").rglob("EXT-*.json")
    )
    assert len(sidecars) == 1
    manifest = ExtractionManifest.model_validate_json(
        sidecars[0].read_text(encoding="utf-8")
    )
    assert manifest.operation.status == OperationStatus.PARTIAL.value
    assert [record.document_id for record in manifest.records] == [record_a.document_id]
    service.verify_manifest(manifest, verify_bytes=True)
    assert len(audit.events) == 1

    # The identical rerun: no engine call, no second sidecar, no second event, and
    # a byte-identical candidate.  Section 7.4 -- an exact replay is REUSED.
    calls_after_first = len(engine.calls)
    second = asyncio.run(service.extract(requests))
    assert len(engine.calls) == calls_after_first, "an exact replay re-runs no engine"
    assert second.status is OperationStatus.PARTIAL
    assert second.data.committed_count == 1
    assert second.data.requested_count == 2
    replayed = {row.study_id: row for row in second.data.item_outcomes}
    assert replayed["STU-a"].extraction_status is ExtractionStatus.REUSED
    assert replayed["STU-a"].content_status is DocumentContentStatus.VALID
    # A document that owns no bytes cannot be REUSED -- section 6.1 reserves the
    # byte-bearing statuses for a committed record -- so the determined failure is
    # re-reported verbatim: same status, same warning, still no path.
    assert replayed["STU-b"].extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert replayed["STU-b"].content_status is DocumentContentStatus.FAILED
    assert replayed["STU-b"].warning is not None
    assert replayed["STU-b"].warning.code == "ENGINE_OUTPUT_UNUSABLE"
    assert replayed["STU-b"].extracted_path is None
    assert second.data.manifest_reference == first.data.manifest_reference
    assert (
        len(list((workspace.root / "literature" / "extraction").rglob("EXT-*.json")))
        == 1
    )
    assert len(audit.events) == 1
    assert second.data.candidate is not None
    assert second.data.candidate.payload == candidate.payload
    assert second.data.candidate.payload_sha256 == candidate.payload_sha256
    service.verify_manifest(manifest, verify_bytes=True)


def test_e2_neg_018d_repaired_mixed_batch_supersedes_the_failed_sibling(
    tmp_path: Path,
) -> None:
    """Limb D of E2-NEG-018: a repair rerun of a *byte-bearing* commit succeeds.

    ``neg_018c`` pins the all-failure successor, where the first commit anchored no
    ``EXT-`` identity.  This is the same section 6.7(9) successor over the harder
    starting point the reviewer named: the first run committed ``STU-a`` and
    *determined* ``STU-b``, so the published sidecar owns bytes and the failed
    sibling sits beside them.  Section 7.4 decides that run by the recomputed
    ``EXT-`` id, not by the record count, so after the engine is repaired the
    rerun must mint a **new** id rather than report the mixed commit as
    ``REUSED``: the caller asked again precisely because the old commit was
    incomplete for one document.

    Three properties are load-bearing and asserted together:

    * the repaired sibling is re-driven, so the engine *is* called -- and only for
      that sibling, because ``STU-a``'s commit is final and is carried;
    * the new sidecar holds exactly one byte-bearing record per document and keeps
      the superseded failure row, referenced by the prior ``EXT-`` id, so the
      recovery is auditable rather than a silent overwrite;
    * a third identical run is the exact replay section 7.4 describes: no engine
      call, no second sidecar, no second audit event.

    The repair is *observable* because a real repair is.  Section 7.4 forbids
    re-running an engine to rediscover a replay, so a rerun may only re-drive a
    determined failure when the environment it failed in is no longer the
    environment it is in.  This fake flips both of the facts the service can probe
    without running it -- the engine now resolves, and it reports a new version --
    exactly as a provider that came back up or a library that was upgraded would,
    and the service compares those probes against the attempt provenance the
    published sidecar already records.
    """

    workspace = make_study_workspace(tmp_path, ["STU-a", "STU-b"])
    acquired = acquire_studies(
        workspace, {"STU-a": pdf_bytes("A"), "STU-b": pdf_bytes("B")}
    )
    manifest_a, record_a = acquired["STU-a"]
    manifest_b, record_b = acquired["STU-b"]
    assert manifest_a.manifest_id == manifest_b.manifest_id

    class RepairableEngine(FakeEngine):
        """``STU-b`` is unreadable until ``repair()``; state, never a sleep."""

        version_value: str = "1.0.0"
        online: bool = False

        def repair(self) -> None:
            self.online = True
            self.version_value = "1.0.1"

        def version(self) -> str:
            return self.version_value

        def extract(
            self,
            data: bytes,
            *,
            grobid_url: str | None = None,
            page_range: str | None = None,
        ) -> EngineExtractionResult:
            self.calls.append({"byte_length": len(data)})
            if b"B" * 64 in data and not self.online:
                raise EngineFailure(
                    FallbackReason.ENGINE_UNAVAILABLE,
                    "ENGINE_UNAVAILABLE",
                    "the fixture engine cannot read this document",
                )
            return EngineExtractionResult(
                text="Real findings follow. " * 40,
                output_format=ExtractionOutputFormat.MARKDOWN,
                page_count=1,
                text_layer_present=True,
            )

    engine = RepairableEngine(ExtractionEngine.PYMUPDF)
    audit = InMemoryAuditSink()
    service = workspace.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: engine}),
        audit_sink=audit,
        fallback_order=("pymupdf",),
    )
    requests = [
        workspace.extraction_request(
            "STU-a", manifest_a, record_a, document_ids=[record_a.document_id]
        ),
        workspace.extraction_request(
            "STU-b", manifest_b, record_b, document_ids=[record_b.document_id]
        ),
    ]
    sidecar_root = workspace.root / "literature" / "extraction"

    # Run 1: the mixed commit the repair will supersede -- one byte-bearing record
    # beside one determined failure.
    first = asyncio.run(service.extract(requests))
    assert first.status is OperationStatus.PARTIAL
    assert first.data.committed_count == 1
    first_reference = first.data.manifest_reference
    assert first_reference is not None
    first_manifest = fixture_sidecar(workspace, first)
    assert [record.document_id for record in first_manifest.records] == [
        record_a.document_id
    ]
    published_first = sorted(sidecar_root.rglob("EXT-*.json"))
    assert len(published_first) == 1
    first_bytes = published_first[0].read_bytes()
    assert len(audit.events) == 1
    calls_after_first = len(engine.calls)
    assert calls_after_first == 2, "both siblings ran once"

    # The engine is repaired and the *identical* set is re-issued.
    engine.repair()
    second = asyncio.run(service.extract(requests))

    # The engine ran again, and only for the document that had no bytes: the
    # carried sibling must not be re-extracted, or a repair would silently
    # re-run work whose commit is final (section 7.5).
    assert len(engine.calls) == calls_after_first + 1, (
        "the repair rerun re-drives exactly the determined sibling"
    )

    # A new ``EXT-`` id under the same key: neither ``REUSED`` nor a conflict.
    assert second.status is OperationStatus.SUCCESS
    assert second_reference_id(second) is not None
    assert second_reference_id(second) != first_reference.manifest_id
    second_reference = second.data.manifest_reference
    assert second_reference is not None
    second_manifest = fixture_sidecar(workspace, second)
    assert second_manifest.idempotency_key == first_manifest.idempotency_key
    assert second_manifest.manifest_id != first_manifest.manifest_id

    # Exactly one byte-bearing record per document: the repaired sibling gained a
    # record, the carried sibling did not gain a second one.
    assert sorted(record.document_id for record in second_manifest.records) == sorted(
        [record_a.document_id, record_b.document_id]
    )
    committed_b = next(
        record
        for record in second_manifest.records
        if record.document_id == record_b.document_id
    )
    assert committed_b.extraction_status is ExtractionStatus.EXTRACTED
    assert committed_b.extracted_path == f"extracted/{record_b.document_id}.md"
    carried_a = next(
        record
        for record in second_manifest.records
        if record.document_id == record_a.document_id
    )
    assert carried_a.extracted_sha256 == first_manifest.records[0].extracted_sha256

    # The superseded failure stays visible, referenced by the prior ``EXT-`` id.
    superseded = [
        row
        for row in second_manifest.item_outcomes
        if row.is_superseded and row.document_id == record_b.document_id
    ]
    assert len(superseded) == 1
    assert superseded[0].prior_outcome_manifest_id == first_reference.manifest_id
    assert superseded[0].prior_outcome_status is ExtractionStatus.EXTRACTION_FAILED
    assert superseded[0].extracted_path is None
    # The envelope reports the current truth only: two requested, two committed.
    assert second.data.requested_count == 2
    assert second.data.committed_count == 2
    assert {row.study_id for row in second.data.item_outcomes} == {"STU-a", "STU-b"}
    candidate = second.data.candidate
    assert isinstance(candidate, DocumentManifestCandidate)
    assert len(candidate.payload["data"]["documents"]) == 2

    # Both sidecars are on disk, both verify, the earlier one is byte-identical,
    # and the successor added exactly one audit event.
    published_second = sorted(sidecar_root.rglob("EXT-*.json"))
    assert len(published_second) == 2
    service.verify_manifest(first_manifest, verify_bytes=True)
    service.verify_manifest(second_manifest, verify_bytes=True)
    assert published_first[0].read_bytes() == first_bytes
    assert len(audit.events) == 2, "one event per committed manifest, no duplicate"

    # Run 3, with the repair in place: the exact replay.  No engine call, no
    # second sidecar, no second event, and the same published commit reported.
    calls_after_second = len(engine.calls)
    third = asyncio.run(service.extract(requests))
    assert len(engine.calls) == calls_after_second, "an exact replay re-runs no engine"
    assert third.data.manifest_reference == second_reference
    assert sorted(sidecar_root.rglob("EXT-*.json")) == published_second
    assert len(audit.events) == 2
    assert {row.extraction_status for row in third.data.item_outcomes} == {
        ExtractionStatus.REUSED
    }
    service.verify_manifest(second_manifest, verify_bytes=True)


def test_e2_neg_018e_a_repair_that_recovers_nothing_republishes_nothing(
    tmp_path: Path,
) -> None:
    """Limb E of E2-NEG-018: a repair rerun that recovers nothing stays a replay.

    ``neg_018d`` is the recovery.  This is the other side of the same decision, and
    it is the case that would silently break a key if the successor path were
    written as "any re-drive mints a new ``EXT-`` id": the provider comes back up
    but the document still will not parse, at the *same* engine version.  Nothing
    about any document changed, so the published commit is still the current truth
    for both of them and is re-reported -- one sidecar, one audit event, no
    ``IDEMPOTENCY_CONFLICT`` on the next rerun.

    The reason this is not a coin flip: a second same-key sidecar whose failure
    row is not superseded by anything would leave two unsuperseded commits under
    one key, and every later replay of that key would be genuinely ambiguous.  The
    key must stay usable after any number of failed repairs.
    """

    workspace = make_study_workspace(tmp_path, ["STU-a", "STU-b"])
    acquired = acquire_studies(
        workspace, {"STU-a": pdf_bytes("A"), "STU-b": pdf_bytes("B")}
    )
    manifest_a, record_a = acquired["STU-a"]
    manifest_b, record_b = acquired["STU-b"]
    assert manifest_a.manifest_id == manifest_b.manifest_id

    class StillBrokenEngine(FakeEngine):
        """Availability and the ability to read STU-b are separate facts."""

        reachable: bool = False
        reads_b: bool = False

        def repair(self) -> None:
            self.reachable = True

        def extract(
            self,
            data: bytes,
            *,
            grobid_url: str | None = None,
            page_range: str | None = None,
        ) -> EngineExtractionResult:
            self.calls.append({"byte_length": len(data)})
            if b"B" * 64 in data and not self.reads_b:
                raise EngineFailure(
                    FallbackReason.ENGINE_UNAVAILABLE,
                    "ENGINE_UNAVAILABLE",
                    "the fixture engine cannot read this document",
                )
            return EngineExtractionResult(
                text="Real findings follow. " * 40,
                output_format=ExtractionOutputFormat.MARKDOWN,
                page_count=1,
                text_layer_present=True,
            )

    engine = StillBrokenEngine(ExtractionEngine.PYMUPDF, version_value="7.7.7")
    audit = InMemoryAuditSink()
    service = workspace.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: engine}),
        audit_sink=audit,
        fallback_order=("pymupdf",),
    )
    requests = [
        workspace.extraction_request(
            "STU-a", manifest_a, record_a, document_ids=[record_a.document_id]
        ),
        workspace.extraction_request(
            "STU-b", manifest_b, record_b, document_ids=[record_b.document_id]
        ),
    ]
    sidecar_root = workspace.root / "literature" / "extraction"

    first = asyncio.run(service.extract(requests))
    assert first.status is OperationStatus.PARTIAL
    first_reference = first.data.manifest_reference
    assert first_reference is not None
    first_manifest = fixture_sidecar(workspace, first)
    published_first = sorted(sidecar_root.rglob("EXT-*.json"))
    assert len(published_first) == 1
    first_bytes = published_first[0].read_bytes()
    assert len(audit.events) == 1
    calls_after_first = len(engine.calls)
    assert calls_after_first == 2

    # The provider is reachable again, at the same version; the document is still
    # unreadable.  The rerun re-drives the determined sibling -- the environment it
    # failed in is not the environment it is in -- and finds the same truth.
    engine.repair()
    second = asyncio.run(service.extract(requests))
    assert len(engine.calls) == calls_after_first + 1, (
        "the determined sibling is re-driven once the provider is reachable"
    )

    # Nothing recovered, so nothing is republished: the same manifest id, the same
    # single sidecar, byte-identical, and no second audit event.
    assert second.status is OperationStatus.PARTIAL
    second_reference = second.data.manifest_reference
    assert second_reference is not None
    assert second_reference == first_reference
    second_manifest = fixture_sidecar(workspace, second)
    assert second_manifest.manifest_id == first_manifest.manifest_id
    assert [record.document_id for record in second_manifest.records] == [
        record_a.document_id
    ]
    rows = {row.study_id: row for row in second.data.item_outcomes}
    assert rows["STU-a"].extraction_status is ExtractionStatus.REUSED
    assert rows["STU-b"].extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert rows["STU-b"].extracted_path is None
    assert not any(row.is_superseded for row in second_manifest.item_outcomes)
    published_second = sorted(sidecar_root.rglob("EXT-*.json"))
    assert published_second == published_first
    assert published_second[0].read_bytes() == first_bytes
    assert len(audit.events) == 1
    service.verify_manifest(first_manifest, verify_bytes=True)

    # The key is still usable.  The determined sibling is re-driven on every
    # rerun -- the commit is frozen, so the record of the environment it failed in
    # never clears, and a re-drive of an unrecoverable sibling is a bounded retry
    # of one document, never a silent replay -- but it republishes nothing and
    # never trips an ambiguity the service created for itself.
    calls_after_second = len(engine.calls)
    third = asyncio.run(service.extract(requests))
    assert third.status is OperationStatus.PARTIAL
    assert len(engine.calls) == calls_after_second + 1, (
        "a rerun retries the unrecoverable sibling once, and only that sibling"
    )
    assert third.data.manifest_reference == first_reference
    assert third.data.committed_count == 1
    third_rows = {row.study_id: row for row in third.data.item_outcomes}
    assert third_rows["STU-a"].extraction_status is ExtractionStatus.REUSED
    assert third_rows["STU-b"].extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert sorted(sidecar_root.rglob("EXT-*.json")) == published_first
    assert len(audit.events) == 1
    # The envelope still explains the failure it is reporting, as 7.4 requires of
    # a replay: a re-reported PARTIAL is not a silent success.
    assert [error.code for error in third.errors] == ["ENGINE_OUTPUT_UNUSABLE"]


# ---------------------------------------------------------------------------
# E2-NEG-018 -- idempotency conflict and successor accounting
# ---------------------------------------------------------------------------


def test_e2_neg_018_idempotency_conflict_never_overwrites_the_earlier_manifest(
    tmp_path: Path,
) -> None:
    """Limb A of E2-NEG-018: the same key with a different payload is a conflict.

    Packet 7.4 fixes the idempotency key to the workspace, run, acquisition
    reference, and the ``{document_id, source_sha256, requested_engine}`` set, so
    the reachable "changed non-volatile payload under an existing key" is a
    changed *page range* or *provider endpoint* -- inputs that change the work and
    its bytes but are deliberately outside that key.  The test-ID table's
    parenthetical ("different requested engine, different document set") names
    inputs that 7.4 puts *into* the key, so those mint a different key and a new
    ``EXT-`` id; limb B pins that behaviour instead of faking a conflict.
    """

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    service = fixture.service(registry)
    first = asyncio.run(service.extract([fixture.request()]))
    assert first.status is OperationStatus.SUCCESS
    committed = fixture.manifest_path() / (
        f"{first.data.manifest_reference.manifest_id}.json"
    )
    before = committed.read_bytes()
    before_manifest = fixture.sidecar(first)
    assert before_manifest.idempotency_key == first.provenance["idempotency_key"]

    # Limb A: same key (page range is not in it), different non-volatile input.
    for changed in (
        fixture.request(page_range="1-1"),
        fixture.request(grobid_url="http://127.0.0.1:9999/api"),
    ):
        second = asyncio.run(service.extract([changed]))
        assert second.status is OperationStatus.FAILED
        assert second.data.manifest_reference is None, "a conflict writes nothing"
        assert second.data.candidate is None
        assert any(error.code == "IDEMPOTENCY_CONFLICT" for error in second.errors), [
            error.code for error in second.errors
        ]
        item = second.data.item_outcomes[0]
        assert item.error is not None
        assert item.error.code == "IDEMPOTENCY_CONFLICT"
        assert item.extraction_status is ExtractionStatus.FAILED
        # The earlier manifest is byte-identical, still the only one, and it
        # still verifies against its own committed bytes.
        assert committed.read_bytes() == before
        assert fixture.sidecars() == [committed]
        service.verify_manifest(before_manifest, verify_bytes=True)


def test_e2_neg_018b_a_different_engine_is_a_different_key_not_a_conflict(
    tmp_path: Path,
) -> None:
    """Limb B of E2-NEG-018: a changed request-side document set mints a new key.

    A different requested engine is not a conflict either: it is a different
    request, so it mints its own key and its own ``EXT-`` identity.  A conflict is
    reserved for a changed payload *under* one key.

    Packet 7.4 puts ``requested_engine`` *inside* the key, so a different engine
    is a different key rather than "the same key with a changed payload".  The
    storage contract then decides: authoritative extraction storage is addressed
    by document identity (7.5), and the identity-addressed path already holds
    the first engine's body.  The sanctioned outcome is 7.5's "conflict-safe
    explicit result" -- an explicit per-item failure, a fail-closed sidecar, and a
    published body that is never removed by the failed refresh.
    """

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry(
        {
            ExtractionEngine.PYMUPDF: usable_engine(),
            ExtractionEngine.DOCLING: usable_engine(markdown="Other findings. " * 40),
        }
    )
    service = fixture.service(registry)
    first = asyncio.run(service.extract([fixture.request()]))
    published_body = fixture.extracted().read_bytes()
    first_manifest = fixture.sidecar(first)

    second = asyncio.run(
        service.extract([fixture.request(requested_engine=ExtractionEngine.DOCLING)])
    )
    second_manifest = fixture.sidecar(second)

    assert first.status is OperationStatus.SUCCESS
    assert first_manifest.records[0].requested_engine == ExtractionEngine.PYMUPDF.value

    # A different key, therefore a different manifest id and its own sidecar.
    assert first_manifest.idempotency_key != second_manifest.idempotency_key
    assert first_manifest.manifest_id != second_manifest.manifest_id
    assert len(fixture.sidecars()) == 2
    assert not any(error.code == "IDEMPOTENCY_CONFLICT" for error in second.errors), [
        error.code for error in second.errors
    ]

    # The conflict is explicit, per item, and pre-record.
    assert second.status is OperationStatus.FAILED
    assert second.data.candidate is None
    assert second_manifest.records == []
    item = second.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.FAILED
    assert item.error is not None
    assert item.error.code == "CONTENT_COLLISION"
    assert item.stage is ExtractionStage.PROMOTION
    assert item.document_id == first_manifest.records[0].document_id

    # Section 7.5: a valid existing output is never removed by a failed refresh.
    assert fixture.extracted().read_bytes() == published_body
    service.verify_manifest(first_manifest, verify_bytes=True)
    service.verify_manifest(second_manifest, verify_bytes=True)


def test_e2_neg_018c_repaired_rerun_publishes_a_successor_and_keeps_the_prior_row(
    tmp_path: Path,
) -> None:
    """Limb C of E2-NEG-018: recovery is additive and every prior row accounted for.

    A first run that fails publishes a sidecar with no bytes.  When the engine is
    repaired, the rerun commits a *new* manifest whose ``item_outcomes`` retains
    the superseded failure row bound to the prior manifest id, and the envelope
    reports the current truth only -- the superseded row is not counted as a
    requested document and not handed back as if it were current.

    Named ``neg_018c`` so it cannot be confused with limb A (``neg_018``,
    same-key/different-payload conflict), limb B (``neg_018b``, a different
    requested engine minting a different key), limb D (``neg_018d``, a recovery
    over an already byte-bearing commit) or limb E (``neg_018e``, a recovery that
    recovers nothing).  The starting point is what distinguishes this limb: only a
    sidecar that owns *no* records has to re-drive the whole batch, because with
    no record there is nothing to carry and nothing to reproduce its ``EXT-`` id.
    A byte-bearing commit takes the same successor path as soon as a determined
    sibling is re-driven and recovers (limb D), and a byte-bearing commit whose
    documents all hold bytes is the ordinary ``REUSED`` replay.  A byte-bearing
    commit is never superseded *as a whole*: only the document that recovered gains
    a record, and every document keeps exactly one byte-bearing record.
    """

    fixture = acquired_fixture(tmp_path)
    audit = InMemoryAuditSink()
    failed_registry = EngineRegistry(
        {ExtractionEngine.PYMUPDF: failing_engine(message="engine offline")}
    )
    first = asyncio.run(
        fixture.service(failed_registry, audit_sink=audit).extract([fixture.request()])
    )
    assert first.status is OperationStatus.FAILED
    first_reference = first.data.manifest_reference
    assert first_reference is not None
    first_manifest = fixture.sidecar(first)
    assert first_manifest.records == []
    assert first_manifest.operation.status == OperationStatus.FAILED.value

    repaired_registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    second = asyncio.run(
        fixture.service(repaired_registry, audit_sink=audit).extract(
            [fixture.request()]
        )
    )

    assert second.status is OperationStatus.SUCCESS
    second_manifest = fixture.sidecar(second)
    assert second_reference_id(second) != first_reference.manifest_id
    assert len(second_manifest.records) == 1
    assert second_manifest.records[0].extraction_status is ExtractionStatus.EXTRACTED
    rows = {
        (row.study_id, row.document_id): row for row in second_manifest.item_outcomes
    }
    superseded = rows[
        (
            first_manifest.item_outcomes[0].study_id,
            first_manifest.item_outcomes[0].document_id,
        )
    ]
    assert superseded.is_superseded is True
    assert superseded.prior_outcome_manifest_id == first_reference.manifest_id
    assert superseded.prior_outcome_status is ExtractionStatus.EXTRACTION_FAILED
    assert superseded.extracted_path is None
    assert superseded.extraction_method is not None
    # The envelope reports the current truth only.
    assert [row.document_id for row in second.data.item_outcomes] == [
        row.document_id for row in second.data.item_outcomes if not row.is_superseded
    ]
    assert second.data.requested_count == 1, "a superseded row is not a request"
    assert second.data.committed_count == 1
    assert second_manifest.operation.status == OperationStatus.SUCCESS.value
    # One audit event per committed manifest, no duplicates.
    committed = {
        str(event["parameters"]["manifest_id"])
        for event in audit.events
        if event.get("parameters", {}).get("manifest_id")
    }
    assert first_reference.manifest_id in committed
    assert second.data.manifest_reference is not None
    assert second.data.manifest_reference.manifest_id in committed
    assert len(audit.events) == 2
    # Both sidecars still verify on disk.
    fixture.service(repaired_registry).verify_manifest(
        first_manifest, verify_bytes=True
    )
    fixture.service(repaired_registry).verify_manifest(
        second_manifest, verify_bytes=True
    )


def second_reference_id(outcome: Any) -> str | None:
    reference = outcome.data.manifest_reference
    return None if reference is None else reference.manifest_id


# ---------------------------------------------------------------------------
# E2-NEG-019/024/025/027/030/036/046 -- boundary, containment, parity
# ---------------------------------------------------------------------------


def test_e2_neg_019_unbound_raw_path_extraction_is_non_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy bare-path command claims nothing: no sidecar, no candidate.

    The raw engine is stubbed so the test needs neither docling nor a GROBID
    service: the point is that the legacy surface *does* write text and still
    claims nothing authoritative about it.
    """

    class LegacyEngine:
        @staticmethod
        def extract_markdown(pdf: Path, output_dir: Path) -> Path:
            output_dir.mkdir(parents=True, exist_ok=True)
            destination = output_dir / f"{pdf.stem}.md"
            destination.write_text(
                "# Raw legacy text\n\nNo lineage, no identity, no parent.\n",
                encoding="utf-8",
            )
            return destination

    monkeypatch.setattr(cli, "DoclingEngine", LegacyEngine)
    output = tmp_path / "raw-output"
    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(REAL_PDF_FIXTURE),
            "--output",
            str(output),
            "--engine",
            "docling",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "non-authoritative" in result.output
    produced = sorted(path.name for path in output.glob("*.md"))
    assert produced, "the legacy path still extracts text"
    for name in produced:
        raw = (output / name).read_text(encoding="utf-8")
        # No acquisition binding, no E2 identity, and no acceptance claim.
        for forbidden in (
            "acquisition_manifest_id",
            "acquisition_manifest_sha256",
            "document_id",
            "source_sha256",
            "contract_acceptance",
        ):
            assert forbidden not in raw, forbidden
    # Nothing authoritative was written anywhere: no sidecar, no E1 manifest, no
    # identity-addressed output, and no Contract artifact id.
    assert not list(tmp_path.rglob("EXT-*.json"))
    assert not list(tmp_path.rglob("ACQ-*.json"))
    assert not list(tmp_path.rglob("extracted"))
    assert "ART-" not in result.output
    # The declaration is part of the surface, not just of this run's output.
    help_text = CliRunner().invoke(app, ["extract", "--help"]).output
    assert "NON-AUTHORITATIVE" in help_text
    assert "extract-run" in help_text
    # And the authoritative surface is a different command with a real contract.
    assert (
        "ExtractionRunConfig"
        in CliRunner().invoke(app, ["extract-run", "--help"]).output
    )


def test_e2_neg_024_escaping_storage_prefix_is_refused_before_any_filesystem_work(
    tmp_path: Path,
) -> None:
    """``..``, absolute, drive-letter, and separator prefixes never reach a write.

    The portable-path rule on the request refuses an escaping prefix before the
    service is involved, which is the strongest possible containment: there is no
    read and no write to undo.
    """

    fixture = acquired_fixture(tmp_path)
    for storage_prefix in ("../escape", "extracted/../../escape", "nested/../../.."):
        with pytest.raises(ValueError, match="path must remain inside the workspace"):
            fixture.request(storage_prefix=storage_prefix)
    # Separator tricks, drive letters, and absolute paths are refused as
    # non-portable before they can be resolved against any root.
    for storage_prefix, message in (
        ("..\\escape", "non-empty workspace-relative POSIX path"),
        ("C:/escape", "must not be absolute or contain a drive prefix"),
        ("/absolute/escape", "must not be absolute or contain a drive prefix"),
        ("", "non-empty workspace-relative POSIX path"),
    ):
        with pytest.raises(ValueError, match=message):
            fixture.request(storage_prefix=storage_prefix)
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.rglob("escape*"))


def test_e2_neg_024b_symlinked_storage_parent_cannot_escape_the_root(
    tmp_path: Path,
) -> None:
    """A lexically in-root prefix whose parent is a symlink out is refused."""

    fixture = acquired_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = fixture.root / "linked"
    try:
        os.symlink(outside, linked, target_is_directory=True)
    except OSError as error:  # pragma: no cover - platform capability
        pytest.skip(f"symlink creation is unavailable: {error}")
    engine = usable_engine()
    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [fixture.request(storage_prefix="linked")]
        )
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {
        "PATH_OUTSIDE_WORKSPACE",
        "SIDECAR_DIRECTORY_INVALID",
        "WORKSPACE_ROOT_UNAVAILABLE",
    }
    assert engine.calls == [], "an escaping destination is refused before any write"
    assert not list(outside.iterdir()), "nothing is written outside the root"


def test_e2_neg_024c_unvalidated_escaping_or_non_regular_destination_fails_closed(
    tmp_path: Path,
) -> None:
    """The containment limb that needs no symlink privilege to be meaningful.

    Covers the E2-NEG-024 service-level limb.  The limb above proves the request
    *model* refuses an escaping prefix; this one proves the *service* fails closed
    when a hostile caller never went through the model -- a hand-built request, a
    legacy adapter, a model that gained a field later.  ``model_copy`` is used
    deliberately as that hostile-caller simulation: the portable-path rule is
    bypassed on purpose, so the anchor check is the only thing standing between a
    ``../``, absolute, drive-letter, or UNC prefix and a write outside the
    canonical root, plus a destination that already exists as a non-regular file.
    Every limb must report ``OperationStatus.FAILED`` with a ``None`` candidate and
    leave no write outside the root, on any platform, without a symlink.

    This limb is also the *portable* no-publication proof for E2-NEG-024, and it
    is the only one: the symlinked variants are ``pytest.skip``ped wherever
    creating a symlink needs a privilege (Windows), so on those platforms the
    escaping-prefix loop below is what actually pins the semantic -- a containment
    violation is a request-level refusal, refused in preflight (section 7.3(1)),
    so it mints no ``EXT-`` sidecar, no ``manifest_reference``, no artifact
    checksum, and never enters the idempotency key space.  The last limb is
    deliberately *not* a refusal: a destination that already exists as a
    non-regular file is a determined failure of a valid request, so it keeps its
    fail-closed sidecar and is asserted separately below.
    """

    escaping_prefixes = (
        "../escape",
        "nested/../../escape",
        "/absolute/escape",
        "C:/escape",
        "//server/share/escape",
    )
    for index, storage_prefix in enumerate(escaping_prefixes):
        fixture = acquired_fixture(tmp_path / f"escaping-{index}")
        engine = usable_engine()
        outcome = asyncio.run(
            fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
                [
                    fixture.request().model_copy(
                        update={"storage_prefix": storage_prefix}
                    )
                ]
            )
        )
        assert outcome.status is OperationStatus.FAILED, storage_prefix
        assert outcome.data.candidate is None, storage_prefix
        # A containment violation is refused, not determined: no sidecar is
        # minted, so there is no manifest reference to hand back.  The symlinked
        # limbs are skipped without the privilege to create a symlink, so this
        # assertion is the regression-proof on every platform.
        assert outcome.data.manifest_reference is None, storage_prefix
        assert not fixture.sidecars(), storage_prefix
        assert outcome.data.committed_count == 0, storage_prefix
        item = outcome.data.item_outcomes[0]
        assert item.extraction_status is ExtractionStatus.FAILED, storage_prefix
        assert item.extracted_path is None, storage_prefix
        assert item.error is not None, storage_prefix
        assert item.error.code in {
            "PATH_OUTSIDE_WORKSPACE",
            "SIDECAR_DIRECTORY_INVALID",
            "WORKSPACE_ROOT_UNAVAILABLE",
        }, item.error.code
        # Refused in preflight, so the request is invalid before any extraction
        # attempt: no engine ever saw the verified bytes.
        assert engine.calls == [], storage_prefix
        assert not list((fixture.root / "extracted").rglob("*.md")), storage_prefix

    # A pre-existing non-regular destination cannot be replaced or written through.
    fixture = acquired_fixture(tmp_path / "occupied")
    record = fixture.record()
    destination = fixture.root / "extracted" / f"{record.document_id}.md"
    destination.mkdir(parents=True)
    (destination / "occupied").write_text("occupied", encoding="utf-8")
    engine = usable_engine()
    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [fixture.request()]
        )
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.candidate is None
    # The counter-shape of the refusal above, and the reason the refusal guard is
    # not "any failure": this request is valid, the engine ran, and the write was
    # refused at the commit anchor.  That is a *determined* failure, so it keeps
    # its fail-closed zero-byte sidecar exactly as an all-engine-failure batch
    # does (E2-NEG-018c/E2-NEG-018e).  Only a batch whose every outcome is a
    # request-level refusal publishes nothing.
    assert outcome.data.manifest_reference is not None
    assert len(fixture.sidecars()) == 1
    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.FAILED
    assert item.error is not None
    assert item.error.code in {
        "FILESYSTEM_ERROR",
        "PATH_OUTSIDE_WORKSPACE",
        "SIDECAR_DIRECTORY_INVALID",
    }, item.error.code
    assert (destination / "occupied").read_text(encoding="utf-8") == "occupied"
    assert not list(destination.glob("*.md"))

    # Nothing escaped, on any limb: no sibling of the root and no trace of the
    # absolute, drive-letter, or UNC destinations.
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "absolute").exists()
    assert not (tmp_path / "nested").exists()
    assert not list(tmp_path.rglob("escape*"))
    assert not list(Path(fixture.root.anchor or "/").glob("server"))
    assert len(engine.calls) == 1, "the write was refused at the commit anchor"


def test_e2_neg_025_cross_workspace_extraction_is_rejected(tmp_path: Path) -> None:
    """A parent bound to another workspace cannot authorize this extraction."""

    fixture = acquired_fixture(tmp_path)
    outsider = make_study_workspace(
        tmp_path / "outsider", ["STU-one"], workspace_id="WSP-other"
    )
    foreign_screening = outsider.parents[1]

    service = PDFExtractionService(
        accepted_parents=[foreign_screening],
        workspace_bindings={"WSP-test": fixture.binding},
        producer=fixture.producer,
        audit_sink=InMemoryAuditSink(),
        engines=EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()}),
    )
    outcome = asyncio.run(service.extract([fixture.request()]))

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {
        "PARENT_BINDING_MISMATCH",
        "PARENT_NOT_ACCEPTED",
        "WORKSPACE_BINDING_MISMATCH",
    }
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_026_two_studies_identical_text_produce_two_identities(
    tmp_path: Path,
) -> None:
    """Text is not identity: identical bytes in two studies stay two documents."""

    workspace = make_study_workspace(tmp_path, ["STU-one", "STU-two"])
    payload = pdf_bytes("same")
    acquired = acquire_studies(workspace, {"STU-one": payload, "STU-two": payload})
    left, right = acquired["STU-one"][1], acquired["STU-two"][1]
    assert left.source_sha256 == right.source_sha256
    assert left.document_id != right.document_id
    assert (
        deterministic_document_id(
            study_id="STU-one",
            source_hash=left.source_sha256,
            workspace_id=workspace.workspace_id,
            algorithm_version="v1",
        )
        == left.document_id
    )

    outcomes = {}
    records = {}
    for index, study_id in enumerate(("STU-one", "STU-two")):
        manifest, record = acquired[study_id]
        outcome = asyncio.run(
            workspace.service(
                EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
            ).extract(
                [
                    workspace.extraction_request(
                        study_id,
                        manifest,
                        record,
                        run_id=f"RUN-extraction-{index}",
                    )
                ]
            )
        )
        assert outcome.status is OperationStatus.SUCCESS
        outcomes[study_id] = outcome
        records[study_id] = fixture_sidecar(workspace, outcome).records[0]

    assert records["STU-one"].document_id != records["STU-two"].document_id
    assert records["STU-one"].extracted_path != records["STU-two"].extracted_path
    assert (workspace.root / records["STU-one"].extracted_path).read_text(
        encoding="utf-8"
    ).split("---", 2)[-1] == (
        workspace.root / records["STU-two"].extracted_path
    ).read_text(encoding="utf-8").split("---", 2)[-1], (
        "the same bytes yield the same body"
    )
    # The two runs are different manifests with different identities, and each
    # candidate names its own document -- text is not identity.
    for study_id, outcome in outcomes.items():
        candidate = outcome.data.candidate
        assert isinstance(candidate, DocumentManifestCandidate)
        document_ids = {
            entry["document_id"] for entry in candidate.payload["data"]["documents"]
        }
        assert document_ids == {
            next(
                entry["document_id"]
                for entry in candidate.payload["data"]["documents"]
                if entry["study_id"] == study_id
            )
        }, "each run's candidate holds only its own study"
    # Identical text is not identity: the two candidates name two documents, so
    # their payload digests must differ even though the extracted body does not.
    assert records["STU-one"].extracted_sha256 == records["STU-two"].extracted_sha256, (
        "identical text yields the same body hash"
    )
    assert (
        outcomes["STU-one"].data.candidate.payload_sha256
        != outcomes["STU-two"].data.candidate.payload_sha256
    ), "two identities never collapse into one candidate payload"
    assert (
        outcomes["STU-one"].data.candidate.artifact_id
        != outcomes["STU-two"].data.candidate.artifact_id
    )


def test_e2_neg_027_unknown_study_is_rejected_before_emission(
    tmp_path: Path,
) -> None:
    """A study absent from the accepted lineage never reaches an engine."""

    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [fixture.request(study_id="STU-ghost")]
        )
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.data.candidate is None
    assert engine.calls == []
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code in {
        "NO_DOCUMENTS_SELECTED",
        "DOCUMENT_NOT_IN_ACQUISITION_MANIFEST",
        "UNKNOWN_DOCUMENT_STUDY",
    }
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_030_api_and_cli_report_the_same_operation_and_candidate(
    tmp_path: Path,
) -> None:
    """The standalone API and the CLI agree on status, rows, errors, and candidate.

    The CLI is driven through the same ``ExtractionRunConfig`` a caller would
    write, with the real local engine over the real fixture PDF, so neither side
    of the comparison depends on a scripted engine.
    """

    payload = REAL_PDF_FIXTURE.read_bytes()
    api_workspace = make_study_workspace(tmp_path / "api", ["STU-real"])
    api_acquired = acquire_studies(api_workspace, {"STU-real": payload})
    api_manifest, api_record = api_acquired["STU-real"]
    api_request = api_workspace.extraction_request("STU-real", api_manifest, api_record)
    api_config = ExtractionRunConfig(
        requests=[api_request],
        accepted_parents=api_workspace.parents,
        workspace_bindings={api_workspace.workspace_id: api_workspace.binding},
        producer=api_workspace.producer,
    )
    api_outcome = asyncio.run(
        PDFExtractionService.from_config(
            api_config, audit_sink=InMemoryAuditSink()
        ).extract(api_config.requests)
    )
    assert api_outcome.status is OperationStatus.SUCCESS

    cli_workspace = make_study_workspace(tmp_path / "cli", ["STU-real"])
    cli_acquired = acquire_studies(cli_workspace, {"STU-real": payload})
    cli_manifest, cli_record = cli_acquired["STU-real"]
    cli_config = ExtractionRunConfig(
        requests=[
            cli_workspace.extraction_request("STU-real", cli_manifest, cli_record)
        ],
        accepted_parents=cli_workspace.parents,
        workspace_bindings={cli_workspace.workspace_id: cli_workspace.binding},
        producer=cli_workspace.producer,
    )
    config_path = tmp_path / "extraction-config.json"
    config_path.write_text(cli_config.model_dump_json(), encoding="utf-8")
    logger_path = tmp_path / "log_event.py"
    write_logger_script(logger_path)

    result = CliRunner().invoke(
        app,
        [
            "extract-run",
            str(config_path),
            "--audit-logger",
            str(logger_path),
            "--human",
        ],
    )
    assert result.exit_code == 0, result.output
    # ``--human`` appends a rendered table after the JSON envelope, so the
    # envelope is read as the leading JSON document and the table is asserted
    # separately: the two must not be confused with one another.
    cli_payload, end = json.JSONDecoder().raw_decode(result.stdout)
    remainder = result.stdout[end:]
    assert "NON-AUTHORITATIVE" in remainder, "the human table states the boundary"
    assert result.stdout.count("pdf-extraction-operation-v1") == 1
    assert cli_payload["status"] == api_outcome.status.value
    assert cli_payload["data"]["committed_count"] == api_outcome.data.committed_count
    assert cli_payload["data"]["requested_count"] == api_outcome.data.requested_count
    assert cli_payload["errors"] == [
        error.model_dump(mode="json") for error in api_outcome.errors
    ]
    assert cli_payload["warnings"] == api_outcome.warnings
    api_item = api_outcome.data.item_outcomes[0]
    cli_item = cli_payload["data"]["item_outcomes"][0]
    assert cli_item["extraction_status"] == api_item.extraction_status.value
    assert cli_item["content_status"] == api_item.content_status.value
    assert cli_item["extraction_method"] == api_item.extraction_method
    assert cli_item["extracted_path"].endswith(api_item.extracted_path)
    # Each surface reports *its own* commit truthfully, and the two provenance
    # projections have the same key set.  The checksums cannot be equal across
    # two different workspaces: the manifest identity binds workspace and run.
    api_sidecar = fixture_sidecar(api_workspace, api_outcome)
    cli_sidecar = fixture_sidecar(
        cli_workspace, ExtractionBatchOutcome.model_validate(cli_payload)
    )
    assert set(cli_payload["provenance"]) == set(api_outcome.provenance)
    assert (
        cli_payload["provenance"]["manifest_checksum"] == cli_sidecar.artifact_checksum
    )
    assert api_outcome.provenance["manifest_checksum"] == api_sidecar.artifact_checksum
    assert cli_payload["provenance"]["manifest_id"] == cli_sidecar.manifest_id
    assert api_outcome.provenance["manifest_id"] == api_sidecar.manifest_id
    assert cli_payload["provenance"]["idempotency_key"] == (cli_sidecar.idempotency_key)
    # The candidate is reported as a non-authoritative candidate on both sides,
    # and neither surface exposes an accepted-artifact reference.
    assert cli_payload["provenance"]["contract_acceptance"] == "not_performed_by_kit"
    assert cli_payload["data"]["candidate"]["contract_acceptance"] == (
        CONTRACT_ACCEPTANCE_NOT_PERFORMED
    )
    assert api_outcome.data.candidate is not None
    assert (
        api_outcome.data.candidate.payload_sha256
        == (cli_payload["data"]["candidate"]["payload_sha256"])
    )
    assert "accepted_artifact" not in json.dumps(cli_payload)
    assert result.stdout.count("extraction_method") >= 1, "--human must render"


def test_e2_neg_036_fingerprint_disagreement_fails_closed(tmp_path: Path) -> None:
    """A protocol/corpus fingerprint mismatch is never reconciled silently."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    for updates in (
        {"protocol_fingerprint": "sha256:" + "3" * 64},
        {"corpus_fingerprint": "sha256:" + "4" * 64},
    ):
        outcome = asyncio.run(
            fixture.service(registry).extract([fixture.request(**updates)])
        )
        assert outcome.status is OperationStatus.FAILED, updates
        assert outcome.data.manifest_reference is None, updates
        assert outcome.data.candidate is None, updates
        item = outcome.data.item_outcomes[0]
        assert item.error is not None, updates
        assert item.error.code in {
            "PARENT_BINDING_MISMATCH",
            "ACQUISITION_CONTEXT_MISMATCH",
            "METADATA_CONFLICT",
            "PARENT_LINEAGE_MISMATCH",
        }, updates
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_046_relocated_sidecar_is_never_adopted_as_the_commit(
    tmp_path: Path,
) -> None:
    """A sidecar outside its deterministic in-root path is not a replay source.

    Section 7.4 makes the deterministic in-root path the only commit marker: a
    copy of a valid sidecar sitting anywhere else is an orphan, never a replay
    source and never authority.  Recovery therefore re-derives the same identity
    from the deterministic key and republishes it in place, and a *corrupt* copy
    elsewhere cannot influence the run at all.
    """

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    service = fixture.service(registry)
    first = asyncio.run(service.extract([fixture.request()]))
    reference = first.data.manifest_reference
    assert reference is not None
    committed = fixture.root / reference.workspace_relative_path
    relocated = fixture.root / "literature" / "extraction" / "elsewhere.json"
    relocated.parent.mkdir(parents=True, exist_ok=True)
    relocated.write_text(committed.read_text(encoding="utf-8"), encoding="utf-8")
    # The stray copy is not even self-consistent: it must never be read.
    payload = json.loads(relocated.read_text(encoding="utf-8"))
    payload["producer"]["version"] = "0.0.0-stray"
    relocated.write_text(json.dumps(payload), encoding="utf-8")
    committed.unlink()

    replay = asyncio.run(fixture.service(registry).extract([fixture.request()]))

    assert replay.status is OperationStatus.SUCCESS
    assert replay.data.candidate is not None
    assert replay.data.committed_count == 1
    item = replay.data.item_outcomes[0]
    # Not a replay: the stray copy was not the commit marker, so the engine ran
    # again and the status is the real current one, never REUSED.
    assert item.extraction_status is not ExtractionStatus.REUSED
    assert item.extraction_status is ExtractionStatus.EXTRACTED
    recovered = fixture.sidecar(replay)
    assert recovered.manifest_id == reference.manifest_id, (
        "the deterministic identity recovers the same EXT- id"
    )
    assert recovered.producer.version != "0.0.0-stray", (
        "the stray bytes are not adopted"
    )
    assert fixture.sidecars() == [committed]
    service.verify_manifest(recovered, verify_bytes=True)
    # The orphan stays an orphan: it was never promoted or deleted by the run.
    assert relocated.is_file()
    assert json.loads(relocated.read_text(encoding="utf-8"))["producer"]["version"] == (
        "0.0.0-stray"
    )


def test_e2_neg_046b_symlinked_sidecar_candidate_is_rejected(tmp_path: Path) -> None:
    """A symlinked sidecar at the identity path cannot be adopted."""

    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    first = asyncio.run(fixture.service(registry).extract([fixture.request()]))
    committed = fixture.root / first.data.manifest_reference.workspace_relative_path
    body = committed.read_bytes()
    committed.unlink()
    outside = tmp_path / "outside" / committed.name
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(body)
    try:
        os.symlink(outside, committed)
    except OSError as error:  # pragma: no cover - platform capability
        pytest.skip(f"symlink creation is unavailable: {error}")

    outcome = asyncio.run(fixture.service(registry).extract([fixture.request()]))

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.candidate is None
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code == "PATH_OUTSIDE_WORKSPACE"
    assert not list(outside.parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
# E2-NEG-033/040/041/042 -- binding, mutation, fail-closed accounting
# ---------------------------------------------------------------------------


def test_e2_neg_033_extracted_frontmatter_is_bound_to_the_sidecar(
    tmp_path: Path,
) -> None:
    """Frontmatter identity, the sidecar, and the body checksum must agree."""

    fixture = acquired_fixture(tmp_path)
    service = fixture.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    )
    outcome = asyncio.run(service.extract([fixture.request()]))
    manifest = fixture.sidecar(outcome)
    record = manifest.records[0]
    extracted = fixture.extracted()

    values, parsed = parse_bound_frontmatter(extracted.read_bytes())
    body = measure_extracted_body(parsed).body
    assert (
        values["extracted_sha256"]
        == "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    ), "the body checksum binds the exact emitted body"
    assert values["document_id"] == record.document_id
    assert values["source_sha256"] == record.source_sha256

    # A frontmatter value that disagrees with the sidecar is not authoritative.
    tampered = extracted.read_text(encoding="utf-8").replace(
        record.document_id, "DOC-" + "2" * 32
    )
    extracted.write_text(tampered, encoding="utf-8")
    with pytest.raises(ExtractionCommitError) as excinfo:
        service.verify_manifest(manifest, verify_bytes=True)
    assert excinfo.value.code in {
        "REUSED_CONTENT_INVALID",
        "EXTRACTED_FILE_MUTATED",
        "EXTRACTED_CONTENT_MISMATCH",
    }


def test_e2_neg_017_replay_recomputes_and_a_changed_body_is_never_republished(
    tmp_path: Path,
) -> None:
    """E2-NEG-017: exact replay matches; only the clock may differ on re-extract.

    Section 6.6 keeps ``extracted_at`` as "provenance only; never in the identity
    payload", so a second *fresh* run over the same bytes recomputes the same
    ``EXT-`` id and the same body digest while its file bytes would differ.  It
    must therefore coalesce onto the file already committed (section 7.5) instead
    of colliding with it -- and a genuinely changed body must still be refused.
    """

    fixture = acquired_fixture(tmp_path)
    service = fixture.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    )
    first = asyncio.run(service.extract([fixture.request()]))
    first_manifest = fixture.sidecar(first)
    committed_bytes = fixture.extracted().read_bytes()

    # A *different* run id forces a real re-extraction instead of a replay.
    fresh = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request(run_id="RUN-second")])
    )
    fresh_manifest = fixture.sidecar(fresh)

    assert fresh.status is OperationStatus.SUCCESS
    # A distinct run publishes its own run-addressed sidecar, but the extraction
    # identity -- the identity-addressed artifact -- is recomputed identically.
    assert fresh_manifest.manifest_id != first_manifest.manifest_id
    assert fresh_manifest.records[0].extracted_path == (
        first_manifest.records[0].extracted_path
    )
    assert fresh_manifest.records[0].extracted_sha256 == (
        first_manifest.records[0].extracted_sha256
    )
    # The committed artifact is the one already on disk, byte for byte: the
    # record's file checksum describes reality rather than a rewritten file.
    assert fixture.extracted().read_bytes() == committed_bytes
    assert fresh_manifest.records[0].extracted_file_sha256 == (
        first_manifest.records[0].extracted_file_sha256
    )

    # A changed body under a committed path is refused, never re-published.
    divergent = asyncio.run(
        fixture.service(
            EngineRegistry(
                {
                    ExtractionEngine.PYMUPDF: usable_engine(
                        markdown="Divergent findings that are long enough to be usable text. "
                        * 40
                    )
                }
            )
        ).extract([fixture.request(run_id="RUN-third")])
    )
    assert divergent.status is OperationStatus.FAILED
    item = divergent.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code == "CONTENT_COLLISION"
    assert item.stage is ExtractionStage.PROMOTION
    assert fixture.extracted().read_bytes() == committed_bytes


def test_e2_neg_043_no_filename_url_or_regex_metadata_is_invented(
    tmp_path: Path,
) -> None:
    """E2-NEG-043: a heuristic title/DOI/workspace never becomes authoritative.

    The legacy `_pdf_metadata` path still guesses a title from the file stem, a
    DOI by regex over the stem, and a workspace id by regex over the path, and
    E2 does not remove that behaviour.  What E2 forbids is those guesses
    reaching an authoritative frontmatter, sidecar, or candidate, so the
    authoritative run is driven from a filename and a path that would each
    invite a guess and must still record none.
    """

    fixture = acquired_fixture(tmp_path / "SCI-42-some_paper-10.1000_zzz")
    source = fixture.root / fixture.record().workspace_relative_path
    assert source.stem == fixture.record().document_id, (
        "the stem must be identity-addressed"
    )

    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )

    assert outcome.status is OperationStatus.SUCCESS
    values, _body = parse_bound_frontmatter(fixture.extracted().read_bytes())
    # The declared workspace binds; nothing is read out of the directory name.
    assert values["workspace_id"] == "WSP-test"
    assert "SCI-42" not in values["workspace_id"]
    for heuristic in ("some_paper", "10.1000_zzz", "10.1000/zzz", "SCI-42"):
        assert heuristic not in str(values), heuristic
    assert not values.get("title"), "no caller-supplied title means no title"
    # The DOI is the E1 record's, never a regex guess read out of the stem.
    assert values["doi"] == fixture.record().normalized_doi
    assert values["doi"] != "10.1000/zzz"

    manifest = fixture.sidecar(outcome)
    record = manifest.records[0]
    # The record carries no bibliographic field of its own, so a heuristic
    # title or DOI has nowhere to hide there.
    assert not hasattr(record, "title")
    assert not hasattr(record, "doi")
    candidate = outcome.data.candidate
    assert candidate is not None
    payload = candidate.model_dump(mode="json")["payload"]
    assert "SCI-42" not in str(payload)
    assert "10.1000_zzz" not in str(payload)
    # The frozen `DocumentRecord` has no bibliographic field, so the candidate
    # carries identity and provenance only -- and no invented title or DOI.
    document = payload["data"]["documents"][0]
    assert set(document) <= set(DOCUMENT_RECORD_KEYS)
    assert "10.1000" not in str(document)


def test_e2_neg_040_committed_content_mutation_is_not_republished(
    tmp_path: Path,
) -> None:
    """Edited committed bytes are detected on replay and never re-published.

    Covers the E2-NEG-017 replay limb as well: a published sidecar whose
    committed body no longer verifies is a fail-closed replay *rejection*, not a
    silent re-extraction, and not an exception escaping the public API.
    """

    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    service = fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine}))
    first = asyncio.run(service.extract([fixture.request()]))
    manifest = fixture.sidecar(first)
    # Byte-mode, not text-mode: a text write would translate newlines and the
    # "restored" body would differ from the committed one on Windows.
    original = fixture.extracted().read_bytes()

    calls_after_first = len(engine.calls)
    truncated = fixture.extracted().read_bytes()
    fixture.extracted().write_bytes(truncated[: len(truncated) // 2])
    replay = asyncio.run(service.extract([fixture.request()]))

    assert len(engine.calls) == calls_after_first, "replay must not re-extract"
    # The mutated bytes fail closed.  This assertion is unconditional: the guard
    # it replaces was vacuous, because a body that no longer matches its recorded
    # checksum can never produce a SUCCESS rerun -- so "assert REUSED" was dead
    # code that asserted nothing.  The replay is refused instead.
    assert replay.status is OperationStatus.FAILED
    assert replay.data.manifest_reference is None
    assert replay.data.candidate is None
    item = replay.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.FAILED
    assert item.content_status is DocumentContentStatus.FAILED
    assert item.stage is ExtractionStage.PREFLIGHT
    assert item.extracted_path is None
    assert item.error is not None
    assert item.error.code == "REPLAY_VERIFICATION_FAILED"
    # The earlier commit is left exactly as it was: still one sidecar, still the
    # same manifest id, and the mutation is reported rather than adopted.
    assert (
        len(list((fixture.root / "literature" / "extraction").rglob("EXT-*.json"))) == 1
    )
    with pytest.raises(ExtractionCommitError) as excinfo:
        service.verify_manifest(manifest, verify_bytes=True)
    assert excinfo.value.code in {
        "REUSED_CONTENT_INVALID",
        "EXTRACTED_FILE_MUTATED",
        "EXTRACTED_CONTENT_MISMATCH",
    }

    # Restoring the bytes makes the same rerun an *unconditional* REUSED: the
    # published commit is adopted, with no engine call and no second sidecar.
    fixture.extracted().write_bytes(original)
    reused = asyncio.run(service.extract([fixture.request()]))
    assert reused.status is OperationStatus.SUCCESS
    assert reused.data.committed_count == 1
    assert reused.data.item_outcomes[0].extraction_status is ExtractionStatus.REUSED
    assert reused.data.manifest_reference == first.data.manifest_reference
    assert len(engine.calls) == calls_after_first
    assert (
        len(list((fixture.root / "literature" / "extraction").rglob("EXT-*.json"))) == 1
    )
    service.verify_manifest(manifest, verify_bytes=True)


def test_e2_neg_041_all_failure_batch_publishes_zero_records_and_failed_status(
    tmp_path: Path,
) -> None:
    """Every document failed: explicit rows, zero records, no empty success.

    Limb A of E2-NEG-041, and the E2-NEG-030 structured-outcome requirement the
    P0-A repair enforces: a batch that commits part of a request is ``PARTIAL`` and
    explains itself, and a batch that commits nothing is still an explicit, published
    ``FAILED`` sidecar rather than a silent absence.
    """

    fixture = acquired_fixture(tmp_path)
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: failing_engine()}),
            fallback_order=("pymupdf",),
        ).extract([fixture.request()])
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is not None, "failures must be explicit"
    assert outcome.data.candidate is None, "no bytes means no candidate"
    assert outcome.data.committed_count == 0
    manifest = fixture.sidecar(outcome)
    assert manifest.records == []
    assert manifest.operation.status == OperationStatus.FAILED.value
    assert manifest.item_outcomes[0].content_status is DocumentContentStatus.FAILED
    assert manifest.item_outcomes[0].extraction_method is not None
    # Distinguishable from a missing manifest: the sidecar exists and carries
    # the per-item diagnostics a caller needs.  Section 7.4 keeps a failed item
    # visible "with an explicit error or warning" -- an unusable body is reported
    # as a warning carrying the usefulness diagnostic -- so either limb is
    # acceptable here as long as the failure is never silent.
    item = manifest.item_outcomes[0]
    assert item.error is not None or item.warning is not None
    assert outcome.errors
    assert not list((fixture.root / "extracted").glob("*.md"))


def test_e2_neg_041b_cancelled_documents_are_accounted_not_authoritative(
    tmp_path: Path,
) -> None:
    """Limb B of E2-NEG-041: a cancelled item is accounted for, never authoritative.

    Named ``neg_041b`` to match limb A (``neg_041``, the all-failure batch): a
    cancelled run is still a published, explicitly reported outcome -- it claims no
    bytes, no candidate, no effective engine, and no extraction method, because an
    attempt that never ran is not a deterministic determination.
    """

    fixture = acquired_fixture(tmp_path)

    class CancellingEngine(FakeEngine):
        def extract(self, data, *, grobid_url=None, page_range=None):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError()

    outcome = asyncio.run(
        fixture.service(
            EngineRegistry(
                {ExtractionEngine.PYMUPDF: CancellingEngine(ExtractionEngine.PYMUPDF)}
            )
        ).extract([fixture.request()])
    )

    assert outcome.status is OperationStatus.CANCELLED
    assert outcome.data.committed_count == 0
    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.CANCELLED
    assert item.extracted_path is None
    # The engine never ran, so the outcome names no effective engine and claims
    # no extraction method: a cancelled attempt is not a deterministic one.
    assert item.effective_engine is None
    assert item.attempts == []
    assert item.error is not None
    assert item.error.code == "EXTRACTION_CANCELLED"
    assert outcome.data.candidate is None
    assert not list((fixture.root / "extracted").glob("*.md"))
    # A cancelled envelope is never silent.
    assert outcome.errors


def test_e2_neg_042_candidate_identity_is_stable_across_runs_and_input_order(
    tmp_path: Path,
) -> None:
    """The candidate id and payload digest do not move with run, root, or clock."""

    left_fixture = acquired_fixture(tmp_path / "left")
    right_fixture = acquired_fixture(tmp_path / "right")
    left = asyncio.run(
        left_fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([left_fixture.request()])
    )
    # A second, independent workspace: different root, different run, different
    # wall clock -- the candidate identity must not move.
    right = asyncio.run(
        right_fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([right_fixture.request()])
    )

    left_candidate = left.data.candidate
    right_candidate = right.data.candidate
    assert isinstance(left_candidate, DocumentManifestCandidate)
    assert isinstance(right_candidate, DocumentManifestCandidate)
    assert left_candidate.artifact_id == right_candidate.artifact_id
    assert left_candidate.payload_sha256 == right_candidate.payload_sha256
    assert left_candidate.artifact_id.startswith("ART-")
    assert left_candidate.artifact_type == "document_manifest"
    # The kit never labels its own candidate as accepted.
    assert left_candidate.contract_acceptance == CONTRACT_ACCEPTANCE_NOT_PERFORMED
    assert left_candidate.payload["producer"]["package"] == "scholar-pdf-kit"
    # created_at is inherited from the immutable screening parent, not now.
    assert left_candidate.payload["created_at"] == "2026-01-01T00:00:00Z"
    # inputs is exactly the accepted screening parent -- never the ACQ- manifest.
    assert left_candidate.payload["inputs"] == [
        {
            "artifact_id": "ART-screening",
            "sha256": left_fixture.parents[1].sha256,
        }
    ]


def test_e2_neg_028_engine_version_provenance_is_required(tmp_path: Path) -> None:
    """An attempt without a version, or a same-engine step claim, is rejected."""

    fixture = acquired_fixture(tmp_path)
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    payload = _record_fields(fixture.sidecar(outcome).records[0])
    assert payload["attempts"][0]["engine_version"]

    # An attempt may not omit its version.
    without_version = json.loads(json.dumps(payload))
    without_version["attempts"][0]["engine_version"] = ""
    with pytest.raises(ValueError):
        ExtractedDocumentRecord.model_validate(without_version)

    # A substituted step may not be claimed when nothing was substituted.
    same_engine_step = json.loads(json.dumps(payload))
    same_engine_step["fallback_chain"] = [
        {
            "engine": ExtractionEngine.PYMUPDF.value,
            "engine_version": "1.2.3",
            "reason": FallbackReason.ENGINE_ERROR.value,
        }
    ]
    with pytest.raises(ValueError, match="must not claim a substituted fallback"):
        ExtractedDocumentRecord.model_validate(same_engine_step)

    # The effective engine must be one of the recorded attempts.
    unattempted = json.loads(json.dumps(payload))
    unattempted["effective_engine"] = ExtractionEngine.DOCLING.value
    with pytest.raises(ValueError, match="one of the recorded attempts"):
        ExtractedDocumentRecord.model_validate(unattempted)
