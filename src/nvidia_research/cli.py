"""Pinned annual facts -> calculations -> RDF graph -> evidence and reports.

Amounts remain Decimal until display. No model performs the arithmetic.
"""
import argparse
import hashlib
import html
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef, XSD

EX = Namespace("https://example.org/finance/")
PROV = Namespace("http://www.w3.org/ns/prov#")
API = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json"
ACCESSION = "0001045810-26-000021"
FILING = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm"
PERIODS = {
    2024: ("2023-01-30", "2024-01-28"),
    2025: ("2024-01-29", "2025-01-26"),
    2026: ("2025-01-27", "2026-01-25"),
}
TAGS = {
    "revenue": "Revenues",
    "operating_income": "OperatingIncomeLoss",
    "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
}


def select_fact(payload, tag, start, end, as_of):
    """Select a precise period from one filing, never by SEC's `fy` alone.

    `fy` is the filing's fiscal year even for older comparative observations.
    Conflicting values cause a failure, rather than an arbitrary selection.
    """
    rows = payload["facts"]["us-gaap"][tag]["units"].get("USD", [])
    matches = [r for r in rows if r.get("start") == start and r["end"] == end
               and r["form"] == "10-K" and r["accn"] == ACCESSION
               and r["filed"] <= as_of]
    if not matches:
        raise ValueError(f"No eligible USD annual fact: {tag}, {start}..{end}, as of {as_of}")
    if len({(r["val"], r["filed"]) for r in matches}) != 1:
        raise ValueError(f"Conflicting facts: {tag}, {end}")
    return matches[0]


def percentage(numerator, denominator):
    if not denominator:
        raise ValueError("Cannot calculate a percentage with a zero denominator")
    return Decimal(numerator) / Decimal(denominator) * 100


def normalize(payload, as_of):
    if int(payload["cik"]) != 1045810:
        raise ValueError("Expected NVIDIA CIK 1045810")
    observations, annual = [], []
    for year, (start, end) in PERIODS.items():
        row = {"year": year, "start": start, "end": end}
        for metric, tag in TAGS.items():
            fact = select_fact(payload, tag, start, end, as_of)
            row[metric] = Decimal(str(fact["val"]))
            observations.append({"id": f"fy{year}-{metric}", "year": year,
                                 "metric": metric, "tag": f"us-gaap:{tag}",
                                 "value": str(fact["val"]), "unit": "USD",
                                 "start": start, "end": end, "filed": fact["filed"],
                                 "accession": fact["accn"], "source": FILING})
        row["operating_margin"] = percentage(row["operating_income"], row["revenue"])
        row["revenue_growth"] = (percentage(row["revenue"] - annual[-1]["revenue"],
                                          annual[-1]["revenue"]) if annual else None)
        annual.append(row)
    return observations, annual


def make_graph(observations, annual, commentary):
    g = Graph()
    g.bind("fin", EX); g.bind("prov", PROV)
    for name in ["Company", "Security", "Filing", "FiscalPeriod", "Metric",
                 "Observation", "ReportedObservation", "DerivedObservation", "ManagementExplanation"]:
        g.add((EX[name], RDF.type, RDFS.Class))
    g.add((EX.ReportedObservation, RDFS.subClassOf, EX.Observation))
    g.add((EX.DerivedObservation, RDFS.subClassOf, EX.Observation))
    g.add((EX.NVIDIA, RDF.type, EX.Company))
    g.add((EX.NVIDIA, RDFS.label, Literal("NVIDIA Corporation")))
    g.add((EX.NVDA, RDF.type, EX.Security))
    g.add((EX.NVDA, EX.issuedBy, EX.NVIDIA))
    for o in observations:
        node, period, source = EX[o["id"]], EX[f"FY{o['year']}"], URIRef(o["source"])
        g.add((node, RDF.type, EX.ReportedObservation))
        g.add((node, EX.company, EX.NVIDIA))
        g.add((node, EX.period, period))
        g.add((node, EX.metric, EX[o["metric"]]))
        g.add((EX[o["metric"]], RDF.type, EX.Metric))
        g.add((EX[o["metric"]], EX.xbrlTag, Literal(o["tag"])))
        g.add((node, EX.value, Literal(o["value"], datatype=XSD.decimal)))
        g.add((node, EX.unit, Literal(o["unit"])))
        g.add((node, PROV.wasDerivedFrom, source))
        g.add((source, RDF.type, EX.Filing))
        g.add((source, EX.published, Literal(o["filed"], datatype=XSD.date)))
        g.add((source, EX.accession, Literal(o["accession"])))
        g.add((period, RDF.type, EX.FiscalPeriod))
        g.add((period, EX.start, Literal(o["start"], datatype=XSD.date)))
        g.add((period, EX.end, Literal(o["end"], datatype=XSD.date)))
    for row in annual:
        node = EX[f"fy{row['year']}-operating_margin"]
        g.add((node, RDF.type, EX.DerivedObservation))
        g.add((node, EX.company, EX.NVIDIA))
        g.add((node, EX.period, EX[f"FY{row['year']} ".strip()]))
        g.add((node, EX.metric, EX.operating_margin))
        g.add((EX.operating_margin, RDF.type, EX.Metric))
        g.add((node, EX.value, Literal(row["operating_margin"])))
        g.add((node, EX.unit, Literal("percent")))
        g.add((node, EX.formula, Literal("100 * operating_income / revenue")))
        for metric in ["revenue", "operating_income"]:
            g.add((node, PROV.wasDerivedFrom, EX[f"fy{row['year']}-{metric}"]))
    for note in commentary:
        node = EX[note["id"]]
        g.add((node, RDF.type, EX.ManagementExplanation))
        g.add((node, EX.period, EX[f"FY{note['year']} ".strip()]))
        g.add((node, EX.text, Literal(note["text"])))
        g.add((node, EX.evidenceKind, Literal(note["kind"])))
        g.add((node, EX.locator, Literal(note["locator"])))
        source = URIRef(note["source"])
        g.add((node, PROV.wasDerivedFrom, source))
        g.add((source, RDF.type, EX.Filing))
        g.add((source, EX.published, Literal(note["published"], datatype=XSD.date)))
    return g


