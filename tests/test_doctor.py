"""Setup problems stop cleanly with a fix to try, and doctor checks this computer."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.test_e2e import _browser_available


class _NoBrowser:
    """Stands in for sync_playwright() when Chromium was never installed."""

    def __enter__(self):
        class Chromium:
            def launch(self, **kwargs):
                raise Exception("BrowserType.launch: Executable doesn't exist at /x/chrome-headless-shell\n"
                                "Looks like Playwright was just installed or updated.")

        class Playwright:
            chromium = Chromium()

        return Playwright()

    def __exit__(self, *exc):
        return False


class SetupProblemTests(unittest.TestCase):
    def test_run_task_names_the_fix_and_leaves_no_folder(self):
        from cic.agent.runner import HarnessError, run_task
        from cic.agent.scripted import RuleAgent
        with tempfile.TemporaryDirectory() as out:
            with mock.patch("playwright.sync_api.sync_playwright", lambda: _NoBrowser()):
                with self.assertRaisesRegex(HarnessError, "python -m playwright install chromium"):
                    run_task("T01", RuleAgent("careful"), out)
            self.assertEqual(list(Path(out).iterdir()), [])

    def test_check_browser_reports_the_fix(self):
        from cic.agent import runner
        with mock.patch("playwright.sync_api.sync_playwright", lambda: _NoBrowser()):
            self.assertIn("python -m playwright install chromium", runner.check_browser())

    def test_run_stops_before_any_run_when_the_browser_is_missing(self):
        import cic.__main__ as cli
        with tempfile.TemporaryDirectory() as out:
            with mock.patch("cic.agent.runner.check_browser", return_value="Chromium is not installed. Run: X"):
                with self.assertRaisesRegex(SystemExit, "Nothing was run"):
                    cli.main(["run", "--tasks", "T01", "--agent", "scripted:careful", "--out", out])
            self.assertEqual(list(Path(out).iterdir()), [])


class _OkModel:
    model = "fake/ok"

    def complete(self, messages, max_tokens=None):
        return "OK", {}


@unittest.skipUnless(_browser_available(), "Playwright Chromium is not installed")
class DoctorTests(unittest.TestCase):
    def test_doctor_passes_here(self):
        import cic.__main__ as cli
        buffer = io.StringIO()
        with mock.patch.object(cli, "_client", return_value=_OkModel()), redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["doctor"])
        text = buffer.getvalue()
        self.assertEqual(caught.exception.code, 0, text)
        for expected in ("[ok]   flask and playwright installed", "[ok]   Chromium starts",
                         "[ok]   the simulated inbox opens in Chromium", "[ok]   the model answered 'OK'"):
            self.assertIn(expected, text)

    def test_doctor_reports_a_model_failure(self):
        import cic.__main__ as cli
        buffer = io.StringIO()
        with mock.patch.object(cli, "_client", side_effect=SystemExit("Not signed in with ChatGPT.")), \
                redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["doctor"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("[FAIL] Not signed in with ChatGPT.", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
