import re
from pathlib import Path

from discovery.constants import RUN_STATUSES


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


def test_discovery_view_statuses_match_backend_contract() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    status_tuple = re.search(
        r"const DISCOVERY_RUN_STATUSES = \[(.*?)\] as const",
        page,
        re.DOTALL,
    )
    assert status_tuple is not None
    frontend_statuses = set(re.findall(r"'([^']+)'", status_tuple.group(1)))
    assert frontend_statuses == RUN_STATUSES

    mapping = re.search(
        r"const STATUS_ZH: Record<DiscoveryRunStatus, string> = \{(.*?)\n\}",
        page,
        re.DOTALL,
    )
    assert mapping is not None
    mapped_statuses = set(
        re.findall(r"^\s{2}([a-z_]+):", mapping.group(1), re.MULTILINE)
    )
    assert mapped_statuses == RUN_STATUSES
    assert "function isDiscoveryRun(value: unknown): value is DiscoveryRun" in page
    assert page.count("isDiscoveryRun(body)") == 2
    assert "response.json()) as DiscoveryRun" not in page


def test_discovery_view_announces_async_states() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    live_region = re.search(
        r'<p\s+role="status"\s+aria-live="polite"\s+'
        r'aria-atomic="true">\s*\{\{ announcement \}\}\s*</p>',
        page,
    )
    assert live_region is not None
    for message in (
        "正在读取本周批次",
        "正在创建本周发现",
        "本周发现创建成功",
        "本周尚未创建批次",
        "无法读取本周发现",
    ):
        assert message in page
