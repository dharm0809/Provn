"""Tests for the synthesize.CostLedger — format, cumsum, hard refusal at $30."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from synthesize import (  # noqa: E402
    BUDGET_HARD_USD,
    BUDGET_WARN_USD,
    USD_PER_INPUT_TOKEN,
    USD_PER_OUTPUT_TOKEN,
    CostLedger,
    CostRow,
)


def _row(**overrides) -> CostRow:
    base = dict(
        timestamp="2026-04-27T19:00:00Z",
        model="claude-opus-4-7",
        input_tokens=1000,
        output_tokens=500,
        usd_cost=1000 * USD_PER_INPUT_TOKEN + 500 * USD_PER_OUTPUT_TOKEN,
        batch_id="abc",
        source_spec="test",
    )
    base.update(overrides)
    return CostRow(**base)


def test_pricing_constants_are_sane():
    # Opus-tier pricing $15 / 1M input, $75 / 1M output (verify before run)
    assert USD_PER_INPUT_TOKEN == 15.0 / 1_000_000
    assert USD_PER_OUTPUT_TOKEN == 75.0 / 1_000_000


def test_ledger_appends_jsonl_with_required_fields(tmp_path: pathlib.Path):
    p = tmp_path / "ledger.jsonl"
    led = CostLedger(p)
    led.record(_row())
    led.close()
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    for k in ("timestamp", "model", "input_tokens", "output_tokens", "usd_cost", "batch_id", "source_spec"):
        assert k in rows[0]


def test_cumulative_recovered_from_existing_ledger(tmp_path: pathlib.Path):
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(
        json.dumps({**_row().__dict__}) for _ in range(3)
    ))
    led = CostLedger(p)
    expected = 3 * (1000 * USD_PER_INPUT_TOKEN + 500 * USD_PER_OUTPUT_TOKEN)
    assert abs(led.cumulative_usd - expected) < 1e-9
    led.close()


def test_hard_refuse_exits_on_precheck(tmp_path: pathlib.Path):
    p = tmp_path / "ledger.jsonl"
    expensive_row = _row(input_tokens=int(BUDGET_HARD_USD / USD_PER_INPUT_TOKEN) + 1, output_tokens=0,
                         usd_cost=BUDGET_HARD_USD + 0.01)
    p.write_text(json.dumps(expensive_row.__dict__) + "\n")
    led = CostLedger(p)
    assert led.cumulative_usd >= BUDGET_HARD_USD
    with pytest.raises(SystemExit) as si:
        led.precheck()
    assert si.value.code == 4


def test_warn_threshold_below_hard(capfd, tmp_path: pathlib.Path):
    p = tmp_path / "ledger.jsonl"
    led = CostLedger(p)
    # Push to between $25 and $30
    over_warn = _row(usd_cost=BUDGET_WARN_USD + 1.0)
    led.record(over_warn)
    out, err = capfd.readouterr()
    assert "BUDGET WARN" in err
    assert led.cumulative_usd < BUDGET_HARD_USD
    led.close()
