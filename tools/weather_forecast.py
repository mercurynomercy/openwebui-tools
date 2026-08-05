"""
title: Weather Forecast
description: Fetches weather forecasts from the Open-Meteo API (no API key required) and renders an interactive HTML weather widget with current conditions, hourly and daily forecasts. Icons are inline SVG, so the widget makes no external image requests.
author: Ryan Pan/ Open-Meteo port
author_url: https://github.com/mercurynomercy/openwebui-tools
funding_url: https://github.com/mercurynomercy/openwebui-tools
version: 2.1.0
license: MIT
required_open_webui_version: 0.11.0
"""

import asyncio
import html
import re
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import aiohttp
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

# --------------------------------------------------------------------------
# WMO weather code -> (description, icon key)
# https://open-meteo.com/en/docs  (WMO 4677)
# --------------------------------------------------------------------------
WMO_CODES: Dict[int, Tuple[str, str]] = {
    0: ("clear sky", "clear"),
    1: ("mainly clear", "partly"),
    2: ("partly cloudy", "partly"),
    3: ("overcast", "cloudy"),
    45: ("fog", "fog"),
    48: ("depositing rime fog", "fog"),
    51: ("light drizzle", "drizzle"),
    53: ("moderate drizzle", "drizzle"),
    55: ("dense drizzle", "drizzle"),
    56: ("light freezing drizzle", "sleet"),
    57: ("dense freezing drizzle", "sleet"),
    61: ("slight rain", "rain"),
    63: ("moderate rain", "rain"),
    65: ("heavy rain", "rain"),
    66: ("light freezing rain", "sleet"),
    67: ("heavy freezing rain", "sleet"),
    71: ("slight snowfall", "snow"),
    73: ("moderate snowfall", "snow"),
    75: ("heavy snowfall", "snow"),
    77: ("snow grains", "snow"),
    80: ("slight rain showers", "showers"),
    81: ("moderate rain showers", "showers"),
    82: ("violent rain showers", "showers"),
    85: ("slight snow showers", "snow"),
    86: ("heavy snow showers", "snow"),
    95: ("thunderstorm", "thunder"),
    96: ("thunderstorm with slight hail", "thunder"),
    99: ("thunderstorm with heavy hail", "thunder"),
}


def _describe(code: Optional[int]) -> Tuple[str, str]:
    """Return (description, icon_key) for a WMO code, with a safe fallback."""
    try:
        return WMO_CODES[int(code)]
    except (KeyError, TypeError, ValueError):
        return ("unknown", "cloudy")


# --------------------------------------------------------------------------
# Inline SVG icon set (no external requests, no CDN, theme-independent)
# --------------------------------------------------------------------------
_SUN_FULL = (
    '<circle cx="12" cy="12" r="4.6" fill="#fbbf24"/>'
    '<g stroke="#fbbf24" stroke-width="1.7" stroke-linecap="round">'
    '<path d="M12 1.8v2.3"/><path d="M12 19.9v2.3"/>'
    '<path d="M22.2 12h-2.3"/><path d="M4.1 12H1.8"/>'
    '<path d="M19.2 4.8l-1.6 1.6"/><path d="M6.4 17.6l-1.6 1.6"/>'
    '<path d="M19.2 19.2l-1.6-1.6"/><path d="M6.4 6.4L4.8 4.8"/></g>'
)
_MOON_FULL = '<path d="M20.5 14.8A8.6 8.6 0 0 1 9.2 3.5 8.6 8.6 0 1 0 20.5 14.8z" fill="#e2e8f0"/>'
_SUN_SMALL = (
    '<circle cx="8.4" cy="7.4" r="3.3" fill="#fbbf24"/>'
    '<g stroke="#fbbf24" stroke-width="1.5" stroke-linecap="round">'
    '<path d="M8.4 1.6v1.6"/><path d="M2.6 7.4H1"/>'
    '<path d="M4.1 3.1L3 2"/><path d="M4.1 11.7L3 12.8"/></g>'
)
_MOON_SMALL = (
    '<path d="M12.4 8.6A5.2 5.2 0 0 1 5.6 1.8 5.2 5.2 0 1 0 12.4 8.6z" fill="#e2e8f0"/>'
)
_CLOUD_LOW = '<path d="M17.8 19.2H6.4A4.4 4.4 0 0 1 6 10.4 6.3 6.3 0 0 1 17.6 9.6 4.8 4.8 0 0 1 17.8 19.2z" fill="#cbd5e1"/>'
_CLOUD_HIGH = '<path d="M17.4 16.2H6.2A4.1 4.1 0 0 1 5.8 8.1 6 6 0 0 1 17.1 7.4 4.5 4.5 0 0 1 17.4 16.2z" fill="#cbd5e1"/>'
_CLOUD_SIDE = '<path d="M18.6 19.4H8.2A4.1 4.1 0 0 1 7.9 11.3 6 6 0 0 1 18.4 10.6 4.4 4.4 0 0 1 18.6 19.4z" fill="#cbd5e1"/>'


