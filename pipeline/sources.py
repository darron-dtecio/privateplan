"""Multi-format document ingestion keyed to a ticker.

Discovers SEC filings via the submissions index (10-K/10-Q/8-K + press-release
exhibits) and parses a selective set of them — plus any user-supplied documents
(local paths or URLs) — into normalized JSON the analyst/Claude can read.
A user URL that resolves to an HTML page is treated as a website: same-host
links below it are crawled breadth-first (depth ≤ CRAWL_MAX_DEPTH, at most
CRAWL_MAX_PAGES fetches) so linked pages and document files deeper in the
site hierarchy are ingested too.

Formats: HTML (bs4 text + tables), PDF (pdfplumber), CSV, XLSX (openpyxl grids).

Usage:
    python pipeline/sources.py NVDA [--filings 8] [--add URL_OR_PATH ...]

Writes:
    data/<T>/filings.json   — index of recent SEC filings with document lists
    data/<T>/docs/<slug>.json — one parsed document each
    data/<T>/sources.json   — flat manifest of every parsed document
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests

import edgar

ROOT = Path(__file__).resolve().parent.parent

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"

MAX_TEXT_CHARS = 200_000
MAX_TABLES = 40

# SEC's 10 req/s ceiling applies to the client as a whole, so when bulk.py runs
# several tickers at once each process has to slow down proportionally or the
# batch trips the limit and starts collecting 403s. bulk.py sets this; a lone
# run leaves it at 1 and paces exactly as before.
WORKERS = max(1, int(os.environ.get("PIPELINE_WORKERS") or 1))

ARCHIVE_SLEEP = 0.15 * WORKERS  # stay well under SEC's 10 req/s ceiling

# user-supplied website crawling
CRAWL_MAX_DEPTH = 2
CRAWL_MAX_PAGES = 30
CRAWL_SLEEP = 0.3 * WORKERS
CRAWL_SKIP_EXTS = ("png", "jpg", "jpeg", "gif", "svg", "ico", "webp", "css", "js",
                   "zip", "mp3", "mp4", "webm", "woff", "woff2", "ttf", "eot", "xml")


# ------------------------------------------------------------------ fetching --
def _get_bytes(url: str, retries: int = 3) -> tuple[bytes, str]:
    """GET a URL with the SEC-compliant UA; returns (content, content_type)."""
    for attempt in range(retries):
        resp = requests.get(url, headers=edgar.HEADERS, timeout=60)
        if resp.status_code == 200:
            return resp.content, resp.headers.get("Content-Type", "")
        if resp.status_code in (403, 429):
            time.sleep(1 + attempt * 2)
            continue
        resp.raise_for_status()
    resp.raise_for_status()
    return b"", ""


def fetch_submissions(cik: int) -> dict:
    return edgar._get(SUBMISSIONS_URL.format(cik=cik))


def list_filings(subs: dict, forms=("10-K", "10-Q", "8-K"), limit: int = 8) -> list[dict]:
    """Walk the submissions index's parallel arrays into filing dicts.

    Returns the `limit` most recent matching filings, but always scans deeper
    to include the latest 10-K and the latest two 10-Qs even when frequent
    8-K filers push them past the recency window.
    """
    recent = subs.get("filings", {}).get("recent", {})
    n = len(recent.get("accessionNumber", []))

    def row(i):
        return {
            "form": recent["form"][i],
            "accession": recent["accessionNumber"][i],
            "filing_date": recent["filingDate"][i],
            "report_date": (recent.get("reportDate") or [None] * n)[i] or None,
            "primary_document": recent["primaryDocument"][i],
            "items": [s.strip() for s in ((recent.get("items") or [""] * n)[i] or "").split(",")
                      if s.strip()],
        }

    out, want_10k, want_10q = [], 1, 2
    for i in range(n):
        form = recent["form"][i]
        if form not in forms:
            continue
        take = len(out) < limit
        if form == "10-K" and want_10k > 0:
            take = True
        elif form == "10-Q" and want_10q > 0:
            take = True
        if take:
            out.append(row(i))
            if form == "10-K":
                want_10k -= 1
            elif form == "10-Q":
                want_10q -= 1
        if len(out) >= limit and want_10k <= 0 and want_10q <= 0:
            break
    return out


def filing_documents(cik: int, accession: str) -> list[dict]:
    """List a filing's documents with their exhibit types.

    Parses the filing's -index.htm page (its document table carries a Type
    column, e.g. EX-99.1) — filenames alone are unreliable for spotting
    press-release exhibits. URLs point at the raw archive files, never the
    ix?doc= inline-XBRL viewer.
    """
    from bs4 import BeautifulSoup
    acc_nodash = accession.replace("-", "")
    base = ARCHIVES.format(cik=cik, acc_nodash=acc_nodash)
    time.sleep(ARCHIVE_SLEEP)
    content, _ = _get_bytes(f"{base}/{accession}-index.htm")
    soup = BeautifulSoup(content, "lxml")
    docs = []
    for table in soup.find_all("table", class_="tableFile"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            a = tr.find("a")
            name = a.get_text(strip=True) if a else ""
            dtype = cells[3].get_text(" ", strip=True).upper()
            desc = cells[1].get_text(" ", strip=True)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if not name or ext not in ("htm", "html", "pdf", "csv", "xlsx", "txt"):
                continue
            kind = "exhibit_99" if dtype.startswith("EX-99") else "document"
            docs.append({"name": name, "url": f"{base}/{name}", "type": dtype,
                         "description": desc,
                         "format": {"htm": "html"}.get(ext, ext), "kind": kind})
    return docs


# ------------------------------------------------------------------- parsing --
def sniff_format(name: str, content: bytes, content_type: str = "") -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("html", "htm"):
        return "html"
    if ext in ("pdf", "csv", "xlsx", "txt"):
        return ext
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:2] == b"PK":
        return "xlsx"
    if "html" in content_type or b"<html" in content[:2000].lower():
        return "html"
    if "csv" in content_type:
        return "csv"
    return "txt"


def _cap(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def _parse_html(content: bytes) -> dict:
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    tables = []
    for t in soup.find_all("table")[:MAX_TABLES]:
        rows = []
        for tr in t.find_all("tr")[:60]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(cells):
                rows.append(cells)
        # keep tables that look like data, not layout scaffolding
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            tables.append(rows)
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return {"text": _cap(text), "tables": tables}


def _parse_pdf(content: bytes) -> dict:
    import pdfplumber
    pages, tables = [], []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:60]:
            pages.append(page.extract_text() or "")
            if len(tables) < MAX_TABLES:
                for t in page.extract_tables():
                    tables.append([[c or "" for c in row] for row in t[:60]])
    return {"text": _cap("\n\n".join(pages)), "tables": tables[:MAX_TABLES]}


def _parse_csv(content: bytes) -> dict:
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096])
    except csv.Error:
        dialect = csv.excel
    rows = [row for _, row in zip(range(500), csv.reader(io.StringIO(text), dialect))]
    return {"text": _cap(text), "tables": [rows] if rows else []}


def _parse_xlsx(content: bytes) -> dict:
    import openpyxl
    import ir_ingest
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    grids = ir_ingest.workbook_to_grids(wb)
    tables = [[[("" if c is None else c) for c in row] for row in grid]
              for grid in grids.values()][:MAX_TABLES]
    text = "\n\n".join(f"[sheet] {name}" for name in grids)
    return {"text": text, "tables": tables, "sheet_names": list(grids.keys())}


def parse_document(content: bytes, fmt: str, origin: str) -> dict:
    parser = {"html": _parse_html, "pdf": _parse_pdf,
              "csv": _parse_csv, "xlsx": _parse_xlsx}.get(fmt)
    if parser:
        parsed = parser(content)
    else:  # txt and anything unknown: best-effort text
        parsed = {"text": _cap(content.decode("utf-8", errors="replace")), "tables": []}
    parsed.update({"format": fmt, "origin": origin,
                   "parsed_at": datetime.now().isoformat(timespec="seconds")})
    return parsed


# ------------------------------------------------------------------- storage --
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:80]


def _crawl_slug(url: str, name: str) -> str:
    """A filename unique to the URL, not just to its last path segment.

    Investor sites name every section index the same thing — Amazon's crawl hit
    twelve different pages all called default.aspx — and slugging on the last
    segment alone made each one overwrite the last. Twelve fetches, twelve
    parses and twelve manifest entries all resolved to a single surviving file,
    so most of that work was thrown away and the manifest pointed at documents
    that were not there. The path digest keeps them distinct.
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return _slug(f"user_crawl_{name}_{digest}")


