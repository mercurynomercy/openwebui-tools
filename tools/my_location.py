"""
title: My Location
description: Resolves the user's current location so other tools (weather, maps, local search) don't need it typed every time. Tries browser GPS first, falls back to IP geolocation, then to a manually configured home location. No API key required.
author: Ryan Pan
author_url: https://github.com/mercurynomercy/openwebui-tools
funding_url: https://github.com/mercurynomercy/openwebui-tools
icon_url: https://raw.githubusercontent.com/mercurynomercy/openwebui-tools/main/icons/my-location.svg
version: 1.1.0
license: MIT
required_open_webui_version: 0.11.0
"""

import ipaddress
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
 
import aiohttp
from pydantic import BaseModel, Field
 
IPWHO_URL = "https://ipwho.is/"
IPAPI_URL = "https://ipapi.co/"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=5)
 
# user_id -> (expires_at_epoch, location_dict)
_LOCATION_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
 
# Runs in the MAIN PAGE CONTEXT (not the sandboxed iframe), so the browser's
# permission prompt works normally. Resolves instead of rejecting so a denial
# comes back as data rather than an exception.
#
# The reverse geocode is done HERE rather than server-side on purpose:
# BigDataCloud's free client-side endpoint requires that requests originate
# from the device being located, using live coordinates. Calling it from the
# Open WebUI server would violate that and can get the server IP banned.
_GEOLOCATION_JS = """
return await new Promise((resolve) => {
    if (!window.isSecureContext) {
        resolve({ ok: false, error: 'insecure_context' });
        return;
    }
    if (!navigator.geolocation) {
        resolve({ ok: false, error: 'unsupported' });
        return;
    }
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const guard = setTimeout(() => done({ ok: false, error: 'timeout' }), 25000);
    navigator.geolocation.getCurrentPosition(
        async (pos) => {
            const lat = pos.coords.latitude;
            const lon = pos.coords.longitude;
            let place = null;
            try {
                const url = 'https://api.bigdatacloud.net/data/reverse-geocode-client'
                    + '?latitude=' + encodeURIComponent(lat)
                    + '&longitude=' + encodeURIComponent(lon)
                    + '&localityLanguage=__LANG__';
                const r = await fetch(url);
                if (r.ok) { place = await r.json(); }
            } catch (e) { /* coordinates alone are still useful */ }
            clearTimeout(guard);
            done({
                ok: true,
                latitude: lat,
                longitude: lon,
                accuracy: pos.coords.accuracy,
                place: place
            });
        },
        (err) => {
            clearTimeout(guard);
            const map = { 1: 'denied', 2: 'unavailable', 3: 'timeout' };
            done({ ok: false, error: map[err.code] || 'unavailable' });
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 600000 }
    );
});
"""
 
 
def _safe_lang(language: str) -> str:
    """Only letters and dashes reach the JS string literal."""
    cleaned = "".join(c for c in (language or "en") if c.isalpha() or c == "-")
    return cleaned[:8] or "en"
 
 
def _parse_bdc(place: Any) -> Dict[str, Any]:
    """Pull the useful fields out of a BigDataCloud reverse-geocode response."""
    if not isinstance(place, dict):
        return {}
    return {
        # 'locality' is the most granular named place: suburb, village or town.
        "suburb": place.get("locality") or "",
        "city": place.get("city") or "",
        "region": place.get("principalSubdivision") or "",
        "country": place.get("countryName") or "",
        "country_code": place.get("countryCode") or "",
        "postcode": place.get("postcode") or "",
    }
 
 
