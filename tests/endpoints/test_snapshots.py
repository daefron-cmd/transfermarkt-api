import json
from typing import Any

import pytest

from tests.endpoints.cases import CASES, RECORD_COMMAND, SNAPSHOTS_DIR


def _snapshot_of(response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        body.pop("updatedAt", None)
    return {"status_code": response.status_code, "body": body}


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("case", list(CASES))
def test_endpoint_snapshot(case, client, unrecorded_urls, request):
    response = client.get(CASES[case])
    assert not unrecorded_urls, f"Unrecorded upstream URLs {unrecorded_urls}. Run: {RECORD_COMMAND} --case {case}"

    actual = _snapshot_of(response)
    path = SNAPSHOTS_DIR / f"{case}.json"
    if request.config.getoption("--snapshot-update"):
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(actual))
        return
    assert path.exists(), f"Missing snapshot {path.name}. Run: uv run pytest tests/endpoints --snapshot-update"
    assert actual == json.loads(path.read_text())
