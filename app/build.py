"""Build app/index.html from app/template.html with the sample data embedded.

    python app/build.py data/demo_all.csv data/demo_ranks.csv --label "Sep 2026, 5 keywords x 3 cities"

The tracker's CSVs are embedded as JSON inside a <script> tag so the page is a
single file that works from disk or GitHub Pages with no server.
"""
import argparse, csv, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))

def rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("all_csv"); ap.add_argument("ranks_csv", nargs="?")
    ap.add_argument("--label", default="sample capture")
    ap.add_argument("--repo", default="https://github.com/illusionist-creator/qcomm-shelf-monitor")
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    a = ap.parse_args()
    sample = {"label": a.label, "repo": a.repo, "all": rows(a.all_csv), "ranks": rows(a.ranks_csv) if a.ranks_csv else []}
    payload = json.dumps(sample, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    marker = "/*__SAMPLE_DATA__*/"
    if marker not in html:
        sys.exit("marker not found in template")
    html = html.replace(marker, "window.SAMPLE = " + payload + ";")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {a.out}: {len(sample['all'])} result rows, {len(sample['ranks'])} rank rows, {os.path.getsize(a.out)//1024} KB")

if __name__ == "__main__":
    main()
