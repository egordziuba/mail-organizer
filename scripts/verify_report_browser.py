"""Offline browser verification of the generated read-only report."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def verify(report):
    report = Path(report).resolve()
    output = report.parent / "_qa"
    output.mkdir(exist_ok=True)
    html = report.read_text(encoding="utf-8")
    script = output / "report-inline.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)), encoding="utf-8")
    subprocess.run(["node", "--check", str(script)], check=True, capture_output=True, text=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe", headless=True)
        context = browser.new_context(viewport={"width":1400,"height":1000})
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(("file:", "data:")) else route.abort())
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(report.as_uri())
        expect(page.locator("tbody tr")).to_have_count(6)
        expect(page.locator("tbody tr:not([hidden])")).to_have_count(6)
        assert "SYNTHETIC TEST DATA" in page.locator("body").inner_text()
        assert page.locator("h2").inner_text() == "Календарь не изменяется"
        page.screenshot(path=str(output / "report-desktop.png"), full_page=True)
        jobs = page.locator('tbody tr[data-topic="jobs"]').count()
        page.locator('button[data-filter="jobs"]').click()
        expect(page.locator("tbody tr:not([hidden])")).to_have_count(jobs)
        page.locator('button[data-filter="all"]').click()
        page.locator("#search").fill("Hochschule")
        expect(page.locator("tbody tr:not([hidden])")).to_have_count(1)
        page.locator("#search").fill("")
        expect(page.locator("tbody tr:not([hidden])")).to_have_count(6)
        page.locator("summary").first.click()
        assert page.locator("details").first.get_attribute("open") is not None
        page.set_viewport_size({"width":430,"height":932})
        page.screenshot(path=str(output / "report-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        browser.close()
    result = {"status":"BROWSER_QA_OK", "html_sha256":hashlib.sha256(report.read_bytes()).hexdigest(),
              "synthetic_rows":6,"job_rows":jobs,"search_results":1,"javascript_errors":errors,
              "external_network_allowed":False,"calendar_write_available":False,
              "screenshots":[str(output / "report-desktop.png"), str(output / "report-mobile.png")]}
    (output / "browser-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    verify(sys.argv[1])
