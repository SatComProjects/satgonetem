"""satonetem utilities."""

# Import built-in custom strategies so their decorators run and register
# them before any SimulationManager is constructed.
from satgonetem.utils import custom_connection_strategies  # noqa: F401

# Apply the sat_com_trajectopy monkey patch as soon as the utilities package is
# imported, before any builder module can bind PyOrbitalModel by name. This
# keeps the patch effective for direct project_builder imports as well as the
# regular service entry point.
from satgonetem.utils.satcom_fix import apply_satcom_fix

apply_satcom_fix()
