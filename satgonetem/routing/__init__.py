from satgonetem.routing.base_daemon import RoutingContext, RoutingDaemon
from satgonetem.routing.isis_sr_bird_daemon import ISISBirdSRDaemon
from satgonetem.routing.ospf_daemon import OSPFDaemon
from satgonetem.routing.registry import register_builtin
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.static_daemon import StaticRoutingDaemon

register_builtin("static", StaticRoutingDaemon)
register_builtin("dynamic-ospf", OSPFDaemon)
register_builtin("dynamic-isis", ISISBirdSRDaemon)
register_builtin("sr-mpls", SRMPLSDaemon)

__all__ = [
    "RoutingContext",
    "RoutingDaemon",
    "ISISBirdSRDaemon",
    "OSPFDaemon",
    "SRMPLSDaemon",
    "StaticRoutingDaemon",
]