def _save_doc(ticker: str, slug: str, parsed: dict, manifest: list, meta: dict) -> None:
    docs_dir = ROOT / "data" / ticker / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / f"{slug}.json"
    path.write_text(json.dumps(parsed, indent=1, default=str), encoding="utf-8")
    manifest.append({**meta, "slug": slug,
                     "parsed_path": f"docs/{slug}.json",
                     "format": parsed["format"],
                     "chars": len(parsed.get("text", "")),
                     "n_tables": len(parsed.get("tables", []))})


def _extract_links(content: bytes, base_url: str) -> list[str]:
    """Absolute, fragment-stripped hrefs from an HTML page, in document order."""
    from urllib.parse import urldefrag, urljoin

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "lxml")
    links, seen = [], set()          # set for the membership test, list for order
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _crawl_site(ticker: str, root_url: str, root_content: bytes, manifest: list) -> None:
    """BFS same-host crawl below a user-supplied page: ingest linked HTML pages
    and document files (PDF/XLSX/CSV) sitting lower in the site hierarchy."""
    from urllib.parse import urlparse

    host = urlparse(root_url).netloc
    seen = {root_url}
    # deque: this is a FIFO and list.pop(0) shifts every remaining entry
    queue = deque((url, 1) for url in _extract_links(root_content, root_url))
    fetched = 0
    while queue and fetched < CRAWL_MAX_PAGES:
        url, depth = queue.popleft()
        parts = urlparse(url)          # parsed once; it was being redone 4x below
        if url in seen or parts.netloc != host:
            continue
        seen.add(url)
        ext = parts.path.rsplit(".", 1)[-1].lower() if "." in parts.path else ""
        if ext in CRAWL_SKIP_EXTS:
            continue
        time.sleep(CRAWL_SLEEP)
        try:
            content, ctype = _get_bytes(url)
        except requests.RequestException:
            continue
        fetched += 1
        name = parts.path.rstrip("/").rsplit("/", 1)[-1] or "page"
        fmt = sniff_format(name, content, ctype)
        parsed = parse_document(content, fmt, url)
        _save_doc(ticker, _crawl_slug(url, name), parsed, manifest,
                  {"kind": "user_doc_crawled", "origin_url": url, "date": None,
                   "form": None, "depth": depth})
        if fmt == "html" and depth < CRAWL_MAX_DEPTH:
            queue.extend((u, depth + 1) for u in _extract_links(content, url) if u not in seen)


