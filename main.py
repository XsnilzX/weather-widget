#!/usr/bin/env python3
# pip install openmeteo-requests requests-cache retry-requests numpy

import json
from datetime import datetime, timezone
import re
from typing import List, Dict, Any, Optional

from niquests.api import get
import numpy as np
import openmeteo_requests
import requests_cache
from retry_requests import retry

import requests

# --- Mapping ---------------------------------------------------------------

WMO = {
    0: ("☀️", "klar"),
    1: ("🌤️", "überwiegend klar"),
    2: ("⛅", "teilweise bewölkt"),
    3: ("☁️", "bedeckt"),
    45: ("🌫️", "nebel"),
    48: ("🌫️", "reifnebel"),
    51: ("🌦️", "niesel (leicht)"),
    53: ("🌦️", "niesel (mäßig)"),
    55: ("🌧️", "niesel (stark)"),
    56: ("🌧️", "gefrierender niesel (leicht)"),
    57: ("🌧️", "gefrierender niesel (stark)"),
    61: ("🌦️", "regen (leicht)"),
    63: ("🌧️", "regen (mäßig)"),
    65: ("🌧️", "regen (stark)"),
    66: ("🌧️", "gefrierender regen (leicht)"),
    67: ("🌧️", "gefrierender regen (stark)"),
    71: ("🌨️", "schnee (leicht)"),
    73: ("🌨️", "schnee (mäßig)"),
    75: ("❄️", "schnee (stark)"),
    77: ("🌨️", "schneekörner"),
    80: ("🌦️", "schauer (leicht)"),
    81: ("🌧️", "schauer (mäßig)"),
    82: ("🌧️", "schauer (heftig)"),
    85: ("🌨️", "schneeschauer (leicht)"),
    86: ("❄️", "schneeschauer (heftig)"),
    95: ("⛈️", "gewitter"),
    96: ("⛈️", "gewitter mit hagel (leicht)"),
    99: ("⛈️", "gewitter mit hagel (stark)"),
}

def wmo_to_icon_desc(code: int):
    return WMO.get(int(code), ("❓", "unbekannt"))

# --- Tooltip: Stündliche Vorhersage ---------------------------------------

def format_hourly_forecast(hourly: List[Dict[str, Any]], hours: int = 6, tz: str = "Europe/Berlin") -> str:
    """
    Erwartet eine Liste wie:
      hourly = [{"time": datetime, "temp": int, "wmo": int}, ...]
    Gibt einen formatierten Block für den Tooltip zurück (mit führendem \n\n).
    """
    if not hourly:
        return ""

    # Nur die nächsten N Stunden ab jetzt
    now = datetime.now(timezone.utc)
    future = [h for h in hourly if h.get("time") and h["time"].astimezone().replace(minute=0, second=0, microsecond=0) >= now.astimezone().replace(minute=0, second=0, microsecond=0)]
    rows = future[:hours] if future else hourly[:hours]

    lines = ["", "", "Stündlich:"]  # zwei Zeilenumbrüche für saubere Trennung
    for h in rows:
        t_local = h["time"].astimezone().strftime("%H:%M")
        icon, desc = wmo_to_icon_desc(h.get("wmo", -1))
        temp = h.get("temp")
        lines.append(f"{t_local}  {icon}  {temp}°C")
    return "\n".join(lines)

# --- Waybar Builder --------------------------------------------------------

def build_waybar_output(response, location_info: Dict[str, str], weather_data: Dict[str, Any],
                        request_limit_warning: str = "", hourly_formatter=format_hourly_forecast) -> Dict[str, Any]:
    """
    Baut den JSON-Output für Waybar aus openmeteo_requests.Response + vorbereitetem weather_data.
    Erwartet weather_data im "alten" Format, das wir in get_weather() erzeugen.
    """
    # current aus weather_data (stabil gegenüber Änderungen)
    current = weather_data["current"]
    temp = round(current["temp"])
    feels_like = round(current.get("feels_like", temp))
    humidity = current.get("humidity")
    wind_speed = round(current.get("wind_speed", 0))  # bereits km/h
    description = current["weather"][0]["description"]
    icon_code = current["weather"][0]["icon_code"]  # hier: WMO Code als String
    icon = current["weather"][0]["icon"]

    tooltip_text = (
        f"📍 {location_info['city']}, {location_info['region']}, {location_info['country']}"
        f"{request_limit_warning}\n"
        f"<span size='xx-large'>{temp}°C</span>\n"
        f"<big>{icon} {description.capitalize()}</big>\n"
        f"Gefühlte: {feels_like}°C\n"
        f"Feuchtigkeit: {humidity}%\n"
        f"Wind: {wind_speed} km/h"
        f"{hourly_formatter(weather_data.get('hourly', []))}"
    )

    output = {
        "text": f"{icon} {temp}°C",
        "alt": description,
        "tooltip": tooltip_text,
        "class": icon_code  # fürs Styling in Waybar (z.B. .wmo-63)
    }
    return output

# --- Fetch + Normalisierung in dein altes Schema --------------------------

