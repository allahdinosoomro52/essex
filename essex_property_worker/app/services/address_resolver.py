from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from app.models import Parcel


class AddressResolutionError(RuntimeError):
    pass


class OutsideEssexCountyError(AddressResolutionError):
    pass


class AddressResolver:
    """Resolve an address to Essex County municipality/block/lot.

    The resolver uses ArcGIS public services as a first implementation:
    1. Geocode the entered address.
    2. Confirm the candidate is in Essex County, NJ.
    3. Query the NJ parcel layer at the candidate point.

    This class is deliberately isolated so a county-specific GIS endpoint can be
    swapped in if it proves more precise during field testing.
    """

    geocode_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
    parcel_query_url = "https://mapsdep.nj.gov/arcgis/rest/services/Applications/RSP_Base_Layers/MapServer/0/query"

    async def resolve(self, address: str) -> Parcel:
        candidate = await asyncio.to_thread(self._geocode, address)
        self._assert_essex_county(candidate)
        parcel_feature = await asyncio.to_thread(self._lookup_parcel, candidate)
        attrs = parcel_feature.get("attributes", {})

        municipality = self._first_present(attrs, ["PCL_MUN_NAME", "MUN_NAME", "MUNICIPALITY", "MUN"])
        block = self._first_present(attrs, ["PCLBLOCK", "BLOCK", "PROP_BLOCK"])
        lot = self._first_present(attrs, ["PCLLOT", "LOT", "PROP_LOT"])

        if not municipality or not block or not lot:
            raise AddressResolutionError(
                "Address was geocoded in Essex County, but the parcel source did not return municipality/block/lot."
            )

        return Parcel(
            input_address=address,
            normalized_address=candidate.get("address") or address,
            county="Essex",
            municipality=str(municipality).upper(),
            block=str(block),
            lot=str(lot),
            qualifier=self._first_present(attrs, ["QUALIFIER", "PCLQCODE"]),
            parcel_id=self._first_present(attrs, ["PAMS_PIN", "PROP_ID", "PARCEL_ID"]),
            owner_name=self._first_present(attrs, ["OWNER_NAME", "OWN_NAME", "OWNER"]),
            source="ArcGIS geocoder + NJ parcel layer",
            raw={"geocode": candidate, "parcel": attrs},
        )

    def _geocode(self, address: str) -> dict[str, Any]:
        params = {
            "SingleLine": address,
            "f": "json",
            "outFields": "*",
            "sourceCountry": "USA",
            "maxLocations": 5,
        }
        data = self._get_json(self.geocode_url, params)
        candidates = data.get("candidates") or []
        if not candidates:
            raise AddressResolutionError("Address could not be geocoded.")
        return candidates[0]

    def _lookup_parcel(self, candidate: dict[str, Any]) -> dict[str, Any]:
        location = candidate.get("location") or {}
        x = location.get("x")
        y = location.get("y")
        if x is None or y is None:
            raise AddressResolutionError("Geocoder did not return a usable point for this address.")

        params = {
            "f": "json",
            "geometry": json.dumps({"x": x, "y": y, "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "false",
            "outSR": 4326,
        }
        data = self._get_json(self.parcel_query_url, params)
        features = data.get("features") or []
        if not features:
            raise AddressResolutionError("No parcel was found at the resolved address point.")
        return features[0]

    def _assert_essex_county(self, candidate: dict[str, Any]) -> None:
        attrs = candidate.get("attributes") or {}
        region = str(attrs.get("Region") or attrs.get("RegionAbbr") or "").upper()
        subregion = str(attrs.get("Subregion") or "").upper()
        if region not in {"NJ", "NEW JERSEY"}:
            raise OutsideEssexCountyError("Address is not in New Jersey.")
        if "ESSEX" not in subregion:
            raise OutsideEssexCountyError("Address is not in Essex County, NJ.")

    # The public ArcGIS geocoder and the NJ parcel layer are both live third-party
    # services that occasionally respond slowly. A single timeout should not fail the
    # whole lookup, so transient network errors are retried a few times with backoff.
    _http_attempts = 3
    _http_timeout = 30

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        request_url = f"{url}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self._http_attempts):
            try:
                with urlopen(request_url, timeout=self._http_timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self._http_attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise AddressResolutionError(
            f"A geocoding/parcel service did not respond after {self._http_attempts} attempts. "
            "This is a transient upstream issue - please try again."
        ) from last_error

    def _first_present(self, attrs: dict[str, Any], keys: list[str]) -> str | None:
        for key in keys:
            value = attrs.get(key)
            if value not in (None, ""):
                return str(value)
        return None

