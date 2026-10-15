import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from rdflib import RDF
from nvidia_research.cli import (
    ACCESSION, EX, PROV, TAGS, normalize, select_fact, percentage,
    make_graph, write_reports,
)

ROOT = Path(__file__).resolve().parents[1]


class FinancialPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((ROOT/"data/raw/companyfacts.json").read_text())
        cls.observations, cls.annual = normalize(cls.payload, "2026-09-26")
        cls.notes = json.loads((ROOT/"data/curated/commentary.json").read_text())

    def test_annual_values_match_reviewed_filing(self):
        # Independent table values verified in the consolidated income/cash-flow statements.
        self.assertEqual([int(r["revenue"]) for r in self.annual],
                         [60922000000, 130497000000, 215938000000])
        self.assertEqual([int(r["operating_income"]) for r in self.annual],
                         [32972000000, 81453000000, 130387000000])
        self.assertEqual([int(r["operating_cash_flow"]) for r in self.annual],
                         [28090000000, 64089000000, 102718000000])
        self.assertEqual([round(r["operating_margin"], 2) for r in self.annual],
                         [Decimal("54.12"), Decimal("62.42"), Decimal("60.38")])

    def test_cutoff_prevents_future_filing_use(self):
        with self.assertRaisesRegex(ValueError, "No eligible"):
            normalize(self.payload, "2026-02-24")
        normalize(self.payload, "2026-02-25")

    def test_conflicting_values_fail(self):
        payload = copy.deepcopy(self.payload)
        rows = payload["facts"]["us-gaap"][TAGS["revenue"]]["units"]["USD"]
        row = next(r for r in rows if r["accn"] == ACCESSION)
        rows.append(dict(row, val=row["val"]+1))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            normalize(payload, "2026-09-26")

    def test_quarterly_decoy_and_wrong_units_are_not_selected(self):
        tag = TAGS["revenue"]
        payload = {"facts": {"us-gaap": {tag: {"units": {"USD": [
            {"start": "2025-10-27", "end": "2026-01-25", "val": 99,
             "form": "10-K", "accn": ACCESSION, "filed": "2026-02-25"}
        ], "EUR": [
            {"start": "2025-01-27", "end": "2026-01-25", "val": 1,
             "form": "10-K", "accn": ACCESSION, "filed": "2026-02-25"}
        ]}}}}}
        with self.assertRaisesRegex(ValueError, "No eligible"):
            select_fact(payload, tag, "2025-01-27", "2026-01-25", "2026-09-26")

    def test_undefined_ratio_fails(self):
        with self.assertRaises(ValueError):
            percentage(1, 0)

    def test_graph_query_and_calculation_lineage(self):
        graph = make_graph(self.observations, self.annual, self.notes)
        result = list(graph.query((ROOT/"docs/revenue.sparql").read_text()))
        self.assertEqual(len(result), 3)
        self.assertEqual(int(result[-1].value), 215938000000)
        for node in graph.subjects(RDF.type, EX.DerivedObservation):
            inputs = list(graph.objects(node, PROV.wasDerivedFrom))
            self.assertEqual(len(inputs), 2)
            for source in inputs:
                self.assertIn((source, RDF.type, EX.ReportedObservation), graph)

    def test_report_has_evidence_and_no_baseline_growth(self):
        self.assertIsNone(self.annual[0]["revenue_growth"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/"reports").mkdir()
            write_reports(root, self.annual, self.notes, "2026-09-26")
            report = (root/"reports/nvidia.html").read_text()
            self.assertIn("60.38%", report)
            self.assertIn("000104581026000021", report)
            self.assertIn("Manually reviewed", report)


if __name__ == "__main__":
    unittest.main()