def table_rows(annual):
    for r in annual:
        yield [f"FY{r['year']}", r["end"], f"{r['revenue']/10**9:.3f}",
               "—" if r["revenue_growth"] is None else f"{r['revenue_growth']:.2f}%",
               f"{r['operating_income']/10**9:.3f}", f"{r['operating_margin']:.2f}%",
               f"{r['operating_cash_flow']/10**9:.3f}"]


def write_reports(root, annual, commentary, as_of):
    headers = ["Fiscal year", "Period end", "Revenue ($bn)", "Revenue YoY",
               "Operating income ($bn)", "Operating margin", "Operating cash flow ($bn)"]
    rows = list(table_rows(annual))
    basis = (f"Research cutoff: {as_of}. GAAP, consolidated, USD. FY2024–FY2026 annual "
             "comparatives from the FY2026 10-K, published February 25, 2026. "
             "These are not calendar years or historical point-in-time estimates. "
             "Later quarterly results are outside this first milestone.")
    calculations = ("Operating margin = operating income / revenue × 100. "
                    "Revenue YoY = (current revenue / previous revenue − 1) × 100. "
                    "The first year has no growth figure because the comparison baseline is outside this dataset.")
    md = ["# NVIDIA research lab — first financial comparison", "", basis, "",
          "| " + " | ".join(headers) + " |", "| " + " | ".join(["---"]*len(headers)) + " |"]
    md.extend("| " + " | ".join(r) + " |" for r in rows)
    md += ["", f"Financial evidence: [FY2026 10-K]({FILING}), Consolidated Statements of Income and Cash Flows.",
           "", "## Calculations", "", calculations, "", "## Management's explanations", "",
           "Manually reviewed paraphrases, attributed to management; not independently established causal conclusions.", ""]
    for c in commentary:
        md += [f"- **FY{c['year']}:** {c['text']} [Source]({c['source']}). Location: {c['locator']}."]
    md += ["", "## What we can conclude", "",
           "Revenue and operating cash flow increased in both comparisons. Operating margin rose in FY2025 and fell in FY2026. "
           "This describes reported business performance; it does not estimate stock value or predict returns.", "",
           "## Next learning step", "", "Open `docs/lesson-01.md` and trace one observation through `data/curated/knowledge.ttl`.", ""]
    (root/"reports/nvidia.md").write_text("\n".join(md))
    esc = html.escape
    table = "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"
    table += "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    bars = "".join(f'<div class="barrow"><span>FY{r["year"]}</span><div class="bar" style="width:{r["revenue"]/annual[-1]["revenue"]*65:.1f}%"></div><strong>${r["revenue"]/10**9:.1f}bn</strong></div>' for r in annual)
    notes = "".join(f'<article><h3>FY{c["year"]}</h3><p>{esc(c["text"])}</p><a href="{esc(c["source"])}">Source filing</a><small>{esc(c["locator"])}</small></article>' for c in commentary)
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NVIDIA · Research lab</title><style>
body{{font:16px/1.6 system-ui,sans-serif;background:#101715;color:#e6eee8;margin:0}}main{{max-width:1100px;margin:auto;padding:48px 24px}}h1{{font-size:clamp(2rem,5vw,3.5rem);line-height:1.1}}.eyebrow,a{{color:#b5ed82}}p,small{{color:#b6c6bd}}section{{margin:38px 0}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}td,th{{padding:14px;text-align:right;border-bottom:1px solid #34473a;white-space:nowrap}}th{{font-size:12px;color:#b6c6bd}}td:first-child,th:first-child{{text-align:left}}.barrow{{display:flex;align-items:center;gap:16px;margin:16px 0}}.bar{{height:28px;background:#a8db79;border-radius:3px}}.barrow span{{min-width:65px}}.barrow strong{{white-space:nowrap}}article{{padding:20px;background:#1b2721;margin:12px 0;border-radius:8px}}small{{display:block}}a{{text-underline-offset:3px}}</style>
<main><div class="eyebrow">COMPANY RESEARCH LAB / MILESTONE 01</div><h1>NVIDIA, in evidence.</h1><p>{esc(basis)}</p>
<section><h2>Annual revenue</h2>{bars}</section><section><h2>Three years of financial performance</h2><div class="scroll"><table>{table}</table></div><p><a href="{FILING}">Financial source: FY2026 10-K</a> · Consolidated Statements of Income and Cash Flows</p><p>{esc(calculations)}</p></section>
<section><h2>Management's explanations</h2><p>Manually reviewed paraphrases, attributed to management; these are not independently established causal conclusions.</p>{notes}</section>
<section><h2>Your first ontology lesson</h2><p>A number becomes useful evidence when it has a company, metric, period, unit and source. The graph records those connections. Operating margin is a derived observation with links to its inputs.</p><a href="../docs/lesson-01.md">Read lesson 01</a></section>
<footer><p>Offline, reproducible research snapshot. Arithmetic is calculated in Python. Automated extraction, an LLM agent and price analysis are future milestones.</p></footer></main></html>'''
    (root/"reports/nvidia.html").write_text(page)


def build(root, as_of):
    raw = root/"data/raw/companyfacts.json"
    manifest = json.loads((root/"data/raw/manifest.json").read_text())
    entry = next(m for m in manifest if m["path"] == "data/raw/companyfacts.json")
    if hashlib.sha256(raw.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("Raw snapshot checksum mismatch")
    observations, annual = normalize(json.loads(raw.read_text()), as_of)
    commentary = [c for c in json.loads((root/"data/curated/commentary.json").read_text()) if c["published"] <= as_of]
    for directory in ["data/curated", "reports"]:
        (root/directory).mkdir(parents=True, exist_ok=True)
    graph = make_graph(observations, annual, commentary)
    graph.serialize(root/"data/curated/knowledge.ttl", format="turtle")
    (root/"data/curated/observations.json").write_text(json.dumps(observations, indent=2)+"\n")
    context = {"question": "How did NVIDIA revenue, operating margin and operating cash flow change across FY2024–FY2026?",
               "as_of": as_of, "basis": "Annual comparatives in the pinned FY2026 filing, not historical backtesting",
               "source_snapshot_sha256": entry["sha256"], "reported_observations": observations,
               "calculated_annual_results": annual, "management_explanations": commentary,
               "limitations": ["Commentary is manually curated, not automatically extracted.",
                              "No causal or investment-return prediction.", "FY2027 quarters are not included."]}
    (root/"reports/context.json").write_text(json.dumps(context, indent=2, default=str)+"\n")
    write_reports(root, annual, commentary, as_of)
    print(f"Built {len(observations)} sourced observations and {len(graph)} RDF triples.")
    print(f"Open {root/'reports/nvidia.html'}")


def fetch(root, user_agent):
    """Explicit refresh; normal builds work offline from the preserved snapshot."""
    request = Request(API, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=45) as response:
        data = response.read()
    payload = json.loads(data)
    if int(payload.get("cik", 0)) != 1045810:
        raise ValueError("Unexpected API response; snapshot not replaced")
    raw = root/"data/raw"; raw.mkdir(parents=True, exist_ok=True)
    (raw/"companyfacts.json").write_bytes(data)
    manifest = [{"path": "data/raw/companyfacts.json", "url": API,
                 "sha256": hashlib.sha256(data).hexdigest(),
                 "retrieved_at": datetime.now(timezone.utc).isoformat()}]
    (raw/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print("Refreshed SEC snapshot. Run build to regenerate derived files.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project directory (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="Build offline report, context and RDF graph")
    p.add_argument("--as-of", default="2026-09-26", type=date.fromisoformat)
    p = sub.add_parser("fetch", help="Refresh the raw SEC snapshot")
    p.add_argument("--user-agent", required=True, help="Project name and your contact email for SEC access")
    p = sub.add_parser("query", help="Run a local SPARQL SELECT query")
    p.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args.root.resolve(), str(args.as_of))
        elif args.command == "fetch":
            fetch(args.root.resolve(), args.user_agent)
        else:
            graph = Graph().parse(args.root/"data/curated/knowledge.ttl")
            result = graph.query(args.file.read_text())
            print(result.serialize(format="csv").decode(), end="")
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
