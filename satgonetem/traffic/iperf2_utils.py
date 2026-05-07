import os
import platform
from pathlib import Path
import re
import pandas as pd
import matplotlib

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt


def generate_iperf2_plots(
    log_file_path: str, log_file_name: str = "iperf2", use_tmp_dir=True
) -> list[str]:
    """
    Parse an iperf2 log file and generate plots similar to iperf3.
    Supports TCP and UDP logs.
    """
    # ---------- Output dir ----------
    if use_tmp_dir:
        out_dir = Path("/tmp/results")
    else:
        path = Path(log_file_path)
        path = path.parent
        out_dir = Path(f"{path}/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Parse log ----------
    lines = []
    with open(log_file_path) as f:
        lines = f.readlines()

    # Regex patterns
    tcp_pattern = re.compile(
        r"\[\s*(\d+)\]\s*(\d+\.\d+)-\s*(\d+\.\d+)\s*sec\s*([\d\.]+)\s*MBytes\s*([\d\.]+)\s*Mbits/sec(?:\s*(\d+)\s*retransmits)?"
    )
    udp_pattern = re.compile(
        r"\[\s*(\d+)\]\s*(\d+\.\d+)-\s*(\d+\.\d+)\s*sec\s*([\d\.]+)\s*MBytes\s*([\d\.]+)\s*Mbits/sec\s*([\d\.]+)\s*ms\s*(\d+)\s*/\s*(\d+)\s*\(([\d\.]+)%\)"
    )

    data_rows = []
    summary_data = None
    protocol = "TCP"  # default assumption

    for line in lines:
        line = line.strip()
        m_tcp = tcp_pattern.match(line)
        m_udp = udp_pattern.match(line)
        if m_udp:
            protocol = "UDP"
            stream, start, end, mb, mbps, jitter, lost, total, loss_pct = m_udp.groups()
            start, end = float(start), float(end)
            # Check if this is a summary line (interval spans entire test duration, e.g., > 1 second)
            if end - start > 1.0:
                summary_data = {
                    "bytes": float(mb) * 1_000_000,
                    "bits_per_second": float(mbps) * 1_000_000,
                    "jitter_ms": float(jitter),
                    "lost_packets": int(lost),
                    "total_packets": int(total),
                    "lost_percent": float(loss_pct),
                }
            else:
                data_rows.append(
                    {
                        "_label": f"Socket {stream}",
                        "start": start,
                        "end": end,
                        "bytes": float(mb) * 1_000_000,
                        "bits_per_second": float(mbps) * 1_000_000,
                        "jitter_ms": float(jitter),
                        "lost_packets": int(lost),
                        "total_packets": int(total),
                        "lost_percent": float(loss_pct),
                    }
                )
        elif m_tcp:
            stream, start, end, mb, mbps, retrans = m_tcp.groups()
            data_rows.append(
                {
                    "_label": f"Socket {stream}",
                    "start": float(start),
                    "end": float(end),
                    "bytes": float(mb) * 1_000_000,
                    "bits_per_second": float(mbps) * 1_000_000,
                    "retransmits": int(retrans) if retrans else 0,
                }
            )

    if not data_rows:
        print("No valid iperf2 data found in log.")
        return []

    df = pd.DataFrame(data_rows)

    # ---------- Plot helper ----------
    def plot_metric(df, metric, ylabel, title, filename):
        plt.figure(figsize=(10, 5))
        for label, g in df.groupby("_label"):
            plt.plot(g["end"], g[metric], label=label)
        plt.xlabel("Time (s)")
        plt.ylabel(ylabel)
        plt.title(title)
        plt.legend()
        plt.grid(True)
        fpath = out_dir / filename
        plt.savefig(fpath, bbox_inches="tight", dpi=150)
        plt.close()
        return str(fpath)

    saved_files = []

    # Throughput
    saved_files.append(
        plot_metric(
            df,
            "bits_per_second",
            "Throughput (Mbps)",
            f"{protocol} Throughput per Socket",
            f"{log_file_name}_{protocol.lower()}_throughput.png",
        )
    )

    # Bytes
    saved_files.append(
        plot_metric(
            df,
            "bytes",
            "Bytes (MB)",
            f"{protocol} Bytes per Socket",
            f"{log_file_name}_{protocol.lower()}_bytes.png",
        )
    )

    # Retransmits for TCP
    if protocol == "TCP" and "retransmits" in df.columns:
        saved_files.append(
            plot_metric(
                df,
                "retransmits",
                "Retransmits",
                f"{protocol} Retransmits per Socket",
                f"{log_file_name}_{protocol.lower()}_retransmits.png",
            )
        )

    # Jitter/Loss for UDP
    if protocol == "UDP":
        if "jitter_ms" in df.columns:
            saved_files.append(
                plot_metric(
                    df,
                    "jitter_ms",
                    "Jitter (ms)",
                    f"{protocol} Jitter per Socket",
                    f"{log_file_name}_{protocol.lower()}_jitter.png",
                )
            )
        if "lost_percent" in df.columns:
            saved_files.append(
                plot_metric(
                    df,
                    "lost_percent",
                    "Packet Loss (%)",
                    f"{protocol} Packet Loss per Socket",
                    f"{log_file_name}_{protocol.lower()}_loss.png",
                )
            )

    # Cumulative data plot
    plt.figure(figsize=(10, 5))
    for label, g in df.groupby("_label"):
        g_sorted = g.sort_values("end")
        cumulative_mb = g_sorted["bytes"].cumsum() / 1_000_000
        plt.plot(g_sorted["end"], cumulative_mb, label=label)
    plt.xlabel("Time (s)")
    plt.ylabel("Cumulative Data Transmitted (MB)")
    plt.title(f"{protocol} Cumulative Data per Socket")
    plt.legend()
    plt.grid(True)
    fpath = out_dir / f"{log_file_name}_{protocol.lower()}_cumulative.png"
    plt.savefig(fpath, bbox_inches="tight", dpi=150)
    plt.close()
    saved_files.append(str(fpath))

    # ---------- Print summary ----------
    print("=" * 60)
    print(f"           {protocol} IPERF2 TEST STATISTICS")
    print("=" * 60)
    # Throughput
    avg_throughput_mbps = df["bits_per_second"].mean() / 1_000_000
    print(f"Average Throughput:      {avg_throughput_mbps:.2f} Mbps")
    # Retransmits / Loss
    if protocol == "TCP":
        if "retransmits" in df.columns:
            total_retransmits = df["retransmits"].sum()
            print(f"Total Retransmits:       {total_retransmits}")
    else:
        if summary_data:
            total_lost = summary_data["lost_packets"]
            total_mb = summary_data["bytes"] / 1_000_000
            avg_loss = summary_data["lost_percent"]
            avg_jitter = summary_data["jitter_ms"]
        else:
            total_lost = df["lost_packets"].sum()
            total_mb = df["bytes"].sum() / 1_000_000
            avg_loss = df["lost_percent"].mean()
            avg_jitter = df["jitter_ms"].mean()
        print(f"Total Lost Packets:      {total_lost}")
        print(f"Packet Loss %:           {avg_loss:.2f}%")
        print(f"Average Jitter:          {avg_jitter:.2f} ms")
    # Total data
    print(f"Total Data Transferred:  {total_mb:.2f} MB")
    print("=" * 60)
    print(f"Plots saved to: {out_dir}")
    print("=" * 60)

    # ---------- Open folder ----------
    if use_tmp_dir:
        try:
            system = platform.system()
            if system == "Darwin":
                os.system(f'open "{out_dir}"')
            elif system == "Windows":
                win_path = Path(out_dir).as_posix().replace("/", "\\")
                os.system(f'explorer "{win_path}"')
            else:
                os.system(f'xdg-open "{out_dir}"')
        except Exception:
            pass

    return saved_files


# Example:
# files = generate_iperf2_plots("/path/to/iperf2.log")
# print("Saved plots:", files)
