"""Ground-object builder models and CSV serialization."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Literal

from sat_com_builder.models import GroundConnectivityProperty


@dataclass
class GroundStationEntry:
    """A single ground station or user terminal entry.

    Args:
        index: Unique integer index for this entry.
        name: Human-readable name (e.g. city name).
        latitude: Latitude in decimal degrees.
        longitude: Longitude in decimal degrees.
        elevation_km: Elevation above sea level in kilometres.
    """

    index: int
    name: str
    latitude: float
    longitude: float
    elevation_km: float

    def to_csv_line(self) -> str:
        """Return the entry formatted as a single CSV line (no newline).

        Returns:
            Comma-separated string: index,name,latitude,longitude,elevation_km.
        """
        return f"{self.index},{self.name},{self.latitude},{self.longitude},{self.elevation_km}"


class GroundObjectFile:
    """A named collection of ground object entries that writes to disk on demand.

    Entries are written in CSV format (no header) compatible with the
    sat_com_topology ground station file format:
        index,name,latitude,longitude,elevation_km

    Args:
        identifier: Human-readable name used as the filename stem and as the
            ground object identifier in the project configuration.
        entries: Ordered list of GroundStationEntry objects to write.
    """

    def __init__(self, identifier: str, entries: list[GroundStationEntry]) -> None:
        self.identifier = identifier
        self.entries = entries

    def write(self, base_dir: str = "/tmp") -> str:
        """Write all entries to a CSV file under base_dir and return its path.

        The filename is derived from identifier by lowercasing and replacing
        spaces with underscores (e.g. "Ground Stations" -> "ground_stations.txt").

        Args:
            base_dir: Directory in which to create the file. Defaults to /tmp.

        Returns:
            Absolute path of the written file as a string.

        Raises:
            OSError: If the file cannot be written.
        """
        safe_name = self.identifier.replace(" ", "_").lower()
        file_path = pathlib.Path(base_dir) / f"{safe_name}.txt"
        for entry in self.entries:
            if any(separator in entry.name for separator in (",", "\r", "\n")):
                raise ValueError(
                    "Ground station names must not contain commas or line "
                    f"breaks: {entry.name!r}"
                )
        with open(file_path, "w") as f:
            for entry in self.entries:
                f.write(entry.to_csv_line() + "\n")
        return str(file_path)

    @classmethod
    def from_csv(cls, identifier: str, csv_path: str) -> GroundObjectFile:
        """Create a GroundObjectFile from a CSV file.

        The CSV file should have no header and follow the format:
            index,name,latitude,longitude,elevation_km
        Args:
            identifier: Human-readable name for this ground object file.
            csv_path: Path to the input CSV file.
        Returns:
            A GroundObjectFile instance with entries parsed from the CSV.
        Raises:
            OSError: If the file cannot be read.
            ValueError: If any line in the CSV is malformed.
        """
        entries = []
        with open(csv_path) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 5:
                    raise ValueError(f"Malformed line in CSV: {line}")
                index, name, lat, lon, elev = parts
                entry = GroundStationEntry(
                    index=int(index),
                    name=name,
                    latitude=float(lat),
                    longitude=float(lon),
                    elevation_km=float(elev),
                )
                entries.append(entry)
        return cls(identifier=identifier, entries=entries)


class GroundObject:
    """Builder that couples a GroundObjectFile with its type and connectivity settings.

    This class is the user-facing builder equivalent of the sat_com_models
    GroundObject Pydantic model. It defers file writing until to_dict() is called.

    Args:
        ground_object_file: The GroundObjectFile containing station entries.
        object_type: Either "ground_station" or "user_terminal".
        connectivity_properties: Connectivity settings for this ground object group.
    """

    def __init__(
        self,
        ground_object_file: GroundObjectFile,
        object_type: Literal["ground_station", "user_terminal"],
        connectivity_properties: GroundConnectivityProperty,
    ) -> None:
        self.ground_object_file = ground_object_file
        self.object_type = object_type
        self.connectivity_properties = connectivity_properties

    def to_dict(self, base_dir: str = "/tmp") -> dict[str, Any]:
        """Write the ground object file and return the configuration dict entry.

        Args:
            base_dir: Directory in which to write the ground object CSV file.

        Returns:
            Dict matching the ground_objects_properties entry schema expected
            by create_satcom_project().

        Raises:
            OSError: If the underlying file cannot be written.
        """
        file_path = self.ground_object_file.write(base_dir)
        return {
            "identifier": self.ground_object_file.identifier,
            "data_file": file_path,
            "type": self.object_type,
            "connectivity_properties": self.connectivity_properties.model_dump(),
        }
