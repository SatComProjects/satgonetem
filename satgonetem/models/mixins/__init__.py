"""
Mixin classes for composing node behaviour.

Each mixin encapsulates a single, well-defined cross-cutting concern so that
Node subclasses (Satellite, GroundStation) can opt in without inheriting
unrelated responsibilities.
"""

from satgonetem.models.mixins.qos_mixin import QoSCapableMixin

__all__ = [
    "QoSCapableMixin",
]
