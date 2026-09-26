from mech import verdict


def test_passed_shape():
    v = verdict.passed()
    assert v == {"outcome": "pass", "findings": [], "summary": None}


def test_failed_carries_findings():
    f = verdict.finding("ruff", "E501 line too long", code="E501", path="x.py", line=3)
    v = verdict.failed([f], summary="1 finding")
    assert v["outcome"] == "fail"
    assert v["findings"] == [
        {
            "tool": "ruff",
            "code": "E501",
            "path": "x.py",
            "line": 3,
            "message": "E501 line too long",
        }
    ]
    assert v["summary"] == "1 finding"


def test_errored_wraps_message_as_finding():
    assert verdict.errored("mutmut crashed") == {
        "outcome": "error",
        "findings": [
            {
                "tool": "mech",
                "code": None,
                "path": None,
                "line": None,
                "message": "mutmut crashed",
            }
        ],
        "summary": "mutmut crashed",
    }
