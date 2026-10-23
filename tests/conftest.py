"""Explicit historical unit-test capability; never enabled automatically."""
import pytest


@pytest.fixture
def legacy_v7_algorithm(monkeypatch):
    """Only individually marked archived economics/ledger tests may request it.

    Current workflow/release/acceptance tests use the real v8-only production
    boundary. This fixture does not grant a v8 publication authorization.
    """
    monkeypatch.setattr('discovery.rulers.AVAILABLE_BUILDS', frozenset({'discovery-v7'}))
    from discovery.rulers import RETIRED_BUILDS
    monkeypatch.setattr('discovery.rulers.RETIRED_BUILDS', RETIRED_BUILDS - {'discovery-v7'})
    monkeypatch.setattr('discovery.builds.AVAILABLE_BUILDS', frozenset({'discovery-v7'}))
    monkeypatch.setattr('scripts.calibrate_discovery.BUILD_VERSION', 'discovery-v7')


@pytest.fixture
def isolated_discovery_authority(monkeypatch):
    """Explicit lease/commit/error unit seam; never current API or release tests."""
    monkeypatch.setattr('jobs.discovery.execution_authorized', lambda session, run: True)
