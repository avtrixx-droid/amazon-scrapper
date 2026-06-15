#!/usr/bin/env python3
"""
issue_key.py — Admin CLI for the AmazonScraper license server.

Reads server URL + admin token from env vars or `~/.amazon_scraper_admin`
(two lines: URL=...  TOKEN=...).

Subcommands:
  issue --customer NAME --days N [--machines N] [--notes TEXT]
  list
  extend --key K --days N
  revoke --key K
  release-machine --key K --machine-id M
  info --key K

Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests


# ── Config loading ─────────────────────────────────────────────────────────────
def load_config() -> tuple[str, str]:
    """Return (url, token). Env vars override the dotfile."""
    url = os.environ.get("LICENSE_SERVER_URL", "")
    token = os.environ.get("LICENSE_ADMIN_TOKEN", "")

    dotfile = Path.home() / ".amazon_scraper_admin"
    if dotfile.exists():
        try:
            for line in dotfile.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().upper()
                v = v.strip().strip('"').strip("'")
                if k == "URL" and not url:
                    url = v
                elif k == "TOKEN" and not token:
                    token = v
        except OSError:
            pass

    if not url:
        die("LICENSE_SERVER_URL not set (env or ~/.amazon_scraper_admin).")
    if not token:
        die("LICENSE_ADMIN_TOKEN not set (env or ~/.amazon_scraper_admin).")
    return url.rstrip("/"), token


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ── HTTP helpers ───────────────────────────────────────────────────────────────
def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def post(url: str, path: str, token: str, body: dict) -> dict:
    try:
        r = requests.post(url + path, headers=headers(token),
                          data=json.dumps(body), timeout=30)
    except requests.RequestException as e:
        die(f"network error: {e}")
    return _handle_response(r)


def get(url: str, path: str, token: str, params: dict | None = None) -> dict:
    try:
        r = requests.get(url + path, headers=headers(token),
                         params=params or {}, timeout=30)
    except requests.RequestException as e:
        die(f"network error: {e}")
    return _handle_response(r)


def _handle_response(r) -> dict:
    try:
        data = r.json()
    except ValueError:
        die(f"non-JSON response {r.status_code}: {r.text[:200]}")
    if not r.ok or not data.get("ok"):
        reason = data.get("reason", f"HTTP {r.status_code}")
        die(f"server rejected request: {reason}")
    return data


# ── Pretty printing ────────────────────────────────────────────────────────────
def print_table(rows: list[dict], columns: list[tuple[str, str]]) -> None:
    """rows: list of dicts. columns: list of (header, key) tuples."""
    if not rows:
        print("(no rows)")
        return
    widths = []
    for header, key in columns:
        col_width = len(header)
        for row in rows:
            v = str(row.get(key, ""))
            if len(v) > col_width:
                col_width = len(v)
        widths.append(min(col_width, 60))

    header_line = "  ".join(h.ljust(w) for (h, _), w in zip(columns, widths))
    print(header_line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        cells = []
        for (header, key), w in zip(columns, widths):
            v = str(row.get(key, ""))
            if len(v) > w:
                v = v[: w - 1] + "…"
            cells.append(v.ljust(w))
        print("  ".join(cells))


# ── Subcommand handlers ────────────────────────────────────────────────────────
def cmd_issue(args, url: str, token: str) -> None:
    body = {
        "customer": args.customer,
        "days": args.days,
        "max_machines": args.machines,
        "notes": args.notes or "",
    }
    data = post(url, "/admin/issue", token, body)
    print(f"Issued key for: {data['customer']}")
    print(f"  Key:          {data['key']}")
    print(f"  Issued:       {data['issued_at']}")
    print(f"  Expires:      {data['expires_at']}")
    print(f"  Max machines: {data['max_machines']}")
    if data.get("notes"):
        print(f"  Notes:        {data['notes']}")


def cmd_list(args, url: str, token: str) -> None:
    data = get(url, "/admin/list", token)
    rows = data.get("keys", [])
    for r in rows:
        r["status"] = "REVOKED" if r.get("revoked") else "active"
        r["machines"] = f"{r.get('machines_used', 0)}/{r.get('max_machines', 0)}"
    columns = [
        ("KEY", "key"),
        ("CUSTOMER", "customer"),
        ("EXPIRES", "expires_at"),
        ("MACHINES", "machines"),
        ("STATUS", "status"),
        ("NOTES", "notes"),
    ]
    print_table(rows, columns)


def cmd_extend(args, url: str, token: str) -> None:
    data = post(url, "/admin/extend", token, {"key": args.key, "days": args.days})
    print(f"Extended {data['key']} → new expiry: {data['expires_at']}")


def cmd_revoke(args, url: str, token: str) -> None:
    data = post(url, "/admin/revoke", token, {"key": args.key})
    print(f"Revoked: {data['key']}")


def cmd_unrevoke(args, url: str, token: str) -> None:
    data = post(url, "/admin/unrevoke", token, {"key": args.key})
    print(f"Un-revoked (re-enabled): {data['key']}")


def cmd_release_machine(args, url: str, token: str) -> None:
    data = post(url, "/admin/release-machine", token,
                {"key": args.key, "machine_id": args.machine_id})
    print(f"Released machine {data['machine_id']} from key {data['key']}")


def cmd_info(args, url: str, token: str) -> None:
    data = get(url, "/admin/info", token, params={"key": args.key})
    k = data["key"]
    print(f"Key:          {k['key']}")
    print(f"Customer:     {k['customer']}")
    print(f"Issued:       {k['issued_at']}")
    print(f"Expires:      {k['expires_at']}")
    print(f"Max machines: {k['max_machines']}")
    print(f"Revoked:      {'YES' if k['revoked'] else 'no'}")
    if k.get("notes"):
        print(f"Notes:        {k['notes']}")

    activations = data.get("activations", [])
    print()
    print(f"Activations ({len(activations)}):")
    if activations:
        columns = [
            ("MACHINE_ID", "machine_id"),
            ("ACTIVATED", "activated_at"),
            ("LAST HEARTBEAT", "last_heartbeat"),
            ("APP VER", "app_version"),
        ]
        print_table(activations, columns)
    else:
        print("  (none)")


# ── Entry ──────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Admin CLI for the AmazonScraper license server.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue", help="Issue a new license key.")
    p_issue.add_argument("--customer", required=True)
    p_issue.add_argument("--days", type=int, required=True)
    p_issue.add_argument("--machines", type=int, default=1)
    p_issue.add_argument("--notes", default="")
    p_issue.set_defaults(func=cmd_issue)

    p_list = sub.add_parser("list", help="List all keys.")
    p_list.set_defaults(func=cmd_list)

    p_extend = sub.add_parser("extend", help="Extend a key's expiry.")
    p_extend.add_argument("--key", required=True)
    p_extend.add_argument("--days", type=int, required=True)
    p_extend.set_defaults(func=cmd_extend)

    p_revoke = sub.add_parser("revoke", help="Revoke a key.")
    p_revoke.add_argument("--key", required=True)
    p_revoke.set_defaults(func=cmd_revoke)

    p_unrevoke = sub.add_parser("unrevoke", help="Re-enable a revoked key.")
    p_unrevoke.add_argument("--key", required=True)
    p_unrevoke.set_defaults(func=cmd_unrevoke)

    p_release = sub.add_parser("release-machine",
                               help="Release one machine's activation slot.")
    p_release.add_argument("--key", required=True)
    p_release.add_argument("--machine-id", required=True)
    p_release.set_defaults(func=cmd_release_machine)

    p_info = sub.add_parser("info", help="Show a key + its activations.")
    p_info.add_argument("--key", required=True)
    p_info.set_defaults(func=cmd_info)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    url, token = load_config()
    args.func(args, url, token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        die(f"unexpected error: {e}")