def get_weather(lat: float, lon: float, place_name: Optional[str] = None, tz: str = "Europe/Berlin", region: Optional[str] = None, country: Optional[str] = None):
    """
    Holt Daten über open-meteo und gibt (weather_data, location_info) in deiner alten Struktur zurück.
    - wind_speed bereits in km/h
    - description/icon aus WMO
    - hourly als Liste mit {"time": datetime-tzaware, "temp": int, "wmo": int}
    """
    # Session mit Cache + Retry
    cache_session = requests_cache.CachedSession(".om_cache", expire_after=300)
    retry_session = retry(cache_session, retries=2, backoff_factor=0.3)
    client = openmeteo_requests.Client(session=retry_session)

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "current": ["temperature_2m", "apparent_temperature", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "weather_code"],
        "hourly": ["temperature_2m", "weather_code"],
    }

    # Default location_info
    city = place_name or f"{lat:.2f},{lon:.2f}"
    location_info = {"city": city, "region": region, "country": country}
    request_limit_reached = False

    try:
        responses = client.weather_api(url, params=params)
        response = responses[0]

        # --- Current
        cur = response.Current()
        temp = float(cur.Variables(0).Value())
        feels_like = float(cur.Variables(1).Value())
        humidity = int(round(cur.Variables(2).Value()))
        wind_speed = float(cur.Variables(3).Value())       # km/h (Open-Meteo liefert km/h für wind_speed_10m)
        wind_deg = float(cur.Variables(4).Value())
        wmo_code = int(cur.Variables(5).Value())
        icon, desc = wmo_to_icon_desc(wmo_code)

        current = {
            "temp": round(temp),
            "feels_like": round(feels_like),
            "humidity": humidity,
            "wind_speed": round(wind_speed),
            "wind_deg": round(wind_deg),
            "weather": [{
                "description": desc,
                "icon": icon,
                "icon_code": str(wmo_code)  # für CSS-Klassen/Filter
            }]
        }

        # --- Hourly
        hourly_block = response.Hourly()
        # Zeiten
        start = datetime.fromtimestamp(int(hourly_block.Time()), tz=timezone.utc)
        end = datetime.fromtimestamp(int(hourly_block.TimeEnd()), tz=timezone.utc)
        step = int(hourly_block.Interval())  # Sekunden
        n = int((end - start).total_seconds() // step)

        t2m = np.array(hourly_block.Variables(0).ValuesAsNumpy()).astype(float)
        wmo = np.array(hourly_block.Variables(1).ValuesAsNumpy()).astype(int)

        # Safety: kürze auf gleiche Länge
        n_ok = min(n, t2m.size, wmo.size)
        hours: List[Dict[str, Any]] = []
        for i in range(n_ok):
            t = start + i * (end - start) / max(n, 1)  # robust bei edge cases
            hours.append({
                "time": t,
                "temp": int(round(t2m[i])),
                "wmo": int(wmo[i])
            })

        weather_data = {
            "request_limit_reached": request_limit_reached,
            "current": current,
            "hourly": hours
        }

        return weather_data, location_info, response

    except Exception:
        # Fallback bei Netz-/API-Fehlern
        request_limit_reached = True
        weather_data = {
            "request_limit_reached": request_limit_reached,
            "current": {
                "temp": 0,
                "feels_like": 0,
                "humidity": 0,
                "wind_speed": 0,
                "wind_deg": 0,
                "weather": [{"description": "unbekannt", "icon": "❓", "icon_code": "-1"}]
            },
            "hourly": []
        }
        return weather_data, location_info, None

# --- get_ip_location() -------------------------------------------------------
import requests

def get_ip_location(timeout=4):
    """
    Liefert (lat, lon, city, region, country) basierend auf der öffentlichen IP-Adresse.
    Probiert nacheinander ipapi.co, ipinfo.io und ip-api.com.
    Wirft RuntimeError, wenn alle Provider fehlschlagen.
    """
    providers = [
        (
            "https://ipapi.co/json/",
            lambda d: (
                float(d["latitude"]),
                float(d["longitude"]),
                d.get("city"),
                d.get("region"),
                d.get("country_name") or d.get("country"),
            ),
        ),
        (
            "https://ipinfo.io/json",
            lambda d: (
                float(d["loc"].split(",")[0]),
                float(d["loc"].split(",")[1]),
                d.get("city"),
                d.get("region"),
                d.get("country"),
            ),
        ),
        (
            "http://ip-api.com/json",
            lambda d: (
                float(d["lat"]),
                float(d["lon"]),
                d.get("city"),
                d.get("regionName") or d.get("region"),
                d.get("country"),
            ),
        ),
    ]

    last_err = None
    for url, parser in providers:
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "weather-widget/1.0"})
            r.raise_for_status()
            data = r.json()
            return parser(data)
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"IP-Geolokation fehlgeschlagen: {last_err}")

# --- main() ---------------------------------------------------------------

def main():
    # Beispiel: Berlin
    lat, lon, city, region, country = get_ip_location()
    weather_data, location_info, response = get_weather(lat, lon, place_name=city, region=region, country=country)

    # Hinweis, falls Cache/Fallback genutzt wurde
    request_limit_warning = ""
    if weather_data.get("request_limit_reached"):
        request_limit_warning = "\n⚠️ API request limit reached - showing cached/offline data"

    output = build_waybar_output(
        response=response,
        location_info=location_info,
        weather_data=weather_data,
        request_limit_warning=request_limit_warning,
        hourly_formatter=format_hourly_forecast
    )

    print(json.dumps(output, ensure_ascii=False))
    try:
        lat, lon, city, region, country = get_ip_location()
        print(f"📍 {city}, {region}, {country} → {lat:.6f}, {lon:.6f}")
    except Exception as e:
        print("Fehler:", e)


if __name__ == "__main__":
    main()
