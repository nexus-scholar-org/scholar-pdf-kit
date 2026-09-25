"""WP01-E1 acquisition contract tests (offline, local fixtures only)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from scholar_pdf.acquisition import (
    InMemoryAuditSink,
    PDFAcquisitionService,
    TransportResult,
)
from scholar_pdf.acquisition_models import (
    AcceptedParentBinding,
    AccessAssertion,
    AccessStatus,
    AcquisitionRequest,
    AcquisitionSourceKind,
    AcquisitionStatus,
    OperationStatus,
    ProducerProvenance,
    SourceMode,
    WorkspaceRootBinding,
)
from scholar_pdf.canonical import (
    canonical_fingerprint,
    canonical_json_bytes,
    corpus_snapshot_fingerprint,
    deterministic_document_id,
    source_sha256,
)

PROTOCOL = "sha256:" + "1" * 64


@dataclass
class WorkspaceFixture:
    root: Path
    parents: list[AcceptedParentBinding]
    binding: WorkspaceRootBinding
    producer: ProducerProvenance

    def request(
        self, *, source: Path | None = None, **updates: Any
    ) -> AcquisitionRequest:
        if source is None:
            source = self.root / "inbox" / "paper.pdf"
        values: dict[str, Any] = {
            "workspace_id": "WSP-test",
            "workspace_root": self.root,
            "run_id": "RUN-acquisition",
            "study_id": "STU-one",
            "protocol_fingerprint": PROTOCOL,
            "corpus_fingerprint": self.parents[0].corpus_fingerprint,
            "inputs": {
                "corpus_snapshot": {
                    "artifact_id": self.parents[0].artifact_id,
                    "artifact_type": "corpus_snapshot",
                    "sha256": self.parents[0].sha256,
                    "workspace_relative_path": self.parents[0].workspace_relative_path,
                },
                "screening_decisions": {
                    "artifact_id": self.parents[1].artifact_id,
                    "artifact_type": "screening_decisions",
                    "sha256": self.parents[1].sha256,
                    "workspace_relative_path": self.parents[1].workspace_relative_path,
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
            "doi": "10.1000/one",
        }
        values.update(updates)
        return AcquisitionRequest.model_validate(values)

    def service(
        self, transport: Any | None = None, **kwargs: Any
    ) -> PDFAcquisitionService:
        return PDFAcquisitionService(
            accepted_parents=self.parents,
            workspace_bindings={"WSP-test": self.binding},
            producer=self.producer,
            audit_sink=kwargs.pop("audit_sink", InMemoryAuditSink()),
            transport=transport,
            **kwargs,
        )


class FakeTransport:
    owns_resources = False

    def __init__(
        self, payload: bytes, *, result: TransportResult | None = None
    ) -> None:
        self.payload = payload
        self.result = result or TransportResult(
            http_status=200,
            resolved_url="https://example.test/paper.pdf?token=secret",
            observed_media_type="text/html",
        )
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def download(
        self,
        *,
        url: str,
        destination: Path,
        forward_proxy_url: str | None,
        max_bytes: int,
    ) -> TransportResult:
        self.calls.append(
            {
                "url": url,
                "destination": destination,
                "forward_proxy_url": forward_proxy_url,
                "max_bytes": max_bytes,
            }
        )
        destination.write_bytes(self.payload)
        return self.result

    async def close(self) -> None:
        self.closed = True


def pdf_bytes(description_character: str = "x") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    # The kit's cheap validator requires a realistic-sized file.  Metadata is
    # part of the valid PDF object graph, so the fixture remains parseable.
    writer.add_metadata({"/Description": description_character * 11000})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.add_metadata({"/Description": "x" * 11000})
    buffer = io.BytesIO()
    writer.write(buffer)
    writer.encrypt("fixture-password")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    return encrypted.getvalue()


def make_workspace(tmp_path: Path) -> WorkspaceFixture:
    root = tmp_path / "workspace"
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "paper.pdf").write_bytes(pdf_bytes())

    corpus_data = {
        "corpus_id": "COR-test",
        "identity_algorithm_version": "v1",
        "studies": [
            {
                "study_id": "STU-one",
                "source_record_ids": ["REC-one"],
                "alias_ids": [],
                "external_ids": {"doi": ["10.1000/one"]},
                "title": "A local fixture study",
                "publication_year": 2024,
            }
        ],
        "record_to_study": {"REC-one": "STU-one"},
    }
    corpus_fingerprint = corpus_snapshot_fingerprint(corpus_data)
    corpus_payload = {
        "schema_version": "1.0.0",
        "artifact_id": "ART-corpus",
        "artifact_type": "corpus_snapshot",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_id": "WSP-test",
        "run_id": "RUN-screening",
        "protocol_fingerprint": PROTOCOL,
        "corpus_fingerprint": corpus_fingerprint,
        "inputs": [],
        "data": corpus_data,
        "producer": {
            "package": "fixture",
            "version": "1",
            "commit": "abcdef1",
        },
    }
    screening_payload = {
        "schema_version": "1.0.0",
        "artifact_id": "ART-screening",
        "artifact_type": "screening_decisions",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_id": "WSP-test",
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
                    "decision_id": "SCR-one",
                    "study_id": "STU-one",
                    "screener_id": "human:fixture",
                    "method": "HUMAN",
                    "decision": "INCLUDE",
                    "reason": "Included by the fixture.",
                    "decided_at": "2026-01-01T00:00:00Z",
                    "parent_decision_ids": [],
                }
            ],
        },
        "producer": {
            "package": "fixture",
            "version": "1",
            "commit": "abcdef1",
        },
    }
    parents = [
        AcceptedParentBinding(
            artifact_id="ART-corpus",
            artifact_type="corpus_snapshot",
            sha256=canonical_fingerprint(corpus_payload),
            workspace_relative_path="literature/corpus.json",
            workspace_id="WSP-test",
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
            workspace_id="WSP-test",
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
        workspace_id="WSP-test",
        canonical_root=root,
        binding_fingerprint=canonical_fingerprint(
            {
                "algorithm_version": "v1",
                "canonical_root": str(root.resolve()),
                "workspace_id": "WSP-test",
            }
        ),
    )
    producer = ProducerProvenance(
        package="scholar-pdf-kit", version="0.1.0", commit="abcdef1"
    )
    return WorkspaceFixture(
        root=root, parents=parents, binding=binding, producer=producer
    )


def test_e1_pos_001_user_path_commits_and_round_trips(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    audit = InMemoryAuditSink()
    service = fixture.service(audit_sink=audit)
    outcome = asyncio.run(service.acquire([fixture.request()]))

    assert outcome.status.value == "SUCCESS"
    assert (
        outcome.data.item_outcomes[0].acquisition_status is AcquisitionStatus.ACQUIRED
    )
    assert outcome.data.manifest_reference is not None
    manifest_path = (
        fixture.root / outcome.data.manifest_reference.workspace_relative_path
    )
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = manifest["records"][0]
    assert record["selected_source"] == "inbox/paper.pdf"
    assert record["selected_source_url"] is None
    assert not Path(record["workspace_relative_path"]).is_absolute()
    assert len(audit.events) == 1


def test_e1_pos_002_and_neg_035_exact_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    audit = InMemoryAuditSink()
    service = fixture.service(audit_sink=audit)
    request = fixture.request(title_similarity=0.95)
    first = asyncio.run(service.acquire([request]))
    second = asyncio.run(service.acquire([request]))

    assert first.data.manifest_reference == second.data.manifest_reference
    assert second.data.item_outcomes[0].acquisition_status is AcquisitionStatus.REUSED
    assert second.data.item_outcomes[0].warning is not None
    assert second.data.item_outcomes[0].warning.code == "TITLE_SIMILARITY_REVIEW"
    assert (
        len(list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json")))
        == 1
    )
    assert len(audit.events) == 1


def test_e1_pos_003_network_fake_transport_is_offline_and_records_access(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    payload = pdf_bytes()
    transport = FakeTransport(payload)
    request = fixture.request(
        source_mode=SourceMode.DISCOVERY,
        source_path=None,
        source_kind=AcquisitionSourceKind.OPENALEX,
        access_status=AccessStatus.VERIFIED_OPEN_ACCESS,
        access_assertion=None,
        provider_evidence={"is_oa": True, "license": "cc-by", "access_token": "secret"},
        requested_source="https://example.test/paper.pdf?token=secret",
        selected_source_url="https://example.test/paper.pdf?token=secret",
    )
    outcome = asyncio.run(fixture.service(transport).acquire([request]))

    assert outcome.status.value == "SUCCESS"
    assert transport.calls
    record = outcome.data.item_outcomes[0]
    assert record.acquisition_status is AcquisitionStatus.ACQUIRED
    manifest = json.loads(
        (
            fixture.root / outcome.data.manifest_reference.workspace_relative_path
        ).read_text()
    )
    assert "secret" not in json.dumps(manifest)
    assert transport.calls[0]["forward_proxy_url"] is None


def test_e1_request_and_parent_inputs_are_snapshotted(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    request = fixture.request(
        source_mode=SourceMode.DISCOVERY,
        source_path=None,
        source_kind=AcquisitionSourceKind.OPENALEX,
        access_status=AccessStatus.VERIFIED_OPEN_ACCESS,
        access_assertion=None,
        provider_evidence={"is_oa": True, "license": "cc-by"},
        requested_source="https://example.test/paper.pdf",
        selected_source_url="https://example.test/paper.pdf",
    )

    class MutatingTransport(FakeTransport):
        async def download(self, **kwargs: Any) -> TransportResult:
            request.provider_evidence["license"] = "forged"
            fixture.parents[0].payload["data"]["studies"][0]["title"] = "forged"
            return await super().download(**kwargs)

    outcome = asyncio.run(
        fixture.service(MutatingTransport(pdf_bytes())).acquire([request])
    )

    assert outcome.status.value == "SUCCESS"
    manifest = json.loads(
        (
            fixture.root / outcome.data.manifest_reference.workspace_relative_path
        ).read_text(encoding="utf-8")
    )
    assert manifest["item_outcomes"][0]["attempts"][0]["provider_evidence"] == {
        "is_oa": True,
        "license": "cc-by",
    }


def test_e1_pos_004_clean_isolated_wheel_import_and_help(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to execute the packaging acceptance test"

    repository_root = Path(__file__).resolve().parents[1]
    wheel_directory = tmp_path / "dist"
    environment_directory = tmp_path / "venv"
    offline_environment = {**os.environ, "UV_OFFLINE": "1"}

    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(wheel_directory)],
        cwd=repository_root,
        env=offline_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_directory.glob("scholar_pdf_kit-*.whl"))
    assert len(wheels) == 1

    create_environment = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment_directory)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert create_environment.returncode == 0, (
        create_environment.stdout + create_environment.stderr
    )
    scripts_directory = environment_directory / (
        "Scripts" if os.name == "nt" else "bin"
    )
    environment_python = scripts_directory / (
        "python.exe" if os.name == "nt" else "python"
    )
    install = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--python",
            str(environment_python),
            str(wheels[0]),
        ],
        env=offline_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    public_import = subprocess.run(
        [
            str(environment_python),
            "-I",
            "-c",
            (
                "from scholar_pdf import AcquisitionRequest, "
                "AcquiredDocumentManifest, PDFAcquisitionService; "
                "print(AcquisitionRequest.__name__, "
                "AcquiredDocumentManifest.__name__, "
                "PDFAcquisitionService.__name__)"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert public_import.returncode == 0, public_import.stdout + public_import.stderr
    assert "AcquisitionRequest AcquiredDocumentManifest PDFAcquisitionService" in (
        public_import.stdout
    )

    entrypoint = scripts_directory / (
        "scholar-pdf.exe" if os.name == "nt" else "scholar-pdf"
    )
    help_result = subprocess.run(
        [str(entrypoint), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "acquire" in help_result.stdout


def test_canonical_identity_golden_payload() -> None:
    digest = source_sha256(b"validated bytes")
    assert canonical_json_bytes(
        {
            "algorithm_version": "v1",
            "input": {
                "media_type": "application/pdf",
                "source_sha256": digest,
                "study_id": "STU-one",
            },
            "kind": "document",
            "workspace_namespace": "WSP-test",
        }
    ).startswith(b'{"algorithm_version":"v1","input":{"media_type":"application/pdf"')

    golden_digest = "sha256:" + ("0" * 64)
    assert canonical_json_bytes(
        {
            "algorithm_version": "v1",
            "input": {
                "media_type": "application/pdf",
                "source_sha256": golden_digest,
                "study_id": "STU-x",
            },
            "kind": "document",
            "workspace_namespace": "WSP-x",
        }
    ) == (
        b'{"algorithm_version":"v1","input":{"media_type":"application/pdf",'
        b'"source_sha256":"sha256:'
        + (b"0" * 64)
        + b'","study_id":"STU-x"},"kind":"document",'
        b'"workspace_namespace":"WSP-x"}'
    )
    assert (
        deterministic_document_id(
            study_id="STU-x", source_hash=golden_digest, workspace_id="WSP-x"
        )
        == "DOC-1a8f0c6cd05e514f8f82134eea2fb45c"
    )


@pytest.mark.parametrize(
    "case",
    ["missing", "excluded", "unknown", "fingerprint", "hash", "cross_workspace"],
)
def test_e1_neg_001_002_003_004_parent_preflight_fails_closed(
    tmp_path: Path, case: str
) -> None:
    fixture = make_workspace(tmp_path)
    if case == "unknown":
        request = fixture.request(study_id="STU-unknown")
    elif case == "excluded":
        screening = fixture.parents[1]
        payload = dict(screening.payload)
        data = dict(payload["data"])
        decisions = [dict(data["decisions"][0])]
        decisions[0]["decision"] = "EXCLUDE"
        data["decisions"] = decisions
        payload["data"] = data
        fixture.parents[1] = screening.model_copy(
            update={"payload": payload, "sha256": canonical_fingerprint(payload)}
        )
        request = fixture.request()
    elif case == "fingerprint":
        request = fixture.request(protocol_fingerprint="sha256:" + "9" * 64)
    elif case == "hash":
        request = fixture.request(
            inputs={
                "corpus_snapshot": {
                    "artifact_id": "ART-corpus",
                    "artifact_type": "corpus_snapshot",
                    "sha256": "sha256:" + "8" * 64,
                    "workspace_relative_path": "literature/corpus.json",
                },
                "screening_decisions": {
                    "artifact_id": "ART-screening",
                    "artifact_type": "screening_decisions",
                    "sha256": fixture.parents[1].sha256,
                    "workspace_relative_path": "literature/screening.json",
                },
            }
        )
    elif case == "cross_workspace":
        fixture.parents[0] = fixture.parents[0].model_copy(
            update={"workspace_id": "WSP-other"}
        )
        request = fixture.request()
    else:
        request = fixture.request()
        fixture.parents = fixture.parents[:1]
    before = sorted(
        path.relative_to(fixture.root).as_posix() for path in fixture.root.rglob("*")
    )
    outcome = asyncio.run(fixture.service().acquire([request]))
    after = sorted(
        path.relative_to(fixture.root).as_posix() for path in fixture.root.rglob("*")
    )
    assert outcome.status.value == "FAILED"
    assert outcome.data.committed_count == 0
    assert before == after


def test_e1_neg_005_identity_is_source_and_workspace_specific() -> None:
    digest = source_sha256(b"same bytes")
    first = deterministic_document_id(
        study_id="STU-one", source_hash=digest, workspace_id="WSP-a"
    )
    second = deterministic_document_id(
        study_id="STU-one", source_hash=digest, workspace_id="WSP-b"
    )
    assert first != second
    assert first == deterministic_document_id(
        study_id="STU-one", source_hash=digest, workspace_id="WSP-a"
    )


def test_e1_neg_006_byte_mutation_changes_identity() -> None:
    first = source_sha256(b"same bytes")
    second = source_sha256(b"same byteS")
    assert first != second
    assert deterministic_document_id(
        study_id="STU-one", source_hash=first, workspace_id="WSP-a"
    ) != deterministic_document_id(
        study_id="STU-one", source_hash=second, workspace_id="WSP-a"
    )


def test_e1_neg_012_gateway_and_proxy_are_distinct(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    request = fixture.request(
        source_mode=SourceMode.DISCOVERY,
        source_path=None,
        source_kind=AcquisitionSourceKind.OPENALEX,
        access_status=AccessStatus.UNRESOLVED,
        access_assertion=None,
        requested_source="https://example.test/paper.pdf",
        selected_source_url="https://example.test/paper.pdf",
        institutional_gateway_url="https://gateway.test",
        forward_proxy_url="https://proxy.test",
    )
    outcome = asyncio.run(fixture.service().acquire([request]))
    assert outcome.status.value in {"UNRESOLVED", "FAILED"}
    conflicting = request.model_copy(
        update={"forward_proxy_url": "https://gateway.test"}
    )
    result = asyncio.run(fixture.service().acquire([conflicting]))
    assert result.errors[0].code == "GATEWAY_PROXY_CONFLATION"


def test_e1_neg_014_not_found_is_distinct(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    transport = FakeTransport(b"", result=TransportResult(http_status=404))
    request = fixture.request(
        source_mode=SourceMode.DISCOVERY,
        source_path=None,
        source_kind=AcquisitionSourceKind.OPENALEX,
        access_status=AccessStatus.UNRESOLVED,
        access_assertion=None,
        requested_source="https://example.test/missing.pdf",
        selected_source_url="https://example.test/missing.pdf",
    )
    outcome = asyncio.run(fixture.service(transport).acquire([request]))
    assert (
        outcome.data.item_outcomes[0].acquisition_status is AcquisitionStatus.NOT_FOUND
    )
    assert outcome.status.value == "FAILED"
    assert not list(
        (fixture.root / "literature" / "acquisition" / request.run_id).glob(
            ".acquisition-*.tmp"
        )
    )


def test_e1_neg_012_gateway_rewrites_target_while_proxy_stays_transport_level(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    transport = FakeTransport(pdf_bytes())
    request = fixture.request(
        source_mode=SourceMode.DISCOVERY,
        source_path=None,
        source_kind=AcquisitionSourceKind.OPENALEX,
        access_status=AccessStatus.UNRESOLVED,
        access_assertion=None,
        requested_source="https://example.test/paper.pdf",
        selected_source_url="https://example.test/paper.pdf",
        institutional_gateway_url="https://www.sndl1.arn.dz",
        forward_proxy_url="http://transport-proxy.test:8080",
    )
    outcome = asyncio.run(fixture.service(transport).acquire([request]))
    assert outcome.status.value == "SUCCESS"
    assert (
        transport.calls[0]["url"] == "https://example-test.www.sndl1.arn.dz/paper.pdf"
    )
    assert transport.calls[0]["forward_proxy_url"] == "http://transport-proxy.test:8080"


def test_e1_neg_023_validation_rejects_magic_malformed_and_encrypted(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    invalid_payloads = (
        b"not-a-pdf" + b"x" * 11000,
        b"%PDF-1.7\n" + b"x" * 11000,
        encrypted_pdf_bytes(),
    )
    for payload in invalid_payloads:
        transport = FakeTransport(payload)
        request = fixture.request(
            source_mode=SourceMode.DISCOVERY,
            source_path=None,
            source_kind=AcquisitionSourceKind.OPENALEX,
            access_status=AccessStatus.UNRESOLVED,
            access_assertion=None,
            requested_source="https://example.test/paper.pdf",
            selected_source_url="https://example.test/paper.pdf",
        )
        outcome = asyncio.run(fixture.service(transport).acquire([request]))
        assert (
            outcome.data.item_outcomes[0].acquisition_status
            is AcquisitionStatus.INVALID_CONTENT
        )
        assert outcome.data.committed_count == 0


def test_e1_neg_027_029_paths_are_relative_and_reject_traversal(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    with pytest.raises(ValueError, match="path"):
        fixture.request(storage_prefix="../escape")


def test_e1_neg_032_all_failure_manifest_is_explicit(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(
        fixture.service().acquire(
            [fixture.request(source_path=fixture.root / "missing.pdf")]
        )
    )
    assert outcome.status.value == "FAILED"
    assert outcome.data.manifest_reference is not None
    manifest = json.loads(
        (
            fixture.root / outcome.data.manifest_reference.workspace_relative_path
        ).read_text()
    )
    assert manifest["records"] == []
    assert manifest["operation"]["status"] == "FAILED"
    assert manifest["item_outcomes"]


def test_e1_neg_036_037_user_path_contract(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.data.item_outcomes[0].selected_source_url is None
    assert outcome.data.item_outcomes[0].selected_source == "inbox/paper.pdf"
    with pytest.raises(ValueError, match="access_assertion"):
        fixture.request(access_assertion=None)


def test_e1_neg_041_042_status_mapping_and_cancellation(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(
        fixture.service().acquire(
            [fixture.request(source_path=fixture.root / "missing.pdf")]
        )
    )
    assert outcome.status.value == "FAILED"
    assert outcome.data.committed_count == 0


def _corrupt_parent(fixture, index: int, mutate) -> None:
    """Rewrite a parent payload, keeping its registry hash and file consistent.

    Only the payload shape changes, so the preflight must classify the parent as
    an invalid Contract v1 payload instead of failing later on a hash or file
    mismatch, and so a *valid* shape must still be accepted.
    """

    parent = fixture.parents[index]
    payload = json.loads(json.dumps(parent.payload))
    mutate(payload)
    fixture.parents[index] = parent.model_copy(
        update={"payload": payload, "sha256": canonical_fingerprint(payload)}
    )
    (fixture.root / parent.workspace_relative_path).write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("case", "index", "mutate"),
    [
        (
            "corpus_studies_not_a_list",
            0,
            lambda payload: payload["data"].__setitem__("studies", {}),
        ),
        (
            "corpus_study_missing_study_id",
            0,
            lambda payload: payload["data"]["studies"][0].pop("study_id"),
        ),
        (
            "corpus_study_missing_title",
            0,
            lambda payload: payload["data"]["studies"][0].pop("title"),
        ),
        (
            "corpus_study_id_wrong_prefix",
            0,
            lambda payload: payload["data"]["studies"][0].__setitem__(
                "study_id", "STUDY-one"
            ),
        ),
        (
            "corpus_record_to_study_not_a_mapping",
            0,
            lambda payload: payload["data"].__setitem__("record_to_study", []),
        ),
        (
            "corpus_data_not_a_mapping",
            0,
            lambda payload: payload.__setitem__("data", "not-a-mapping"),
        ),
        (
            "screening_decisions_not_a_list",
            1,
            lambda payload: payload["data"].__setitem__("decisions", "none"),
        ),
        (
            "screening_decision_missing_decision_id",
            1,
            lambda payload: payload["data"]["decisions"][0].pop("decision_id"),
        ),
        (
            "screening_decision_missing_study_id",
            1,
            lambda payload: payload["data"]["decisions"][0].pop("study_id"),
        ),
        (
            "screening_binding_not_a_mapping",
            1,
            lambda payload: payload["data"].__setitem__("binding", None),
        ),
        (
            "screening_inputs_not_a_list",
            1,
            lambda payload: payload.__setitem__("inputs", {}),
        ),
        (
            "record_to_study_not_total",
            0,
            lambda payload: payload["data"].__setitem__("record_to_study", {}),
        ),
        (
            "record_to_study_maps_to_another_study",
            0,
            lambda payload: payload["data"]["record_to_study"].__setitem__(
                "REC-one", "STU-two"
            ),
        ),
        (
            "duplicate_study_ids",
            0,
            lambda payload: payload["data"]["studies"].append(
                dict(payload["data"]["studies"][0])
            ),
        ),
        (
            "duplicate_source_record_ids",
            0,
            lambda payload: payload["data"]["studies"][0].__setitem__(
                "source_record_ids", ["REC-one", "REC-one"]
            ),
        ),
        (
            "external_ids_not_unique",
            0,
            lambda payload: payload["data"]["studies"][0].__setitem__(
                "external_ids", {"doi": ["10.1000/one", "10.1000/one"]}
            ),
        ),
        (
            "duplicate_decision_ids",
            1,
            lambda payload: payload["data"]["decisions"].append(
                dict(payload["data"]["decisions"][0])
            ),
        ),
        (
            "decided_at_not_utc",
            1,
            lambda payload: payload["data"]["decisions"][0].__setitem__(
                "decided_at", "2026-01-01T00:00:00+02:00"
            ),
        ),
        (
            "publication_year_out_of_range",
            0,
            lambda payload: payload["data"]["studies"][0].__setitem__(
                "publication_year", 42
            ),
        ),
    ],
)
def test_e1_neg_001_002_003_004_parent_shape_is_strictly_validated(
    tmp_path: Path, case: str, index: int, mutate
) -> None:
    """A duck-typed parent payload must fail closed before any transfer I/O."""

    fixture = make_workspace(tmp_path)
    _corrupt_parent(fixture, index, mutate)
    request = fixture.request()
    before = sorted(
        path.relative_to(fixture.root).as_posix() for path in fixture.root.rglob("*")
    )
    outcome = asyncio.run(fixture.service().acquire([request]))
    after = sorted(
        path.relative_to(fixture.root).as_posix() for path in fixture.root.rglob("*")
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.committed_count == 0
    assert before == after, case
    assert any(error.code == "PARENT_PAYLOAD_INVALID" for error in outcome.errors), case


def test_e1_parent_shape_allows_unknown_forward_compatible_fields(
    tmp_path: Path,
) -> None:
    """Unknown keys are forward-compatible: only known shapes are enforced."""

    fixture = make_workspace(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["future_envelope_field"] = {"anything": [1, 2, 3]}
        payload["data"]["future_data_field"] = "ignored"
        payload["data"]["decisions"][0]["future_decision_field"] = None

    _corrupt_parent(fixture, 1, mutate)
    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.status is OperationStatus.SUCCESS


def _mutate_corpus_data(fixture, mutate) -> None:
    """Mutate corpus ``data`` and resync every fingerprint that depends on it.

    ``corpus_fingerprint`` is computed over ``data``, so a shape-only change
    there also has to be reflected in the screening envelope/binding.  Doing it
    here isolates the structural assertion from the semantic fingerprint gate.
    """

    corpus_parent = fixture.parents[0]
    corpus_payload = json.loads(json.dumps(corpus_parent.payload))
    mutate(corpus_payload["data"])
    corpus_fingerprint = corpus_snapshot_fingerprint(corpus_payload["data"])
    corpus_payload["corpus_fingerprint"] = corpus_fingerprint
    corpus_hash = canonical_fingerprint(corpus_payload)

    screening_parent = fixture.parents[1]
    screening_payload = json.loads(json.dumps(screening_parent.payload))
    screening_payload["corpus_fingerprint"] = corpus_fingerprint
    screening_payload["data"]["binding"]["corpus_fingerprint"] = corpus_fingerprint
    screening_payload["inputs"][0]["sha256"] = corpus_hash

    fixture.parents[0] = corpus_parent.model_copy(
        update={
            "payload": corpus_payload,
            "sha256": corpus_hash,
            "corpus_fingerprint": corpus_fingerprint,
        }
    )
    fixture.parents[1] = screening_parent.model_copy(
        update={
            "payload": screening_payload,
            "sha256": canonical_fingerprint(screening_payload),
            "corpus_fingerprint": corpus_fingerprint,
        }
    )
    (fixture.root / "literature" / "corpus.json").write_text(
        json.dumps(corpus_payload, sort_keys=True), encoding="utf-8"
    )
    (fixture.root / "literature" / "screening.json").write_text(
        json.dumps(screening_payload, sort_keys=True), encoding="utf-8"
    )


def test_e1_alias_ids_are_not_prefix_constrained(tmp_path: Path) -> None:
    """The frozen ``alias_ids`` is a bare ``list[str]`` and must stay permissive.

    A study may carry alias identifiers from an upstream system whose prefix is
    not registered in the Contract v1 identifier table.  The parent is still a
    valid frozen artifact, so rejecting it here would fail a legitimate review.
    """

    fixture = make_workspace(tmp_path)
    _mutate_corpus_data(
        fixture,
        lambda data: data["studies"][0].__setitem__(
            "alias_ids", ["legacy-local-alias", "PMID:12345", "STU-aliased"]
        ),
    )
    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.status is OperationStatus.SUCCESS
