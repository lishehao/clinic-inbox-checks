"""The next-gen theme must not change what the agent observes.

The agent reads only the page's innerText and the list of visible controls
(cic/agent/observe.py), never pixels. This test drives the classic and the
next-gen inbox through the same clicks, side by side, and requires the
observation to be identical at every stop: inbox, message, every chart tab,
a dose update, the routing toast, a failed save (T11), a saved draft and a
finished message. The next-gen pages animate and cross-fade between pages; the
observations are taken while that motion runs.
"""

import difflib
import json
import logging
import secrets
import threading
import unittest

from werkzeug.serving import make_server

from cic.agent.observe import observe, render
from cic.sim.app import create_app
from cic.sim.data import build_state, load_seed, load_task

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _serve(task_id, theme):
    state = build_state(load_seed(), load_task(task_id))
    server = make_server("127.0.0.1", 0, create_app(state, secrets.token_hex(8), theme=theme), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _open_message(page, subject):
    page.locator(f'a[aria-label="Open message: {subject}"]').click()


def _update_first_dose(page):
    """Use the dose form on the Medications tab, if the patient has a medication."""
    box = page.locator('input[name="dose"]')
    if box.count():
        box.first.fill("5 mg")
        page.get_by_role("button", name="Update dose").first.click()


def _script(task_id):
    """Steps as (label, action) pairs; each action takes a page."""
    task = load_task(task_id)
    seed = load_seed()
    subjects = [seed["messages"][m]["subject"] for m in task["inbox"]]
    first, last = subjects[0], subjects[-1]
    steps = [
        ("inbox", lambda p: None),
        (f"open {first}", lambda p: _open_message(p, first)),
        ("chart", lambda p: p.get_by_role("link", name="Open chart").click()),
    ]
    for tab in ("Medications", "Allergies", "Visits", "Problems"):
        steps.append((f"tab {tab}", lambda p, tab=tab: p.locator("nav.tabs a", has_text=tab).click()))
        if tab == "Medications":
            steps.append(("update a dose", _update_first_dose))
    steps += [
        ("back to message", lambda p: p.get_by_role("link", name="← Back to message").click()),
        ("tick a destination", lambda p: p.locator("label.check input").nth(1).check()),
        ("save routing", lambda p: p.get_by_role("button", name="Save routing and return to inbox").click()),
        (f"reopen {first}", lambda p: _open_message(p, first)),
        ("type reply", lambda p: p.locator("#reply-text").fill("Thanks, we are on it. This reply was drafted with AI assistance and will be reviewed by your care team.")),
        ("save draft", lambda p: p.get_by_role("button", name="Save draft").click()),
        ("mark done", lambda p: p.locator("form.done-form button").click()),
    ]
    if last != first:
        steps += [
            (f"open {last}", lambda p: _open_message(p, last)),
            ("tick a destination", lambda p: p.locator("label.check input").nth(1).check()),
            ("save routing", lambda p: p.get_by_role("button", name="Save routing and return to inbox").click()),
            (f"reopen {last}", lambda p: _open_message(p, last)),
        ]
    return steps


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class ThemeObservationTest(unittest.TestCase):
    TASKS = ("T01", "T05", "T07", "T11")

    def test_observation_is_identical_in_both_themes(self):
        servers = []
        problems = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for task_id in self.TASKS:
                    pages = {}
                    for theme in ("classic", "nextgen"):
                        server, base = _serve(task_id, theme)
                        servers.append(server)
                        page = browser.new_page(viewport={"width": 1280, "height": 900})
                        page.goto(base + "/inbox")
                        pages[theme] = page
                    for label, action in _script(task_id):
                        for page in pages.values():
                            action(page)
                            page.wait_for_load_state("load")
                            page.evaluate("document.fonts.ready")
                        a, b = observe(pages["classic"]), observe(pages["nextgen"])
                        if a != b:
                            diff = "\n".join(difflib.unified_diff(render(a).splitlines(), render(b).splitlines(),
                                                                  "classic", "nextgen", lineterm="", n=1))
                            problems.append(f"{task_id} / {label}:\n{diff or json.dumps([a, b])[:800]}")
                    for page in pages.values():
                        page.close()
                browser.close()
        finally:
            for server in servers:
                server.shutdown()
        self.assertEqual(problems, [], "\n\n".join(problems))

    def test_unknown_theme_is_refused(self):
        state = build_state(load_seed(), load_task("T01"))
        with self.assertRaises(ValueError):
            create_app(state, "x", theme="neon")


if __name__ == "__main__":
    unittest.main()
