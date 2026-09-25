"""WP01-E2 extraction atomicity and crash-recovery tests (offline, hermetic)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_extraction_contract import acquired_fixture, usable_engine

from scholar_pdf.acquisition import InMemoryAuditSink
from scholar_pdf.acquisition_models import OperationStatus
from scholar_pdf.extraction import (
    ExtractionCommitError,
    ExtractionFault,
)
from scholar_pdf.extraction_engines import EngineRegistry
from scholar_pdf.extraction_models import ExtractionEngine, ExtractionManifest
from scholar_pdf.frontmatter import parse_bound_frontmatter

_TESTS_DIR = str(Path(__file__).resolve().parent)


def _registry() -> EngineRegistry:
    return EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()})


# A child process that dies at the sidecar publication point.  It re-derives the
# workspace bindings from disk, so it exercises the restart path too.  Paths are
# passed as argv so no quoting survives into the child's source.
_CRASH_CHILD = """
import asyncio, os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from test_extraction_contract import acquired_fixture, usable_engine
from scholar_pdf.acquisition import InMemoryAuditSink
from scholar_pdf.extraction import ExtractionFault
from scholar_pdf.extraction_engines import EngineRegistry
from scholar_pdf.extraction_models import ExtractionEngine


def _die(point, identity):
    # Process death runs no cleanup handler.
    if point is ExtractionFault.SIDECAR_REPLACE:
        os._exit(9)


