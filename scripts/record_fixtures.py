"""Record upstream Transfermarkt responses for the offline endpoint tests.

Runs each case in tests/endpoints/cases.py through the FastAPI TestClient while wrapping
TransfermarktClient.get, so the real throttle/cache/retry path is used. Every upstream URL
is saved as raw bytes to tests/fixtures/<sha1(url)[:16]>.<html|json> and listed in
tests/fixtures/index.json. Upstream errors (UpstreamError from TransfermarktClient.get) are listed
with their status, reason and detail and no file.

Usage:
    uv run python scripts/record_fixtures.py              # all cases; rebuilds the index, prunes orphans
    uv run python scripts/record_fixtures.py --case NAME  # only these cases; merges into the index
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.http import TransfermarktClient, UpstreamError, UpstreamResponse
from app.main import app
from tests.endpoints.cases import CASES, FIXTURES_DIR, FIXTURES_INDEX


def fixture_name(url: str, body: bytes) -> str:
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    try:
        json.loads(body)
    except ValueError:
        return f"{digest}.html"
    return f"{digest}.json"


def load_index() -> dict[str, dict[str, Any]]:
    if not FIXTURES_INDEX.exists():
        return {}
    return {entry["url"]: entry for entry in json.loads(FIXTURES_INDEX.read_text())}


def write_index(entries: dict[str, dict[str, Any]]) -> None:
    ordered = [entries[url] for url in sorted(entries)]
    FIXTURES_INDEX.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", choices=sorted(CASES), help="record only this case (repeatable)")
    args = parser.parse_args()

    selected = args.case or list(CASES)
    full_run = not args.case
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict[str, Any]] = {} if full_run else load_index()
    recorded: dict[str, dict[str, Any]] = {}

    real_get = TransfermarktClient.get

    async def recording_get(self: TransfermarktClient, url: str) -> UpstreamResponse:
        recorded_at = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            response = await real_get(self, url)
        except UpstreamError as e:
            recorded[e.url] = {
                "url": e.url,
                "status": e.status_code,
                "reason": e.reason,
                "detail": e.detail,
                "recorded_at": recorded_at,
            }
            raise
        name = fixture_name(response.url, response.content)
        (FIXTURES_DIR / name).write_bytes(response.content)
        recorded[response.url] = {
            "url": response.url,
            "file": name,
            "status": response.status_code,
            "recorded_at": recorded_at,
        }
        return response

    TransfermarktClient.get = recording_get  # type: ignore[method-assign]
    try:
        # The context manager runs the app lifespan, which creates the shared client on one event loop.
        with TestClient(app, raise_server_exceptions=False) as client:
            for case in selected:
                before = len(recorded)
                response = client.get(CASES[case])
                print(f"{case:28} {response.status_code}  ({len(recorded) - before} new upstream URLs)")
    finally:
        TransfermarktClient.get = real_get  # type: ignore[method-assign]

    entries.update(recorded)
    write_index(entries)

    if full_run:
        referenced = {entry["file"] for entry in entries.values() if "file" in entry}
        for path in FIXTURES_DIR.iterdir():
            if path.name != FIXTURES_INDEX.name and path.name not in referenced:
                path.unlink()
                print(f"pruned orphan fixture {path.name}")

    errors = [entry for entry in recorded.values() if "file" not in entry]
    print(f"\nrecorded {len(recorded)} upstream URLs ({len(errors)} errors); index has {len(entries)} entries")
    for entry in errors:
        print(f"  error {entry['status']}: {entry['url']} -- {entry['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
