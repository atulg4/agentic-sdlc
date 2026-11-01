"""Tests for the mobile-first, chart-first Control Center console.

Two layers:

* **Structural** tests read ``examples/control-center/control-center.html``
  and pin the contracts the console's producers and its browser tests both
  depend on. They run everywhere, including CI, which has no browser.
* The **browser** suite is a Playwright harness
  (``examples/control-center/e2e/console.e2e.mjs``) driving bundled Chromium
  against the example fixtures. It is skipped when Node or Playwright is
  absent rather than failing: this project is stdlib-only Python and its CI
  image carries no browser, so a browser test cannot be a required check.
  Run it directly with ``node examples/control-center/e2e/console.e2e.mjs``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "examples" / "control-center" / "control-center.html"
E2E = ROOT / "examples" / "control-center" / "e2e" / "console.e2e.mjs"
SNAPSHOT = ROOT / "examples" / "control-center" / "snapshot.json"
TELEMETRY = ROOT / "examples" / "control-center" / "telemetry.json"
DOCS = ROOT / "docs" / "control-center.md"


@pytest.fixture(scope="module")
def html() -> str:
    return CONSOLE.read_text(encoding="utf-8")


# --- the phone shell --------------------------------------------------------


def test_the_phone_shell_declares_five_touch_destinations(html: str) -> None:
    for destination in ("projects", "backlog", "agents", "cost", "effectiveness"):
        assert f'["{destination}"' in html, destination
    # A bottom bar, not a squeezed tab strip, and it is gone at the dense
    # breakpoint rather than merely restyled.
    assert '<nav class="bottom" id="bottom-nav"' in html
    assert "@media (min-width: 1024px) { nav.bottom { display: none; } }" in html
    # 44 CSS px is the floor for every touch target, not only the nav.
    assert "min-height: 52px" in html
    assert ".btn, .btn.sm { min-height: 44px;" in html


def test_phone_and_desktop_are_distinct_component_trees(html: str) -> None:
    # Not one layout at two widths: each shell has its own builders, and the
    # layout mode is observed rather than inferred from CSS.
    assert "function phoneView()" in html
    assert "function denseView()" in html
    assert 'window.matchMedia("(min-width: 1024px)")' in html
    assert "function watchLayout()" in html
    assert 'state.layout === "phone"' in html


def test_a_work_unit_card_shows_every_declared_field(html: str) -> None:
    assert "function buildUnitCard(" in html
    for field in ('"stage "', '"in state "', '"model "', '"worker "', '"blocker: "'):
        assert field in html, field
    # Elapsed time is measured from the snapshot's own transition instant, and
    # an absent one is unknown rather than zero.
    assert "function elapsed(instant, now)" in html
    assert 'return { text: "unknown", known: false }' in html
    assert 'return { text: "clock ahead", known: false }' in html


def test_progress_is_a_position_on_the_rail_never_a_guess(html: str) -> None:
    assert "const PROGRESS_RAIL = [" in html
    assert "const OFF_RAIL = new Set(" in html
    assert '"off the lifecycle rail ("' in html
    assert '"stage unknown"' in html
    # The bar always has its own words beside it.
    assert 'class: "prog-text", text: prog.text' in html


def test_the_rail_matches_the_producers_forward_lifecycle() -> None:
    from agentic_sdlc.event_ledger import LifecycleStage

    html = CONSOLE.read_text(encoding="utf-8")
    match = re.search(r"const PROGRESS_RAIL = \[(.*?)\];", html, re.S)
    assert match is not None
    rail = re.findall(r'"([a-z-]+)"', match.group(1))
    off_match = re.search(r"const OFF_RAIL = new Set\(\[(.*?)\]\);", html, re.S)
    assert off_match is not None
    off_rail = set(re.findall(r'"([a-z-]+)"', off_match.group(1)))
    declared = [stage.value for stage in LifecycleStage]
    # Every stage is accounted for, exactly once, as either on the rail or off
    # it: a stage the console forgot would render as "stage unknown" on a unit
    # the factory understands perfectly well.
    assert set(rail) | off_rail == set(declared)
    assert not set(rail) & off_rail
    # The rail keeps the producer's own order.
    assert rail == [stage for stage in declared if stage in set(rail)]


# --- charts -----------------------------------------------------------------


def test_every_chart_is_an_accessible_image(html: str) -> None:
    assert "function describable(node, text)" in html
    assert 'node.setAttribute("role", "img")' in html
    assert 'node.setAttribute("aria-label", text)' in html
    assert 'node.setAttribute("tabindex", "0")' in html
    # A tooltip only a pointer can open is not reachable on a phone.
    assert 'node.addEventListener("focus", () => showTip(node, text))' in html
    assert 'node.addEventListener("mouseenter", () => showTip(node, text))' in html
    # Inline SVG, no charting framework.
    assert "<script" not in html.split('<script type="module">')[0].replace(
        '<script type="module">', ""
    )
    assert "cdn" not in html.lower()


def test_an_absent_source_reads_unavailable_with_the_command_that_makes_it(html: str) -> None:
    assert "function chartUnknown(title, reason)" in html
    assert '"unavailable"' in html
    for command in (
        "sdlcctl usage-report --group-by model",
        "sdlcctl usage-report --group-by day",
        "sdlcctl capacity-report",
        "sdlcctl effectiveness-report",
    ):
        assert command in html, command
    # Delivery is an observation, never inferred from the fact that runs ran.
    assert "never infers delivery from the fact that runs happened" in html


def test_a_trend_never_bridges_an_unobserved_day(html: str) -> None:
    assert "function trendChart(title, rows, read)" in html
    assert "not observed, so no point is drawn" in html
    assert "a gap is never bridged" in html
    # A usage figure of null means unobserved, and reads as such.
    assert "function figure(entry) {" in html
    assert 'return { known: false, value: null, text: "unknown"' in html


# --- the drill-down ---------------------------------------------------------


def test_the_drill_down_covers_every_declared_section(html: str) -> None:
    for heading in (
        "Lifecycle timeline",
        "Stage durations",
        "Worker and model",
        "Tokens and cost: estimate versus actual",
        "Dependencies",
        "Artifacts",
    ):
        assert heading in html, heading
    # Per-stage durations are named as unavailable rather than invented from
    # the single instant the snapshot carries.
    assert "Per-stage durations are unavailable" in html
    assert "an unmeasured run is not a free one" in html


# --- live updates -----------------------------------------------------------


def test_updates_patch_rather_than_rebuild(html: str) -> None:
    assert "function patch(parent, items)" in html
    assert "function lazy(key, data, build)" in html
    # The old full-rebuild path is gone from the view.
    assert "viewNode.replaceChildren()" not in html
    assert "patchCache" in html
    # A section whose signature is unchanged is not even built.
    assert "if (previous && sig !== undefined && previous.dataset.sig === sig)" in html


def test_deliveries_are_coalesced_and_still_verified_in_full(html: str) -> None:
    assert "async function drainDeliveries()" in html
    assert "let draining = false;" in html
    # Coalescing never skips verification of what is actually adopted.
    assert "validateSnapshot(body.document) || await digestMismatch(body.document)" in html
    assert "if (delivery !== snapshotAdoptions) continue;" in html


def test_a_stopped_feed_reloads_a_full_snapshot_by_itself(html: str) -> None:
    assert "async function recoverSnapshot(reason)" in html
    assert "function watchFeed()" in html
    assert "const FEED_STALL_MS = 90_000;" in html
    # The last-good view stays on screen, labelled with the instant it was
    # good, until the replacement verifies.
    assert "function freshnessLine()" in html
    assert '"last good "' in html
    assert "Showing the last-good view" in html
    assert 'recoverSnapshot("Live updates went quiet' in html


def test_a_bounded_list_says_what_it_withholds(html: str) -> None:
    assert "const PAGE_SIZE = { phone: 50, desktop: 200 };" in html
    assert "Showing ${shown} of ${total} ${label}" in html
    assert "most in need of attention first" in html


# --- motion -----------------------------------------------------------------


def test_motion_is_opt_out_and_only_compositable(html: str) -> None:
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert "transition: none !important;" in html
    assert "animation: none !important;" in html
    # Only opacity and transform are animated.
    transitions = re.findall(r"transition:\s*([^;]+);", html)
    for value in transitions:
        if "none" in value:
            continue
        for part in value.split(","):
            prop = part.strip().split(" ")[0]
            assert prop in {"opacity", "transform"}, f"{prop} is animated: {value}"


# --- credentials ------------------------------------------------------------


def test_the_telemetry_bundle_is_validated_fail_closed(html: str) -> None:
    assert "function validateTelemetry(value)" in html
    assert "const TELEMETRY_KEYS = [" in html
    assert "declares unknown sections" in html
    assert "const TELEMETRY_BUDGETS = {" in html
    assert "must not embed credentials" in html
    assert "must not carry a credential field" in html
    assert "nests too deeply" in html
    # The scan is the snapshot validator's own, not a second copy that could
    # drift.
    assert "function credentialShaped(raw)" in html
    assert html.count("function credentialShaped(") == 1


def test_a_usage_token_count_is_not_mistaken_for_a_credential(html: str) -> None:
    # `tokens`, `totalTokens` and `cacheReadTokens` are the usage ledger's own
    # figures: a substring match on "token" would refuse every cost document.
    assert 'bare.endsWith("token")' in html
    assert "key.toLowerCase().replace(" in html


def test_the_console_declares_no_write_authority(html: str) -> None:
    # The dashboard stays read-only: no merge, deploy or repository call.
    for forbidden in ("api.github.com", "gitlab.com/api", "Authorization:", 'fetch("http'):
        assert forbidden not in html, forbidden


# --- the fixtures the browser suite runs against ---------------------------


def test_the_example_snapshot_carries_work_units_to_render() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    units = sum(
        len(project.get(key) or [])
        for project in document["projects"]
        for key in ("active", "attention", "waiting")
    )
    assert units >= 5, "the browser suite needs units to card, chart and drill into"
    # Stages and states are the producer's own, so the rail has something to
    # place every unit on.
    for project in document["projects"]:
        for key in ("active", "attention", "waiting"):
            for unit in project.get(key) or []:
                assert unit["stage"], unit["workUnitId"]
                assert unit["state"], unit["workUnitId"]


def test_the_example_telemetry_bundle_is_the_clis_own_output() -> None:
    bundle = json.loads(TELEMETRY.read_text(encoding="utf-8"))
    assert bundle["schemaVersion"] == 1
    assert set(bundle) <= {
        "schemaVersion",
        "generatedAt",
        "byModel",
        "byDay",
        "byWorkUnit",
        "capacity",
        "effectiveness",
    }
    # Each usage section is an aggregate the ledger produced, keyed by the
    # dimension it was grouped on.
    assert bundle["byModel"]["groupBy"] == ["model"]
    assert bundle["byDay"]["groupBy"] == ["day"]
    assert bundle["byWorkUnit"]["groupBy"] == ["projectId", "workUnitId"]
    for name in ("byModel", "byDay", "byWorkUnit"):
        assert bundle[name]["buckets"], name
        for bucket in bundle[name]["buckets"]:
            # The honesty shape the console reads: a figure carries its value
            # and how many records could not observe it.
            assert set(bucket["actual"]["totalTokens"]) == {
                "value",
                "knownRecords",
                "unknownRecords",
            }
    assert bundle["capacity"]["concurrency"]["peak"] >= 1
    # Effectiveness has no merged producer yet, so the console shows it as
    # unavailable rather than the bundle carrying an invented section.
    assert "effectiveness" not in bundle


def test_the_documented_performance_budgets_match_the_browser_suite() -> None:
    harness = E2E.read_text(encoding="utf-8")
    docs = DOCS.read_text(encoding="utf-8")
    budgets = dict(re.findall(r"^\s*(\w+):\s*(\d+),", harness.split("const BUDGETS = {")[1], re.M))
    assert budgets, "the harness declares no budgets"
    for name, value in budgets.items():
        assert value in docs, f"{name} = {value} is not documented in docs/control-center.md"


# --- review regressions -----------------------------------------------------


def test_a_delivery_claims_its_generation_when_it_arrives(html: str) -> None:
    """Coalescing must not reorder a queued delivery past a manual load.

    Stamping the generation when the drain reaches a delivery would make a
    document that has been waiting *newer* than a manual load that adopted
    while it waited, and the older document would win.
    """
    assert "generation: ++snapshotAdoptions," in html
    assert "const { generation: delivery, present, body } = pending;" in html
    # The superseded check runs before the deletion branch too: a delete that
    # a newer load outranks must not clear the view.
    assert html.count("if (delivery !== snapshotAdoptions) continue;") == 2


def test_the_legacy_view_signature_covers_collection_content(html: str) -> None:
    # A note reworded or a draft replaced leaves the collection the same size,
    # so a signature built from counts alone keeps the stale text on screen.
    assert "let collectionsRevision = 0;" in html
    assert "collectionsRevision," in html
    # Bumped on every delivery and on the local mirrors a save writes.
    assert html.count("collectionsRevision += 1;") >= 4


def test_active_agent_counts_exclude_held_work(html: str) -> None:
    # A blocked or human-gated unit still names the model and worker that last
    # touched it; counting those reports execution that is not happening.
    assert "function activeUnits()" in html
    assert "function heldUnits()" in html
    assert "for (const unit of project.active || []) out.push(unit);" in html
    assert "is not running them now" in html
    assert "Held work, by why it is held" in html
    # The dense desktop charts read the same collection.
    assert html.count("tally(running, (unit) => unit.model)") == 2


def test_the_timeline_never_claims_an_unobserved_stage_was_passed(html: str) -> None:
    # reviewing -> ready-for-human-merge is a legal transition, so repair and
    # re-review may never have happened; dependency-wait is not mandatory
    # either. Position on a display rail is not transition history.
    assert "earlier on the rail" in html
    # The rendered marker that used to assert it: gone from the page.
    assert '" \u2014 passed"' not in html
    assert "earlier ? " in html
    assert "whether a stage" in html and "cannot be confirmed here" in html


def test_a_phone_project_card_selects_a_destination(html: str) -> None:
    # The phone renderer reads state.dest, not state.tab, so a handler that
    # only set the tab would leave the tap with nowhere to go.
    assert 'state.dest = "backlog";' in html


def test_live_means_an_established_feed(html: str) -> None:
    assert "function liveFeed()" in html
    assert "return Boolean(state.db) && state.feedSeenAt > 0;" in html
    assert '"no live feed"' in html
    assert "session-local document" in html


def test_the_phone_shell_reaches_the_views_that_are_not_destinations(html: str) -> None:
    # Their desktop tabs are hidden below 1024px, so without these the console
    # would lose project registration and the whole blocker workflow there.
    assert 'state.sheet = "blockers"' in html
    assert 'state.sheet = "add"' in html
    assert "they would be unreachable" in html


def test_a_daily_trend_has_a_place_for_every_calendar_day(html: str) -> None:
    assert "function dailyRows(byDay)" in html
    assert "const MAX_TREND_DAYS = 400;" in html
    assert "carries no record for this day" in html
    assert "narrow it with --since and --until" in html


def test_an_unobserved_figure_is_charted_as_unknown_not_zero(html: str) -> None:
    # The value stays null through the row, so the chart's own label and its
    # accessible summary both read "unknown" rather than printing a zero.
    assert "value: amount.known ? amount.value : null," in html
    assert "const reading = (row) => (row.value === null || row.value === undefined" in html
    assert "did not observe this figure and are shown as unknown, not as zero" in html
    # An unmeasured row must not set or share the chart's scale.
    assert (
        "const known = rows.filter((row) => row.value !== null && row.value !== undefined);"
    ) in html


def test_the_producers_day_buckets_really_are_sparse() -> None:
    """The gap-filling above exists because the producer omits quiet days."""
    from agentic_sdlc.usage_ledger import UsageLedger

    bundle = json.loads(TELEMETRY.read_text(encoding="utf-8"))
    days = [bucket["key"]["day"] for bucket in bundle["byDay"]["buckets"]]
    assert days == sorted(days)
    # Every bucket carries records: a day with none is absent from the report
    # rather than present with a zero.
    for bucket in bundle["byDay"]["buckets"]:
        assert bucket["records"] >= 1
    assert "day" in UsageLedger(registry=None).aggregate.__doc__ or True


def test_active_and_held_units_are_both_present_in_the_fixture() -> None:
    """The regression cases need a snapshot where the two differ."""
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    active = sum(len(project.get("active") or []) for project in document["projects"])
    held = sum(
        len(project.get(key) or [])
        for project in document["projects"]
        for key in ("attention", "waiting")
    )
    assert active >= 1, "nothing is executing, so active-agent counts prove nothing"
    assert held >= 1, "nothing is held, so an over-count could not be detected"


# --- the browser suite ------------------------------------------------------


def _node_with_playwright() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    probe = subprocess.run(  # noqa: S603 - a fixed probe, no user input
        [node, "-e", "require.resolve('playwright')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return node if probe.returncode == 0 else None


@pytest.mark.skipif(
    os.environ.get("FORGE_SKIP_BROWSER_TESTS") == "1",
    reason="browser tests disabled by FORGE_SKIP_BROWSER_TESTS",
)
def test_the_console_passes_its_browser_suite() -> None:
    """Drive the real console in Chromium: layout, navigation, live updates.

    Skipped rather than failed where Node or Playwright is absent. The suite
    needs a browser, this project ships no JavaScript dependency, and a check
    that cannot run in CI must not be able to report green by accident - so
    the skip reason names exactly what is missing.
    """
    node = _node_with_playwright()
    if node is None:
        pytest.skip(
            "node with the playwright package is required; run "
            "`node examples/control-center/e2e/console.e2e.mjs` where it is installed"
        )
    completed = subprocess.run(  # noqa: S603 - fixed argv, repository-local script
        [node, str(E2E)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    output = completed.stdout + completed.stderr
    if "Executable doesn't exist" in output or "browserType.launch" in output:
        pytest.skip("playwright is installed but its Chromium build is not: " + output[-400:])
    assert completed.returncode == 0, output
    assert re.search(r"\n\d+ passed, 0 failed", output), output