def _coerce_result(raw: Any) -> Dict[str, Any]:
    """__event_call__ may hand back a dict, a JSON string, or a wrapper."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if isinstance(raw, dict):
        # Some Open WebUI versions wrap the JS return value.
        for key in ("result", "value", "data"):
            inner = raw.get(key)
            if isinstance(inner, dict) and ("ok" in inner or "latitude" in inner):
                return inner
        return raw
    return {}
 
 
def _client_ip(request: Any) -> Optional[str]:
    """Best-effort public IP of the browser, not the Open WebUI server."""
    if request is None:
        return None
    try:
        headers = request.headers
    except AttributeError:
        return None
 
    candidates = []
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        candidates.extend(p.strip() for p in forwarded.split(","))
    for header in ("cf-connecting-ip", "x-real-ip", "true-client-ip"):
        value = headers.get(header)
        if value:
            candidates.append(value.strip())
    try:
        if request.client and request.client.host:
            candidates.append(request.client.host)
    except AttributeError:
        pass
 
    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        # Private / loopback means we're behind a reverse proxy or on LAN;
        # an empty IP makes the service geolocate the caller instead.
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return candidate
    return None
 
 
def _round_coords(lat: float, lon: float, precision: int) -> Tuple[float, float]:
    return round(float(lat), precision), round(float(lon), precision)
 
 
async def _ip_lookup(
    session: aiohttp.ClientSession, ip: Optional[str]
) -> Dict[str, Any]:
    """Try ipwho.is, then ipapi.co. Both are keyless and HTTPS."""
    try:
        async with session.get(IPWHO_URL + (ip or "")) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if data.get("success") and data.get("latitude") is not None:
                    return {
                        "latitude": data["latitude"],
                        "longitude": data["longitude"],
                        "city": data.get("city", ""),
                        "region": data.get("region", ""),
                        "country": data.get("country", ""),
                        "country_code": data.get("country_code", ""),
                    }
    except (aiohttp.ClientError, ValueError):
        pass
 
    path = f"{ip}/json/" if ip else "json/"
    async with session.get(IPAPI_URL + path) as resp:
        if resp.status != 200:
            raise LookupError(f"IP geolocation returned HTTP {resp.status}")
        data = await resp.json(content_type=None)
 
    if data.get("error") or data.get("latitude") is None:
        raise LookupError("IP geolocation could not resolve a position")
    return {
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "city": data.get("city", ""),
        "region": data.get("region", ""),
        "country": data.get("country_name", ""),
        "country_code": data.get("country_code", ""),
    }
 
 
def _format(loc: Dict[str, Any]) -> str:
    parts = [
        loc.get("suburb"),
        loc.get("city"),
        loc.get("region"),
        loc.get("country"),
    ]
    # Drop "Sydney, Sydney, Australia" style duplication.
    deduped = []
    for p in parts:
        if p and p not in deduped:
            deduped.append(p)
    return ", ".join(deduped) or "Unknown location"
 
 
def _place_query(loc: Dict[str, Any]) -> str:
    """A string other tools can pass straight into a geocoder.

    Most specific first, so a geocoder that indexes suburbs can use one and a
    geocoder that doesn't can fall back to the city behind it.
    """
    parts = []
    for key in ("suburb", "city", "region"):
        value = loc.get(key)
        if value and value not in parts:
            parts.append(value)
    if not parts:
        return _format(loc)
    code = loc.get("country_code") or ""
    if code:
        parts.append(code)
    return ", ".join(parts)


def _coord_query(loc: Dict[str, Any]) -> str:
    """Coordinates in the 'lat,lon' form location-aware tools accept."""
    return f"{loc.get('latitude')},{loc.get('longitude')}"
 
 
class Tools:
    class Valves(BaseModel):
        enable_browser_gps: bool = Field(
            default=True,
            description=(
                "Ask the browser for GPS/WiFi position first. Most accurate, but "
                "requires HTTPS or localhost and shows a one-time permission prompt."
            ),
        )
        enable_ip_fallback: bool = Field(
            default=True,
            description="Fall back to IP-based geolocation (city-level accuracy).",
        )
        coordinate_precision: int = Field(
            default=2,
            description=(
                "Decimal places kept on coordinates before anything leaves this "
                "tool. 2 is about 1 km, 3 about 110 m, 4 about 11 m."
            ),
        )
        cache_minutes: int = Field(
            default=30,
            description="Reuse the resolved location for this many minutes.",
        )
        language: str = Field(
            default="en", description="Language for place names, e.g. 'en', 'zh'."
        )
 
    class UserValves(BaseModel):
        home_location: str = Field(
            default="",
            description=(
                "Your fallback location, e.g. 'Sydney, AU'. Used when GPS is "
                "denied and IP lookup fails. Leave empty to disable."
            ),
        )
        always_use_home: bool = Field(
            default=False,
            description="Skip detection entirely and always use Home Location.",
        )
 
    def __init__(self):
        self.valves = self.Valves()
 
    def _user_valves(self, __user__: Optional[dict]) -> "Tools.UserValves":
        raw = (__user__ or {}).get("valves")
        if isinstance(raw, self.UserValves):
            return raw
        if isinstance(raw, dict):
            return self.UserValves(**raw)
        return self.UserValves()
 
    async def get_my_location(
        self,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __event_call__: Optional[Callable[[Any], Awaitable[Any]]] = None,
    ) -> str:
        """
        Get the user's current location (city, region, country and coordinates).
 
        Call this whenever the user asks about "here", "my area", "nearby", or
        asks a location-dependent question (weather, local time, nearby places)
        without naming a place. Pass the returned place name to other tools.
        """
        user_valves = self._user_valves(__user__)
        user_id = str((__user__ or {}).get("id") or "anonymous")
 
        async def status(text: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": text, "done": done}}
                )
 
        # 0. Manual override wins outright.
        if user_valves.always_use_home and user_valves.home_location.strip():
            await status("Using your configured home location.", True)
            return (
                f"User location: {user_valves.home_location.strip()} "
                "(source: manually configured home location). "
                "Use this place name for location-dependent tools."
            )
 
        # 1. Cache.
        cached = _LOCATION_CACHE.get(user_id)
        if cached and cached[0] > time.time():
            loc = cached[1]
            await status(f"Location: {_format(loc)} (cached)", True)
            return self._render(loc, cached=True)
 
        precision = max(0, min(int(self.valves.coordinate_precision), 5))
        loc: Optional[Dict[str, Any]] = None
 
        # 2. Browser geolocation (position + reverse geocode, both client-side).
        if self.valves.enable_browser_gps and __event_call__:
            await status("Asking your browser for its location...")
            code = _GEOLOCATION_JS.replace("__LANG__", _safe_lang(self.valves.language))
            try:
                raw = await __event_call__({"type": "execute", "data": {"code": code}})
                result = _coerce_result(raw)
            except Exception:
                result = {}
 
            if result.get("ok") and result.get("latitude") is not None:
                lat, lon = _round_coords(
                    result["latitude"], result["longitude"], precision
                )
                place = _parse_bdc(result.get("place"))
                loc = {
                    "suburb": place.get("suburb", ""),
                    "city": place.get("city", ""),
                    "region": place.get("region", ""),
                    "country": place.get("country", ""),
                    "country_code": place.get("country_code", ""),
                    "postcode": place.get("postcode", ""),
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy_m": result.get("accuracy"),
                    "source": "browser geolocation"
                    if place
                    else "browser geolocation (no place name)",
                }
            else:
                reason = result.get("error", "unavailable")
                if reason == "insecure_context":
                    await status(
                        "Browser location needs HTTPS or localhost; trying IP lookup."
                    )
                elif reason == "denied":
                    await status("Location permission denied; trying IP lookup.")
 
        # 3. IP geolocation.
        if loc is None and self.valves.enable_ip_fallback:
            await status("Estimating your location from your IP address...")
            try:
                async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                    data = await _ip_lookup(session, _client_ip(__request__))
                lat, lon = _round_coords(
                    data["latitude"], data["longitude"], precision
                )
                loc = {**data, "latitude": lat, "longitude": lon, "source": "IP address"}
            except Exception:
                loc = None
 
        # 4. Configured fallback.
        if loc is None:
            if user_valves.home_location.strip():
                await status("Falling back to your configured home location.", True)
                return (
                    f"User location: {user_valves.home_location.strip()} "
                    "(source: configured home location, because automatic detection "
                    "failed). Use this place name for location-dependent tools."
                )
            await status("Could not determine location.", True)
            return (
                "Could not determine the user's location. Browser geolocation was "
                "unavailable or denied and IP lookup failed. Ask the user which "
                "city they want, and mention they can set a Home Location in this "
                "tool's user settings to avoid being asked again."
            )
 
        _LOCATION_CACHE[user_id] = (
            time.time() + max(0, int(self.valves.cache_minutes)) * 60,
            loc,
        )
        await status(f"Location: {_format(loc)}", True)
        return self._render(loc)
 
    def _render(self, loc: Dict[str, Any], cached: bool = False) -> str:
        lines = [f"User location: {_format(loc)}"]
        if loc.get("suburb"):
            lines.append(f"Suburb/locality: {loc['suburb']}")
        if loc.get("postcode"):
            lines.append(f"Postcode: {loc['postcode']}")
        lines += [
            f"Place name for other tools: {_place_query(loc)}",
            f"Coordinates for other tools: {_coord_query(loc)}",
            f"Source: {loc.get('source', 'unknown')}"
            + (" (cached)" if cached else ""),
        ]
        accuracy = loc.get("accuracy_m")
        if accuracy:
            lines.append(f"Reported accuracy: about {round(float(accuracy))} m")
        if loc.get("source") == "IP address":
            lines.append(
                "Note: IP-based location is city-level only, gives no suburb, "
                "and can be wrong on a VPN."
            )
        lines.append(
            "For location-aware tools, prefer the coordinates above (they are "
            "exact and need no lookup) and pass the place name as the display "
            "label, so the result is named for the suburb the user is in."
        )
        return "\n".join(lines)
 
    async def clear_location_cache(self, __user__: Optional[dict] = None) -> str:
        """
        Forget the cached location and force a fresh lookup next time.
 
        Use this if the user says the detected location is wrong, or that they
        have moved or changed network.
        """
        _LOCATION_CACHE.pop(str((__user__ or {}).get("id") or "anonymous"), None)
        return "Cached location cleared. The next location request will re-detect."