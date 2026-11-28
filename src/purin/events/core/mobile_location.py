"""Raw system location observations."""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

LOCATION_STATE_KEY = "mobile.location"


class WifiSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    state: Literal["connected", "disconnected", "unknown"]
    ssid: Annotated[str, Field(max_length=128)] | None = None


class LocationFix(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]
    accuracy_m: Annotated[float, Field(ge=0)]
    coordinate_system: Literal["wgs84"]
    kind: Literal["fix", "arrival", "departure"]
    wifi: WifiSnapshot | None = None

    def distance_to(self, other: "LocationFix") -> float:
        lat1, lat2 = math.radians(self.latitude), math.radians(other.latitude)
        dlon = math.radians(other.longitude - self.longitude)
        a = (
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return 12_742_000 * math.asin(math.sqrt(min(1.0, a)))
