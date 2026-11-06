"""Exact-value metadata and calendar arithmetic for the statistics tooltips."""

from collections import Counter
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
import chart_style
import loc_graph
import loc_roadmap_graph
import pr_stats_graphs
import roadmap_completion


def metadata(path):
    svg = ET.parse(path).getroot()
    return json.loads(svg.find("{http://www.w3.org/2000/svg}metadata").text)


class StatisticsHoverTests(unittest.TestCase):
    def test_growth_uses_calendar_days_including_quiet_days_and_deletions(self):
        data = loc_graph.carry_quiet_days([
            ("2026-01-01", 100), ("2026-01-03", 80), ("2026-01-08", 170)])
        daily, weekly = loc_graph.growth_metrics(data)
        self.assertEqual(daily, [None, 0, -20, 0, 0, 0, 0, 90])
        self.assertEqual(weekly, [None] * 7 + [10])
        # A missing calendar baseline must not become an adjacent-sample difference.
        self.assertEqual(loc_graph.growth_metrics([data[0], data[-1]]),
                         ([None, None], [None, 10]))

    def test_axis_stays_close_to_peak_across_round_number_boundaries(self):
        for peak in [1, 10, 101, 25001, 5_000_001, 7_650_000, 10_000_001]:
            with self.subTest(peak=peak):
                top = loc_graph.loc_axis_max(peak)
                self.assertGreater(top, peak)
                self.assertLessEqual(top, peak * 1.15 + 1)
        self.assertEqual(loc_graph.loc_axis_max(0), 1)
        self.assertEqual(loc_graph.loc_axis_max(5_000_001), 5_300_000)

    def test_svg_round_trips_exact_values_and_fixed_mathlib_comparison(self):
        data = [(f"2026-01-{i+1:02d}", 100 + 10*i) for i in range(8)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "loc.svg"
            loc_graph.render(data, "Lean <&>", "#ff9d4d", path,
                             comparison={"lines": 50, "ref": "a" * 40})
            plot = metadata(path)[0]
        self.assertEqual(plot["labels"], [d for d, _ in data])
        self.assertEqual(plot["series"][0]["values"], [v for _, v in data])
        self.assertEqual(plot["series"][1]["values"], [None] + [10] * 7)
        self.assertEqual(plot["series"][2]["values"], [None] * 7 + [10])
        self.assertEqual(plot["series"][3]["values"], [v / 50 for _, v in data])
        self.assertIn("50 LOC at pinned aaaaaaaaaaaa", plot["note"])
        self.assertEqual((plot["x"][0], plot["x"][-1]), (72, 872))

    def test_metadata_escapes_xml_without_changing_labels(self):
        plots = [{"label": '<script> & "someone" </metadata>'}]
        node = ET.fromstring(chart_style.hover_metadata(plots))
        self.assertEqual(json.loads(node.text), plots)

    def test_all_history_renderers_export_the_values_they_plot(self):
        dates = ["2026-01-01", "2026-01-02"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            loc_roadmap_graph.render(dates, ["roadmap/Widgets"],
                                    {"roadmap/Widgets": [10, 15]}, {"roadmap/Widgets": 15},
                                    "Roadmaps", root / "roadmaps.svg")
            self.assertEqual(metadata(root / "roadmaps.svg")[0]["series"][1]["values"], [10, 15])
            rows = [dict(date=day, layers=10, completed_layers=i, unassessed_layers=3)
                    for i, day in enumerate(dates)]
            roadmap_completion.render_history(rows, "layers", "completed_layers", "unassessed_layers",
                                              "Layers", "layers", root / "layers.svg")
            self.assertEqual(metadata(root / "layers.svg")[0]["series"][2]["values"], [3, 3])
            rolling = [dict(date=day, merges=i, active_authors=i+2,
                            median_merge_hours=None if i == 0 else 2.5, p90_merge_hours=None)
                       for i, day in enumerate(dates)]
            pr_stats_graphs.render_rolling(root / "rolling.svg", rolling)
            plots = metadata(root / "rolling.svg")
            self.assertEqual(len(plots), 4)
            self.assertEqual(plots[2]["series"][0]["values"], [None, 2.5])
            self.assertEqual(plots[3]["series"][0]["values"], [None, None])
            pr_stats_graphs.render_cumulative_contributors(root / "contributors.svg", "Merges", "merges",
                                                          dates, ["a&b"], {"a&b": [1, 2]}, Counter({"a&b": 2}), 1)
            self.assertEqual(metadata(root / "contributors.svg")[0]["series"],
                             [{"label": "a&b", "unit": "merges", "values": [1, 2]}])


if __name__ == "__main__":
    unittest.main()
