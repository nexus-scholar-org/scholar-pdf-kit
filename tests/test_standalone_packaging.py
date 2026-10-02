"""E3/T-110b regression guard: no relative editable sibling source, direct ref.

scholar-pdf-kit declared its only runtime sibling as a BARE name inside the
``search`` OPTIONAL extra::

    [project.optional-dependencies]
    search = ["scholar-search-kit"]

    [tool.uv.sources]
    scholar-search-kit = { path = "../scholar-search-kit", editable = true }

That relative editable source resolves only inside the harness monorepo (or
beside a sibling checkout on disk). For a standalone
``uv pip install scholar-pdf-kit[search]`` of this kit's wheel the relative path
is not consulted at all, so METADATA carried a bare
``Requires-Dist: scholar-search-kit`` that no index can satisfy; for a git
checkout of this kit alone pip/uv aborts with
``has no subdirectory '../scholar-search-kit'``.

This test locks in the T-110b fix: the sibling is a PEP 508 direct git reference
pinned to a full 40-hex canonical SHA and no relative path source may come back.
It is hermetic -- it reads the checked-in ``pyproject.toml`` only, never touches
the network, and never invokes ``uv``. When a wheel has already been built into
``dist/`` the built METADATA is additionally checked.

Unlike the sibling kits, the ``scholar-*`` requirement here lives in an
``optional-dependencies`` extra, so this module asserts over BOTH
``project.dependencies`` and every ``project.optional-dependencies`` group.
"""

from __future__ import annotations

import re
import tomllib
import zipfile
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# scholar-<name>[@ git+https://github.com/nexus-scholar-org/scholar-<name>@<40-hex>]
# with an optional PEP 508 extras suffix, e.g. scholar-pdf-kit[extract].
DIRECT_REF = re.compile(
    r"^scholar-[a-z0-9-]+(\[[a-z0-9,.-]+\])? @ git\+https://github\.com/nexus-scholar-org/scholar-[a-z0-9-]+@[0-9a-f]{40}$"
)

# The canonical main SHA recorded at E3/T-110 dispatch time.
EXPECTED_SHA = {
    "scholar-search-kit": "911d864fcb6a706d4c0339f80524a46f591e2cad",
}


def _load() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _all_declared_requirements() -> list[str]:
    """Every declared requirement: base deps plus every optional-extra group.

    The ``scholar-*`` ref lives in the ``search`` extra here, so a base-deps-only
    scan would vacuously pass and lock nothing.
    """
    project = _load()["project"]
    reqs = list(project.get("dependencies", []))
    for extra_reqs in project.get("optional-dependencies", {}).values():
        reqs.extend(extra_reqs)
    return reqs


def test_every_sibling_is_a_sha_pinned_direct_git_reference() -> None:
    reqs = _all_declared_requirements()
    siblings = [d for d in reqs if d.startswith("scholar-")]
    assert siblings, "expected declared scholar-* sibling requirements"

    for dep in siblings:
        assert DIRECT_REF.match(dep), f"not a SHA-pinned direct git reference: {dep!r}"

    declared = {re.split(r"[ @\[]", d, maxsplit=1)[0]: d for d in siblings}
    assert set(declared) == set(EXPECTED_SHA), (
        f"unexpected sibling set: {sorted(declared)}"
    )
    for name, sha in EXPECTED_SHA.items():
        assert declared[name].endswith(sha), (
            f"{name} is not pinned to canonical main {sha}: {declared[name]!r}"
        )


def test_sibling_ref_is_declared_in_the_search_extra() -> None:
    """Pin the extra that owns the ref, so moving it cannot silently pass."""
    search = _load()["project"]["optional-dependencies"]["search"]
    assert any(
        "scholar-search-kit" in d and d.endswith(EXPECTED_SHA["scholar-search-kit"])
        for d in search
    ), f"the `search` extra no longer carries the pinned ref: {search}"


def test_no_relative_editable_sibling_source_can_come_back() -> None:
    sources = _load().get("tool", {}).get("uv", {}).get("sources", {})
    relative = {
        name: src
        for name, src in sources.items()
        if isinstance(src, dict) and "path" in src
    }
    assert not relative, (
        f"relative sibling sources break standalone installs: {relative}"
    )


def test_built_wheel_metadata_carries_the_direct_refs() -> None:
    wheels = sorted((PYPROJECT.parent / "dist").glob("*.whl"))
    if not wheels:
        import pytest

        pytest.skip("no built wheel in dist/; run `uv build --wheel .` first")

    archive = zipfile.ZipFile(wheels[-1])
    metadata_name = next(
        n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
    )
    requires = [
        line.removeprefix("Requires-Dist: ")
        for line in archive.read(metadata_name).decode().splitlines()
        if line.startswith("Requires-Dist: ")
    ]

    # A bare sibling name in METADATA is exactly the bug: unresolvable outside
    # the monorepo because no index serves these kits.
    bare = [r for r in requires if "scholar-" in r and "git+" not in r]
    assert not bare, f"bare scholar-* Requires-Dist is unresolvable: {bare}"

    git_requires = [r for r in requires if "git+" in r]
    assert len(git_requires) == len(EXPECTED_SHA), (
        f"expected {len(EXPECTED_SHA)} git direct refs, got {git_requires}"
    )
    for name, sha in EXPECTED_SHA.items():
        # hatchling keeps the PEP 508 `name @ git+...` spacing and appends the
        # environment marker for the extra, so match the ref as a substring
        # rather than asserting endswith.
        expected_ref = f"{name} @ git+https://github.com/nexus-scholar-org/{name}@{sha}"
        assert any(expected_ref in r for r in git_requires), (
            f"missing {name}@{sha} in METADATA: {git_requires}"
        )
