#!/usr/bin/env python3
"""Inspect the Plex playback history before scrobbling anything.

Use this to verify the assumptions this project relies on:

  1. Do tracks played offline show up in the history at all?
  2. Does ``viewedAt`` hold the real listening time or the sync time?
  3. Do all played tracks appear, or only some of them?

Usage (from a machine on the same network as the Plex server):

    PLEX_URL=http://plex.example:32400 PLEX_TOKEN=xxxxx ./verify_plex_history.py
    ./verify_plex_history.py --url http://plex.example:32400 --token xxxxx --limit 30

The script neither writes nor submits anything -- it only reads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import requests

TRACK_TYPE = "track"  # value of the "type" field in a history entry


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Show and analyse the Plex history of music tracks")
    p.add_argument("--url", default=os.environ.get("PLEX_URL", ""), help="Base URL of the Plex server")
    p.add_argument("--token", default=os.environ.get("PLEX_TOKEN", ""), help="X-Plex-Token")
    p.add_argument("--limit", type=int, default=30, help="Number of most recent entries (default: 30)")
    p.add_argument("--account-id", default=os.environ.get("PLEX_ACCOUNT_ID", ""), help="Only show this Plex user")
    p.add_argument(
        "--library-section",
        default=os.environ.get("PLEX_LIBRARY_SECTION", ""),
        help="ID of the music library (default: auto-detect)",
    )
    p.add_argument("--json", action="store_true", help="Print the raw JSON of the response")
    return p.parse_args()


def find_music_section(url: str, token: str) -> str:
    """Find the first library of type "artist" -- that is the music library."""
    resp = requests.get(
        f"{url.rstrip('/')}/library/sections",
        headers={"Accept": "application/json", "X-Plex-Token": token},
        timeout=30,
    )
    resp.raise_for_status()
    for section in resp.json().get("MediaContainer", {}).get("Directory", []):
        if section.get("type") == "artist":
            return str(section.get("key"))
    return ""


def fetch(url: str, token: str, limit: int, account_id: str, section: str) -> list[dict]:
    """Query the history.

    Note: the filter ``type=10`` that the Plex documentation often mentions has
    no effect on ``/status/sessions/history/all`` -- it returns an empty result.
    Filtering therefore goes through ``librarySectionID`` plus a client-side
    check on the ``type`` field.
    """
    params = {
        "sort": "viewedAt:desc",
        "X-Plex-Container-Start": 0,
        "X-Plex-Container-Size": limit,
    }
    if section:
        params["librarySectionID"] = section
    if account_id:
        params["accountID"] = account_id
    resp = requests.get(
        f"{url.rstrip('/')}/status/sessions/history/all",
        params=params,
        headers={"Accept": "application/json", "X-Plex-Token": token},
        timeout=30,
    )
    resp.raise_for_status()
    container = resp.json().get("MediaContainer", {})
    entries = container.get("Metadata", []) or []
    return [e for e in entries if e.get("type") == TRACK_TYPE]


def ts(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "?"


def show(entries: list[dict]) -> None:
    print(f"{'viewedAt (local)':<21} {'delta':>8}  {'acct':>5}  Artist - Title [Album]")
    print("-" * 100)
    previous: int | None = None
    for entry in entries:
        viewed = entry.get("viewedAt")
        delta = ""
        if previous is not None and isinstance(viewed, int):
            delta = f"{previous - viewed}s"
        if isinstance(viewed, int):
            previous = viewed
        print(
            f"{ts(viewed):<21} {delta:>8}  {str(entry.get('accountID', '')):>5}  "
            f"{entry.get('grandparentTitle', '?')} - {entry.get('title', '?')} "
            f"[{entry.get('parentTitle', '?')}]"
        )


def analyse(entries: list[dict]) -> None:
    stamps = sorted(e["viewedAt"] for e in entries if isinstance(e.get("viewedAt"), int))
    print()
    print("=" * 100)
    print("Analysis")
    print("=" * 100)
    if not stamps:
        print("No entries with viewedAt found -- question 1 is still open.")
        return

    now = int(datetime.now().timestamp())
    print(f"Entries total ............ {len(entries)}")
    print(f"Oldest entry ............. {ts(stamps[0])}")
    print(f"Newest entry ............. {ts(stamps[-1])}  ({timedelta(seconds=max(now - stamps[-1], 0))} ago)")

    accounts = sorted({str(e.get("accountID")) for e in entries})
    print(f"accountIDs ............... {', '.join(accounts)}")

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    clustered = [g for g in gaps if g <= 5]
    print()
    print("Question 2 -- real listening time or sync time?")
    if not gaps:
        print("  Only one entry -- play 2-3 tracks offline to judge this.")
    elif len(clustered) >= max(1, len(gaps) // 2):
        print(f"  LOOKS LIKE SYNC TIME: {len(clustered)} of {len(gaps)} gaps are below 5 s.")
        print("  Several tracks sharing practically the same viewedAt suggest that Plex")
        print("  records the moment the client reported them, not the real listening time.")
    else:
        print(f"  Gaps between entries: {', '.join(f'{g}s' for g in gaps[-10:])}")
        print("  If the gaps are in the range of track lengths (roughly 120-400 s),")
        print("  viewedAt is the real listening time and the timestamps are usable.")
    print()
    print("Questions 1/3 -- completeness: compare the list above with what was actually")
    print("  played offline. Note any missing tracks.")


def main() -> int:
    args = parse_args()
    if not args.url or not args.token:
        print("PLEX_URL and PLEX_TOKEN are required (argument or environment variable).", file=sys.stderr)
        return 2
    try:
        section = args.library_section or find_music_section(args.url, args.token)
        if not section:
            print("No music library found -- please pass --library-section.", file=sys.stderr)
            return 1
        print(f"Music library: librarySectionID={section}", file=sys.stderr)
        entries = fetch(args.url, args.token, args.limit, args.account_id, section)
    except requests.RequestException as exc:
        print(f"Plex request failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    show(entries)
    analyse(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
