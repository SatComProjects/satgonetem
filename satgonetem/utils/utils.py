import math
import time as _time
from functools import wraps
from typing import Any, Callable, Optional
from urllib.request import urlopen

from sgp4.api import Satrec, WGS72  # type: ignore
from sgp4.api import jday  # type: ignore
from sgp4.exporter import export_tle


def time_(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that prints the execution time of the decorated function.

    Supports regular functions, classmethods and staticmethods.
    """

    if isinstance(func, classmethod):
        original = func.__func__

        @wraps(original)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            start = _time.perf_counter()
            result = original(*args, **kwargs)
            elapsed = _time.perf_counter() - start
            print(f"[time] {original.__name__} executed in {elapsed:.6f}s")
            return result

        return classmethod(_wrapper)

    if isinstance(func, staticmethod):
        original = func.__func__

        @wraps(original)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            start = _time.perf_counter()
            result = original(*args, **kwargs)
            elapsed = _time.perf_counter() - start
            print(f"[time] {original.__name__} executed in {elapsed:.6f}s")
            return result

        return staticmethod(_wrapper)

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = _time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = _time.perf_counter() - start
        print(f"[time] {func.__name__} executed in {elapsed:.6f}s")
        return result

    return wrapper


def generate_satellite_tle(
    satellite_name: str,
    inclination_degree: float,
    mean_motion_revolution_per_day: float,
    eccentricity: float = 0.00001,
    argument_of_perigee_degree: float = 0.0,
    mean_anomaly_degree: float = 0.0,
    raan_degree: float = 0.0,
) -> dict[str, str]:
    """Generate a Two-Line Element (TLE) for a satellite.

    Args:
        satellite_name: Name of the satellite.
        inclination_degree: Inclination of the orbit in degrees.
        mean_motion_revolution_per_day: Mean motion in revolutions per day.
        eccentricity: Eccentricity of the orbit.
        argument_of_perigee_degree: Argument of perigee in degrees.
        mean_anomaly_degree: Mean anomaly in degrees.
        raan_degree: Right ascension of ascending node in degrees.

    Returns:
        Dictionary with keys 'satellite_name', 'line1', 'line2'.

    Raises:
        ValueError: If a TLE line checksum or length validation fails.
    """
    jd, fr = jday(2000, 1, 1, 0, 0, 0)

    sat_sgp4 = Satrec()
    sat_sgp4.sgp4init(  # type: ignore
        WGS72,
        "i",
        0,
        (jd + fr) - 2433281.5,
        0.0,
        0.0,
        0.0,
        eccentricity,
        math.radians(argument_of_perigee_degree),
        math.radians(inclination_degree),
        math.radians(mean_anomaly_degree),
        mean_motion_revolution_per_day * 60 / 13750.9870831397,
        math.radians(raan_degree),
    )

    line1, line2 = export_tle(sat_sgp4)

    tle_line1 = line1[:7] + "U 00000ABC 00001.00000000 " + line1[33:]
    tle_line1 = tle_line1[:68] + str(_tle_line_checksum(tle_line1[:68]))
    tle_line2 = line2

    if len(tle_line1) != 69 or _tle_line_checksum(tle_line1[:68]) != int(tle_line1[68]):
        raise ValueError("TLE line 1 checksum failed")
    if len(tle_line2) != 69 or _tle_line_checksum(tle_line2[:68]) != int(tle_line2[68]):
        raise ValueError("TLE line 2 checksum failed")

    return {"satellite_name": satellite_name, "line1": tle_line1, "line2": tle_line2}


def _tle_line_checksum(tle_line_without_checksum: str) -> int:
    """Compute the TLE line checksum for a 68-character line.

    Args:
        tle_line_without_checksum: Exactly 68-character TLE line (no checksum digit).

    Returns:
        Single-digit checksum (0-9).

    Raises:
        ValueError: If the input is not exactly 68 characters.
    """
    if len(tle_line_without_checksum) != 68:
        raise ValueError("Must have exactly 68 characters")
    total = 0
    for ch in tle_line_without_checksum:
        if ch.isnumeric():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def get_interface_from_name(interface_list: list[Any], intName: str) -> Optional[Any]:
    """Find an interface in a list by its trailing name segment.

    Args:
        interface_list: List of Interface objects to search.
        intName: Name to match against interface.name[3:].

    Returns:
        The matching Interface, or None if not found.
    """
    for interface in interface_list:
        if intName == interface.name[3:]:
            return interface
    return None


def unique_pair_id(a: int, b: int) -> int:
    """Compute a unique integer ID for an unordered pair of non-negative integers.

    Args:
        a: First integer.
        b: Second integer.

    Returns:
        A unique non-negative integer for the pair (a, b).
    """
    x, y = sorted((a, b))
    return y * (y + 1) // 2 + x


def distance_3d_km(
    lat1: float,
    lon1: float,
    alt1: float,
    lat2: float,
    lon2: float,
    alt2: float,
) -> float:
    """Calculate the 3D distance in km between two geodetic points.

    Args:
        lat1: Latitude of point 1 in degrees.
        lon1: Longitude of point 1 in degrees.
        alt1: Altitude of point 1 in km.
        lat2: Latitude of point 2 in degrees.
        lon2: Longitude of point 2 in degrees.
        alt2: Altitude of point 2 in km.

    Returns:
        Distance in km.
    """
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    R = 6371.0
    horizontal_distance_km = R * c
    vertical_distance_km = alt2 - alt1

    return math.sqrt(horizontal_distance_km**2 + vertical_distance_km**2)


def fetch_iss_data() -> dict[str, str]:
    """Fetch the current ISS TLE from the ARISS live feed.

    Returns:
        Dictionary with keys 'name', 'tle_line1', 'tle_line2'.

    Raises:
        RuntimeError: If the data cannot be fetched or parsed.
    """
    try:
        with urlopen(
            "https://live.ariss.org/iss.txt", timeout=10
        ) as response:  # noqa: S310
            text = response.read().decode("utf-8").strip()
    except Exception as exc:
        raise RuntimeError("Failed to fetch ISS TLE data") from exc

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        raise RuntimeError("Unexpected ISS TLE format")

    return {
        "name": lines[0],
        "tle_line1": lines[1],
        "tle_line2": lines[2],
    }


def fetch_satellites_data(url: str) -> list[dict[str, str]]:
    """Fetch satellite TLE data from a URL.

    The expected input format is groups of three lines per satellite:
        <satellite name>
        1 <TLE line 1>
        2 <TLE line 2>

    Args:
        url: URL pointing to the TLE data.

    Returns:
        List of dictionaries, each with keys 'name', 'tle_line1', 'tle_line2'.

    Raises:
        RuntimeError: If the data cannot be fetched or parsed.
    """
    try:
        with urlopen(url, timeout=10) as response:  # noqa: S310
            text = response.read().decode("utf-8").strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch satellite TLE data from {url}") from exc

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) % 3 != 0:
        raise RuntimeError(
            f"Unexpected TLE format: expected a multiple of 3 lines, got {len(lines)}"
        )

    satellites: list[dict[str, str]] = []
    for i in range(0, len(lines), 3):
        name = lines[i]
        tle_line1 = lines[i + 1]
        tle_line2 = lines[i + 2]

        if not tle_line1.startswith("1 ") or not tle_line2.startswith("2 "):
            raise RuntimeError(
                f"Invalid TLE lines for satellite '{name}': lines must start with '1 ' and '2 '"
            )

        satellites.append(
            {
                "name": name,
                "tle_line1": tle_line1,
                "tle_line2": tle_line2,
            }
        )

    return satellites


def read_satellites_data(filepath: str) -> list[dict[str, str]]:
    """Read satellite TLE data from a local file.

    The expected input format is groups of three lines per satellite:
        <satellite name>
        1 <TLE line 1>
        2 <TLE line 2>

    Args:
        filepath: Path to the local TLE file.

    Returns:
        List of dictionaries, each with keys 'name', 'tle_line1', 'tle_line2'.

    Raises:
        RuntimeError: If the file cannot be read or parsed.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to read satellite TLE file: {filepath}") from exc

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) % 3 != 0:
        raise RuntimeError(
            f"Unexpected TLE format in {filepath}: expected a multiple of 3 lines, got {len(lines)}"
        )

    satellites: list[dict[str, str]] = []
    for i in range(0, len(lines), 3):
        name = lines[i]
        tle_line1 = lines[i + 1]
        tle_line2 = lines[i + 2]

        if not tle_line1.startswith("1 ") or not tle_line2.startswith("2 "):
            raise RuntimeError(
                f"Invalid TLE lines for satellite '{name}' in {filepath}: lines must start with '1 ' and '2 '"
            )

        satellites.append(
            {
                "name": name,
                "tle_line1": tle_line1,
                "tle_line2": tle_line2,
            }
        )

    return satellites
