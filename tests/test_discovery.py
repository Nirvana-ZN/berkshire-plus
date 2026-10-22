import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.discovery_models import DiscoveryRun
from app.main import create_app
from discovery.builds import BUILD_VERSION
from discovery.constants import RUN_STATUSES
from market.quotes import FakeQuoteClient


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


def test_discovery_view_guides_stages_with_typed_structured_results() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    helper = (ROOT / "web/src/views/discoveryViewModel.ts").read_text(
        encoding="utf-8"
    )

    for label in (
        "共同排雷",
        "优质折价",
        "深度价值",
        "合理价成长",
        "轨道门槛",
        "候选竞争",
        "人工裁决",
        "通过",
        "淘汰",
        "数据不足",
        "本层固定规则",
        "尺子版本/改因",
        "计数",
        "指标分布",
        "阈值位置",
        "waiting_confirmation",
        "failed",
        "data_not_ready",
        "ruler_not_ready",
    ):
        assert label in page

    assert "interface DiscoveryRow" in page
    assert "interface StageSummary" in page
    assert "/stages/safety" in page
    assert "/tracks/${view.track}/stages/${view.stage}" in page
    assert "activeRunId" in page
    assert "activeStageKey" in page
    assert "AbortController" in page
    assert "currentRequestSequence" in page
    assert "stagePayloadCache" in page
    assert "selectedTrackStages" in page
    assert "original_verdict" in page
    assert "snapshot_at" in page
    assert "STAGE_POLL_INTERVAL_MS" in page
    assert "STAGE_POLL_INTERVAL_MS = 800" in helper
    assert "onBeforeUnmount" in page
    assert "snapshot_json" not in page
    assert "买入" not in page
    assert "卖出" not in page


def test_discovery_view_requires_override_reason_and_terminal_guidance() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )

    assert "仅瞬时失败可重试" in page
    assert "放弃本批次并重新创建" in page
    assert "Number.isFinite" in page


def test_discovery_view_review_guards_and_adjudication() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    helper = (ROOT / "web/src/views/discoveryViewModel.ts").read_text(
        encoding="utf-8"
    )

    assert "stageCacheKey(runId, view.track, view.stage)" in page
    assert "clearStageCache" in page
    assert "CURRENT_REFRESH_MS" in page
    assert "beforeMutation" in page
    assert "current.run.tracks" in page
    assert "transitionAnnouncement" in page
    assert 'aria-labelledby="`discovery-stage-' in page
    assert "通过结果" in page
    assert "淘汰结果" in page
    assert "数据不足结果" in page
    assert "stageAriaLabel" in page
    assert "stage !== 'adjudication'" in helper
    apply_run = page[page.index("async function applyRun") : page.index("async function readCurrent")]
    assert apply_run.index("schedulePoll()") < apply_run.index("void loadStagesInOrder")
    assert "if (stageControllers.has(cacheKey)) {" in page
    assert "armStageFetchTimeout" in page
    assert "reviewedStageActions" in page
    assert "stagePayloadFingerprint" in page
    assert "sameStageReview" in page
    preflight = page[page.index("async function beforeMutation") : page.index("async function stageMutation")]
    assert "await readCurrent()" in preflight
    assert "loadStage(view, expectedRunId, true)" in preflight
    assert "pointer.attempt !== identity.attempt" in preflight
    assert "CURRENT_FETCH_TIMEOUT_MS" in page
    assert "scheduleCurrentRefresh(CURRENT_RETRY_MS)" in page
    assert "expected_stage_id" in page
    assert "expected_input_fingerprint" in page
    assert "expected_stage_revision" in page
    assert "expected_mutation_version" in page
    assert "stageExpectation(validated.identity)" in page
    assert "attempt: validated.identity.attempt" in page
    assert "beginMutation" in page
    assert "endMutation" in page
    assert "expected_week_key" in page
    assert 'v-if="canAbandonRun(state.run.current, state.run.status)"' in page
    pause = page[page.index("function beginMutation") : page.index("function endMutation")]
    assert "stopPoll()" in pause
    assert "stopCurrentRefresh()" in pause
    assert "currentRequestSequence += 1" in pause
    assert "currentController?.abort()" in pause
    assert "abortStageRequests()" in pause
    resume = page[page.index("function endMutation") : page.index("async function postMutation")]
    assert resume.count("schedulePoll()") == 1
    assert resume.count("scheduleCurrentRefresh(") == 1


