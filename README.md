# openwebui-tools

A small collection of [Open WebUI](https://github.com/open-webui/open-webui) **Tools** — Python
files you paste into Open WebUI's *Workspace → Tools* editor so your models can call them during a
chat. Every tool here is keyless: no API accounts, no secrets to manage.

## What's inside

| Tool | File | Functions | What it does |
| --- | --- | --- | --- |
| **Open-Meteo Weather Forecast** | [`tools/weather_forecast.py`](tools/weather_forecast.py) | `get_weather_forecast(location, location_label)` | Fetches current conditions, an hourly strip and a multi-day forecast from [Open-Meteo](https://open-meteo.com), then renders an interactive HTML weather card in the chat and returns a text summary for the model to narrate. |
| **My Location** | [`tools/my_location.py`](tools/my_location.py) | `get_my_location()`, `clear_location_cache()` | Resolves where the user is, so location-aware tools don't need a city typed every time. Tries browser GPS first, falls back to IP geolocation, then to a manually configured home location. |

### Open-Meteo Weather Forecast

- Accepts `"Melbourne"`, `"Tokyo, JP"`, a suburb-first chain like `"Carlton, Melbourne, AU"`, or
  raw coordinates `"-37.80,144.97"`. Chained names are tried most specific first, so a suburb the
  geocoder knows wins over the city behind it, and an unknown suburb falls back to that city
  instead of erroring. `location_label` overrides the name shown on the card, which is what you
  want when passing coordinates.
- The rest of the chain disambiguates the suburb: `"Carlton, Melbourne, AU"` lands on Carlton in
  Victoria, while `"Carlton, Sydney, AU"` lands on the Carlton in New South Wales — neither gets
  the 996-person Carlton in Tasmania. Settlements also outrank landforms, so a suburb never loses
  to a same-named park, dam or cape.
- Weather icons are **inline SVG**, so the widget makes no external image or CDN requests.
- The card is responsive (grid on wide screens, list on narrow) and reports its height back to
  Open WebUI so the sandboxed iframe sizes itself correctly.
- Degrades gracefully: if the widget's `<script>` is stripped, the card still renders, and the
  model always receives the plain-text summary.
- Attribution: weather data by Open-Meteo.com (CC BY 4.0).

**Valves** (admin settings): `units` (`metric` °C·m/s, `metric_kmh` °C·km/h, `imperial` °F·mph),
`forecast_days` (1–16), `hourly_hours` (1–24), `language` for place-name lookup, and
`show_weather_embed` to turn the widget off and return text only.

### My Location

- Browser GPS runs in the main page context (not the sandboxed iframe), so the browser's own
  permission prompt works. Reverse geocoding is done client-side via BigDataCloud's free
  client-side endpoint — deliberately, because that endpoint expects requests to come from the
  device being located.
- IP fallback uses `ipwho.is`, then `ipapi.co`, both keyless and HTTPS. It reads the client IP
  from proxy headers so it locates the browser rather than your server.
- Coordinates are rounded before they leave the tool (default 2 decimals ≈ 1 km), and resolved
  locations are cached per user (default 30 minutes).
- Reports a suburb-first place name (`Carlton, Melbourne, Victoria, AU`) **and**
  coordinates, and tells the model to prefer the coordinates with the name as a display label —
  so downstream tools stay accurate to the suburb rather than the nearest big city.

**Valves** (admin): `enable_browser_gps`, `enable_ip_fallback`, `coordinate_precision`,
`cache_minutes`, `language`.
**User valves** (per user): `home_location` (e.g. `Melbourne, AU`) and `always_use_home` to skip
detection entirely.

The two tools are designed to work together — ask *"what's the weather like here?"* and the model
calls `get_my_location`, then passes the returned place name to `get_weather_forecast`.

## Prerequisites

- **Open WebUI 0.11.0 or newer** (both tools declare `required_open_webui_version: 0.11.0`).
- A model with **function/tool calling** enabled in Open WebUI (native or default tool mode).
- **Outbound HTTPS** from the Open WebUI server to `open-meteo.com`, `ipwho.is` and `ipapi.co`.
- `aiohttp`, `pydantic` and `fastapi` — all already bundled with Open WebUI, so there is nothing
  to `pip install`.
- For **My Location**'s browser GPS path: Open WebUI must be served over **HTTPS or on
  `localhost`**. Browsers block the Geolocation API in insecure contexts; the tool detects this
  and falls back to IP lookup.

## Quick start

1. **Copy the tool source.** Open the file you want in this repo (or `git clone` it) and copy the
   whole file, including the docstring header at the top — Open WebUI parses `title`,
   `description`, `version` and `required_open_webui_version` from it.

   ```bash
   git clone https://github.com/mercurynomercy/openwebui-tools.git
   cd openwebui-tools
   ```

2. **Create the tool in Open WebUI.** Go to **Workspace → Tools → `+`**, paste the source, give it
   an ID (e.g. `weather_forecast`), and hit **Save**.

3. **Enable it for a model.** **Workspace → Models →** *your model* **→ Tools**, tick the tool.
   (Or toggle it per-chat from the **➕** menu in the message box.)

4. **Adjust the valves** if you want — the gear icon on the tool card sets admin valves; users set
   their own `home_location` under **Settings → Tools**.

5. **Try it.**

   ```text
   What's the weather in Tokyo tomorrow?
   Do I need an umbrella here today?
   ```

   The first location request will trigger a browser permission prompt. If you deny it, the tool
   silently falls back to IP geolocation.

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Weather card is clipped or tiny | Open WebUI older than 0.11.0 — the iframe height message isn't handled. |
| `Browser location needs HTTPS or localhost` | Open WebUI is served over plain HTTP on a non-local host. Use HTTPS, or set a `home_location` user valve. |
| Location is a different city | IP geolocation is city-level and follows your VPN exit node. Run `clear_location_cache` after switching networks, or set `always_use_home`. |
| `could not find a location named …` | Add a country code: `Springfield, US`. |
| Card names the city, not your suburb | Both tools must be on the new versions (weather ≥ 2.1.0, location ≥ 1.1.0); re-paste the source and hit Save. |

## License

[MIT](LICENSE). Weather data from [Open-Meteo](https://open-meteo.com) under CC BY 4.0.
