#!/usr/bin/env python3
"""Bridge a local terminal to a GoNetEm node via the NodeExec gRPC stream.

This script is spawned inside a local terminal emulator (xterm, gnome-terminal,
etc.) by ``TopologyManager.open_bash_terminal``.  It puts stdin into raw mode,
starts a bidirectional NodeExec stream running ``/bin/bash`` with a TTY on the
remote node, and relays bytes in both directions.  SIGWINCH is caught so that
local terminal resizes are forwarded to the remote shell.

Usage:
    python -m satgonetem.utils.node_terminal_bridge <server> <project_id> <node_name>
"""

from __future__ import annotations

import fcntl
import os
import queue
import select
import signal
import struct
import sys
import termios
import threading
import tty

import grpc

# Import empty_pb2 first to populate the descriptor pool before netem_pb2 loads.
from google.protobuf import empty_pb2  # noqa: F401

from satgonetem.proto import netem_pb2, netem_pb2_grpc


def _find_python() -> str:
    """Return the current Python interpreter path."""
    return sys.executable


def _log_error(msg: str) -> None:
    """Write an error message to a log file and to stdout."""
    log_path = f"/tmp/satgonetem_bridge_{os.getpid()}.log"
    try:
        with open(log_path, "a") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass
    sys.stdout.write(msg + "\r\n")
    sys.stdout.flush()


def main() -> None:
    if len(sys.argv) != 4:
        print(
            f"Usage: {_find_python()} -m satgonetem.utils.node_terminal_bridge "
            "<server> <project_id> <node_name>",
            file=sys.stderr,
        )
        sys.exit(1)

    server = sys.argv[1]
    project_id = sys.argv[2]
    node_name = sys.argv[3]

    try:
        channel = grpc.insecure_channel(server)
        stub = netem_pb2_grpc.NetemStub(channel)

        input_queue: queue.Queue = queue.Queue()
        done_event = threading.Event()

        def request_generator():
            yield netem_pb2.ExecCltMsg(
                code=netem_pb2.ExecCltMsg.CMD,
                prjId=project_id,
                node=node_name,
                cmd=["/bin/bash"],
                tty=True,
            )
            while True:
                msg = input_queue.get()
                if msg is None:
                    break
                yield msg

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

        def restore_tty():
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass

        try:
            stream = stub.NodeExec(request_generator())

            def send_resize() -> None:
                try:
                    size = fcntl.ioctl(
                        fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
                    )
                    h, w, _, _ = struct.unpack("HHHH", size)
                    input_queue.put(
                        netem_pb2.ExecCltMsg(
                            code=netem_pb2.ExecCltMsg.RESIZE,
                            prjId=project_id,
                            node=node_name,
                            ttyWidth=w,
                            ttyHeight=h,
                        )
                    )
                except Exception:
                    pass

            # Forward initial terminal size.
            send_resize()
            signal.signal(signal.SIGWINCH, lambda _sig, _frame: send_resize())

            def recv_loop() -> None:
                try:
                    for response in stream:
                        if response.code == netem_pb2.ExecSrvMsg.STDOUT:
                            sys.stdout.buffer.write(response.data)
                            sys.stdout.buffer.flush()
                        elif response.code == netem_pb2.ExecSrvMsg.STDERR:
                            sys.stdout.buffer.write(response.data)
                            sys.stdout.buffer.flush()
                        elif response.code in (
                            netem_pb2.ExecSrvMsg.ERROR,
                            netem_pb2.ExecSrvMsg.CLOSE,
                        ):
                            break
                except Exception:
                    pass
                finally:
                    done_event.set()
                    input_queue.put(None)

            recv_thread = threading.Thread(target=recv_loop, daemon=True)
            recv_thread.start()

            while not done_event.is_set():
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    input_queue.put(
                        netem_pb2.ExecCltMsg(
                            code=netem_pb2.ExecCltMsg.DATA,
                            prjId=project_id,
                            node=node_name,
                            data=data,
                        )
                    )

            recv_thread.join(timeout=2)

        finally:
            restore_tty()
            channel.close()

    except Exception as exc:
        _log_error(f"[satgonetem bridge] Error: {exc}")
        import traceback
        _log_error(traceback.format_exc())
        _log_error("Press Enter to close...")
        try:
            input()
        except Exception:
            import time
            time.sleep(5)


if __name__ == "__main__":
    main()