def test_discovery_view_renders_actionable_case_cards() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )

    for label in (
        "候选案例",
        "入围原因",
        "关键证据",
        "风险与反方证据",
        "数据缺口与新鲜度",
        "变化分类",
        "值得深研",
        "观察",
        "加入本周深研",
        "候选不足",
    ):
        assert label in page
    assert "尚无结构化反方证据" in page
    assert 'v-for="caseItem in adjudicationCases(view, row.code)"' in page
    assert '<details class="case-details">' in page
    assert "/research" in page
    assert "stageExpectation(validated.identity)" in page
    assert "keeperCount(view)" in page
    assert "router.push(`/research/${data.id}`)" in page
    assert "买入" not in page
    assert "卖出" not in page


def test_candidate_cards_fail_closed_with_retry_and_scoped_drafts() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    helper = (ROOT / "web/src/views/discoveryViewModel.ts").read_text(
        encoding="utf-8"
    )

    loader = page[
        page.index("async function loadAdjudicationPayload(") :
        page.index("async function loadAdjudicationPayloads(")
    ]
    assert "delete nextPayloads[track]" in loader
    assert "response.ok" in loader
    assert "候选案例数据无效" in loader
    assert "候选案例读取超时" in loader
    assert "kind: 'error'" in loader
    assert "@click=\"retryAdjudicationPayload(view)\"" in page
    assert "候选案例不可用于裁决" in page
    assert "`${item.track}:${item.code}`" in page
    assert "change.classifications" in helper
    assert "weeklyChangeText(view, caseItem)" in page


def test_case_cards_enforce_keeper_limit_and_show_weekly_details() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )

    assert "keeperRange(keeperCount(view)) === 'over-limit'" not in page
    assert "已达到 3 家最低候选数" not in page
    assert "通过候选竞争固定尺子" not in page
    assert "candidateSurfacedReason(row" in page
    for label in (
        "变化分类",
        "上周状态",
        "本周状态",
        "上周理由",
        "本周理由",
    ):
        assert label in page


def test_discovery_view_renders_compact_weekly_change_rows() -> None:
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(
        encoding="utf-8"
    )
    helper = (ROOT / "web/src/views/discoveryViewModel.ts").read_text(
        encoding="utf-8"
    )

    mappings = {
        "new": "本周新入围",
        "dropped": "本周退出",
        "reentered": "本周重新入围",
        "continuous": "连续入围",
        "reason_changed": "入围原因变化",
        "data_changed": "数据变化",
        "rule_changed": "尺子变化",
        "both_changed": "数据与尺子同时变化",
        "decision_changed": "人工裁决变化",
    }
    for value, label in mappings.items():
        assert re.search(rf"\b{value}: '{label}'", helper)

    assert "本周变化" in page
    assert 'v-for="change in view.payload.changes"' in page
    assert "changeName(view, change)" in page
    assert "discoveryChangeLabels(change)" in page
    assert "weeklyStateText(change, 'previous')" in page
    assert "weeklyStateText(change, 'current')" in page
    assert "weeklyReasonText(change, 'previous')" in page
    assert "weeklyReasonText(change, 'current')" in page


def test_home_reuses_refresh_for_current_discovery_and_links_jobs() -> None:
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    helper = (ROOT / "web/src/views/homeViewModel.ts").read_text(
        encoding="utf-8"
    )

    assert "discovery: '每周发现'" in home
    assert "if (job.type === 'discovery') return '/discover'" in home
    assert "'/api/discoveries/current'" in helper
    assert "本周批次" in home
    assert "if (workbenchController !== null) return" in home
    assert "requestSequence" in home
    assert "new AbortController()" in home
    assert "AbortSignal.timeout(WORKBENCH_FETCH_TIMEOUT_MS)" in home
    assert "workbenchController?.abort()" in home
    assert "signal," in home
    assert "jobStatusText(job.type, job.status)" in home
    assert home.count("window.setInterval(") == 1
    assert "WORKBENCH_REFRESH_MS" in home


def test_jobs_include_discovery_runs(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "app.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
    )
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = DiscoveryRun(
                user_id=1,
                week_key="2026-W38",
                current=True,
                status="running",
                created_at=now,
                updated_at=now,
                snapshot_at=now,
                data_version="data-v1",
                financial_version="financial-v1",
                valuation_version="valuation-v1",
                algorithm_version=BUILD_VERSION,
            )
            session.add(run)
            session.commit()
            run_id = run.id

        jobs = client.get("/api/jobs").json()["jobs"]

    assert any(
        item["id"] == run_id
        and item["type"] == "discovery"
        and item["status"] == "running"
        and item["name"] == "2026-W38"
        for item in jobs
    )