workspace = acquired_fixture(Path(sys.argv[2]))
service = workspace.service(
    EngineRegistry({ExtractionEngine.PYMUPDF: usable_engine()}),
    fault_injector=_die,
    audit_sink=InMemoryAuditSink(),
)
asyncio.run(service.extract([workspace.request()]))
print("child completed without crashing", file=sys.stderr)
"""


def test_e2_sidecar_is_the_commit_marker_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    """Re-running the same request reuses the sidecar and never re-extracts."""

    fixture = acquired_fixture(tmp_path)
    engine = usable_engine()
    service = fixture.service(EngineRegistry({ExtractionEngine.PYMUPDF: engine}))
    request = fixture.request()

    first = asyncio.run(service.extract([request]))
    second = asyncio.run(service.extract([request]))

    assert first.status is OperationStatus.SUCCESS
    assert second.status is OperationStatus.SUCCESS
    # Same sidecar, same checksum, no second run directory.
    assert first.data.manifest_reference == second.data.manifest_reference
    sidecars = list((fixture.root / "literature" / "extraction").rglob("EXT-*.json"))
    assert len(sidecars) == 1
    # The engine is only invoked on the first (real) run.
    assert len(engine.calls) == 1


def test_e2_crash_before_sidecar_leaves_no_authoritative_output(
    tmp_path: Path,
) -> None:
    """A crash between promotion and sidecar write leaves bytes but no claim.

    The promoted file is real content, but with no sidecar it is not
    authoritative.  Process death cannot run a cleanup handler, which is exactly
    the condition the commit-intent marker and the publication lock must
    survive -- so the crash runs in a real child process.
    """

    fixture = acquired_fixture(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, _TESTS_DIR, str(fixture.root)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,  # the child is *expected* to die
    )
    assert completed.returncode == 9, completed.stderr
    # The crash prevented the sidecar: nothing authoritative was published.
    assert not list((fixture.root / "literature" / "extraction").rglob("EXT-*.json"))


def test_e2_fault_injection_leaves_no_partial_sidecar(
    tmp_path: Path,
) -> None:
    """An abort before publication leaves no sidecar and no staging litter.

    The injected fault raises, exactly as a real abort would; what matters is
    that the workspace is left with no half-published claim.
    """

    fixture = acquired_fixture(tmp_path)

    def die(point: ExtractionFault, identity: str) -> None:
        if point is ExtractionFault.ENGINE:
            raise RuntimeError("injected engine crash")

    service = fixture.service(
        _registry(), fault_injector=die, audit_sink=InMemoryAuditSink()
    )
    with pytest.raises(RuntimeError):
        asyncio.run(service.extract([fixture.request()]))

    assert not list((fixture.root / "literature" / "extraction").rglob("EXT-*.json"))
    assert not list((fixture.root / "extracted").glob("*.tmp"))


def test_e2_sidecar_verification_detects_field_mutation(tmp_path: Path) -> None:
    """Any sidecar field mutation fails verification (E2-NEG-009)."""

    fixture = acquired_fixture(tmp_path)
    service = fixture.service(_registry())
    outcome = asyncio.run(service.extract([fixture.request()]))
    reference = outcome.data.manifest_reference
    assert reference is not None
    sidecar = fixture.root / reference.workspace_relative_path
    manifest = ExtractionManifest.model_validate_json(
        sidecar.read_text(encoding="utf-8")
    )
    # A clean manifest verifies.
    service.verify_manifest(manifest, verify_bytes=True)

    # Mutating the committed_at provenance field must be detected.
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    raw["committed_at"] = "2026-01-02T00:00:00Z"
    sidecar.write_text(json.dumps(raw), encoding="utf-8")
    mutated = ExtractionManifest.model_validate_json(
        sidecar.read_text(encoding="utf-8")
    )
    with pytest.raises(ExtractionCommitError):
        service.verify_manifest(mutated, verify_bytes=True)


def test_e2_audit_event_is_appended_once_per_commit(tmp_path: Path) -> None:
    fixture = acquired_fixture(tmp_path)
    audit = InMemoryAuditSink()
    service = fixture.service(_registry(), audit_sink=audit)
    request = fixture.request()
    asyncio.run(service.extract([request]))
    asyncio.run(service.extract([request]))  # replay must not double-log
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "PDF_TEXT_EXTRACTION"


def test_e2_body_and_frontmatter_survive_a_restart(tmp_path: Path) -> None:
    """After a crash, a fresh service re-verifies the committed file."""

    fixture = acquired_fixture(tmp_path)
    service = fixture.service(_registry())
    outcome = asyncio.run(service.extract([fixture.request()]))
    reference = outcome.data.manifest_reference
    assert reference is not None
    document_id = fixture.acquisition.records[0].document_id
    extracted = fixture.root / "extracted" / f"{document_id}.md"

    # A brand-new service instance (as after restart) re-verifies cleanly.
    fresh = fixture.service(_registry())
    manifest = ExtractionManifest.model_validate_json(
        (fixture.root / reference.workspace_relative_path).read_text(encoding="utf-8")
    )
    fresh.verify_manifest(manifest, verify_bytes=True)

    values, _body = parse_bound_frontmatter(extracted.read_bytes())
    assert values["document_id"] == document_id


def test_e2_pos_005_clean_isolated_wheel_import_and_help(tmp_path: Path) -> None:
    """The E2 surface installs, imports, and answers --help from a clean wheel.

    PDF-012: `pyyaml` is declared, so a minimal install of the wheel alone must
    be able to import the extraction package and drive the CLI, with none of the
    heavy engine extras installed.
    """

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to execute the packaging acceptance test"

    repository_root = Path(__file__).resolve().parents[1]
    wheel_directory = tmp_path / "dist"
    environment_directory = tmp_path / "venv"
    offline_environment = {**os.environ, "UV_OFFLINE": "1"}

    def run(command: list[str], **extra: object) -> subprocess.CompletedProcess[str]:
        """Run a subprocess and capture output; every exit code is asserted."""
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **extra,
        )

    build = run(
        [uv, "build", "--wheel", "--out-dir", str(wheel_directory)],
        cwd=repository_root,
        env=offline_environment,
        timeout=180,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_directory.glob("scholar_pdf_kit-*.whl"))
    assert len(wheels) == 1

    create_environment = run(
        [uv, "venv", "--python", sys.executable, str(environment_directory)],
        timeout=60,
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
    install = run(
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
        timeout=180,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    # The E2 public surface imports from the wheel alone.
    public_import = run(
        [
            str(environment_python),
            "-I",
            "-c",
            (
                "from scholar_pdf import PDFExtractionService, ExtractionRequest, "
                "parse_bound_frontmatter, compute_extraction_idempotency_key; "
                "print(PDFExtractionService.__name__, ExtractionRequest.__name__, "
                "parse_bound_frontmatter.__name__, "
                "compute_extraction_idempotency_key.__name__)"
            ),
        ],
        cwd=tmp_path,
        timeout=60,
    )
    assert public_import.returncode == 0, public_import.stdout + public_import.stderr
    assert (
        "PDFExtractionService ExtractionRequest parse_bound_frontmatter "
        "compute_extraction_idempotency_key" in public_import.stdout
    )

    # Importing the package must not drag in a heavy or optional engine.
    lightweight = run(
        [
            str(environment_python),
            "-I",
            "-c",
            (
                "import sys, scholar_pdf; "
                "print(sorted(m for m in ('fitz', 'docling') if m in sys.modules))"
            ),
        ],
        cwd=tmp_path,
        timeout=60,
    )
    assert lightweight.returncode == 0, lightweight.stdout + lightweight.stderr
    assert lightweight.stdout.strip() == "[]", (
        "package import must stay lazy: " + lightweight.stdout
    )

    entrypoint = scripts_directory / (
        "scholar-pdf.exe" if os.name == "nt" else "scholar-pdf"
    )
    for arguments, expected in (
        (["--help"], "extract-run"),
        (["extract-run", "--help"], "--audit-logger"),
    ):
        result = run([str(entrypoint), *arguments], cwd=tmp_path, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert expected in result.stdout
