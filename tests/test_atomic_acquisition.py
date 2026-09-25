"""WP01-E1 atomicity, recovery, containment, and surface tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_acquisition_contract import (
    FakeTransport,
    WorkspaceFixture,
    make_workspace,
    pdf_bytes,
)
from typer.testing import CliRunner

from scholar_pdf.acquisition import (
    AcquisitionFault,
    AiohttpAcquisitionTransport,
    InMemoryAuditSink,
    PDFAcquisitionService,
    TransportResult,
    WorkspaceManagerCliAuditSink,
)
from scholar_pdf.acquisition_models import (
    AcceptedParentBinding,
    AccessStatus,
    AcquisitionRequest,
    AcquisitionRunConfig,
    AcquisitionSourceKind,
    AcquisitionStatus,
    OperationStatus,
    ProducerProvenance,
    SourceMode,
    WorkspaceRootBinding,
)
from scholar_pdf.canonical import (
    canonical_fingerprint,
    corpus_snapshot_fingerprint,
    deterministic_document_id,
    source_sha256,
)
from scholar_pdf.cli import app


def _network_request(fixture, **updates: Any) -> AcquisitionRequest:
    values: dict[str, Any] = {
        "source_mode": SourceMode.DISCOVERY,
        "source_path": None,
        "source_kind": AcquisitionSourceKind.OPENALEX,
        "access_status": AccessStatus.UNRESOLVED,
        "access_assertion": None,
        "requested_source": "https://example.test/paper.pdf",
        "selected_source_url": "https://example.test/paper.pdf",
    }
    values.update(updates)
    return fixture.request(**values)


def _config(fixture, request: AcquisitionRequest) -> AcquisitionRunConfig:
    return AcquisitionRunConfig(
        requests=[request],
        accepted_parents=fixture.parents,
        workspace_bindings={"WSP-test": fixture.binding},
        producer=fixture.producer,
    )


def _add_second_study(fixture) -> None:
    corpus_parent = fixture.parents[0]
    screening_parent = fixture.parents[1]
    corpus_payload = json.loads(json.dumps(corpus_parent.payload))
    corpus_payload["data"]["studies"].append(
        {
            "study_id": "STU-two",
            "source_record_ids": ["REC-two"],
            "alias_ids": [],
            "external_ids": {"doi": ["10.1000/two"]},
            "title": "A second local fixture study",
            "publication_year": 2024,
        }
    )
    corpus_payload["data"]["record_to_study"]["REC-two"] = "STU-two"
    corpus_fingerprint = corpus_snapshot_fingerprint(corpus_payload["data"])
    corpus_payload["corpus_fingerprint"] = corpus_fingerprint

    screening_payload = json.loads(json.dumps(screening_parent.payload))
    screening_payload["corpus_fingerprint"] = corpus_fingerprint
    screening_payload["data"]["binding"]["corpus_fingerprint"] = corpus_fingerprint
    screening_payload["inputs"][0]["sha256"] = canonical_fingerprint(corpus_payload)
    screening_payload["data"]["decisions"].append(
        {
            "decision_id": "SCR-two",
            "study_id": "STU-two",
            "screener_id": "human:fixture",
            "method": "HUMAN",
            "decision": "INCLUDE",
            "reason": "Included by the fixture.",
            "decided_at": "2026-01-01T00:00:00Z",
            "parent_decision_ids": [],
        }
    )

    corpus_hash = canonical_fingerprint(corpus_payload)
    screening_hash = canonical_fingerprint(screening_payload)
    fixture.parents[:] = [
        corpus_parent.model_copy(
            update={
                "payload": corpus_payload,
                "sha256": corpus_hash,
                "corpus_fingerprint": corpus_fingerprint,
            }
        ),
        screening_parent.model_copy(
            update={
                "payload": screening_payload,
                "sha256": screening_hash,
                "corpus_fingerprint": corpus_fingerprint,
            }
        ),
    ]
    literature = fixture.root / "literature"
    (literature / "corpus.json").write_text(
        json.dumps(corpus_payload, sort_keys=True), encoding="utf-8"
    )
    (literature / "screening.json").write_text(
        json.dumps(screening_payload, sort_keys=True), encoding="utf-8"
    )
    (fixture.root / "inbox" / "paper-two.pdf").write_bytes(pdf_bytes())


def _write_logger_script(path: Path) -> None:
    path.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "def log_project_event(slug, action, agent, description, inputs=None, outputs=None, parameters=None, metrics=None, status='SUCCESS'):\n"
        "    root = Path(sys.argv[2])\n"
        "    (root / 'audit').mkdir(parents=True, exist_ok=True)\n"
        "    event = {'action': action, 'agent_or_tool': agent, 'description': description, 'inputs': inputs or [], 'outputs': outputs or [], 'parameters': parameters or {}, 'metrics': metrics or {}, 'status': status}\n"
        "    with (root / 'audit' / 'journal.jsonl').open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(event, sort_keys=True) + '\\n')\n",
        encoding="utf-8",
    )


def test_e1_neg_021_torn_download_has_no_authoritative_partial(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    class TornTransport(FakeTransport):
        async def download(self, **kwargs: Any) -> TransportResult:
            kwargs["destination"].write_bytes(b"%PDF-1.7\npartial")
            raise OSError("torn stream")

    outcome = asyncio.run(
        fixture.service(TornTransport(b""), max_attempts=1).acquire(
            [_network_request(fixture)]
        )
    )
    assert outcome.status is OperationStatus.FAILED
    item = outcome.data.item_outcomes[0]
    assert item.acquisition_status is AcquisitionStatus.FAILED
    assert item.error is not None and item.error.code == "ACQUISITION_FAILED"
    assert item.attempts == []
    assert outcome.data.committed_count == 0
    assert not list((fixture.root / "pdfs" / "acquired").glob("*.pdf"))
    assert not list((fixture.root / "pdfs" / "acquired").glob(".acquisition-*"))


def test_e1_neg_022_download_fault_is_classified_and_bounded(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    def fail_download(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.DOWNLOAD:
            raise TimeoutError("injected transport timeout")

    outcome = asyncio.run(
        fixture.service(
            FakeTransport(b""), fault_injector=fail_download, max_attempts=2
        ).acquire([_network_request(fixture)])
    )
    item = outcome.data.item_outcomes[0]
    assert outcome.status is OperationStatus.FAILED
    assert item.acquisition_status is AcquisitionStatus.NETWORK_FAILED
    assert item.error is not None
    assert item.error.code == "NETWORK_ERROR"
    assert item.error.retryable is True
    assert len(item.attempts) == 2
    assert outcome.data.committed_count == 0
    assert not list((fixture.root / "pdfs" / "acquired").glob("*.pdf"))


def test_e1_neg_022_023_aiohttp_transport_enforces_stream_size_bound(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    class Content:
        async def iter_chunked(self, _size: int):
            yield b"oversized"

    class Response:
        status = 200
        url = "https://example.test/paper.pdf"
        content = Content()

        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/pdf"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

    class Session:
        closed = False

        def get(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()

        async def close(self) -> None:
            self.closed = True

    session = Session()
    transport = AiohttpAcquisitionTransport(session=session)  # type: ignore[arg-type]
    outcome = asyncio.run(
        fixture.service(transport, minimum_pdf_bytes=1, maximum_pdf_bytes=4).acquire(
            [_network_request(fixture)]
        )
    )

    item = outcome.data.item_outcomes[0]
    assert item.acquisition_status is AcquisitionStatus.INVALID_CONTENT
    assert item.error is not None
    assert item.error.code == "CONTENT_SIZE_EXCEEDED"
    assert outcome.data.committed_count == 0
    assert not list((fixture.root / "pdfs" / "acquired").glob(".acquisition-*"))
    assert session.closed is False


def test_e1_neg_024_content_move_failure_preserves_prior_state(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    def fail_move(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.CONTENT_MOVE:
            raise OSError("injected rename failure")

    outcome = asyncio.run(
        fixture.service(FakeTransport(pdf_bytes()), fault_injector=fail_move).acquire(
            [_network_request(fixture)]
        )
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.committed_count == 0
    assert not list((fixture.root / "pdfs" / "acquired").glob("*.pdf"))
    assert not list((fixture.root / "pdfs" / "acquired").glob(".acquisition-*"))


def test_e1_neg_015_filesystem_failure_preserves_prior_valid_state(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    first = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert first.status is OperationStatus.SUCCESS
    assert first.data.manifest_reference is not None
    original_manifest = (
        fixture.root / first.data.manifest_reference.workspace_relative_path
    )
    original_manifest_bytes = original_manifest.read_bytes()
    original_content = next((fixture.root / "pdfs" / "acquired").glob("DOC-*.pdf"))
    original_content_bytes = original_content.read_bytes()

    fixture.root.joinpath("inbox", "paper.pdf").write_bytes(pdf_bytes("y"))

    def fail_move(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.CONTENT_MOVE:
            raise OSError("injected filesystem move failure")

    failed = asyncio.run(
        fixture.service(fault_injector=fail_move).acquire(
            [fixture.request(run_id="RUN-second")]
        )
    )
    assert failed.status is OperationStatus.FAILED
    assert failed.data.committed_count == 0
    assert original_content.read_bytes() == original_content_bytes
    assert original_manifest.read_bytes() == original_manifest_bytes


def test_e1_neg_025_manifest_replace_failure_publishes_no_manifest(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    def fail_manifest(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.MANIFEST_REPLACE:
            raise OSError("injected manifest replace failure")

    outcome = asyncio.run(
        fixture.service(
            FakeTransport(pdf_bytes()), fault_injector=fail_manifest
        ).acquire([_network_request(fixture)])
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.manifest_reference is None
    assert not list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json"))
    assert not list((fixture.root / "pdfs" / "acquired").glob("*.pdf"))


def test_e1_neg_026_post_commit_audit_failure_is_recoverable(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    audit = InMemoryAuditSink()

    def fail_audit(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.AUDIT_APPEND:
            raise RuntimeError("injected audit outage")

    request = _network_request(fixture)
    first = asyncio.run(
        fixture.service(
            FakeTransport(pdf_bytes()),
            audit_sink=audit,
            fault_injector=fail_audit,
        ).acquire([request])
    )
    assert first.status is OperationStatus.PARTIAL
    assert first.data.manifest_reference is not None
    assert not audit.events

    second = asyncio.run(
        fixture.service(FakeTransport(pdf_bytes()), audit_sink=audit).acquire([request])
    )
    assert second.status is OperationStatus.SUCCESS
    assert second.data.item_outcomes[0].acquisition_status is AcquisitionStatus.REUSED
    assert len(audit.events) == 1


def test_e1_audit_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    outside = tmp_path / "outside-audit"
    outside.mkdir()
    try:
        os.symlink(
            outside,
            fixture.root / "audit",
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    sink = WorkspaceManagerCliAuditSink(
        workspace_root=fixture.root,
        logger_path=tmp_path / "logger.py",
    )
    outcome = asyncio.run(
        fixture.service(FakeTransport(pdf_bytes()), audit_sink=sink).acquire(
            [fixture.request()]
        )
    )
    assert outcome.status is OperationStatus.PARTIAL
    assert not list(outside.iterdir())


def test_e1_neg_026_035_workspace_manager_cli_audit_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path / "workspace")
    logger_path = tmp_path / "log_event.py"
    _write_logger_script(logger_path)
    sink = WorkspaceManagerCliAuditSink(
        workspace_root=fixture.root,
        logger_path=logger_path,
    )
    service = fixture.service(FakeTransport(pdf_bytes()), audit_sink=sink)
    request = fixture.request()

    first = asyncio.run(service.acquire([request]))
    second = asyncio.run(service.acquire([request]))

    assert first.status is OperationStatus.SUCCESS
    assert second.status is OperationStatus.SUCCESS
    assert second.data.item_outcomes[0].acquisition_status is AcquisitionStatus.REUSED
    journal = fixture.root / "audit" / "journal.jsonl"
    events = [
        json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["action"] == "PDF_DISCOVERY_DOWNLOAD"
    assert events[0]["agent_or_tool"] == "scholar-pdf-kit/0.1.0@abcdef1"
    assert events[0]["status"] == "SUCCESS"
    assert events[0]["parameters"]["idempotency_key"].startswith("sha256:")
    assert events[0]["parameters"]["manifest_id"].startswith("ACQ-")
    assert events[0]["outputs"] == [
        (
            "literature/acquisition/RUN-acquisition/"
            f"{events[0]['parameters']['manifest_id']}.json"
        )
    ]


@pytest.mark.parametrize("bad_path", ["../escape", "/absolute", "C:\\escape", "a\\b"])
def test_e1_neg_027_containment_rejects_unsafe_storage_paths(
    tmp_path: Path, bad_path: str
) -> None:
    fixture = make_workspace(tmp_path)
    with pytest.raises(ValueError, match="path"):
        fixture.request(storage_prefix=bad_path)


def test_e1_neg_028_symlinked_destination_cannot_escape(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, fixture.root / "escape", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    outcome = asyncio.run(
        fixture.service().acquire([fixture.request(storage_prefix="escape/pdfs")])
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.committed_count == 0
    assert not list(outside.iterdir())


def test_e1_neg_028_symlinked_manifest_run_directory_cannot_escape(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    outside = tmp_path / "outside-acquisition"
    outside.mkdir()
    acquisition_root = fixture.root / "literature" / "acquisition"
    acquisition_root.mkdir(parents=True)
    try:
        os.symlink(
            outside,
            acquisition_root / "RUN-other",
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    request = fixture.request(run_id="RUN-other")
    outcome = asyncio.run(fixture.service().acquire([request]))
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.committed_count == 0
    assert not list(outside.iterdir())


def test_e1_neg_029_manifest_round_trip_contains_only_relative_paths(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.data.manifest_reference is not None
    manifest_path = (
        fixture.root / outcome.data.manifest_reference.workspace_relative_path
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [record["workspace_relative_path"] for record in payload["records"]]
    paths.append(payload["item_outcomes"][0]["workspace_relative_path"])
    assert all(not Path(value).is_absolute() for value in paths)
    assert all("\\" not in value and ".." not in Path(value).parts for value in paths)
    assert str(fixture.root) not in manifest_path.read_text(encoding="utf-8")


def test_e1_neg_031_api_and_cli_json_use_the_same_statuses(
    tmp_path: Path,
) -> None:
    api_fixture = make_workspace(tmp_path / "api")
    cli_fixture = make_workspace(tmp_path / "cli")
    request = api_fixture.request()
    config = _config(api_fixture, request)
    api_outcome = asyncio.run(
        PDFAcquisitionService.from_config(
            config, audit_sink=InMemoryAuditSink()
        ).acquire(config.requests)
    )

    cli_request = cli_fixture.request()
    cli_config = _config(cli_fixture, cli_request)
    config_path = tmp_path / "acquisition-config.json"
    config_path.write_text(cli_config.model_dump_json(), encoding="utf-8")
    logger_path = tmp_path / "log_event.py"
    _write_logger_script(logger_path)

    result = CliRunner().invoke(
        app,
        ["acquire", str(config_path), "--audit-logger", str(logger_path)],
    )
    assert result.exit_code == 0, result.output
    cli_payload = json.loads(result.stdout)
    assert cli_payload["status"] == api_outcome.status.value
    assert cli_payload["data"]["committed_count"] == api_outcome.data.committed_count
    assert cli_payload["data"]["item_outcomes"][0]["acquisition_status"] == (
        api_outcome.data.item_outcomes[0].acquisition_status.value
    )
    assert cli_payload["data"]["manifest_reference"] == (
        api_outcome.data.manifest_reference.model_dump(mode="json")
    )
    assert cli_payload["errors"] == [
        error.model_dump(mode="json") for error in api_outcome.errors
    ]
    assert cli_payload["warnings"] == api_outcome.warnings


def test_e1_neg_033_unresolved_is_not_a_paywall_claim(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    request = fixture.request(
        source_mode=SourceMode.DISCOVERY,
        source_path=None,
        source_kind=AcquisitionSourceKind.OPENALEX,
        access_status=AccessStatus.UNRESOLVED,
        access_assertion=None,
        requested_source=None,
        selected_source_url=None,
    )
    outcome = asyncio.run(fixture.service().acquire([request]))
    item = outcome.data.item_outcomes[0]
    assert outcome.status is OperationStatus.FAILED
    assert item.acquisition_status is AcquisitionStatus.UNRESOLVED
    assert item.access_status is AccessStatus.UNRESOLVED
    assert item.error is not None
    assert item.error.code == "UNRESOLVED_NO_LEGAL_OA_COPY_FOUND"
    assert "not a paywall determination" in item.error.message.lower()


def test_e1_neg_038_reuse_revalidates_manifest_bound_bytes(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    first = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert first.data.manifest_reference is not None
    manifest = json.loads(
        (
            fixture.root / first.data.manifest_reference.workspace_relative_path
        ).read_text()
    )
    final_path = fixture.root / manifest["records"][0]["workspace_relative_path"]
    final_path.write_bytes(b"corrupted")
    replay = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert replay.status is OperationStatus.FAILED
    assert replay.data.manifest_reference is None
    assert final_path.read_bytes() == b"corrupted"


def test_e1_neg_009_matching_doi_alone_does_not_drive_reuse(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    first = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert first.status is OperationStatus.SUCCESS
    fixture.root.joinpath("inbox", "paper.pdf").write_bytes(pdf_bytes("y"))
    second = asyncio.run(
        fixture.service().acquire([fixture.request(run_id="RUN-different-bytes")])
    )
    assert second.status is OperationStatus.SUCCESS
    assert second.data.item_outcomes[0].acquisition_status is AcquisitionStatus.ACQUIRED
    first_id = first.data.item_outcomes[0].document_id
    second_id = second.data.item_outcomes[0].document_id
    assert first_id is not None and second_id is not None and first_id != second_id
    assert len(list((fixture.root / "pdfs" / "acquired").glob("DOC-*.pdf"))) == 2


def test_e1_neg_040_changed_user_path_bytes_fail_exact_replay(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    request = fixture.request()
    first = asyncio.run(fixture.service().acquire([request]))
    assert first.status is OperationStatus.SUCCESS
    assert first.data.manifest_reference is not None
    final_path = next((fixture.root / "pdfs" / "acquired").glob("DOC-*.pdf"))
    committed = final_path.read_bytes()

    fixture.root.joinpath("inbox", "paper.pdf").write_bytes(pdf_bytes("y"))
    replay = asyncio.run(fixture.service().acquire([request]))
    assert replay.status is OperationStatus.FAILED
    assert replay.errors[0].code == "REPLAY_VERIFICATION_FAILED"
    assert final_path.read_bytes() == committed
    assert (
        len(list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json")))
        == 1
    )


def test_e1_neg_039_duplicate_batch_inputs_coalesce_to_one_manifest(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    audit = InMemoryAuditSink()
    request = _network_request(fixture)
    outcome = asyncio.run(
        fixture.service(FakeTransport(pdf_bytes()), audit_sink=audit).acquire(
            [request, request]
        )
    )
    assert outcome.status is OperationStatus.SUCCESS
    assert outcome.data.requested_count == 1
    assert outcome.data.committed_count == 1
    assert len(outcome.data.item_outcomes) == 1
    assert (
        len(list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json")))
        == 1
    )
    assert len(audit.events) == 1


def test_e1_neg_039_concurrent_duplicate_inputs_coalesce(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    class SlowTransport(FakeTransport):
        async def download(self, **kwargs: Any) -> TransportResult:
            await asyncio.sleep(0.01)
            return await super().download(**kwargs)

    audit = InMemoryAuditSink()
    service = fixture.service(SlowTransport(pdf_bytes()), audit_sink=audit)
    request = _network_request(fixture)

    async def run_both() -> list[Any]:
        return await asyncio.gather(
            service.acquire([request]), service.acquire([request])
        )

    first, second = asyncio.run(run_both())
    assert first.status in {OperationStatus.SUCCESS, OperationStatus.PARTIAL}
    assert second.status in {OperationStatus.SUCCESS, OperationStatus.PARTIAL}
    manifests = list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json"))
    assert len(manifests) == 1
    assert len(audit.events) == 1
    ids = {
        item.document_id
        for outcome in (first, second)
        for item in outcome.data.item_outcomes
        if item.document_id is not None
    }
    assert len(ids) == 1


def test_e1_neg_040_manifest_mutation_is_not_silently_recovered(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    first = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert first.data.manifest_reference is not None
    manifest_path = fixture.root / first.data.manifest_reference.workspace_relative_path
    original_manifest = manifest_path.read_bytes()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["records"][0]["byte_length"] += 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    before = manifest_path.read_bytes()
    replay = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert replay.status is OperationStatus.FAILED
    assert replay.data.manifest_reference is None
    assert manifest_path.read_bytes() == before

    payload = json.loads(original_manifest)
    payload["artifact_checksum"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    checksum_mutation = manifest_path.read_bytes()
    replay = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert replay.status is OperationStatus.FAILED
    assert replay.data.manifest_reference is None
    assert manifest_path.read_bytes() == checksum_mutation


def test_e1_neg_015_http_client_error_is_not_network_failure(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    transport = FakeTransport(b"", result=TransportResult(http_status=400))
    outcome = asyncio.run(
        fixture.service(transport, max_attempts=3).acquire([_network_request(fixture)])
    )
    item = outcome.data.item_outcomes[0]
    assert outcome.status is OperationStatus.FAILED
    assert item.acquisition_status is AcquisitionStatus.FAILED
    assert item.error is not None and item.error.code == "HTTP_CLIENT_ERROR"
    assert len(item.attempts) == 1


def test_e1_neg_042_cancellation_before_commit_is_truthful(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingTransport(FakeTransport):
        async def download(self, **kwargs: Any) -> TransportResult:
            started.set()
            await release.wait()
            return await super().download(**kwargs)

    transport = BlockingTransport(pdf_bytes())
    transport.owns_resources = True

    async def run_cancelled() -> Any:
        service = fixture.service(transport)
        task = asyncio.create_task(service.acquire([_network_request(fixture)]))
        await started.wait()
        task.cancel()
        return await task

    outcome = asyncio.run(run_cancelled())
    assert outcome.status is OperationStatus.CANCELLED
    assert (
        outcome.data.item_outcomes[0].acquisition_status is AcquisitionStatus.CANCELLED
    )
    assert outcome.data.committed_count == 0
    assert transport.closed is True


def test_e1_neg_043_external_input_is_redacted_and_outputs_stay_inside(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    external = tmp_path / "external-paper.pdf"
    external.write_bytes(pdf_bytes())
    request = fixture.request(
        source_path=external,
        allow_external_source=True,
        external_source_label="authorized-copy",
    )
    outcome = asyncio.run(fixture.service().acquire([request]))
    assert outcome.status is OperationStatus.SUCCESS
    record = outcome.data.item_outcomes[0]
    assert record.selected_source is not None
    assert record.selected_source.startswith("external:authorized-copy-")
    assert str(external) not in json.dumps(outcome.model_dump(mode="json"))


def test_e1_neg_045_cross_workspace_identity_is_distinct() -> None:
    digest = source_sha256(pdf_bytes())

    assert deterministic_document_id(
        study_id="STU-one", source_hash=digest, workspace_id="WSP-one"
    ) != deterministic_document_id(
        study_id="STU-one", source_hash=digest, workspace_id="WSP-two"
    )


def test_e1_neg_046_http_success_does_not_fabricate_access(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    transport = FakeTransport(
        pdf_bytes(),
        result=TransportResult(
            http_status=200,
            resolved_url="https://example.test/paper.pdf",
            observed_media_type="text/html",
        ),
    )
    request = _network_request(
        fixture,
        institutional_gateway_url="https://gateway.test",
        forward_proxy_url="https://proxy.test",
    )
    outcome = asyncio.run(fixture.service(transport).acquire([request]))
    assert outcome.status is OperationStatus.SUCCESS
    item = outcome.data.item_outcomes[0]
    assert item.access_status is AccessStatus.UNRESOLVED
    assert item.attempts[0].gateway_used is True
    assert item.attempts[0].forward_proxy_used is True
    assert item.attempts[0].provider_evidence.get("is_oa") is None


@pytest.mark.parametrize("status_code", [401, 403])
def test_e1_neg_033_046_http_access_restriction_is_not_retryable_or_access_claim(
    tmp_path: Path, status_code: int
) -> None:
    fixture = make_workspace(tmp_path)
    transport = FakeTransport(
        b"",
        result=TransportResult(
            http_status=status_code,
            resolved_url="https://example.test/restricted.pdf?token=secret",
        ),
    )
    request = _network_request(fixture)
    outcome = asyncio.run(fixture.service(transport, max_attempts=3).acquire([request]))
    item = outcome.data.item_outcomes[0]
    assert outcome.status is OperationStatus.FAILED
    assert item.acquisition_status is AcquisitionStatus.FAILED
    assert item.error is not None and item.error.code == "SOURCE_ACCESS_RESTRICTED"
    assert item.access_status is AccessStatus.UNRESOLVED
    assert len(item.attempts) == 1
    assert item.attempts[0].http_status == status_code
    assert len(transport.calls) == 1
    assert outcome.data.committed_count == 0


def test_e1_neg_046_selected_source_fallback_is_redacted_in_manifest(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    request = _network_request(
        fixture,
        selected_source_url=None,
        requested_source="https://example.test/paper.pdf?token=secret",
    )
    transport = FakeTransport(pdf_bytes())
    outcome = asyncio.run(fixture.service(transport).acquire([request]))
    assert outcome.status is OperationStatus.SUCCESS
    assert outcome.data.item_outcomes[0].selected_source_url == (
        "https://example.test/paper.pdf"
    )
    manifest = json.loads(
        (
            fixture.root / outcome.data.manifest_reference.workspace_relative_path
        ).read_text(encoding="utf-8")
    )
    assert "secret" not in json.dumps(manifest)


def test_e1_neg_020_user_path_cannot_be_its_content_destination(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    payload = pdf_bytes()
    document_id = deterministic_document_id(
        study_id="STU-one",
        source_hash=source_sha256(payload),
        workspace_id="WSP-test",
    )
    destination = fixture.root / "pdfs" / "acquired" / f"{document_id}.pdf"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    request = fixture.request(source_path=destination)
    outcome = asyncio.run(fixture.service().acquire([request]))
    item = outcome.data.item_outcomes[0]
    assert outcome.status is OperationStatus.FAILED
    assert item.error is not None and item.error.code == "SOURCE_EQUALS_DESTINATION"
    assert destination.read_bytes() == payload


def test_e1_neg_016_017_018_owned_transport_cleanup_and_caller_ownership(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    owned = FakeTransport(pdf_bytes())
    owned.owns_resources = True
    asyncio.run(fixture.service(owned).acquire([_network_request(fixture)]))
    assert owned.closed is True

    def fail_timeout(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.DOWNLOAD:
            raise TimeoutError("injected timeout")

    retry_owned = FakeTransport(pdf_bytes())
    retry_owned.owns_resources = True
    retried = asyncio.run(
        fixture.service(
            retry_owned,
            fault_injector=fail_timeout,
            max_attempts=2,
        ).acquire([_network_request(fixture, run_id="RUN-retry-close")])
    )
    assert retried.data.item_outcomes[0].acquisition_status is (
        AcquisitionStatus.NETWORK_FAILED
    )
    assert retry_owned.closed is True

    def fail_internal(point: AcquisitionFault, identity: str) -> None:
        if point is AcquisitionFault.DOWNLOAD:
            raise RuntimeError("injected internal failure")

    exception_owned = FakeTransport(pdf_bytes())
    exception_owned.owns_resources = True
    failed = asyncio.run(
        fixture.service(
            exception_owned,
            fault_injector=fail_internal,
        ).acquire([_network_request(fixture, run_id="RUN-exception-close")])
    )
    assert failed.data.item_outcomes[0].acquisition_status is AcquisitionStatus.FAILED
    assert exception_owned.closed is True

    injected = FakeTransport(pdf_bytes())
    asyncio.run(
        fixture.service(injected).acquire(
            [_network_request(fixture, run_id="RUN-injected-close")]
        )
    )
    assert injected.closed is False


def test_e1_neg_005_manifest_identity_ignores_retry_history(
    tmp_path: Path,
) -> None:
    direct_fixture = make_workspace(tmp_path / "direct")
    retried_fixture = make_workspace(tmp_path / "retried")

    class RetryOnceTransport(FakeTransport):
        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.failed_once = False

        async def download(self, **kwargs: Any) -> TransportResult:
            if not self.failed_once:
                self.failed_once = True
                raise ConnectionError("injected retryable disconnect")
            return await super().download(**kwargs)

    direct = asyncio.run(
        direct_fixture.service(FakeTransport(pdf_bytes()), max_attempts=1).acquire(
            [_network_request(direct_fixture)]
        )
    )
    retried = asyncio.run(
        retried_fixture.service(
            RetryOnceTransport(pdf_bytes()), max_attempts=2
        ).acquire([_network_request(retried_fixture)])
    )

    assert direct.data.manifest_reference is not None
    assert retried.data.manifest_reference is not None
    assert (
        direct.data.manifest_reference.manifest_id
        == retried.data.manifest_reference.manifest_id
    )
    direct_manifest = json.loads(
        (
            direct_fixture.root / direct.data.manifest_reference.workspace_relative_path
        ).read_text(encoding="utf-8")
    )
    retried_manifest = json.loads(
        (
            retried_fixture.root
            / retried.data.manifest_reference.workspace_relative_path
        ).read_text(encoding="utf-8")
    )
    assert len(direct_manifest["records"][0]["attempts"]) == 1
    assert len(retried_manifest["records"][0]["attempts"]) == 2


def test_e1_neg_034_changed_nonvolatile_source_conflicts_in_same_run(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    first_transport = FakeTransport(pdf_bytes())
    first = asyncio.run(
        fixture.service(first_transport).acquire(
            [
                _network_request(
                    fixture, selected_source_url="https://example.test/a.pdf"
                )
            ]
        )
    )
    assert first.status is OperationStatus.SUCCESS

    second_transport = FakeTransport(pdf_bytes())
    second = asyncio.run(
        fixture.service(second_transport).acquire(
            [
                _network_request(
                    fixture,
                    requested_source="https://example.test/b.pdf",
                    selected_source_url="https://example.test/b.pdf",
                )
            ]
        )
    )
    assert second.status is OperationStatus.FAILED
    assert second.errors[0].code == "IDEMPOTENCY_CONFLICT"
    assert not second_transport.calls
    assert (
        len(list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json")))
        == 1
    )


def test_e1_neg_013_title_similarity_cannot_override_doi_conflict(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(
        fixture.service().acquire(
            [
                _network_request(
                    fixture,
                    doi="10.1000/different",
                    title_similarity=1.0,
                    provider_evidence={
                        "title": "A local fixture study",
                        "author": "Matching Author",
                    },
                )
            ]
        )
    )
    assert outcome.status is OperationStatus.FAILED
    assert outcome.data.item_outcomes[0].acquisition_status is (
        AcquisitionStatus.IDENTITY_MISMATCH
    )
    assert outcome.errors[0].code == "STUDY_DOI_MISMATCH"
    assert outcome.data.committed_count == 0


def test_e1_neg_008_filename_only_content_is_never_reused(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    payload = pdf_bytes()
    document_id = deterministic_document_id(
        study_id="STU-one",
        source_hash=source_sha256(payload),
        workspace_id="WSP-test",
    )
    destination = fixture.root / "pdfs" / "acquired" / f"{document_id}.pdf"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)

    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    item = outcome.data.item_outcomes[0]
    assert outcome.status is OperationStatus.FAILED
    assert item.error is not None and item.error.code == "UNMANAGED_CONTENT"
    assert destination.read_bytes() == payload
    manifests = list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["records"] == []


def test_e1_symlinked_manifest_candidate_cannot_escape(tmp_path: Path) -> None:
    fixture = make_workspace(tmp_path)
    outside = tmp_path / "outside-manifest.json"
    outside.write_text("{}", encoding="utf-8")
    run_root = fixture.root / "literature" / "acquisition" / "RUN-acquisition"
    run_root.mkdir(parents=True)
    candidate = run_root / f"ACQ-{'a' * 64}.json"
    try:
        os.symlink(outside, candidate)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.status is OperationStatus.FAILED
    assert outcome.errors[0].code == "PATH_OUTSIDE_WORKSPACE"
    assert outside.read_text(encoding="utf-8") == "{}"
    assert not list((fixture.root / "pdfs" / "acquired").glob("*.pdf"))


def test_e1_neg_041_operation_status_mapping_preserves_partial_and_failure(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    _add_second_study(fixture)
    acquired = fixture.request()
    unresolved = _network_request(
        fixture,
        study_id="STU-two",
        doi="10.1000/two",
        requested_source=None,
        selected_source_url=None,
    )
    partial = asyncio.run(fixture.service().acquire([acquired, unresolved]))
    assert partial.status is OperationStatus.PARTIAL
    assert partial.data.committed_count == 1
    assert partial.errors

    failed = asyncio.run(
        fixture.service().acquire(
            [
                _network_request(
                    fixture,
                    run_id="RUN-all-failure",
                    requested_source=None,
                    selected_source_url=None,
                )
            ]
        )
    )
    assert failed.status is OperationStatus.FAILED
    assert failed.data.committed_count == 0
    assert failed.errors


def test_e1_neg_026_042_post_commit_audit_cancellation_is_partial_and_recoverable(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)

    class CancellingAuditSink:
        async def has_event(self, manifest_id: str, idempotency_key: str) -> bool:
            return False

        async def append_once(self, event: dict[str, Any]) -> bool:
            raise asyncio.CancelledError

    request = _network_request(fixture)
    transport = FakeTransport(pdf_bytes())
    transport.owns_resources = True
    first = asyncio.run(
        fixture.service(transport, audit_sink=CancellingAuditSink()).acquire([request])
    )
    assert first.status is OperationStatus.PARTIAL
    assert first.data.manifest_reference is not None
    assert first.errors[0].code == "AUDIT_APPEND_CANCELLED"
    assert transport.closed is True

    audit = InMemoryAuditSink()
    second = asyncio.run(fixture.service(audit_sink=audit).acquire([request]))
    assert second.status is OperationStatus.SUCCESS
    assert second.data.item_outcomes[0].acquisition_status is AcquisitionStatus.REUSED
    assert len(audit.events) == 1


def test_e1_neg_021_owned_orphan_cleanup_preserves_manifest_bound_content(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.data.manifest_reference is not None
    manifest = json.loads(
        (
            fixture.root / outcome.data.manifest_reference.workspace_relative_path
        ).read_text(encoding="utf-8")
    )
    relative = manifest["records"][0]["workspace_relative_path"]
    content = fixture.root / relative
    original = content.read_bytes()

    fixture.service()._remove_owned_orphan(fixture.root, relative)
    assert content.read_bytes() == original


def test_e1_neg_010_011_reuse_rejects_profile_version_and_storage_path(
    tmp_path: Path,
) -> None:
    for case in ("profile_version", "storage_path"):
        fixture = make_workspace(tmp_path / case)
        first = asyncio.run(fixture.service().acquire([fixture.request()]))
        assert first.status is OperationStatus.SUCCESS
        updates: dict[str, Any] = {"run_id": "RUN-reuse-check"}
        if case == "profile_version":
            updates["validation_profile_version"] = "999"
        else:
            updates["storage_prefix"] = "other/pdfs"
        second = asyncio.run(fixture.service().acquire([fixture.request(**updates)]))
        item = second.data.item_outcomes[0]
        assert second.status is OperationStatus.FAILED
        assert item.error is not None
        assert item.error.code == "REUSE_BINDING_MISMATCH"


def test_e1_neg_038_reuse_revalidates_structure_even_when_hash_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_workspace(tmp_path)
    first = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert first.data.manifest_reference is not None
    service = fixture.service()
    monkeypatch.setattr(service, "_structural_validation", lambda _path: False)

    replay = asyncio.run(service.acquire([fixture.request()]))
    assert replay.status is OperationStatus.FAILED
    assert replay.data.manifest_reference is None
    assert replay.errors[0].code == "REPLAY_VERIFICATION_FAILED"


def test_e1_neg_005_manifest_identity_is_independent_of_input_order(
    tmp_path: Path,
) -> None:
    first_fixture = make_workspace(tmp_path / "first")
    second_fixture = make_workspace(tmp_path / "second")
    _add_second_study(first_fixture)
    _add_second_study(second_fixture)
    first = first_fixture.request(
        study_id="STU-two",
        doi="10.1000/two",
        source=first_fixture.root / "inbox" / "paper-two.pdf",
    )
    second = second_fixture.request(
        study_id="STU-two",
        doi="10.1000/two",
        source=second_fixture.root / "inbox" / "paper-two.pdf",
    )

    forward = asyncio.run(
        first_fixture.service(FakeTransport(pdf_bytes())).acquire(
            [first_fixture.request(), first]
        )
    )
    reverse = asyncio.run(
        second_fixture.service(FakeTransport(pdf_bytes())).acquire(
            [second, second_fixture.request()]
        )
    )
    assert forward.data.manifest_reference is not None
    assert reverse.data.manifest_reference is not None
    assert (
        forward.data.manifest_reference.manifest_id
        == reverse.data.manifest_reference.manifest_id
    )


def test_e1_neg_007_019_concurrent_distinct_content_identities_converge_safely(
    tmp_path: Path,
) -> None:
    fixture = make_workspace(tmp_path)
    _add_second_study(fixture)
    first = fixture.request()
    second = fixture.request(
        study_id="STU-two",
        doi="10.1000/two",
        source=fixture.root / "inbox" / "paper-two.pdf",
    )
    audit = InMemoryAuditSink()

    async def run_both() -> list[Any]:
        return await asyncio.gather(
            fixture.service(FakeTransport(pdf_bytes()), audit_sink=audit).acquire(
                [first, second]
            ),
            fixture.service(FakeTransport(pdf_bytes()), audit_sink=audit).acquire(
                [second, first]
            ),
        )

    outcomes = asyncio.run(run_both())
    assert all(
        outcome.status in {OperationStatus.SUCCESS, OperationStatus.PARTIAL}
        for outcome in outcomes
    )
    persisted_ids = {
        item.document_id
        for outcome in outcomes
        for item in outcome.data.item_outcomes
        if item.document_id is not None
    }
    assert len(persisted_ids) == 2
    assert (
        len(list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json")))
        == 1
    )
    assert len(list((fixture.root / "pdfs" / "acquired").glob("DOC-*.pdf"))) == 2
    assert len(audit.events) == 1


def _rebind_workspace(root: Path):
    """Rebuild fixture bindings from an existing workspace, as a restart would."""

    parents = []
    for name, artifact_type in (
        ("corpus.json", "corpus_snapshot"),
        ("screening.json", "screening_decisions"),
    ):
        payload = json.loads((root / "literature" / name).read_text(encoding="utf-8"))
        parents.append(
            AcceptedParentBinding(
                artifact_id=payload["artifact_id"],
                artifact_type=artifact_type,
                sha256=canonical_fingerprint(payload),
                workspace_relative_path=f"literature/{name}",
                workspace_id=payload["workspace_id"],
                run_id=payload["run_id"],
                protocol_fingerprint=payload["protocol_fingerprint"],
                corpus_fingerprint=payload["corpus_fingerprint"],
                payload=payload,
            )
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
    return WorkspaceFixture(
        root=root,
        parents=parents,
        binding=binding,
        producer=ProducerProvenance(
            package="scholar-pdf-kit", version="0.1.0", commit="abcdef1"
        ),
    )


_CRASH_CHILD = """
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, __TESTS_DIR__)
from test_atomic_acquisition import _rebind_workspace
from scholar_pdf.acquisition import (
    AcquisitionFault,
    InMemoryAuditSink,
    PDFAcquisitionService,
)


