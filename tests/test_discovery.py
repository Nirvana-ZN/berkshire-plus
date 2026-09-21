from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_views_show_weekly_discovery() -> None:
    app = (ROOT / "web/src/App.vue").read_text(encoding="utf-8")
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    router = (ROOT / "web/src/router/index.ts").read_text(encoding="utf-8")
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    assert 'to="/discover"' in app
    assert 'to="/discover"' in home
    assert "DiscoveryView" in router
    assert "/api/discoveries/current" in page
    assert "fetch('/api/discoveries'" in page
    assert "每周发现" in page
    assert "买入" not in page
    assert "<pre" not in page.lower()
