"""Download documents into finance_data/inbox/ from URLs you supply — locally.

Reads finance_data/links.txt (one URL per line; blank lines and lines starting
with # are ignored), or takes --url directly. Any direct-download HTTP(S) URL
works. OneDrive share links (1drv.ms, *.sharepoint.com, onedrive.live.com) are
recognised and rewritten through the shares API, because a bare share URL
returns a viewer page rather than the file.

This is a convenience for links you already have. The Upload box on /finance
is the simpler path and needs no configuration; if a link fails because it
wants a sign-in, download it in your browser and upload it instead.

Diagnostics print file names, sizes, and HTTP status only — never content.
Downloaded bytes are written into inbox/ under a sanitised name and never
outside it, so a hostile Content-Disposition cannot escape the directory.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import INBOX, LINKS_PATH, diag

MAX_BYTES = 64 * 1024 * 1024        # a statement that big is a mistake, not a statement
ONEDRIVE_HOSTS = ("1drv.ms", "onedrive.live.com", "sharepoint.com")


def share_id(url: str) -> str:
    """OneDrive's base64url share-id encoding of a share URL."""
    b = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return "u!" + b


def is_onedrive(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ONEDRIVE_HOSTS)


def candidate_urls(url: str) -> list[str]:
    """The URLs to try, in order, for one link."""
    if is_onedrive(url):
        # The shares API returns the bytes directly; the download=1 redirect is
        # the fallback for links the anonymous API rejects with 401.
        return [f"https://api.onedrive.com/v1.0/shares/{share_id(url)}/root/content",
                url + ("&" if "?" in url else "?") + "download=1"]
    return [url]


def safe_name(name: str, idx: int, fallback_ext: str = ".bin") -> str:
    """A filename that cannot escape inbox/ and cannot name a device."""
    name = unquote(name or "").strip().strip('"')
    name = name.replace("\\", "/").rsplit("/", 1)[-1]      # drop any path
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")
    if not name or name.startswith("."):
        return f"document_{idx}{fallback_ext}"
    return name[:120]


def filename_from(resp: requests.Response, url: str, idx: int) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=\s*(?:[\w-]+''|\")?([^\";]+)", cd, re.I)
    if m:
        # strip any charset'' prefix (RFC 5987) the header may still carry
        raw = re.sub(r"^[\w-]+''", "", m.group(1).strip('"'), flags=re.I)
        name = safe_name(raw, idx)
        if "." in name:
            return name
    # a download redirect usually lands on a URL ending in the real filename
    tail = safe_name(unquote(urlsplit(resp.url).path).rsplit("/", 1)[-1], idx)
    if "." in tail and 3 < len(tail) < 120:
        return tail
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    ext = {"application/pdf": ".pdf", "text/csv": ".csv",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
           "application/vnd.ms-excel": ".xls"}.get(ctype)
    if not ext:
        ext = ".xlsx" if "/x/" in url else (".pdf" if "/b/" in url else ".bin")
    return f"document_{idx}{ext}"


def _fetch(url: str) -> requests.Response | None:
    for attempt in candidate_urls(url):
        try:
            resp = requests.get(attempt, timeout=90, allow_redirects=True)
        except requests.RequestException:
            continue
        ctype = resp.headers.get("content-type", "")
        # An HTML body means a viewer or sign-in page, not the document.
        if resp.status_code == 200 and not ctype.startswith("text/html"):
            return resp
    return None


def download(url: str, idx: int) -> bool:
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme not in {"http", "https"}:
        diag(f"[download] link {idx}: refusing non-HTTP URL ({scheme or 'no scheme'})")
        return False

    resp = _fetch(url)
    if resp is None:
        diag(f"[download] link {idx}: could not fetch (sign-in required?) — "
             "download manually and use the Upload box instead")
        return False
    if len(resp.content) > MAX_BYTES:
        diag(f"[download] link {idx}: {len(resp.content) // 1024} KB exceeds the "
             f"{MAX_BYTES // 1024 // 1024} MB limit — skipped")
        return False

    name = filename_from(resp, url, idx)
    dest = (INBOX / name).resolve()
    if dest.parent != INBOX.resolve():
        diag(f"[download] link {idx}: rejected a filename that escapes inbox/")
        return False
    dest.write_bytes(resp.content)
    diag(f"[download] link {idx}: saved {name} ({len(resp.content) // 1024} KB)")
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", action="append", default=[],
                    help="download this URL instead of reading links.txt")
    args = ap.parse_args()

    common.ensure_dirs()
    if args.url:
        links, source = args.url, "--url"
    else:
        if not LINKS_PATH.exists():
            diag(f"[download] no {LINKS_PATH.name} — paste document URLs on the "
                 "/finance page first, or just use the Upload box")
            return 1
        links = [ln.strip() for ln in LINKS_PATH.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        source = LINKS_PATH.name

    diag(f"[download] {len(links)} link(s) from {source}")
    ok = sum(download(u, i + 1) for i, u in enumerate(links))
    diag(f"[download] done: {ok}/{len(links)} downloaded to inbox/")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert is_onedrive("https://1drv.ms/x/s!AbCdEf")
        assert is_onedrive("https://contoso-my.sharepoint.com/:x:/g/personal/x")
        assert not is_onedrive("https://example.com/statement.pdf")
        # a plain URL is tried as-is; a OneDrive link gets the shares API first
        assert candidate_urls("https://example.com/a.pdf") == ["https://example.com/a.pdf"]
        od = candidate_urls("https://1drv.ms/x/s!AbC")
        assert od[0].startswith("https://api.onedrive.com/v1.0/shares/u!")
        assert od[1].endswith("?download=1")
        assert share_id("https://1drv.ms/x/s!AbC").startswith("u!")
        # names are sanitised so nothing can be written outside inbox/
        assert safe_name("../../etc/passwd", 1) == "passwd"
        assert safe_name("C:\\Windows\\evil.pdf", 1) == "evil.pdf"
        assert safe_name("", 3) == "document_3.bin"
        assert safe_name("statement 2026-07.pdf", 1) == "statement 2026-07.pdf"
        assert safe_name(".hidden", 2) == "hidden"          # leading dots stripped
        assert safe_name("...", 4) == "document_4.bin"      # nothing usable left
        print("[OK] download")
        raise SystemExit(0)
    raise SystemExit(main())
