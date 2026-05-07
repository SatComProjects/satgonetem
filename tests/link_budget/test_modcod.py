"""Tests for satgonetem.link_budget.modcod."""


from satgonetem.link_budget.modcod import ModCod


class TestModCodEnum:
    def test_all_members_have_spectral_efficiency(self):
        for mc in ModCod:
            assert isinstance(mc.spectral_efficiency, float)
            assert mc.spectral_efficiency > 0.0

    def test_all_members_have_csat_n0_rs(self):
        for mc in ModCod:
            assert isinstance(mc.csat_n0_rs, float)

    def test_list_modcods(self):
        modcods = ModCod.list_modcods()
        assert len(modcods) == len(ModCod)
        assert all(isinstance(m, ModCod) for m in modcods)


class TestBestForCsatN0Rs:
    def test_very_low_metric_returns_none(self):
        assert ModCod.best_for_csat_n0_rs(-100.0) is None

    def test_high_metric_returns_highest_efficiency(self):
        best = ModCod.best_for_csat_n0_rs(100.0)
        assert best is not None
        assert best == max(ModCod, key=lambda m: m.spectral_efficiency)

    def test_selects_appropriate_modcod(self):
        # Metric between QPSK_11_20 (1.97 dB) and PSK8_23_36 (6.96 dB)
        best = ModCod.best_for_csat_n0_rs(5.0)
        assert best is not None
        assert best == ModCod.QPSK_11_20

    def test_exact_threshold(self):
        # Exactly at a threshold should still qualify
        target = ModCod.APSK16_26_45
        best = ModCod.best_for_csat_n0_rs(target.csat_n0_rs)
        assert best is not None
        assert best.spectral_efficiency >= target.spectral_efficiency
