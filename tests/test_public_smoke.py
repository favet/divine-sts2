from __future__ import annotations

import json
from pathlib import Path

import pytest

from sts2_native_sim import cli


ROOT = Path(__file__).resolve().parents[1]


def test_public_schemas_are_valid_json() -> None:
    state = json.loads((ROOT / "schemas" / "canonical-state.schema.json").read_text(encoding="utf-8"))
    action = json.loads((ROOT / "schemas" / "legal-action.schema.json").read_text(encoding="utf-8"))
    assert state["properties"]["schema_version"]["const"] == 2
    assert "play_card" in action["properties"]["kind"]["enum"]


def test_cli_help_is_side_effect_free(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert "doctor" in capsys.readouterr().out
