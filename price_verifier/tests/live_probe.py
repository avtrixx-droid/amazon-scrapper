"""
live_probe.py — fetch real amazon.in product pages and check the parser on
them, each parse in its own subprocess so a native crash is observed rather
than fatal. Not a unit test (needs internet; run in CI on Windows):

    python -m price_verifier.tests.live_probe --asins 12 --dump 3

For every page it reports, per selectolax engine ("modest" =
selectolax.parser.HTMLParser, "lexbor" = selectolax.lexbor.LexborHTMLParser):
whether parsing crashed the process, and what parse_product_page() read
(kind / price / MRP / seller / availability). --dump N also prints N pages
gzip+base64-encoded between markers, so they can be pulled out of the CI
log and added to the parser's test fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from price_verifier import config

FALLBACK_ASINS = ("B0BSHF7WHW", "B0CHX1W1XY", "B0D1XD1ZV3", "B09G9FPHY6", "B07WFPMPX3", "B08L5WD9D6")

_CHILD = r"""
import json, sys
engine, path, asin = sys.argv[1], sys.argv[2], sys.argv[3]
html = open(path, encoding="utf-8").read()
out = {"engine": engine}
if engine == "modest":
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html)
    out["nodes_with_id"] = sum(1 for _ in tree.css("[id], tr.po-brand"))
elif engine == "lexbor":
    from selectolax.lexbor import LexborHTMLParser
    tree = LexborHTMLParser(html)
    out["nodes_with_id"] = sum(1 for _ in tree.css("[id], tr.po-brand"))
else:  # the tool's own parser, whichever engine it uses
    from price_verifier.fetcher.parser import parse_product_page
    p = parse_product_page(html, asin)
    out.update(kind=p.page_kind, price=p.price, mrp=p.mrp, seller=p.seller, in_stock=p.is_in_stock,
               availability=(p.availability_raw or "")[:60], title=(p.title or "")[:60], brand=p.brand,
               no_featured_offer=getattr(p, "no_featured_offer", None))
print(json.dumps(out))
"""


async def fetch_pages(asins: list[str], query: str, want: int, out_dir: Path) -> dict[str, Path]:
    """Real pages through the tool's own FetchSession (curl_cffi Chrome
    impersonation, 'continue shopping' click-through)."""
    from price_verifier.fetcher.http_client import FetchSession, _continue_shopping_form

    session = FetchSession()
    await session.start()
    pages: dict[str, Path] = {}
    try:
        # More ASINs from a real search, through the same identity.
        ident = session._identity
        for page in (1, 2):
            if len(asins) >= want:
                break
            try:
                r = await ident.client.get(f"/s?k={query}&page={page}")
                html = r.text
                form = _continue_shopping_form(html, session._host)
                if form is not None:
                    r = await ident.client.get(form[0], params=form[1])
                    html = r.text
                    r = await ident.client.get(f"/s?k={query}&page={page}")
                    html = r.text
                found = [a for a in dict.fromkeys(re.findall(r'data-asin="(B0[A-Z0-9]{8})"', html)) if a not in asins]
                print(f"search page {page}: HTTP {r.status_code}, {len(html)} bytes, {len(found)} new ASINs", flush=True)
                asins.extend(found)
            except Exception as e:  # noqa: BLE001
                print(f"search page {page}: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(2)
        for asin in asins[:want]:
            res = await session.fetch(asin)
            size = len(res.html or "")
            print(f"fetch {asin}: status={res.status_code} error={res.error} bytes={size} "
                  f"({res.elapsed_ms:.0f} ms)", flush=True)
            if res.html:
                path = out_dir / f"{asin}.html"
                path.write_text(res.html, encoding="utf-8")
                pages[asin] = path
            await asyncio.sleep(2.5)
    finally:
        await session.close()
    return pages


def probe(path: Path, asin: str) -> dict:
    results = {}
    for engine in ("modest", "lexbor", "tool"):
        proc = subprocess.run([sys.executable, "-c", _CHILD, engine, str(path), asin],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            try:
                results[engine] = json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception:  # noqa: BLE001
                results[engine] = {"error": "bad output", "out": proc.stdout[-300:]}
        else:
            code = proc.returncode & 0xFFFFFFFF
            results[engine] = {"CRASHED": f"exit 0x{code:08X}" if code > 0xFFFF else f"exit {proc.returncode}",
                               "stderr": proc.stderr.strip().splitlines()[-3:]}
    return results


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--asins", type=int, default=12)
    ap.add_argument("--query", default="lapcare")
    ap.add_argument("--dump", type=int, default=3)
    args = ap.parse_args()

    print(f"marketplace: {config.MARKETPLACE_BASE_URL}", flush=True)
    out_dir = Path(tempfile.mkdtemp(prefix="pv_probe_"))
    pages = asyncio.run(fetch_pages(list(FALLBACK_ASINS), args.query, args.asins, out_dir))
    print(f"\n{len(pages)} pages fetched\n", flush=True)

    crashes = {"modest": 0, "lexbor": 0, "tool": 0}
    for asin, path in pages.items():
        t0 = time.time()
        res = probe(path, asin)
        for engine, r in res.items():
            if "CRASHED" in r:
                crashes[engine] += 1
        print(f"== {asin} ({path.stat().st_size} bytes, {time.time() - t0:.1f}s)", flush=True)
        for engine, r in res.items():
            print(f"   {engine:7s} {json.dumps(r, ensure_ascii=False)}", flush=True)
    print(f"\nCRASHES: {crashes} over {len(pages)} pages\n", flush=True)

    for asin, path in list(pages.items())[: args.dump]:
        blob = base64.b64encode(gzip.compress(path.read_bytes(), 9)).decode()
        print(f"=====BEGIN PAGE {asin}=====")
        for i in range(0, len(blob), 4000):
            print(blob[i:i + 4000])
        print(f"=====END PAGE {asin}=====", flush=True)
    return 1 if crashes["tool"] else 0


if __name__ == "__main__":
    sys.exit(main())
