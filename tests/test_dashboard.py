# SPDX-License-Identifier: Apache-2.0
"""Run the dashboard CLI as a subprocess over synthetic run directories.

The fixtures are hand-written `report.json` / execution trees: no model call and no
real run is needed. They pin the three ordinary outcomes (judged success, failed
task, unknown judge) and the escaping of untrusted strings, plus the demo/sweep
"agreement, unverified" view that must never present agreement as accuracy.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def task_row(task_id, *, status="ok", errors=(), modes=None, health=None, degraded=False):
    return {
        "task_id": task_id,
        "domain": "synthetic",
        "status": status,
        "errors": list(errors),
        "modes": modes or {},
        "integration": {
            "editor_health": health
            or {"n_sessions": 2, "n_failed": 0, "n_succeeded": 2, "state": "all_succeeded"},
            "fallback_labels": [],
            "degraded": degraded,
        },
    }


def mode_block(*, selected_frac=None, base_frac=None, delta=None, judged=True, matched=True,
               selected_error="", selection_ok=True):
    block = {
        "finals": ["agg1"],
        "kept_base": [],
        "fell_back": [],
        "ledger": {"R": 1, "P": 1},
        "chosen_label": "agg1",
        "chosen_is_base": False,
        "trivial_selection": False,
        "candidates": 3,
        "selection_ok": selection_ok,
    }
    if selected_frac is not None:
        block["comparison"] = {
            "baseline_label": "base1",
            "selected_label": "agg1",
            "selection_is_a_measurement": False,
            "judged_pair_available": judged,
            "configured_match": matched,
            "resolved_executor_model": "opaque",
            "base_frac": base_frac,
            "selected_frac": selected_frac,
            "delta_frac": delta,
            "base_error": "",
            "selected_error": selected_error,
        }
    return block


def ordinary_report(tasks, *, judge_enabled=True, degraded_tasks=(), notes=None):
    return {
        "schema": 1,
        "name": "synthetic-ordinary",
        "manifest_sha256": "0" * 64,
        "k": 1,
        "modes": ["parallel"],
        "tag": "",
        "output": "/synthetic/run",
        "executor": {"declared_model": None, "declared_model_verified": False},
        "editor": {
            "channel": "opencode",
            "model": "example/provider#high",
            "steps_enforced": False,
            "launcher": "service",
        },
        "judge_enabled": judge_enabled,
        "cost": {"n_sessions": 2, "n_measured": 2, "cost_usd": 0.25, "cost_source": "reported"},
        "editor_health": {
            "n_sessions": 2,
            "n_failed": 0,
            "n_succeeded": 2,
            "state": "all_succeeded",
            "degraded": bool(degraded_tasks),
        },
        "degraded_tasks": list(degraded_tasks),
        "tasks": tasks,
        "finished_at": "2026-10-21T00:00:00+00:00",
        "notes": notes if notes is not None else ["declared executor model is a label only"],
    }


class DashboardCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_run(self, name, report):
        run = self.root / name
        run.mkdir()
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return run

    def dashboard(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "harness2.cli.dashboard", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=60,
        )

    def render(self, report, name="run"):
        run = self.write_run(name, report)
        result = self.dashboard("--run", str(run))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = (run / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        return run, page

    def test_judged_success_reports_the_post_selection_judge_result(self):
        report = ordinary_report(
            [
                task_row(
                    "task-ok",
                    modes={
                        "parallel": mode_block(
                            selected_frac=1.0, base_frac=0.5, delta=0.5
                        )
                    },
                )
            ]
        )
        _run, page = self.render(report)

        self.assertIn("judged: selected 100.0%", page)
        self.assertIn("base 50.0%", page)
        self.assertIn("Δ +50.0 pt", page)
        self.assertIn("class='good'", page)
        self.assertIn("judged pairs: 1", page)
        self.assertIn("unknown: 0", page)
        self.assertIn("editor cost: 0.25 USD (reported; 2/2 sessions measured)", page)
        self.assertIn("ledger R1/P1", page)
        self.assertIn("finals: 1", page)

    def test_failed_task_is_shown_as_failed_with_an_unknown_judge(self):
        report = ordinary_report(
            [
                task_row(
                    "task-crash",
                    status="exception",
                    errors=["RuntimeError: executor failed: rc=2"],
                )
            ]
        )
        _run, page = self.render(report)

        self.assertIn("class='weak'>exception", page)
        self.assertIn("RuntimeError: executor failed: rc=2", page)
        self.assertIn("unknown — task did not produce a mode record", page)
        self.assertNotIn("judged:", page)

    def test_missing_judge_record_is_unknown_not_a_zero(self):
        report = ordinary_report(
            [
                task_row("task-unjudged", modes={"parallel": mode_block()}),
                task_row(
                    "task-judge-error",
                    modes={
                        "parallel": mode_block(
                            selected_frac=0.0,
                            base_frac=0.0,
                            judged=False,
                            selected_error="RuntimeError: judge rc=1: <boom>",
                        )
                    },
                ),
            ]
        )
        _run, page = self.render(report)

        self.assertIn("unknown — no post-selection judge record", page)
        self.assertIn("unknown — judged pair unavailable: RuntimeError: judge rc=1:", page)
        self.assertIn("&lt;boom&gt;", page)
        self.assertNotIn("<boom>", page)
        self.assertIn("unknown: 2", page)
        self.assertIn("judged pairs: 0", page)
        self.assertNotIn("judged:", page)
        self.assertNotIn("selected 0.0%", page)

    def test_disabled_judge_is_unknown_and_says_so(self):
        report = ordinary_report(
            [task_row("task-no-judge", modes={"parallel": mode_block()})],
            judge_enabled=False,
        )
        _run, page = self.render(report)

        self.assertIn("judge: disabled (scores unknown)", page)
        self.assertIn("unknown — judge disabled for this run", page)

    def test_degraded_run_is_banner_flagged(self):
        report = ordinary_report(
            [task_row("task-degraded", modes={"parallel": mode_block()}, degraded=True)],
            degraded_tasks=["task-degraded"],
        )
        _run, page = self.render(report)

        self.assertIn("Degraded: every editor call failed", page)
        self.assertIn("not a tested model effect", page)

    def test_untrusted_strings_are_escaped(self):
        hostile = "</td><script>alert('x')</script>"
        report = ordinary_report(
            [
                task_row(
                    hostile,
                    modes={
                        "parallel": mode_block(
                            selected_frac=0.5,
                            base_frac=0.5,
                            delta=0.0,
                            selected_error="",
                        )
                    },
                ),
                task_row(
                    "task-error",
                    status="partial",
                    errors=['<img src=x onerror="alert(1)"> & <b>bold</b>'],
                ),
            ],
            notes=['"><script>alert(2)</script>'],
        )
        report["tasks"][0]["modes"]["parallel"]["chosen_label"] = "<svg onload=alert(3)>"
        _run, page = self.render(report)

        self.assertNotIn("<script", page.lower())
        self.assertNotIn("<img", page.lower())
        self.assertNotIn("<svg", page.lower())
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", page)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", page)
        self.assertIn("&lt;svg onload=alert(3)&gt;", page)
        self.assertIn("&amp;", page)

    def test_demo_run_without_a_judge_shows_unverified_agreement(self):
        run = self.root / "demo"
        for rollout, total in (("base1", "10.00"), ("agg1", "10.00"), ("agg2", "9.00")):
            directory = run / "executions" / "demo-oc-agg1" / "ledger-1" / rollout
            directory.mkdir(parents=True)
            (directory / "answer.txt").write_text(f"Total: {total}\n", encoding="utf-8")
        selection = run / "runs" / "demo-oc-agg1" / "ledger-1" / "selection" / "agg1"
        (selection / "candidates").mkdir(parents=True)
        (selection / "candidates" / "A.md").write_text("Total: 10.00\n", encoding="utf-8")
        (selection / "selection.json").write_text(
            json.dumps({"chosen_id": "A", "chosen_label": "agg1", "ok": True}), encoding="utf-8"
        )
        (selection.parent.parent / "summary.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )

        result = self.dashboard("--run", str(run))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = (run / "dashboard.html").read_text(encoding="utf-8")

        self.assertIn("agreement with the reference output the run selected — unverified", page)
        self.assertIn("not accuracy, not correctness", page)
        self.assertIn("reference total: 10.00", page)
        self.assertIn("agg1: 10.00 — matches reference (unverified)", page)
        self.assertIn("agg2: 9.00 — differs from reference (unverified)", page)
        self.assertNotIn("class='chip ok'", page)
        self.assertNotIn("class='chip miss'", page)
        self.assertNotIn("accuracy:", page)
        self.assertNotIn("judged:", page)

    def test_compare_writes_a_side_by_side_page(self):
        self.write_run(
            "run-a",
            ordinary_report(
                [task_row("t1", modes={"parallel": mode_block(selected_frac=1.0, base_frac=0.0,
                                                             delta=1.0)})]
            ),
        )
        self.write_run(
            "run-b",
            ordinary_report([task_row("t2", modes={"parallel": mode_block()})]),
        )

        result = self.dashboard(
            "--compare", str(self.root / "run-a"), str(self.root / "run-b")
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = (self.root / "dashboard_compare.html").read_text(encoding="utf-8")

        self.assertIn("harness2 run comparison — 2 runs", page)
        self.assertIn("run-a", page)
        self.assertIn("run-b", page)
        self.assertIn("post-selection judge per task / mode", page)
        self.assertIn("t1 / parallel", page)
        self.assertIn("judged: selected 100.0%", page)
        self.assertIn("not in this run", page)
        self.assertIn("graded share", page)

    def test_compare_names_agreement_runs_as_unverified(self):
        demo = self.root / "demo"
        (demo / "executions" / "m1" / "t1" / "agg1").mkdir(parents=True)
        (demo / "runs" / "m1" / "t1" / "selection" / "s").mkdir(parents=True)
        ordinary = self.write_run(
            "run-a", ordinary_report([task_row("t1", modes={"parallel": mode_block()})])
        )
        result = self.dashboard("--compare", str(ordinary), str(demo))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = (self.root / "dashboard_compare.html").read_text(encoding="utf-8")
        self.assertIn("have no independent judge; their reference-agreement view is unverified", page)

    def test_table_columns_follow_the_header_for_multi_mode_and_no_mode_tasks(self):
        report = ordinary_report(
            [
                task_row(
                    "task-multi",
                    modes={
                        "parallel": mode_block(selected_frac=1.0, base_frac=0.5, delta=0.5),
                        "sequential": mode_block(),
                    },
                ),
                task_row("task-nomode", status="exception", errors=["boom"]),
            ]
        )
        _run, page = self.render(report)

        table = page.split("<h2>tasks</h2><table>", 1)[1].split("</table>", 1)[0]
        rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
        header = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>(.*?)</th>", rows[0])]
        self.assertEqual(
            header,
            ["task", "mode", "native status", "selection", "judge (post-selection)", "editor"],
        )

        def cells(row):
            return [
                re.sub(r"<[^>]+>", "", c).strip()
                for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            ]

        first, second, third = (cells(row) for row in rows[1:4])
        self.assertEqual(first[:3], ["task-multi", "parallel", "ok"])
        self.assertTrue(first[4].startswith("judged: selected 100.0%"), first)
        # The second mode row is covered by the rowspan task/status/editor cells.
        self.assertEqual(second[0], "sequential")
        self.assertTrue(second[1].startswith("agg1 (candidate"), second)
        self.assertTrue(second[2].startswith("unknown — no post-selection judge record"), second)
        self.assertEqual(third[:4], ["task-nomode", "—", "exception", "—"])
        self.assertTrue(third[4].startswith("unknown — task did not produce a mode record"), third)

    def test_non_finite_judge_values_are_unknown_not_a_percentage(self):
        report = ordinary_report(
            [task_row("t-nan", modes={"parallel": mode_block(selected_frac=1.0, base_frac=0.5,
                                                              delta=0.5)})]
        )
        text = json.dumps(report).replace('"selected_frac": 1.0', '"selected_frac": NaN')
        self.assertIn("NaN", text)
        run = self.root / "nan-run"
        run.mkdir()
        (run / "report.json").write_text(text, encoding="utf-8")
        result = self.dashboard("--run", str(run))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = (run / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("nan%", page.lower())
        self.assertNotIn("inf%", page.lower())
        self.assertIn("unknown — judged pair unavailable", page)
        self.assertIn("judged pairs: 0", page)

    def test_negative_or_out_of_range_selected_and_base_fractions_are_unknown(self):
        report = ordinary_report(
            [
                task_row(
                    "t-negative-selected",
                    modes={"parallel": mode_block(selected_frac=-0.5, base_frac=0.5, delta=0.0)},
                ),
                task_row(
                    "t-negative-base",
                    modes={"parallel": mode_block(selected_frac=0.5, base_frac=-0.25, delta=0.0)},
                ),
                task_row(
                    "t-too-large",
                    modes={"parallel": mode_block(selected_frac=1.5, base_frac=0.5, delta=0.0)},
                ),
            ]
        )
        _run, page = self.render(report)

        self.assertNotIn("judged:", page)
        self.assertNotIn("-50.0%", page)
        self.assertNotIn("-25.0%", page)
        self.assertIn("unknown — judged pair unavailable", page)
        self.assertIn("judged pairs: 0", page)
        self.assertIn("unknown: 3", page)

    def test_negative_delta_fraction_is_still_displayed(self):
        report = ordinary_report(
            [
                task_row(
                    "t-regression",
                    modes={"parallel": mode_block(selected_frac=0.75, base_frac=1.0, delta=-0.25)},
                )
            ]
        )
        _run, page = self.render(report)

        self.assertIn("judged: selected 75.0%", page)
        self.assertIn("base 100.0%", page)
        self.assertIn("Δ -25.0 pt", page)
        self.assertIn("class='good'", page)
        self.assertIn("judged pairs: 1", page)

    def test_missing_run_directory_is_a_usage_error(self):
        result = self.dashboard("--run", str(self.root / "absent"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not a directory", result.stderr)

        result = self.dashboard("--compare", str(self.root / "absent"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--compare needs at least two run directories", result.stderr)


if __name__ == "__main__":
    unittest.main()
