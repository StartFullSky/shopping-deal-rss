"""Optional local browser check: python tests/browser_admin_smoke.py --live-feed --artifacts PATH."""

import argparse
import json
import pathlib
import re
import time

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--artifacts", type=pathlib.Path, required=True)
    parser.add_argument("--live-feed", action="store_true")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    result = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.goto(args.url, wait_until="networkidle")
        original = page.request.get(args.url + "/api/config").json()

        def trial(source="latest"):
            page.locator('[data-page="deals"]').click()
            page.locator('[data-filter="all"]').click()
            page.locator("#openTrial").click()
            page.locator("#trialAi").uncheck()
            page.locator(f'input[name="trialSource"][value="{source}"]').check()
            with page.expect_response(lambda response: response.url.endswith("/api/runs") and response.request.method == "POST") as response:
                page.locator("#startTrial").click()
            ident = response.value.json()["run_id"]
            stop_at = time.monotonic() + 150
            while time.monotonic() < stop_at:
                run = page.request.get(args.url + "/api/runs/" + ident).json()
                if run["phase"] not in ("collecting", "reviewing"):
                    assert run["phase"] == "completed", run.get("error")
                    break
                page.wait_for_timeout(350)
            else:
                raise AssertionError("preview run timed out")
            page.locator("#batchCaption").filter(has_text=ident[:8]).wait_for(timeout=10000)
            page.wait_for_function("""expected => document.getElementById('statEligible').textContent === String(expected)
                && document.getElementById('activeBanner').hidden""", arg=run["summary"]["eligible_count"], timeout=10000)
            assert run["report"]["sent_count"] == 0
            return run

        def settings_page():
            page.locator('[data-page="settings"]').click()

        def save_keywords(value):
            settings_page()
            page.locator("#excludeKeywords").fill(value)
            with page.expect_response(lambda response: response.url.endswith("/api/config") and response.request.method == "POST") as response:
                page.locator("#saveConfig").click()
            assert response.value.status == 200, response.value.text()

        if args.live_feed:
            current = trial()
            result["live_feed"] = {key: current["summary"][key] for key in
                                   ("input_count", "selected_count", "eligible_count", "elapsed_seconds", "rate_wait_seconds", "sent_count")}
        else:
            runs = page.request.get(args.url + "/api/runs").json()["runs"]
            assert runs, "Use --live-feed once to create a batch"
            current = page.request.get(args.url + "/api/runs/" + runs[0]["id"]).json()
        result["initial_batch"] = {key: current["summary"][key] for key in
                                  ("input_count", "selected_count", "eligible_count", "elapsed_seconds", "rate_wait_seconds", "sent_count")}
        page.locator("#dealRows tr").first.wait_for(timeout=10000)
        assert page.locator("#dealRows tr").count() > 0
        first_title = page.locator("#dealRows .deal-title").first.text_content()
        keyword = re.search(r"[\u4e00-\u9fff]{2,}", first_title)
        assert keyword, first_title
        keyword = keyword.group()[:8]
        page.locator("#sourceFilter").select_option("kuan")
        assert all("酷安" in text for text in page.locator("#dealRows .source-cell").all_text_contents())
        page.locator("#sourceFilter").select_option("all")
        page.locator("#search").fill("THIS-ITEM-DOES-NOT-EXIST")
        assert page.locator("#dealRows tr").count() == 0
        page.locator("#search").fill("")
        page.locator("#nextPage").click()
        assert page.locator("#pageNumber").text_content().startswith("2")
        page.locator("#previousPage").click()
        page.locator("#dealRows .detail-button").first.click()
        assert page.locator("#detailTitle").text_content() == first_title
        page.screenshot(path=str(args.artifacts / "detail.png"))
        page.locator('[data-close="detailDialog"]').click()
        changed_keywords = original["settings"]["exclude_keywords"] + [keyword]
        settings_page()
        page.locator("#excludeKeywords").fill("\n".join(changed_keywords))
        with page.expect_response(lambda response: response.url.endswith("/api/rules/preview")) as response:
            page.locator("#previewRules").click()
        preview = response.value.json()
        assert preview["newly_removed"] >= 1, preview
        assert page.request.get(args.url + "/api/config").json()["revision"] == original["revision"]
        page.screenshot(path=str(args.artifacts / "rule-preview.png"))
        result["keyword_preview"] = {key: preview[key] for key in ("before_kept", "after_kept", "newly_removed")}
        page.locator('[data-close="ruleDialog"]').first.click()
        try:
            save_keywords("\n".join(changed_keywords))
            page.reload(wait_until="networkidle")
            assert keyword in page.locator("#excludeKeywords").input_value()
            page.screenshot(path=str(args.artifacts / "settings.png"))
            filtered = trial("reuse")
            assert any(row["reason"] == "keyword_excluded" for row in filtered["rows"])
            page.locator('[data-filter="rules"]').click()
            assert page.locator("#dealRows tr").count() >= 1
        finally:
            save_keywords("\n".join(original["settings"]["exclude_keywords"]))
        result["config_restored"] = page.request.get(args.url + "/api/config").json()["settings"] == original["settings"]
        assert result["config_restored"]
        trial("reuse")
        page.locator('[data-filter="all"]').click()
        page.screenshot(path=str(args.artifacts / "dashboard.png"), full_page=True)
        page.screenshot(path=str(args.artifacts / "dashboard-overview.png"))
        page.locator('[data-page="history"]').click()
        assert page.locator("#historyRows tr").count() >= 3
        page.screenshot(path=str(args.artifacts / "history.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-page="deals"]').click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(args.artifacts / "mobile.png"), full_page=True)
        result["browser_errors"] = errors
        assert not errors, errors
        browser.close()
    (args.artifacts / "browser-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