def _drops(color: str, n: int, long: bool) -> str:
    length = 3.4 if long else 2.1
    xs = {2: [9.5, 14.0], 3: [7.6, 11.8, 16.0]}[n]
    parts = "".join(f'<path d="M{x} 17.8l-1 {length}"/>' for x in xs)
    return f'<g stroke="{color}" stroke-width="1.8" stroke-linecap="round">{parts}</g>'


_ICONS = {
    "clear": lambda day: _SUN_FULL if day else _MOON_FULL,
    "partly": lambda day: (_SUN_SMALL if day else _MOON_SMALL) + _CLOUD_SIDE,
    "cloudy": lambda day: _CLOUD_LOW,
    "fog": lambda day: _CLOUD_HIGH
    + '<g stroke="#94a3b8" stroke-width="1.7" stroke-linecap="round">'
    '<path d="M4.8 19.2h14.4"/><path d="M7.2 22.2h10.6"/></g>',
    "drizzle": lambda day: _CLOUD_HIGH + _drops("#60a5fa", 3, False),
    "rain": lambda day: _CLOUD_HIGH + _drops("#3b82f6", 3, True),
    "showers": lambda day: _CLOUD_HIGH + _drops("#60a5fa", 2, True),
    "sleet": lambda day: _CLOUD_HIGH
    + _drops("#60a5fa", 2, False)
    + '<circle cx="16.4" cy="20.4" r="1.2" fill="#e0f2fe"/>',
    "snow": lambda day: _CLOUD_HIGH
    + '<g fill="#e0f2fe"><circle cx="8" cy="19.6" r="1.3"/>'
    '<circle cx="12" cy="21.8" r="1.3"/><circle cx="16" cy="19.6" r="1.3"/></g>',
    "thunder": lambda day: _CLOUD_HIGH
    + '<path d="M13.4 16.6l-4.2 5.3h3L11 24.2l4.6-5.6h-3.1l1.9-2z" fill="#fbbf24"/>',
}