def _die(point, identity):
    # Process death cannot run any cleanup handler, which is exactly the
    # condition the commit-intent marker and OS locks must survive.
    if point is AcquisitionFault.MANIFEST_REPLACE:
        os._exit(42)


fixture = _rebind_workspace(Path(sys.argv[1]))
service = PDFAcquisitionService(
    accepted_parents=fixture.parents,
    workspace_bindings={"WSP-test": fixture.binding},
    producer=fixture.producer,
    audit_sink=InMemoryAuditSink(),
    fault_injector=_die,
)
asyncio.run(service.acquire([fixture.request()]))
print("child completed without crashing", file=sys.stderr)
"""


def _run_crash_child(root: Path) -> subprocess.CompletedProcess[str]:
    tests_dir = str(Path(__file__).parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = tests_dir + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_CHILD.replace("__TESTS_DIR__", repr(tests_dir)),
            str(root),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )


def _intent_for(content: Path) -> Path:
    return content.with_name(content.name + ".commit-intent.json")


def _marker_payload(
    *, idempotency_key: str, document_id: str, study_id: str, relative: str
) -> dict[str, Any]:
    payload = pdf_bytes()
    return {
        "schema_version": "pdf-acquisition-commit-intent-v1",
        "byte_length": len(payload),
        "document_id": document_id,
        "idempotency_key": idempotency_key,
        "source_sha256": source_sha256(payload),
        "study_id": study_id,
        "workspace_relative_path": relative,
    }


def test_e1_crash_at_manifest_replace_recovers_under_the_same_key(
    tmp_path: Path,
) -> None:
    """A killed process leaves a promoted orphan that the same key may adopt."""

    fixture = make_workspace(tmp_path)
    process = _run_crash_child(fixture.root)
    assert process.returncode == 42, process.stderr

    acquired = sorted((fixture.root / "pdfs" / "acquired").glob("DOC-*.pdf"))
    assert len(acquired) == 1
    content = acquired[0]
    # The durable marker is the only evidence separating a crash orphan from an
    # unmanaged file, and it must survive the process death.
    assert _intent_for(content).is_file()
    assert not list((fixture.root / "literature" / "acquisition").glob("*/ACQ-*.json"))
    # Both locks were held by the dead process and must be reclaimable.
    assert list((fixture.root / "literature" / "acquisition").rglob("*.lock"))

    rebound = _rebind_workspace(fixture.root)
    outcome = asyncio.run(rebound.service().acquire([rebound.request()]))
    assert outcome.status is OperationStatus.SUCCESS
    assert outcome.data.manifest_reference is not None
    assert (
        outcome.data.item_outcomes[0].acquisition_status is AcquisitionStatus.ACQUIRED
    )
    # The orphan was adopted, not replaced, and its marker is now redundant.
    assert sorted((fixture.root / "pdfs" / "acquired").glob("DOC-*.pdf")) == [content]
    assert content.read_bytes() == pdf_bytes()
    assert not _intent_for(content).exists()


def test_e1_unmanaged_content_at_the_destination_is_never_adopted(
    tmp_path: Path,
) -> None:
    """A hand-placed file at the content path is not a recoverable orphan."""

    fixture = make_workspace(tmp_path)
    expected = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert expected.status is OperationStatus.SUCCESS
    relative = expected.data.item_outcomes[0].workspace_relative_path
    content = fixture.root / relative
    # Drop the manifest so the content has no manifest-bound reuse record; the
    # only remaining question is whether the path itself is admissible.
    for manifest in (fixture.root / "literature" / "acquisition").rglob("ACQ-*.json"):
        manifest.unlink()

    # Same bytes, same name, but no commit-intent marker: a successful commit
    # consumes its marker, so the only evidence that this path was ever promoted
    # is gone and the path stays unusable.
    assert not _intent_for(content).exists()
    content.unlink()
    content.write_bytes(pdf_bytes())

    rebound = _rebind_workspace(fixture.root)
    outcome = asyncio.run(rebound.service().acquire([rebound.request()]))
    assert outcome.status is OperationStatus.FAILED
    item = outcome.data.item_outcomes[0]
    assert item.error is not None and item.error.code == "UNMANAGED_CONTENT"
    assert content.read_bytes() == pdf_bytes()


def test_e1_orphan_marker_for_another_key_is_never_adopted(
    tmp_path: Path,
) -> None:
    """Recovery evidence is bound to one idempotency key, not to the path."""

    fixture = make_workspace(tmp_path)
    first = asyncio.run(fixture.service().acquire([fixture.request()]))
    relative = first.data.item_outcomes[0].workspace_relative_path
    content = fixture.root / relative
    for manifest in (fixture.root / "literature" / "acquisition").rglob("ACQ-*.json"):
        manifest.unlink()
    content.unlink()
    content.write_bytes(pdf_bytes())
    marker = _marker_payload(
        idempotency_key="sha256:" + "0" * 64,
        document_id="DOC-foreign",
        study_id="STU-one",
        relative=relative,
    )
    _intent_for(content).write_text(
        json.dumps(marker, sort_keys=True), encoding="utf-8"
    )

    rebound = _rebind_workspace(fixture.root)
    outcome = asyncio.run(rebound.service().acquire([rebound.request()]))
    assert outcome.status is OperationStatus.FAILED
    item = outcome.data.item_outcomes[0]
    assert item.error is not None and item.error.code == "UNMANAGED_CONTENT"
    assert content.read_bytes() == pdf_bytes()


def test_e1_orphan_with_marker_but_changed_bytes_fails_verification(
    tmp_path: Path,
) -> None:
    """A marked orphan whose bytes drifted is a replay verification failure."""

    fixture = make_workspace(tmp_path)
    request = fixture.request()
    document_id = deterministic_document_id(
        study_id=request.study_id,
        source_hash=source_sha256(pdf_bytes()),
        workspace_id=request.workspace_id,
    )
    relative = f"pdfs/acquired/{document_id}.pdf"
    acquired_dir = fixture.root / "pdfs" / "acquired"
    acquired_dir.mkdir(parents=True, exist_ok=True)
    content = acquired_dir / f"{document_id}.pdf"
    # The marker claims the canonical bytes, but the file on disk drifted.
    content.write_bytes(pdf_bytes("y"))
    fixture.service()._write_commit_intent(
        content,
        _marker_payload(
            idempotency_key=fixture.service()._preflight([request])[0].idempotency_key,
            document_id=document_id,
            study_id=request.study_id,
            relative=relative,
        ),
    )

    outcome = asyncio.run(fixture.service().acquire([request]))
    assert outcome.status is OperationStatus.FAILED
    item = outcome.data.item_outcomes[0]
    assert item.error is not None
    assert item.error.code == "REPLAY_VERIFICATION_FAILED"
    assert content.is_file(), "verification must not delete unverified content"


def test_e1_stale_locks_from_a_dead_process_are_reclaimed(tmp_path: Path) -> None:
    """A leftover publication lock must not stall a later commit."""

    fixture = make_workspace(tmp_path)
    acquisition_dir = fixture.root / "literature" / "acquisition"
    acquisition_dir.mkdir(parents=True)
    publication_lock = acquisition_dir / ".publication.lock"
    publication_lock.write_bytes(b"0")

    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    assert outcome.status is OperationStatus.SUCCESS
    assert outcome.data.manifest_reference is not None
    assert not publication_lock.exists()


def test_e1_live_lock_is_never_stolen_from_its_owner(tmp_path: Path) -> None:
    """A held lock belongs to a live process and must not be reclaimed."""

    fixture = make_workspace(tmp_path)
    service = fixture.service()
    acquisition_dir = fixture.root / "literature" / "acquisition"
    (acquisition_dir).mkdir(parents=True)
    lock_path = acquisition_dir / ".publication.lock"

    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        assert PDFAcquisitionService._lock_descriptor(descriptor)
        # The probe must report the lock as owned, so it never unlinks a live
        # owner's lock file.
        assert not PDFAcquisitionService._reclaim_stale_lock(lock_path)
        assert lock_path.is_file()
    finally:
        os.close(descriptor)
    lock_path.unlink(missing_ok=True)
    assert service is not None


def test_e1_temporary_cleanup_failure_is_surfaced_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary file that cannot be removed stays visible to the caller."""

    fixture = make_workspace(tmp_path)
    service = fixture.service()
    real_unlink = Path.unlink

    def refuse_staging(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.startswith(".acquisition-"):
            raise PermissionError("injected temporary cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_staging)
    outcome = asyncio.run(service.acquire([fixture.request()]))
    monkeypatch.undo()

    assert outcome.status is OperationStatus.PARTIAL
    assert any("TEMPORARY_CLEANUP_FAILED" in warning for warning in outcome.warnings)
    leftovers = list((fixture.root / "pdfs" / "acquired").glob(".acquisition-*"))
    assert leftovers, "a failed cleanup must keep the file for inspection"


def test_e1_orphan_cleanup_refuses_content_it_does_not_own(tmp_path: Path) -> None:
    """Ownership is proven by identity and bytes, not by path."""

    fixture = make_workspace(tmp_path)
    outcome = asyncio.run(fixture.service().acquire([fixture.request()]))
    record = outcome.data.item_outcomes[0]
    content = fixture.root / record.workspace_relative_path
    original = content.read_bytes()
    service = fixture.service()

    mismatch = service._remove_owned_orphan(
        fixture.root,
        record.workspace_relative_path,
        document_id="DOC-someone-else",
    )
    assert mismatch is not None
    assert mismatch.code == "ORPHAN_OWNERSHIP_MISMATCH"
    assert content.read_bytes() == original

    drifted = service._remove_owned_orphan(
        fixture.root,
        record.workspace_relative_path,
        document_id=record.document_id,
        expected_sha256="sha256:" + "0" * 64,
    )
    assert drifted is not None
    assert drifted.code == "ORPHAN_OWNERSHIP_MISMATCH"
    assert content.read_bytes() == original


def test_e1_orphan_cleanup_reports_unverifiable_manifest(
    tmp_path: Path,
) -> None:
    """An unreadable sibling manifest is reported, not silently ignored."""

    fixture = make_workspace(tmp_path)
    asyncio.run(fixture.service().acquire([fixture.request()]))
    run_dir = fixture.root / "literature" / "acquisition" / "RUN-acquisition"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ACQ-broken.json").write_text("{not json", encoding="utf-8")

    orphan = fixture.root / "pdfs" / "acquired" / "DOC-orphan.pdf"
    orphan.write_bytes(pdf_bytes())
    service = fixture.service()
    anomaly = service._remove_owned_orphan(
        fixture.root,
        "pdfs/acquired/DOC-orphan.pdf",
        document_id="DOC-orphan",
        expected_sha256=source_sha256(pdf_bytes()),
    )
    assert anomaly is not None
    assert anomaly.code == "ORPHAN_MANIFEST_UNVERIFIABLE"
    assert "ORPHAN_MANIFEST_UNVERIFIABLE" in anomaly.details["anomaly_codes"]
    # The orphan is still litter owned by this attempt, so it is removed while
    # the unverifiable manifest is surfaced as an anomaly.
    assert not orphan.exists()