def ingest_user_doc(ticker: str, url_or_path: str, manifest: list) -> None:
    is_url = bool(re.match(r"https?://", url_or_path, re.I))
    if is_url:
        content, ctype = _get_bytes(url_or_path)
        name = url_or_path.rstrip("/").rsplit("/", 1)[-1] or "download"
    else:
        p = Path(url_or_path)
        content, ctype, name = p.read_bytes(), "", p.name
    fmt = sniff_format(name, content, ctype)
    parsed = parse_document(content, fmt, url_or_path)
    _save_doc(ticker, _slug(f"user_{name}"), parsed, manifest,
              {"kind": "user_doc", "origin_url": url_or_path, "date": None, "form": None})
    # A URL that resolves to an HTML page is a website, not a file: crawl below
    # it so data deeper in the site hierarchy (linked pages, PDFs, workbooks)
    # gets ingested too.
    if is_url and fmt == "html":
        _crawl_site(ticker, url_or_path, content, manifest)


# ----------------------------------------------------------------- SEC flow --
def _parse_filing_doc(ticker: str, filing: dict, doc: dict, manifest: list, kind: str) -> None:
    content, ctype = _get_bytes(doc["url"])
    fmt = sniff_format(doc["name"], content, ctype)
    parsed = parse_document(content, fmt, doc["url"])
    slug = _slug(f'{filing["form"]}_{filing["filing_date"]}_{doc["name"].rsplit(".", 1)[0]}')
    _save_doc(ticker, slug, parsed, manifest,
              {"kind": kind, "origin_url": doc["url"], "date": filing["filing_date"],
               "form": filing["form"]})
    doc["parsed"] = f"docs/{slug}.json"