def _icon_svg(code: Optional[int], is_day: bool, size: int) -> str:
    """Build an inline SVG icon for a WMO weather code."""
    _, key = _describe(code)
    body = _ICONS.get(key, _ICONS["cloudy"])(is_day)
    return (
        f'<svg viewBox="0 0 24 25" width="{size}" height="{size}" '
        f'style="display:block;flex-shrink:0;" aria-hidden="true">{body}</svg>'
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
_DIRECTIONS = [
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
]


def _wind_dir(deg: Optional[float]) -> str:
    if deg is None:
        return "--"
    return _DIRECTIONS[round(float(deg) / 22.5) % 16]


def _num(value: Any, default: float = 0.0) -> float:
    """Open-Meteo returns null for unavailable values; coerce them safely."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _at(series: Optional[List[Any]], index: int, default: Any = None) -> Any:
    if not series or index < 0 or index >= len(series):
        return default
    return series[index]


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _hhmm(iso: Optional[str]) -> str:
    """Open-Meteo local ISO timestamps look like '2026-08-05T06:45'."""
    if not iso or len(iso) < 16:
        return "--:--"
    return iso[11:16]


# --------------------------------------------------------------------------
# Widget CSS / JS kept out of the f-string so no brace escaping is needed
# --------------------------------------------------------------------------
_WIDGET_CSS = """
<style>
#weather___WID__ .hourly-strip::-webkit-scrollbar { height: 4px; }
#weather___WID__ .hourly-strip::-webkit-scrollbar-track { background: rgba(255,255,255,0.05); border-radius: 2px; }
#weather___WID__ .hourly-strip::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 2px; }
#weather___WID__ .hourly-strip::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.3); }
</style>
"""

_WIDGET_JS = """
<script>
(function() {
    var wid = '__WID__';
    var tabTemp = document.getElementById('tabTemp_' + wid);
    var tabPrecip = document.getElementById('tabPrecip_' + wid);
    var tabWind = document.getElementById('tabWind_' + wid);
    if (tabTemp) {
        var tabs = [
            { btn: tabTemp,   panel: document.getElementById('hourlyTemp_' + wid) },
            { btn: tabPrecip, panel: document.getElementById('hourlyPrecip_' + wid) },
            { btn: tabWind,   panel: document.getElementById('hourlyWind_' + wid) }
        ];
        tabs.forEach(function(tab) {
            if (!tab.btn || !tab.panel) return;
            tab.btn.addEventListener('click', function() {
                tabs.forEach(function(t) {
                    t.btn.style.background = 'rgba(255,255,255,0.04)';
                    t.btn.style.borderColor = 'rgba(255,255,255,0.08)';
                    t.btn.style.color = '#888';
                    t.panel.style.display = 'none';
                });
                tab.btn.style.background = 'rgba(255,255,255,0.12)';
                tab.btn.style.borderColor = 'rgba(255,255,255,0.15)';
                tab.btn.style.color = '#fff';
                tab.panel.style.display = 'flex';
            });
        });
    }

    var container = document.getElementById('weather_' + wid);
    var grid = container ? container.querySelector('.daily-grid-' + wid) : null;
    var list = container ? container.querySelector('.daily-list-' + wid) : null;
    if (grid && list && typeof ResizeObserver !== 'undefined') {
        var update = function() {
            if (container.offsetWidth < 420) {
                grid.style.display = 'none';
                list.style.display = 'block';
            } else {
                grid.style.display = 'grid';
                list.style.display = 'none';
            }
        };
        update();
        new ResizeObserver(update).observe(container);
    }

    // Report our height so Open WebUI can size the sandboxed iframe.
    // Without this the iframe stays at its small default and clips the card.
    function reportHeight() {
        var h = Math.ceil(document.documentElement.scrollHeight);
        parent.postMessage({ type: 'iframe:height', height: h }, '*');
    }
    window.addEventListener('load', reportHeight);
    if (document.readyState === 'complete') reportHeight();
    if (typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(reportHeight).observe(document.body);
    }
    // Belt and braces: fonts/emoji can settle after load and change height.
    setTimeout(reportHeight, 150);
    setTimeout(reportHeight, 600);
})();
</script>
"""


# --------------------------------------------------------------------------
# Widget rendering
# --------------------------------------------------------------------------
def _generate_weather_embed(
    current: Dict[str, Any],
    hourly_items: List[Dict[str, Any]],
    daily_items: List[Dict[str, Any]],
    location_name: str,
    country: str,
    temp_unit: str,
    wind_unit: str,
) -> str:
    """Render the weather card. Degrades gracefully if <script> is stripped."""

    widget_id = uuid.uuid4().hex[:8]

    cur_temp = round(_num(current.get("temp")))
    cur_feels = round(_num(current.get("feels_like")))
    cur_humidity = round(_num(current.get("humidity")))
    cur_pressure = round(_num(current.get("pressure")))
    cur_wind = round(_num(current.get("wind_speed")), 1)
    cur_gust = round(_num(current.get("wind_gust")), 1)
    cur_code = current.get("code")
    cur_is_day = bool(current.get("is_day", True))
    cur_desc = _describe(cur_code)[0].title()
    wind_dir_str = _wind_dir(current.get("wind_deg"))

    uvi = current.get("uvi")
    uvi_str = "N/A" if uvi is None else str(round(_num(uvi), 1))
    vis = current.get("visibility")
    vis_str = "N/A" if vis is None else str(round(_num(vis) / 1000, 1))

    sunrise_str = _hhmm(current.get("sunrise"))
    sunset_str = _hhmm(current.get("sunset"))

    iso_now = current.get("time") or ""
    try:
        now_dt = datetime.fromisoformat(iso_now)
        date_str = now_dt.strftime("%A, %B %d")
        time_str = now_dt.strftime("%H:%M")
    except ValueError:
        date_str, time_str = "--", "--:--"

    # ---- hourly strips -------------------------------------------------
    hourly_temp_html = ""
    hourly_precip_html = ""
    hourly_wind_html = ""
    cell = (
        "display:flex;flex-direction:column;align-items:center;min-width:60px;"
        "flex:1;gap:4px;padding:6px 2px;"
    )
    for h in hourly_items:
        icon = _icon_svg(h["code"], h["is_day"], 30)
        hourly_temp_html += f"""
        <div style="{cell}">
            <span style="font-size:10px;color:#888;font-weight:500;">{_esc(h["time"])}</span>
            {icon}
            <span style="font-size:11px;color:#999;">{h["pop"]}%</span>
            <span style="font-size:14px;color:#f0f0f0;font-weight:600;">{h["temp"]}{temp_unit}</span>
        </div>"""

        hourly_precip_html += f"""
        <div style="{cell}">
            <span style="font-size:10px;color:#888;font-weight:500;">{_esc(h["time"])}</span>
            {icon}
            <span style="font-size:14px;color:#6cb4ee;font-weight:600;">{h["pop"]}%</span>
            <span style="font-size:11px;color:#999;">{h["temp"]}{temp_unit}</span>
        </div>"""

        hourly_wind_html += f"""
        <div style="{cell}">
            <span style="font-size:10px;color:#888;font-weight:500;">{_esc(h["time"])}</span>
            <svg viewBox="0 0 24 24" style="width:20px;height:20px;fill:#aaa;transform:rotate({h["wind_deg"]}deg);"><path d="M12 2L4.5 20.3l.7.7L12 18l6.8 3 .7-.7z"/></svg>
            <span style="font-size:14px;color:#f0f0f0;font-weight:600;">{h["wind"]}</span>
            <span style="font-size:10px;color:#888;">{wind_unit}</span>
        </div>"""

    # ---- daily: card grid (wide) + list (narrow) -----------------------
    daily_cards_html = ""
    daily_list_html = ""
    for d in daily_items:
        icon = _icon_svg(d["code"], True, 38)
        daily_cards_html += f"""
        <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:12px;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0;">
            <div style="font-size:13px;color:#f0f0f0;font-weight:600;">{_esc(d["day"])}</div>
            <div style="font-size:10px;color:#666;">{_esc(d["date"])}</div>
            {icon}
            <div style="font-size:11px;color:#999;text-transform:capitalize;text-align:center;line-height:1.3;">{_esc(d["desc"])}</div>
            <div style="margin-top:auto;padding-top:4px;">
                <span style="font-size:16px;color:#f0f0f0;font-weight:600;">{d["high"]}°</span>
                <span style="font-size:12px;color:#666;margin-left:2px;">{d["low"]}°</span>
            </div>
            <div style="display:flex;gap:6px;">
                <span style="font-size:10px;color:#6cb4ee;">💧{d["pop"]}%</span>
                <span style="font-size:10px;color:#888;">🌬{d["wind"]}</span>
            </div>
        </div>"""

        daily_list_html += f"""
        <div style="display:flex;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04);gap:8px;">
            <div style="min-width:50px;">
                <div style="font-size:13px;color:#f0f0f0;font-weight:600;">{_esc(d["day"])}</div>
                <div style="font-size:10px;color:#666;">{_esc(d["date"])}</div>
            </div>
            {_icon_svg(d["code"], True, 30)}
            <div style="flex:1;font-size:11px;color:#999;text-transform:capitalize;">{_esc(d["desc"])}</div>
            <div style="display:flex;align-items:center;gap:6px;">
                <span style="font-size:10px;color:#6cb4ee;">💧{d["pop"]}%</span>
                <span style="font-size:10px;color:#888;">🌬{d["wind"]}</span>
            </div>
            <div style="min-width:65px;text-align:right;">
                <span style="font-size:14px;color:#f0f0f0;font-weight:600;">{d["high"]}°</span>
                <span style="font-size:12px;color:#666;margin-left:4px;">{d["low"]}°</span>
            </div>
        </div>"""

    tile = (
        "background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);"
        "border-radius:8px;padding:10px;text-align:center;"
    )
    label = (
        "font-size:8px;color:#555;text-transform:uppercase;letter-spacing:0.5px;"
        "margin-bottom:4px;font-weight:700;"
    )
    value = "font-size:16px;color:#f0f0f0;font-weight:500;"

    hourly_section = ""
    if hourly_items:
        hourly_section = f"""
        <div style="margin-bottom:16px;">
            <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
                <button id="tabTemp_{widget_id}" style="background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.15);color:#fff;padding:6px 14px;border-radius:20px;font-size:11px;cursor:pointer;transition:all 0.2s;font-family:inherit;">Temperature</button>
                <button id="tabPrecip_{widget_id}" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#888;padding:6px 14px;border-radius:20px;font-size:11px;cursor:pointer;transition:all 0.2s;font-family:inherit;">Precipitation</button>
                <button id="tabWind_{widget_id}" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#888;padding:6px 14px;border-radius:20px;font-size:11px;cursor:pointer;transition:all 0.2s;font-family:inherit;">Wind</button>
            </div>
            <div id="hourlyTemp_{widget_id}" class="hourly-strip" style="display:flex;gap:4px;overflow-x:auto;padding-bottom:6px;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,0.2) rgba(255,255,255,0.05);">
                {hourly_temp_html}
            </div>
            <div id="hourlyPrecip_{widget_id}" class="hourly-strip" style="display:none;gap:4px;overflow-x:auto;padding-bottom:6px;scrollbar-width:thin;">
                {hourly_precip_html}
            </div>
            <div id="hourlyWind_{widget_id}" class="hourly-strip" style="display:none;gap:4px;overflow-x:auto;padding-bottom:6px;scrollbar-width:thin;">
                {hourly_wind_html}
            </div>
        </div>"""

    daily_section = ""
    if daily_items:
        daily_section = f"""
        <div style="border-top:1px solid rgba(255,255,255,0.05);padding-top:12px;">
            <div style="font-size:8px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">Forecast</div>
            <div class="daily-grid-{widget_id}" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;">
                {daily_cards_html}
            </div>
            <div class="daily-list-{widget_id}" style="display:none;">
                {daily_list_html}
            </div>
        </div>"""

    gust_line = ""
    if cur_gust:
        gust_line = f'<div style="font-size:9px;color:#666;margin-top:2px;">{wind_dir_str} · gust {cur_gust}</div>'
    else:
        gust_line = f'<div style="font-size:9px;color:#666;margin-top:2px;">{wind_dir_str}</div>'

    # Coordinate lookups have no country name; don't leave a dangling separator.
    subtitle = f"{_esc(country)} · {_esc(date_str)}" if country else _esc(date_str)

    css = _WIDGET_CSS.replace("__WID__", widget_id)
    js = _WIDGET_JS.replace("__WID__", widget_id)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  html, body {{ margin:0; padding:0; background:transparent; }}
  * {{ box-sizing:border-box; }}
</style>
{css}
</head>
<body>
    <div style="display:flex;justify-content:center;width:100%;">
      <div id="weather_{widget_id}" style="background:rgba(20,20,25,0.4);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.08);border-radius:12px;padding:20px 24px;box-shadow:0 8px 32px rgba(0,0,0,0.3);max-width:800px;width:100%;font-family:system-ui,-apple-system,sans-serif;">

        <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px;gap:12px;">
            <div>
                <div style="font-size:18px;font-weight:600;color:#f0f0f0;letter-spacing:-0.2px;">{_esc(location_name)}</div>
                <div style="font-size:10px;color:#888;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">{subtitle}</div>
            </div>
            <div style="font-size:20px;color:#aaa;font-weight:300;text-align:right;">{_esc(time_str)}</div>
        </div>

        <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;">
            {_icon_svg(cur_code, cur_is_day, 68)}
            <div>
                <div style="font-size:42px;font-weight:300;color:#f0f0f0;line-height:1;letter-spacing:-2px;">{cur_temp}<span style="font-size:20px;color:#888;font-weight:400;">{temp_unit}</span></div>
                <div style="font-size:14px;color:#ccc;margin-top:2px;">{_esc(cur_desc)}</div>
            </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:20px;">
            <div style="{tile}">
                <div style="{label}">Feels Like</div>
                <div style="{value}">{cur_feels}{temp_unit}</div>
            </div>
            <div style="{tile}">
                <div style="{label}">Humidity</div>
                <div style="{value}">{cur_humidity}%</div>
            </div>
            <div style="{tile}">
                <div style="{label}">Wind</div>
                <div style="{value}">{cur_wind} <span style="font-size:10px;color:#888;">{wind_unit}</span></div>
                {gust_line}
            </div>
            <div style="{tile}">
                <div style="{label}">Pressure</div>
                <div style="{value}">{cur_pressure} <span style="font-size:10px;color:#888;">hPa</span></div>
            </div>
            <div style="{tile}">
                <div style="{label}">UV Index</div>
                <div style="{value}">{uvi_str}</div>
            </div>
            <div style="{tile}">
                <div style="{label}">Visibility</div>
                <div style="{value}">{vis_str} <span style="font-size:10px;color:#888;">km</span></div>
            </div>
        </div>

        <div style="display:flex;gap:12px;margin-bottom:20px;">
            <div style="flex:1;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:8px;padding:10px;display:flex;align-items:center;gap:8px;">
                <svg viewBox="0 0 24 24" style="width:18px;height:18px;fill:#f59e0b;flex-shrink:0;"><path d="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0-3a1 1 0 0 0 1-1V1a1 1 0 0 0-2 0v2a1 1 0 0 0 1 1zm0 18a1 1 0 0 0-1 1v2a1 1 0 0 0 2 0v-2a1 1 0 0 0-1-1zM5.64 5.64a1 1 0 0 0 0-1.41l-1.42-1.42a1 1 0 1 0-1.41 1.42l1.42 1.41a1 1 0 0 0 1.41 0zM19.78 18.36a1 1 0 1 0-1.41 1.42l1.42 1.41a1 1 0 0 0 1.41-1.41l-1.42-1.42zM4 12a1 1 0 0 0-1-1H1a1 1 0 0 0 0 2h2a1 1 0 0 0 1-1zm19-1h-2a1 1 0 0 0 0 2h2a1 1 0 0 0 0-2zM5.64 18.36l-1.42 1.42a1 1 0 0 0 1.41 1.41l1.42-1.41a1 1 0 0 0-1.41-1.42zM19.78 5.64a1 1 0 0 0 .7-.29l1.42-1.42a1 1 0 1 0-1.41-1.41l-1.42 1.41a1 1 0 0 0 .71 1.71z"/></svg>
                <div>
                    <div style="{label}">Sunrise</div>
                    <div style="font-size:14px;color:#f0f0f0;font-weight:500;">{sunrise_str}</div>
                </div>
            </div>
            <div style="flex:1;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:8px;padding:10px;display:flex;align-items:center;gap:8px;">
                <svg viewBox="0 0 24 24" style="width:18px;height:18px;fill:#8b5cf6;flex-shrink:0;"><path d="M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm0 1a1 1 0 0 0-1 1v2a1 1 0 0 0 2 0v-2a1 1 0 0 0-1-1zm7.78-.64l-1.42-1.42a1 1 0 1 0-1.41 1.42l1.42 1.41a1 1 0 0 0 1.41-1.41zM20 12a1 1 0 0 0 1 1h2a1 1 0 0 0 0-2h-2a1 1 0 0 0-1 1zM5.64 5.64a1 1 0 0 0 0-1.41L4.22 2.81a1 1 0 1 0-1.41 1.42l1.42 1.41a1 1 0 0 0 1.41 0zM4 12a1 1 0 0 0-1-1H1a1 1 0 0 0 0 2h2a1 1 0 0 0 1-1zm1.64 6.36l-1.42 1.42a1 1 0 0 0 1.41 1.41l1.42-1.41a1 1 0 0 0-1.41-1.42zM12 7a1 1 0 0 0 1-1V4a1 1 0 0 0-2 0v2a1 1 0 0 0 1 1zm7.78-4.19l-1.42 1.41a1 1 0 1 0 1.41 1.42l1.42-1.42a1 1 0 0 0-1.41-1.41z"/></svg>
                <div>
                    <div style="{label}">Sunset</div>
                    <div style="font-size:14px;color:#f0f0f0;font-weight:500;">{sunset_str}</div>
                </div>
            </div>
        </div>

        {hourly_section}
        {daily_section}

        <div style="margin-top:12px;text-align:center;">
            <span style="font-size:9px;color:#444;">Weather data by Open-Meteo.com (CC BY 4.0)</span>
        </div>
      </div>
    </div>
{js}
</body>
</html>
"""


# --------------------------------------------------------------------------
# Open-Meteo API access
# --------------------------------------------------------------------------
_COORD_RE = re.compile(
    r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,;/ ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$"
)


def _parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """Accept '-33.83, 151.07' so callers can skip the place-name lookup."""
    match = _COORD_RE.match(text or "")
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return lat, lon
    return None


def _split_location(location: str) -> Tuple[List[str], Optional[str]]:
    """'Carlton, Melbourne, AU' -> (['Carlton', 'Melbourne'], 'AU').

    Open-Meteo's geocoder matches a single place name, not a comma-separated
    hierarchy, so each segment becomes its own search candidate. Segments stay
    in the given order, which is most specific first.
    """
    parts = [p.strip() for p in location.split(",") if p.strip()]
    country = None
    if len(parts) > 1 and len(parts[-1]) == 2 and parts[-1].isalpha():
        country = parts.pop().upper()
    return parts, country


_ADMIN_FIELDS = ("admin1", "admin2", "admin3", "admin4")


def _pick_hit(
    results: List[Dict[str, Any]],
    country: Optional[str],
    context: Tuple[str, ...] = (),
) -> Optional[Dict[str, Any]]:
    """Best match for a name, given the rest of the location chain.

    Ranking, in order:
      1. An administrative area matching another segment of the chain. This is
         what separates Carlton in Melbourne (admin2 'Melbourne') from Carlton
         in Tasmania when the caller said 'Carlton, Melbourne, AU'.
      2. Populated places over landforms. Feature codes starting with PPL are
         settlements and a suburb is PPLX; without this a suburb can lose to a
         same-named park, dam or cape.
      3. Population, so a bare city name lands on the big one.
    """
    if country:
        results = [r for r in results if r.get("country_code") == country]
    if not results:
        return None

    wanted = {c.casefold() for c in context}

    def rank(r: Dict[str, Any]) -> Tuple[int, int, int]:
        areas = {
            str(r[f]).casefold() for f in _ADMIN_FIELDS if r.get(f)
        }
        in_context = 0 if (wanted and areas & wanted) else 1
        is_settlement = 0 if (r.get("feature_code") or "").startswith("PPL") else 1
        return (in_context, is_settlement, -int(r.get("population") or 0))

    return sorted(results, key=rank)[0]


async def _search_name(
    session: aiohttp.ClientSession, name: str, language: str
) -> List[Dict[str, Any]]:
    # Suburbs rank below same-named towns worldwide, so ask for a wide result
    # set and do the choosing here. 100 is the API maximum.
    params = {"name": name, "count": 100, "language": language, "format": "json"}
    async with session.get(GEOCODE_URL, params=params) as resp:
        if resp.status != 200:
            raise LookupError(f"geocoding service returned HTTP {resp.status}")
        data = await resp.json()
    return data.get("results") or []


async def _geocode(
    session: aiohttp.ClientSession, location: str, language: str
) -> Dict[str, Any]:
    """Resolve a place name to coordinates.

    Accepts 'Sydney', 'Sydney, AU' or a suburb-first chain such as
    'Carlton, Melbourne, AU'. Candidates are tried most specific first,
    so a suburb the geocoder knows wins over the city it sits in, and an
    unknown suburb quietly falls back to the city instead of failing.
    """
    candidates, country = _split_location(location)
    if not candidates:
        raise LookupError("no location was provided")

    # Cache responses so the country-agnostic retry costs no extra requests.
    seen: Dict[str, List[Dict[str, Any]]] = {}
    hit = None
    for i, name in enumerate(candidates):
        # The other segments say which of several same-named places is meant.
        context = tuple(c for j, c in enumerate(candidates) if j != i)
        seen[name] = await _search_name(session, name, language)
        hit = _pick_hit(seen[name], country, context)
        if hit:
            break

    if hit is None and country:
        # The country code may simply be wrong; a named place still beats an error.
        for i, name in enumerate(candidates):
            context = tuple(c for j, c in enumerate(candidates) if j != i)
            hit = _pick_hit(seen.get(name, []), None, context)
            if hit:
                break

    if hit is None:
        raise LookupError(
            f"could not find a location named '{location}'. Try a more specific name."
        )

    display = hit.get("name", candidates[0])
    if hit.get("admin1"):
        display = f"{display}, {hit['admin1']}"

    return {
        "latitude": hit.get("latitude"),
        "longitude": hit.get("longitude"),
        "name": display,
        "country": hit.get("country", ""),
    }


async def _fetch_forecast(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    temperature_unit: str,
    wind_speed_unit: str,
    forecast_days: int,
) -> Dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "forecast_days": forecast_days,
        "temperature_unit": temperature_unit,
        "wind_speed_unit": wind_speed_unit,
        "current": ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "is_day",
                "weather_code",
                "cloud_cover",
                "surface_pressure",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
            ]
        ),
        "hourly": ",".join(
            [
                "temperature_2m",
                "weather_code",
                "precipitation_probability",
                "wind_speed_10m",
                "wind_direction_10m",
                "uv_index",
                "visibility",
                "is_day",
            ]
        ),
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "sunrise",
                "sunset",
            ]
        ),
    }

    async with session.get(FORECAST_URL, params=params) as resp:
        if resp.status != 200:
            raise LookupError(f"forecast service returned HTTP {resp.status}")
        return await resp.json()


