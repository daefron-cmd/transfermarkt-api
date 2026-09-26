"""Hook stdin envelope and response emitters (spec REQ-ARCH-7)."""

import json
from typing import IO, Any


def read_event(stream: IO[str]) -> dict[str, Any]:
    return json.loads(stream.read())


def system_message(text: str) -> int:
    print(json.dumps({"systemMessage": text}))
    return 0


def stop_block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0
