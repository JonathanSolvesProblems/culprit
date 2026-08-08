"""Capture b-roll stills for the demo video and the README.

Drives a real browser over the live DataHub instance, the public repo, and the
primary NYC TLC sources that corroborate the incident. Nothing here is mocked:
every frame is a real page.

README images land in docs/img/ and are committed. Everything else lands in
broll/captures/, which is gitignored, because it is video working material.

Usage:
    python scripts/capture_broll.py            # everything
    python scripts/capture_broll.py --datahub  # only the local DataHub pages
"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
BROLL = ROOT / "broll" / "captures"

DATAHUB = "http://localhost:9002"
REPO = "https://github.com/JonathanSolvesProblems/culprit"

VIEWPORT = {"width": 1920, "height": 1080}


def urn(u: str) -> str:
    return urllib.parse.quote(u, safe="")


MODEL = urn("urn:li:mlModel:(urn:li:dataPlatform:duckdb,nyc_fare_predictor,PROD)")
FEATURES_DS = urn(
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "nyc_fares.warehouse.main_marts.fct_trip_features,PROD)"
)
RAW_DS = urn(
    "urn:li:dataset:(urn:li:dataPlatform:dbt,nyc_fares.warehouse.raw.yellow_trips,PROD)"
)

# (filename, url, destination, wait_ms, note, tab)
# `tab` is the visible tab label to click after load. DataHub renders tabs
# client-side, so deep-linking to them by URL is unreliable; clicking the label a
# person would click is both robust and honest about what the page shows.
SHOTS = [
    # Primary sources. These corroborate the incident independently of the repo.
    ("tlc_data_dictionary.png",
     "https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf",
     BROLL, 6000, "Official TLC dictionary: VendorID 7 = Helix", None),
    ("tlc_hackup_providers.png",
     "https://www.nyc.gov/site/tlc/businesses/yellow_cab_hackup.page",
     BROLL, 4000, "TLC authorised technology providers, Helix listed", None),
    ("tlc_trip_records.png",
     "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page",
     BROLL, 4000, "Where the 19.3M records come from", None),

    # The repo, as a judge sees it.
    ("repo_home.png", REPO, BROLL, 3500, "Public repo, Apache 2.0 in the sidebar", None),
    ("pr_diff.png", f"{REPO}/pull/1/files", BROLL, 3500, "The patch Culprit opened", None),
    ("rejected_patch.png",
     f"{REPO}/blob/main/examples/remediation_rejected.json", BROLL, 3000,
     "The patch its own gate refused", None),

    # DataHub. These are the README images.
    ("datahub_model_properties.png", f"{DATAHUB}/mlModels/{MODEL}/", IMG, 6000,
     "THE money shot: vendors_in_training_data = 1,2,6", "Properties"),
    ("datahub_model_summary.png", f"{DATAHUB}/mlModels/{MODEL}/", IMG, 5000,
     "Model details, training metrics, hyperparameters", None),
    ("datahub_model_features.png", f"{DATAHUB}/mlModels/{MODEL}/", IMG, 6000,
     "13 mlFeatures on the model", "Features"),
    ("datahub_model_lineage.png", f"{DATAHUB}/mlModels/{MODEL}/", IMG, 8000,
     "Model lineage into the feature table", "Lineage"),
    # Dataset tabs are addressable by URL, and the schema tab is called
    # "Columns" rather than "Schema". Deep-link rather than hunt for a widget.
    ("datahub_lineage.png",
     f"{DATAHUB}/dataset/{FEATURES_DS}/Lineage?is_lineage_mode=true", IMG, 10000,
     "Column-level lineage from the native dbt connector", None),
    ("datahub_incident.png", f"{DATAHUB}/dataset/{RAW_DS}/Incidents", IMG, 8000,
     "The incident Culprit filed", None),
    ("datahub_schema.png", f"{DATAHUB}/dataset/{RAW_DS}/Columns", IMG, 8000,
     "The annotated vendor_id column", None),
]


def login(page) -> bool:
    """DataHub OSS quickstart uses datahub / datahub.

    The form has no name/id/testid attributes worth relying on, so match on the
    visible placeholder and the input type, which is what a person sees.
    """
    page.goto(f"{DATAHUB}/login", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    try:
        page.locator('input[placeholder="Enter username"]').first.fill("datahub")
        page.locator('input[type="password"]').first.fill("datahub")
        page.locator('button:has-text("Login")').first.click()
        page.wait_for_timeout(6000)
        if "/login" in page.url:
            print(f"  WARNING: still on {page.url} after login")
            return False
        print(f"  logged in, landed on {page.url}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  login failed ({type(exc).__name__}): {str(exc)[:120]}")
        return False


def dismiss_overlays(page) -> None:
    """Close DataHub's onboarding tour and any cookie banner.

    The first-run tour ("Introducing the Asset Sidebar") dims the whole page, so
    a screenshot taken with it open is unusable. It reappears per entity type, so
    this runs before every capture rather than once after login.
    """
    selectors = (
        '.ant-modal-close',
        '.ant-modal-close-x',
        'button[aria-label="Close"]',
        '[data-testid="onboarding-close-button"]',
        'button:has-text("Skip")',
        'button:has-text("Got it")',
        'button:has-text("Accept")',
    )
    for _ in range(3):  # tours can chain
        closed = False
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=500):
                    el.click(timeout=1500)
                    page.wait_for_timeout(700)
                    closed = True
                    break
            except Exception:  # noqa: BLE001, S110
                continue
        if not closed:
            break
    # Anything still floating gets dismissed with Escape.
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:  # noqa: BLE001, S110
        pass


def click_tab(page, label: str) -> None:
    """Click a named tab.

    Entity pages do not use one consistent tab widget: the mlModel page exposes
    proper ARIA tabs, dataset pages render them as plain links. Try the
    strategies in order of specificity rather than assuming either.
    """
    strategies = (
        lambda: page.get_by_role("tab", name=label, exact=True).first,
        lambda: page.get_by_role("link", name=label, exact=True).first,
        lambda: page.locator(f'[role="tab"]:has-text("{label}")').first,
        lambda: page.locator(f'a:has-text("{label}")').first,
        lambda: page.get_by_text(label, exact=True).first,
    )
    for build in strategies:
        try:
            el = build()
            if el.is_visible(timeout=2500):
                el.click(timeout=6000)
                return
        except Exception:  # noqa: BLE001, S110
            continue
    raise RuntimeError(f"could not find a clickable tab named {label!r}")


def main() -> int:
    only_datahub = "--datahub" in sys.argv
    IMG.mkdir(parents=True, exist_ok=True)
    BROLL.mkdir(parents=True, exist_ok=True)

    shots = [s for s in SHOTS if (DATAHUB in s[1]) or not only_datahub]
    ok, failed = 0, []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = ctx.new_page()

        if any(DATAHUB in s[1] for s in shots):
            print("Logging into DataHub...")
            login(page)
            dismiss_overlays(page)

        for name, url, dest, wait, note, tab in shots:
            try:
                print(f"  {name:32s} {note}")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                dismiss_overlays(page)
                if tab:
                    click_tab(page, tab)
                page.wait_for_timeout(wait)
                dismiss_overlays(page)
                page.screenshot(path=str(dest / name), full_page=False)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    FAILED: {type(exc).__name__}: {str(exc)[:110]}")
                failed.append(name)

        browser.close()

    print(f"\ncaptured {ok}/{len(shots)}")
    if failed:
        print("failed: " + ", ".join(failed))
    print(f"README images -> {IMG}")
    print(f"video b-roll  -> {BROLL}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