def _current_hour_index(hourly_times: List[str], current_time: str) -> int:
    """Index of the hourly bucket covering 'now' (ISO strings sort correctly)."""
    if not hourly_times:
        return 0
    prefix = (current_time or "")[:13]
    for i, t in enumerate(hourly_times):
        if t[:13] >= prefix:
            return i
    return max(len(hourly_times) - 1, 0)


def _shape_data(
    raw: Dict[str, Any], place: Dict[str, Any], hourly_count: int
) -> Dict[str, Any]:
    """Turn the Open-Meteo response into the shape the widget expects."""
    cur = raw.get("current") or {}
    hourly = raw.get("hourly") or {}
    daily = raw.get("daily") or {}

    times = hourly.get("time") or []
    idx = _current_hour_index(times, cur.get("time", ""))

    current = {
        "time": cur.get("time"),
        "temp": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "pressure": cur.get("surface_pressure"),
        "wind_speed": cur.get("wind_speed_10m"),
        "wind_deg": cur.get("wind_direction_10m"),
        "wind_gust": cur.get("wind_gusts_10m"),
        "clouds": cur.get("cloud_cover"),
        "code": cur.get("weather_code"),
        "is_day": bool(_num(cur.get("is_day"), 1)),
        "uvi": _at(hourly.get("uv_index"), idx),
        "visibility": _at(hourly.get("visibility"), idx),
        "sunrise": _at(daily.get("sunrise"), 0),
        "sunset": _at(daily.get("sunset"), 0),
    }

    hourly_items = []
    for i in range(idx, min(idx + hourly_count, len(times))):
        hourly_items.append(
            {
                "time": _hhmm(times[i]),
                "temp": round(_num(_at(hourly.get("temperature_2m"), i))),
                "code": _at(hourly.get("weather_code"), i),
                "is_day": bool(_num(_at(hourly.get("is_day"), i), 1)),
                "pop": round(_num(_at(hourly.get("precipitation_probability"), i))),
                "wind": round(_num(_at(hourly.get("wind_speed_10m"), i)), 1),
                "wind_deg": round(_num(_at(hourly.get("wind_direction_10m"), i))),
            }
        )

    daily_items = []
    daily_times = daily.get("time") or []
    for i in range(1, len(daily_times)):  # index 0 is today
        code = _at(daily.get("weather_code"), i)
        try:
            d_dt = datetime.fromisoformat(daily_times[i])
            day_label, date_label = d_dt.strftime("%a"), d_dt.strftime("%d/%m")
        except ValueError:
            day_label, date_label = daily_times[i], ""
        daily_items.append(
            {
                "iso": daily_times[i],
                "day": day_label,
                "date": date_label,
                "high": round(_num(_at(daily.get("temperature_2m_max"), i))),
                "low": round(_num(_at(daily.get("temperature_2m_min"), i))),
                "code": code,
                "desc": _describe(code)[0],
                "pop": round(_num(_at(daily.get("precipitation_probability_max"), i))),
                "wind": round(_num(_at(daily.get("wind_speed_10m_max"), i))),
            }
        )

    # Today's row (index 0) is not shown in the widget, but the text summary
    # needs it so the model never mislabels tomorrow's forecast as "today".
    today_item = None
    if daily_times:
        code0 = _at(daily.get("weather_code"), 0)
        try:
            day0_label = datetime.fromisoformat(daily_times[0]).strftime("%a")
        except ValueError:
            day0_label = daily_times[0]
        today_item = {
            "iso": daily_times[0],
            "day": day0_label,
            "high": round(_num(_at(daily.get("temperature_2m_max"), 0))),
            "low": round(_num(_at(daily.get("temperature_2m_min"), 0))),
            "desc": _describe(code0)[0],
            "pop": round(_num(_at(daily.get("precipitation_probability_max"), 0))),
        }

    return {
        "location_name": place["name"],
        "country": place["country"],
        "current": current,
        "hourly_items": hourly_items,
        "daily_items": daily_items,
        "today_item": today_item,
    }


