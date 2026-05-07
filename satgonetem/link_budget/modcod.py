"""DVB-S2X MODCOD definitions and selection logic.

Spectral efficiency values (bits/symbol) and C_sat/(N0*Rs) thresholds (dB) are
taken from ETSI EN 302 307-2 V1.3.1 (2021-7), Table 20a (non-linear channel,
hard-limiter model).
"""

from enum import Enum
from typing import Optional


class ModCod(Enum):
    """DVB-S2X modulation and coding schemes.

    Each member stores:
      * ``spectral_efficiency`` - bits per symbol
      * ``csat_n0_rs`` - ideal C_sat/(N_0 * R_s) threshold in dB for a
        non-linear hard-limiter channel (ETSI EN 302 307-2 Table 20a).
    """

    QPSK_2_9 = (0.434841, -2.45)
    QPSK_13_45 = (0.567805, -1.60)
    QPSK_9_20 = (0.889135, 0.69)
    QPSK_11_20 = (1.088581, 1.97)

    PSK8_23_36 = (1.896173, 6.96)
    PSK8_25_36 = (2.062148, 7.93)
    PSK8_13_18 = (2.145136, 8.42)

    APSK8_5_9_L = (1.647211, 5.95)
    APSK8_26_45_L = (1.713601, 6.35)

    APSK16_1_2_L = (1.972253, 8.40)
    APSK16_8_15_L = (2.104850, 9.00)
    APSK16_5_9_L = (2.193247, 9.35)
    APSK16_26_45 = (2.281645, 9.17)
    APSK16_3_5 = (2.370043, 9.38)
    APSK16_3_5_L = (2.370043, 9.94)
    APSK16_28_45 = (2.458441, 9.76)
    APSK16_23_36 = (2.524739, 10.04)
    APSK16_2_3_L = (2.635236, 11.06)
    APSK16_25_36 = (2.745734, 11.04)
    APSK16_13_18 = (2.856231, 11.52)
    APSK16_7_9 = (3.077225, 12.50)
    APSK16_77_90 = (3.386618, 14.00)

    APSK32_2_3_L = (3.291954, 13.81)
    APSK32_32_45 = (3.510192, 14.50)
    APSK32_11_15 = (3.620536, 14.91)
    APSK32_7_9 = (3.841226, 15.84)

    APSK64_32_45_L = (4.206428, 17.70)
    APSK64_11_15 = (4.338659, 17.97)
    APSK64_7_9 = (4.603122, 19.10)
    APSK64_4_5 = (4.735354, 19.54)
    APSK64_5_6 = (4.936639, 20.44)

    APSK128_3_4 = (5.163248, 21.43)
    APSK128_7_9 = (5.355556, 22.21)

    APSK256_29_45_L = (5.065690, 21.60)
    APSK256_2_3_L = (5.241514, 21.89)
    APSK256_31_45_L = (5.417338, 22.90)
    APSK256_32_45 = (5.593162, 22.91)
    APSK256_11_15_L = (5.768987, 23.80)
    APSK256_3_4 = (5.900855, 24.02)

    def __init__(self, spectral_efficiency: float, csat_n0_rs: float):
        self.spectral_efficiency = spectral_efficiency
        self.csat_n0_rs = csat_n0_rs

    @classmethod
    def list_modcods(cls) -> list["ModCod"]:
        """Return a list of all defined MODCODs."""
        return list(cls)

    @classmethod
    def best_for_csat_n0_rs(cls, available_csat_n0_rs: float) -> Optional["ModCod"]:
        r"""Select the highest-spectral-efficiency MODCOD that fits the link.

        Args:
            available_csat_n0_rs: Available :math:`C_{sat}/(N_0 \cdot R_s)` in dB.

        Returns:
            The best qualifying :class:`ModCod`, or *None* if none qualify.
        """
        eligible = [m for m in cls if m.csat_n0_rs <= available_csat_n0_rs]
        if not eligible:
            return None
        return max(eligible, key=lambda m: m.spectral_efficiency)
