import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import make_sample  # noqa: E402
from finmatch import analyze  # noqa: E402


def test_sample_comparison(tmp_path):
    make_sample.main()
    inr = ROOT / "balance_sheet_inr.pdf"
    usd = ROOT / "balance_sheet_usd.pdf"
    out_a = tmp_path / "a.pdf"
    out_b = tmp_path / "b.pdf"
    result = analyze(str(inr), str(usd), output_a=str(out_a), output_b=str(out_b))

    # The two deliberate wording differences must be caught...
    assert not result.consistent
    flagged = [r for r in result.rows if r.kind == "mismatch"]
    texts = [(r.a.text if r.a else "") + "|" + (r.b.text if r.b else "") for r in flagged]
    assert any("Goodwill" in t for t in texts)
    assert any("Trade" in t for t in texts)

    # ...and the rest must match despite entirely different figures/currency.
    assert result.matched >= 8
    assert out_a.exists() and out_b.exists()
