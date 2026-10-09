"""Hand-computed rNPV examples (no database or network)."""
from app.analysis.rnpv import compute_rnpv, detect_area

POS = {"default": {"phase2": 0.15, "phase3": 0.55, "filed": 0.90},
       "oncology": {"phase2": 0.10, "phase3": 0.45, "filed": 0.88}}
YEARS = {"phase2": 4, "phase3": 2, "filed": 1}


def test_phase3_asset_with_1b_peak_sales():
    # profit at peak 0.30 * 1,000M = 300M; value at launch 300M * 8 = 2,400M;
    # discounted 2 years at 12%: 2,400M / 1.2544 = 1,913.27M; times 55% = 1,052.30M.
    assets = [{"name": "X", "peak_sales_usd": 1e9, "phase": "phase3", "area": "default"}]
    total, breakdown = compute_rnpv(assets, 0.12, POS, YEARS)
    assert round(total / 1e6, 2) == 1052.30
    assert breakdown[0]["probability"] == 0.55


def test_oncology_uses_lower_probability():
    assets = [{"name": "X", "peak_sales_usd": 1e9, "phase": "phase3", "area": "oncology"}]
    total, _ = compute_rnpv(assets, 0.12, POS, YEARS)
    assert round(total / 1e6, 2) == round(0.45 * 2400 / 1.2544, 2)


def test_two_assets_add_up_and_missing_peak_sales_is_skipped():
    assets = [
        {"name": "A", "peak_sales_usd": 500e6, "phase": "phase2", "area": "default"},
        {"name": "B", "peak_sales_usd": 500e6, "phase": "filed", "area": "default"},
        {"name": "C", "peak_sales_usd": None, "phase": "phase3", "area": "default"},
    ]
    total, breakdown = compute_rnpv(assets, 0.12, POS, YEARS)
    a = 0.15 * (500e6 * 0.30 * 8) / 1.12 ** 4
    b = 0.90 * (500e6 * 0.30 * 8) / 1.12 ** 1
    assert len(breakdown) == 2
    assert abs(total - (a + b)) < 1


def test_approved_asset_is_certain_and_undiscounted():
    assets = [{"name": "A", "peak_sales_usd": 100e6, "phase": "approved", "area": "default"}]
    total, _ = compute_rnpv(assets, 0.12, POS, YEARS)
    assert total == 100e6 * 0.30 * 8


def test_unknown_phase_is_skipped_and_area_detection():
    assets = [{"name": "A", "peak_sales_usd": 100e6, "phase": "preclinical?", "area": "default"}]
    assert compute_rnpv(assets, 0.12, POS, YEARS)[0] == 0
    assert detect_area("Non-small cell lung cancer", None) == "oncology"
    assert detect_area("Rett syndrome", "oncology_bispecifics_adc_radiopharma") == "oncology"
    assert detect_area("Rett syndrome", "neuropsychiatry") == "default"
