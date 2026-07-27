"""Validation for the T1.4 src/ package scaffolding.

Each of the five packages named in architecture Section 5 (acquisition,
etl, scoring, pipeline, dashboard) needs to exist as an importable
top-level package before any of the later M1 tasks (T1.5+) can add real
modules inside them. This file's only job is to prove that scaffolding is
in place and importable -- it deliberately does not test any behavior
(there isn't any yet; every package here is an empty __init__.py).
"""

import importlib

import pytest

PACKAGE_NAMES = ["acquisition", "etl", "scoring", "pipeline", "dashboard"]


@pytest.mark.parametrize("package_name", PACKAGE_NAMES)
def test_each_architecture_named_package_imports_without_error(package_name):
    """src/<package>/__init__.py exists for each of the five modules
    architecture Section 5 names (src/acquisition/, src/etl/,
    src/scoring/, src/pipeline/, src/dashboard/), installed as top-level
    packages via the pyproject.toml package list (T1.1).

    This test checks that `import <package_name>` succeeds for all five --
    the exact validation spec/tasks.md's T1.4 row calls for ("Each
    package imports without error").

    Edge case: this is parametrized over all five names rather than one
    combined test, specifically so a single broken package (e.g. a typo
    in one __init__.py, or a package missing from pyproject.toml's
    package list) fails with that package's name in the pytest output,
    instead of one opaque "some package is broken" failure that would
    require re-running each import manually to find which one.
    """
    module = importlib.import_module(package_name)
    assert module is not None