# --------------------------------------------------------------------------
# Open WebUI tool
# --------------------------------------------------------------------------
class Tools:
    class Valves(BaseModel):
        units: str = Field(
            default="metric_kmh",
            description=(
                "Units: 'metric' (°C, m/s), 'metric_kmh' (°C, km/h), "
                "or 'imperial' (°F, mph)."
            ),
        )
        forecast_days: int = Field(
            default=6,
            description="Total days to request (1-16). Today plus N-1 forecast days.",
        )
        hourly_hours: int = Field(
            default=8, description="Number of hourly entries to display (1-24)."
        )
        language: str = Field(
            default="en",
            description="Language for place-name lookup (e.g. 'en', 'zh', 'de', 'fr').",
        )
        show_weather_embed: bool = Field(
            default=True,
            description="Show the weather widget. If false, only text is returned.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _unit_config(self) -> Tuple[str, str, str, str]:
        """Return (api_temp_unit, api_wind_unit, display_temp, display_wind)."""
        mapping = {
            "metric": ("celsius", "ms", "°C", "m/s"),
            "metric_kmh": ("celsius", "kmh", "°C", "km/h"),
            "imperial": ("fahrenheit", "mph", "°F", "mph"),
        }
        return mapping.get(self.valves.units, mapping["metric_kmh"])

    async def get_weather_forecast(
        self,
        location: str,
        location_label: str = "",
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Union[str, Tuple[HTMLResponse, str]]:
        """
        Get the current weather and multi-day forecast for a location.

        Fetches current conditions, an hourly forecast and a daily forecast from
        Open-Meteo, displays an interactive weather widget, and returns a text
        summary. No API key is required.

        :param location: Either a place name or "latitude,longitude".
            Place names may include a suburb and a 2-letter country code, most
            specific first: "Melbourne", "Tokyo, JP", "Carlton, Melbourne, AU".
            When a location tool has given you coordinates, pass those instead
            ("-37.80,144.97") - they are exact and need no lookup.
        :param location_label: Optional place name to show on the widget. Use it
            with coordinates so the card names the suburb the user is in, e.g.
            "Carlton, Melbourne".
        """
        if not location or not location.strip():
            return "Error: no location was provided."

        api_temp, api_wind, temp_unit, wind_unit = self._unit_config()
        days = max(2, min(int(self.valves.forecast_days), 16))
        hours = max(1, min(int(self.valves.hourly_hours), 24))

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Fetching weather for {location}...",
                        "done": False,
                    },
                }
            )

        coords = _parse_coordinates(location)
        label = (location_label or "").strip()

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                if coords:
                    lat, lon = coords
                    place = {
                        "latitude": lat,
                        "longitude": lon,
                        "name": label or f"{lat:.3f}, {lon:.3f}",
                        "country": "",
                    }
                else:
                    place = await _geocode(session, location, self.valves.language)
                    if label:
                        place["name"] = label
                raw = await _fetch_forecast(
                    session,
                    place["latitude"],
                    place["longitude"],
                    api_temp,
                    api_wind,
                    days,
                )
            result = _shape_data(raw, place, hours)
        except LookupError as e:
            # Safe to surface: these messages never contain URLs or credentials.
            message = f"Error: {e}"
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": message, "done": True}}
                )
            return message
        except asyncio.TimeoutError:
            message = "Error: the weather service timed out. Please try again."
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": message, "done": True}}
                )
            return message
        except aiohttp.ClientError:
            # Never echo the exception: aiohttp errors can embed the request URL.
            message = "Error: could not reach the weather service (network error)."
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": message, "done": True}}
                )
            return message
        except Exception:
            message = "Error: unexpected failure while building the forecast."
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": message, "done": True}}
                )
            return message

        current = result["current"]
        cur_desc = _describe(current.get("code"))[0]
        where = result["location_name"]
        if result["country"]:
            where = f"{where}, {result['country']}"
        summary_lines = [
            f"Weather for {where}:",
            f"Currently: {cur_desc}, {round(_num(current.get('temp')))}{temp_unit} "
            f"(feels like {round(_num(current.get('feels_like')))}{temp_unit})",
            f"Humidity: {round(_num(current.get('humidity')))}% | "
            f"Wind: {round(_num(current.get('wind_speed')), 1)} {wind_unit} "
            f"{_wind_dir(current.get('wind_deg'))}",
            "",
            f"Local date: {current.get('time', '')[:10]}",
            "Daily forecast (dates are ISO, local time):",
        ]

        def _day_line(item: Dict[str, Any], relative: str) -> str:
            return (
                f"  - {item['iso']} ({item['day']}, {relative}): {item['desc']}, "
                f"High {item['high']}{temp_unit}, Low {item['low']}{temp_unit}, "
                f"Precip {item['pop']}%"
            )

        if result.get("today_item"):
            summary_lines.append(_day_line(result["today_item"], "today"))
        for n, d in enumerate(result["daily_items"]):
            relative = "tomorrow" if n == 0 else f"in {n + 1} days"
            summary_lines.append(_day_line(d, relative))
        text_summary = "\n".join(summary_lines)

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": "Weather data loaded!", "done": True},
                }
            )

        tool_result_message = (
            "The weather widget has been successfully embedded above. "
            "Use the following data to give the user a natural language summary:\n\n"
            + text_summary
        )

        if self.valves.show_weather_embed:
            embed_html = _generate_weather_embed(
                current,
                result["hourly_items"],
                result["daily_items"],
                result["location_name"],
                result["country"],
                temp_unit,
                wind_unit,
            )
            return (
                HTMLResponse(
                    content=embed_html, headers={"Content-Disposition": "inline"}
                ),
                tool_result_message,
            )

        return tool_result_message