def run(ticker: str, extra_docs: tuple[str, ...] = (), filing_limit: int = 8) -> dict:
    ticker = ticker.upper()
    out_dir = ROOT / "data" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    errors: list[str] = []

    cik = None
    try:
        cik, _ = edgar.ticker_to_cik(ticker)
        subs = fetch_submissions(cik)
        filings = list_filings(subs, limit=filing_limit)
    except Exception as e:
        errors.append(f"SEC submissions: {e}")
        filings = []

    # Selective parse set: latest 10-K, latest two 10-Qs, EX-99.* of earnings 8-Ks.
    parsed_10k = 0
    parsed_10q = 0
    for filing in filings:
        try:
            docs = filing_documents(cik, filing["accession"])
        except Exception as e:
            errors.append(f'{filing["form"]} {filing["accession"]}: {e}')
            filing["documents"] = []
            continue
        primary = next((d for d in docs if d["name"] == filing["primary_document"]), None)
        filing["primary_doc_url"] = primary["url"] if primary else None
        filing["documents"] = docs

        try:
            if filing["form"] == "10-K" and primary and parsed_10k < 1:
                time.sleep(ARCHIVE_SLEEP)
                _parse_filing_doc(ticker, filing, primary, manifest, "sec_filing")
                parsed_10k += 1
            elif filing["form"] == "10-Q" and primary and parsed_10q < 2:
                time.sleep(ARCHIVE_SLEEP)
                _parse_filing_doc(ticker, filing, primary, manifest, "sec_filing")
                parsed_10q += 1
            elif filing["form"] == "8-K" and "2.02" in filing["items"]:
                for doc in docs:
                    if doc["kind"] == "exhibit_99" and doc["format"] in ("html", "pdf"):
                        time.sleep(ARCHIVE_SLEEP)
                        _parse_filing_doc(ticker, filing, doc, manifest, "press_release")
        except Exception as e:
            errors.append(f'parse {filing["form"]} {filing["filing_date"]}: {e}')

    for extra in extra_docs:
        try:
            ingest_user_doc(ticker, extra, manifest)
        except Exception as e:
            errors.append(f"user doc {extra}: {e}")

    filings_out = {"ticker": ticker, "cik": cik,
                   "fetched_at": datetime.now().isoformat(timespec="seconds"),
                   "filings": filings}
    (out_dir / "filings.json").write_text(
        json.dumps(filings_out, indent=1, default=str), encoding="utf-8")

    sources_out = {"ticker": ticker,
                   "generated_at": datetime.now().isoformat(timespec="seconds"),
                   "documents": manifest, "errors": errors}
    (out_dir / "sources.json").write_text(
        json.dumps(sources_out, indent=1, default=str), encoding="utf-8")
    return sources_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--filings", type=int, default=8, help="how many recent filings to index")
    ap.add_argument("--add", action="append", default=[],
                    help="extra document URL or local path (repeatable)")
    args = ap.parse_args()
    result = run(args.ticker, tuple(args.add), args.filings)
    print(f'{len(result["documents"])} documents parsed into data/{args.ticker.upper()}/docs/')
    for d in result["documents"]:
        print(f'  {d["kind"]:>13}  {d["form"] or "-":>5}  {d["slug"]}.json '
              f'({d["chars"] // 1000} K chars, {d["n_tables"]} tables)')
    for e in result["errors"]:
        print(f"  ! {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
