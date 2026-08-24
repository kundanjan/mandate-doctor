"""Tests for NPCI calibration loading and aggregation."""

import pytest

from eval.generate_batch import (
    compute_weighted_aggregates,
    load_bank_calibration,
)


class TestCalibrationLoading:
    """The frozen NPCI snapshot must parse cleanly and match published values."""

    def test_loads_fifty_remitter_banks(self):
        banks = load_bank_calibration()
        assert len(banks) == 50

    def test_every_bank_has_all_four_categories(self):
        banks = load_bank_calibration()
        for bank in banks:
            assert bank["volume_mn"] >= 0
            assert 0 <= bank["approved_pct"] <= 100
            assert 0 <= bank["bd_pct"] <= 100
            assert 0 <= bank["td_pct"] <= 100

    def test_missing_snapshot_raises_explicit_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Calibration CSV not found"):
            load_bank_calibration(tmp_path / "does_not_exist.csv")

    def test_weighted_aggregates_match_frozen_npci_values(self):
        banks = load_bank_calibration()
        aggregates = compute_weighted_aggregates(banks)
        assert aggregates["approved_pct"] == pytest.approx(22.9527, abs=0.05)
        assert aggregates["bd_pct"] == pytest.approx(76.1478, abs=0.05)
        assert aggregates["td_pct"] == pytest.approx(0.8969, abs=0.05)


class TestCalibrationInvariants:
    """The snapshot must not silently accept the payer-PSP file."""

    def test_payer_psp_snapshot_has_different_column_name(self):
        """The payer-PSP CSV uses payer_psp, not remitter_bank, so the
        remitter loader must fail loudly rather than misparse it."""
        from eval.generate_batch import DATA_DIR

        payer_path = DATA_DIR / "npci-autopay-payer-psp-execution-2026-07.csv"
        with pytest.raises(KeyError):
            load_bank_calibration(payer_path)
