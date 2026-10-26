"""satonetem utilities."""

# Import built-in custom strategies so their decorators run and register
# them before any SimulationManager is constructed.
from satgonetem.utils import custom_connection_strategies  # noqa: F401
