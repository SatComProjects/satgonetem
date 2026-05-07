"""Link budget calculation package for SatGoNetem.

This package provides utilities for computing satellite-ground link budgets.
The atmospheric attenuation calculations require the optional ``itur`` dependency
(install with ``pip install satgonetem[extra]``).
"""

from satgonetem.link_budget.budget import LinkBudgetCalculator, LinkBudgetResult
from satgonetem.link_budget.config import AntennaConfig, LinkBudgetConfig
from satgonetem.link_budget.conversions import db_to_linear, linear_to_db
from satgonetem.link_budget.geometry import get_elevation_angle
from satgonetem.link_budget.modcod import ModCod
from satgonetem.link_budget.modulation import calculate_link_capacity, calculate_theoretical_link_capacity
from satgonetem.link_budget.propagation import calculate_atmospheric_attenuation_dB, calculate_free_space_loss_db
from satgonetem.link_budget.receiver import calculate_carrier_to_noise_power_spectral_density_ratio, calculate_carrier_to_noise_ratio
from satgonetem.link_budget.service import LinkBudgetInputs, LinkBudgetService
from satgonetem.link_budget.strategies import (
    CapacityStrategy,
    ModCodCapacityStrategy,
    ShannonCapacityStrategy,
)
from satgonetem.link_budget.transmitter import calculate_transmitter_eirp

__all__ = [
    "AntennaConfig",
    "CapacityStrategy",
    "LinkBudgetCalculator",
    "LinkBudgetConfig",
    "LinkBudgetInputs",
    "LinkBudgetResult",
    "LinkBudgetService",
    "ModCod",
    "ModCodCapacityStrategy",
    "ShannonCapacityStrategy",
    "calculate_atmospheric_attenuation_dB",
    "calculate_carrier_to_noise_power_spectral_density_ratio",
    "calculate_carrier_to_noise_ratio",
    "calculate_free_space_loss_db",
    "calculate_link_capacity",
    "calculate_theoretical_link_capacity",
    "calculate_transmitter_eirp",
    "db_to_linear",
    "get_elevation_angle",
    "linear_to_db",
]
