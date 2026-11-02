"""Direct Bird service control shared by the routing daemons.

The shipped satellite image has no ``start-service.py`` helper, so the daemons
write their generated configuration inside the container and drive Bird with
``bird``/``birdc`` directly.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.node import Node

BIRD_CONTROL_SOCKET = "/run/bird/bird.ctl"
BIRD_CONFIG_PATH = "/etc/bird/bird.conf"


def apply_bird_config(node: "Node", config: str) -> None:
    """Install *config* and start or reload Bird inside *node*'s container.

    The config is written inside the container, a running daemon is reloaded
    with ``birdc configure``, and an inactive daemon is started with
    ``bird -c``. The method then verifies the daemon reports itself up. Any
    failure raises through :meth:`Node.execute_command` with the command output.

    Args:
        node: The node whose container runs Bird.
        config: Complete Bird configuration as a string.
    """
    payload = base64.b64encode(config.encode("utf-8")).decode("ascii")
    node.execute_command(
        f"mkdir -p /run/bird /etc/bird && "
        f"printf %s '{payload}' | base64 -d > {BIRD_CONFIG_PATH} && "
        f"if birdc -s {BIRD_CONTROL_SOCKET} show status >/dev/null 2>&1; then "
        f"birdc -s {BIRD_CONTROL_SOCKET} configure; "
        f"else rm -f {BIRD_CONTROL_SOCKET}; "
        f"bird -c {BIRD_CONFIG_PATH} -s {BIRD_CONTROL_SOCKET}; fi && "
        f"birdc -s {BIRD_CONTROL_SOCKET} show status"
    )


def stop_bird(node: "Node") -> None:
    """Stop Bird inside *node*'s container.

    A daemon that is not running is not an error: ``birdc down`` fails when no
    control socket exists and the result is ignored.

    Args:
        node: The node whose Bird daemon should stop.
    """
    node.execute_command(
        f"birdc -s {BIRD_CONTROL_SOCKET} down 2>/dev/null || true"
    )
