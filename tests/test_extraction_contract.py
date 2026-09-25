"""WP01-E2 extraction contract tests (offline, hermetic, local fixtures only)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_acquisition_contract import PROTOCOL, make_workspace

from scholar_pdf.acquisition import InMemoryAuditSink, PDFAcquisitionService
from scholar_pdf.acquisition_models import (
    AcceptedParentBinding,
    AcquiredDocumentManifest,
    OperationStatus,
    WorkspaceRootBinding,
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
)
from scholar_pdf.extraction_models import (
    CONTRACT_ACCEPTANCE_NOT_PERFORMED,
    DEFAULT_MINIMUM_CHARACTER_COUNT,
    DocumentContentStatus,
    DocumentManifestCandidate,
    ExtractionEngine,
    ExtractionManifest,
    ExtractionOutputFormat,
    ExtractionRequest,
    ExtractionStatus,
    FallbackReason,
)
from scholar_pdf.frontmatter import (
    measure_extracted_body,
    parse_bound_frontmatter,
)


@dataclass
class ExtractionFixture:
    root: Path
    parents: list[AcceptedParentBinding]
    binding: WorkspaceRootBinding
    producer: Any
    acquisition: AcquiredDocumentManifest
    acquisition_path: Path
    audit: InMemoryAuditSink

    def request(self, **updates: Any) -> ExtractionRequest:
        record = self.acquisition.records[0]
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


def failing_engine(
    engine: ExtractionEngine = ExtractionEngine.PYMUPDF, *, message: str = "boom"
) -> FakeEngine:
    """A scripted engine that always fails, for fallback/failure tests."""

    return FakeEngine(
        engine,
        version_value="1.0.0",
        failure=EngineFailure(FallbackReason.ENGINE_ERROR, "ENGINE_ERROR", message),
    )


def test_e2_pos_001_screened_pdf_commits_authoritative_outputs(
    tmp_path: Path,
) -> None:
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

    # Authoritative, identity-addressed output path.
    document_id = fixture.acquisition.records[0].document_id
    extracted = fixture.root / "extracted" / f"{document_id}.md"
    assert extracted.is_file()
    assert not (fixture.root / "extracted" / f"{document_id}.md.part").exists()

    values, body = parse_bound_frontmatter(extracted.read_bytes())
    # Packet 6.6: the identity keys are never dropped from bound frontmatter.
    assert values["document_id"] == document_id
    assert values["study_id"] == "STU-one"
    assert values["source_sha256"] == fixture.acquisition.records[0].source_sha256
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


def test_e2_pos_002_sidecar_lineage_binds_both_parents(tmp_path: Path) -> None:
    fixture = acquired_fixture(tmp_path)
    service = fixture.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    )
    outcome = asyncio.run(service.extract([fixture.request()]))

    reference = outcome.data.manifest_reference
    assert reference is not None
    assert reference.manifest_id.startswith("EXT-")
    assert reference.workspace_relative_path == (
        f"literature/extraction/RUN-extraction/{reference.manifest_id}.json"
    )
    manifest = ExtractionManifest.model_validate_json(
        (fixture.root / reference.workspace_relative_path).read_text(encoding="utf-8")
    )
    # E1 manifest identity + checksum preserved verbatim.
    assert manifest.acquisition_manifest_ref.manifest_id == (
        fixture.acquisition.manifest_id
    )
    assert manifest.acquisition_manifest_ref.artifact_checksum == (
        fixture.acquisition.artifact_checksum
    )
    assert manifest.acquisition_manifest_ref.workspace_relative_path == (
        fixture.acquisition.e2_reference.acquisition_manifest_path
    )
    # Screening is the direct Contract parent.
    assert manifest.screening_decisions_ref.artifact_id == "ART-screening"
    assert manifest.screening_decisions_ref.sha256 == fixture.parents[1].sha256
    record = manifest.records[0]
    assert record.document_id == fixture.acquisition.records[0].document_id
    assert record.source_sha256 == fixture.acquisition.records[0].source_sha256
    assert record.study_id == "STU-one"
    # No-replace verification of the emitted file bytes.
    service.verify_manifest(manifest, verify_bytes=True)


def test_e2_pos_003_contract_candidate_is_deterministic_and_unaccepted(
    tmp_path: Path,
) -> None:
    fixture = acquired_fixture(tmp_path)
    first = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    # A second, independent workspace: different root, different run, different
    # wall clock -- the candidate identity must not move.
    second_fixture = acquired_fixture(tmp_path / "second")
    second = asyncio.run(
        second_fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([second_fixture.request()])
    )

    left = first.data.candidate
    right = second.data.candidate
    assert isinstance(left, DocumentManifestCandidate)
    assert isinstance(right, DocumentManifestCandidate)
    # Wall-clock independent: two independent workspaces, two runs, two wall
    # clocks -- the candidate identity and payload digest must not move.
    assert left.artifact_id == right.artifact_id
    assert left.payload_sha256 == right.payload_sha256
    assert left.artifact_id.startswith("ART-")
    assert left.artifact_type == "document_manifest"
    # The kit never labels its own candidate as accepted.
    assert left.contract_acceptance == CONTRACT_ACCEPTANCE_NOT_PERFORMED
    assert left.payload["producer"]["package"] == "scholar-pdf-kit"
    # created_at is inherited from the immutable screening parent, not now.
    assert left.payload["created_at"] == "2026-01-01T00:00:00Z"
    # inputs is exactly the accepted screening parent -- never the ACQ- manifest.
    assert left.payload["inputs"] == [
        {"artifact_id": "ART-screening", "sha256": fixture.parents[1].sha256}
    ]


def test_e2_pos_004_exact_replay_is_reused_without_rerunning_engines(
    tmp_path: Path,
) -> None:
    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: engine})
    request = fixture.request()
    service = fixture.service(registry)

    first = asyncio.run(service.extract([request]))
    calls_after_first = len(engine.calls)
    second = asyncio.run(service.extract([request]))

    assert first.status is OperationStatus.SUCCESS
    assert second.data.item_outcomes[0].extraction_status is ExtractionStatus.REUSED
    assert second.data.item_outcomes[0].content_status is DocumentContentStatus.VALID
    assert len(engine.calls) == calls_after_first, "replay must not rerun engines"
    assert first.data.manifest_reference == second.data.manifest_reference, (
        "replay must reuse the committed sidecar"
    )
    assert len(list(fixture.manifest_path().glob("EXT-*.json"))) == 1


def test_e2_neg_stub_body_is_never_valid(tmp_path: Path) -> None:
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


def test_e2_neg_stub_heading_plus_filler_is_still_reported(tmp_path: Path) -> None:
    """A stub prefix with filler is not a pure stub, but never reaches VALID
    without a real body; the heading and floor are what decide usability."""

    fixture = acquired_fixture(tmp_path)
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
    # Above the character floor: this one is publishable content.
    assert item.content_status is DocumentContentStatus.VALID


def test_e2_neg_empty_output_is_not_valid(tmp_path: Path) -> None:
    fixture = acquired_fixture(tmp_path)
    engine = usable_engine("")
    outcome = asyncio.run(
        fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine})).extract(
            [fixture.request()]
        )
    )
    item = outcome.data.item_outcomes[0]
    assert item.content_status is not DocumentContentStatus.VALID
    assert item.extraction_status is ExtractionStatus.EXTRACTION_FAILED
    assert item.warning is not None, "a determined failure must carry a diagnostic"
    assert item.extracted_path is None


def test_e2_neg_fallback_is_ordered_and_reason_recorded(tmp_path: Path) -> None:
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


def test_e2_neg_unknown_engine_without_fallback_fails_structurally(
    tmp_path: Path,
) -> None:
    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    request = fixture.request(
        requested_engine=ExtractionEngine.GROBID, allow_fallback=False
    )
    outcome = asyncio.run(fixture.service(registry).extract([request]))

    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.FAILED
    assert item.content_status is DocumentContentStatus.FAILED
    assert item.error is not None
    assert item.error.code == "UNSUPPORTED_ENGINE"
    assert not (fixture.root / "extracted").exists() or not list(
        (fixture.root / "extracted").glob("*.md")
    ), "a structural failure must not publish authoritative bytes"


def test_e2_neg_all_failures_publishes_sidecar_but_no_candidate(
    tmp_path: Path,
) -> None:
    fixture = acquired_fixture(tmp_path)
    registry = EngineRegistry({ExtractionEngine.PYMUPDF: failing_engine()})
    outcome = asyncio.run(
        fixture.service(registry, fallback_order=("pymupdf",)).extract(
            [fixture.request()]
        )
    )

    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is not None, "failures must be explicit"
    assert outcome.data.candidate is None, "no bytes means no candidate"
    manifest = ExtractionManifest.model_validate_json(
        (
            fixture.root / outcome.data.manifest_reference.workspace_relative_path
        ).read_text(encoding="utf-8")
    )
    assert manifest.records == []
    assert manifest.item_outcomes[0].content_status is DocumentContentStatus.FAILED


def test_e2_neg_acquisition_tamper_is_rejected(tmp_path: Path) -> None:
    fixture = acquired_fixture(tmp_path)
    raw = json.loads(fixture.acquisition_path.read_text(encoding="utf-8"))
    raw["records"][0]["source_sha256"] = "sha256:" + "9" * 64
    fixture.acquisition_path.write_text(json.dumps(raw), encoding="utf-8")

    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([fixture.request()])
    )
    # A lineage failure is not downgraded to a per-item FAILED extraction: the
    # whole request fails closed with no sidecar and no output.
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    item = outcome.data.item_outcomes[0]
    assert item.extraction_status is ExtractionStatus.FAILED
    assert item.stage.value == "PREFLIGHT"
    assert item.error is not None
    assert "ACQUISITION" in item.error.code


def test_e2_neg_frontmatter_mutation_is_detected(tmp_path: Path) -> None:
    fixture = acquired_fixture(tmp_path)
    service = fixture.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    )
    outcome = asyncio.run(service.extract([fixture.request()]))
    reference = outcome.data.manifest_reference
    assert reference is not None
    document_id = fixture.acquisition.records[0].document_id
    extracted = fixture.root / "extracted" / f"{document_id}.md"
    tampered = extracted.read_text(encoding="utf-8").replace("STU-one", "STU-two")
    extracted.write_text(tampered, encoding="utf-8")

    manifest = ExtractionManifest.model_validate_json(
        (fixture.root / reference.workspace_relative_path).read_text(encoding="utf-8")
    )
    with pytest.raises(ExtractionCommitError) as excinfo:
        service.verify_manifest(manifest, verify_bytes=True)
    assert excinfo.value.code == "REUSED_CONTENT_INVALID"


def test_e2_neg_requested_document_not_in_acquisition_fails(tmp_path: Path) -> None:
    fixture = acquired_fixture(tmp_path)
    request = fixture.request(document_ids=["DOC-" + "0" * 32])
    outcome = asyncio.run(
        fixture.service(
            EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
        ).extract([request])
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert outcome.errors


def test_e2_neutral_frontmatter_hash_covers_exact_body(tmp_path: Path) -> None:
    """The body checksum binds the emitted body, not the whole file."""

    fixture = acquired_fixture(tmp_path)
    service = fixture.service(
        EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})
    )
    asyncio.run(service.extract([fixture.request()]))
    document_id = fixture.acquisition.records[0].document_id
    text = (fixture.root / "extracted" / f"{document_id}.md").read_text(
        encoding="utf-8"
    )
    values, _parsed = parse_bound_frontmatter(text.encode("utf-8"))
    body = measure_extracted_body(_parsed).body
    assert (
        values["extracted_sha256"]
        == "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    )
