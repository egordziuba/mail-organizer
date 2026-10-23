"""Rebuild README PNGs using synthetic data only; never used by the package/tests.

From the repository root, with the package installed in the active environment:
    python -m pip install playwright
    python -m playwright install chromium
    python scripts/make_screenshots.py
Or use an installed Chromium: python scripts/make_screenshots.py --chromium /usr/bin/chromium

The temporary demo stays under work/ and is deleted afterward. Only its throwaway
report's temporary absolute path prefix is replaced with "demo-data" for display.
No credentials, runtime configuration, OAuth or mailbox sync are accessed.
On non-Windows, an isolated child substitutes the Windows archive mutex, as the
synthetic tests do. It can only invoke demo on this script's fresh temporary root;
the package's normal CLI guard is unchanged.
"""

import argparse
from functools import partial
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def capture(page, output, top, bottom):
    """Crop to report content at the original 1280 x 800 browser viewport."""
    page.screenshot(
        path=str(output),
        clip={"x": 24, "y": top, "width": 1232, "height": bottom - top},
        animations="disabled",
    )
    if output.stat().st_size > 300_000:
        raise RuntimeError(f"{output.name} exceeds the 300 kB image budget")
    print(f"{output.name}: {output.stat().st_size:,} bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", help="Optional path to an installed Chromium executable")
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright

    repo = Path(__file__).resolve().parents[1]
    work = repo / "work"
    work.mkdir(exist_ok=True)
    output = repo / "docs" / "img"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="screenshots-", dir=work) as temporary:
        root = Path(temporary) / "demo-data"
        command = [sys.executable, "-E", "-X", "utf8"]
        if sys.platform == "win32":
            command += ["-m", "mailorganizer.cli", "demo", "--root", str(root)]
        else:
            command += ["-c", (
                "from contextlib import nullcontext; "
                "import sys; from mailorganizer import cli; "
                "cli._archive_lock = lambda identity: nullcontext(); "
                "raise SystemExit(cli.main(['demo', '--root', sys.argv[1]]))"
            ), str(root)]
        subprocess.run(
            command,
            cwd=repo, check=True, capture_output=True,
        )
        report = root / "report.html"
        html = report.read_text(encoding="utf-8")
        assert "SYNTHETIC TEST DATA" in html
        report.write_text(html.replace(escape(str(root), quote=True), "demo-data"), encoding="utf-8")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(root))
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            with sync_playwright() as playwright:
                options = {"executable_path": args.chromium} if args.chromium else {}
                browser = playwright.chromium.launch(headless=True, **options)
                try:
                    context = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin + "/report.html", wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    expect(page.locator("tbody tr:not([hidden])")).to_have_count(6)
                    toolbar = page.locator(".toolbar").bounding_box()
                    page.evaluate("y => window.scrollTo(0, y)", toolbar["y"] - 12)
                    toolbar = page.locator(".toolbar").bounding_box()
                    table = page.locator(".table-wrap").bounding_box()
                    capture(page, output / "demo-triage.png", toolbar["y"] - 12, table["y"] + table["height"] + 12)

                    page.locator('button[data-filter="university"]').click()
                    expect(page.locator("tbody tr:not([hidden])")).to_have_count(1)
                    row = page.locator('tr[data-topic="university"]')
                    row.locator("summary").click()
                    expect(row.locator("details")).to_have_attribute("open", "")
                    expect(row.locator(".path")).to_contain_text("demo-data")
                    expect(row).to_contain_text("Вложение · extracted")
                    page.evaluate("window.scrollTo(0, 0)")
                    expect(page.get_by_text("SYNTHETIC TEST DATA — не ваша почта", exact=True)).to_be_in_viewport()
                    table = page.locator(".table-wrap").bounding_box()
                    capture(page, output / "demo-provenance.png", 24, table["y"] + table["height"] + 12)
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    main()
