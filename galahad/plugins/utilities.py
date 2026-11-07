"""Weather, time, world clocks, calculator, unit & currency conversion, connectivity."""
from __future__ import annotations

import ast
import math
import operator
import socket
import time
from datetime import datetime, timedelta
from typing import Optional

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log, "log10": math.log10,
          "ln": math.log, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "exp": math.exp,
          "radians": math.radians, "degrees": math.degrees, "factorial": math.factorial}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_eval(expr: str) -> float:
    """Arithmetic only — no names, attributes or calls except a fixed maths set."""
    expr = (expr or "").strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    expr = __import__("re").sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)", r"(\1/100*\2)", expr)
    if len(expr) > 200:
        raise PluginError("Expression too long.")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 1000:
                raise PluginError("Exponent too large.")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise PluginError("I can only do arithmetic and basic maths functions.")
    try:
        return ev(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        raise PluginError("Division by zero.")
    except (SyntaxError, ValueError, OverflowError, TypeError) as e:
        raise PluginError(f"I couldn't evaluate that: {e}")


# unit -> (dimension, factor to base)
_UNITS = {
    "m": ("length", 1), "meter": ("length", 1), "metre": ("length", 1), "km": ("length", 1000), "kilometer": ("length", 1000),
    "kilometre": ("length", 1000), "cm": ("length", .01), "mm": ("length", .001), "mi": ("length", 1609.344),
    "mile": ("length", 1609.344), "ft": ("length", .3048), "foot": ("length", .3048), "feet": ("length", .3048),
    "in": ("length", .0254), "inch": ("length", .0254), "yd": ("length", .9144), "yard": ("length", .9144),
    "kg": ("mass", 1), "kilogram": ("mass", 1), "g": ("mass", .001), "gram": ("mass", .001), "mg": ("mass", 1e-6),
    "lb": ("mass", .45359237), "pound": ("mass", .45359237), "oz": ("mass", .028349523), "ounce": ("mass", .028349523),
    "tonne": ("mass", 1000), "l": ("volume", 1), "liter": ("volume", 1), "litre": ("volume", 1), "ml": ("volume", .001),
    "gal": ("volume", 3.785411784), "gallon": ("volume", 3.785411784), "cup": ("volume", .2365882365),
    "s": ("time", 1), "sec": ("time", 1), "second": ("time", 1), "min": ("time", 60), "minute": ("time", 60),
    "h": ("time", 3600), "hr": ("time", 3600), "hour": ("time", 3600), "day": ("time", 86400), "week": ("time", 604800),
    "b": ("data", 1), "byte": ("data", 1), "kb": ("data", 1024), "mb": ("data", 1024 ** 2), "gb": ("data", 1024 ** 3),
    "tb": ("data", 1024 ** 4), "kmh": ("speed", 1 / 3.6), "km/h": ("speed", 1 / 3.6), "mph": ("speed", .44704),
    "m/s": ("speed", 1), "knot": ("speed", .514444), "sqm": ("area", 1), "sqft": ("area", .09290304),
    "acre": ("area", 4046.8564224), "hectare": ("area", 10000), "ha": ("area", 10000),
}
_TEMPS = {"c": "c", "celsius": "c", "°c": "c", "f": "f", "fahrenheit": "f", "°f": "f", "k": "k", "kelvin": "k"}


def convert_units(value: float, src: str, dst: str) -> float:
    a, b = src.strip().lower().rstrip("s") if src.strip().lower() not in _UNITS and src.strip().lower() not in _TEMPS else src.strip().lower(), \
        dst.strip().lower().rstrip("s") if dst.strip().lower() not in _UNITS and dst.strip().lower() not in _TEMPS else dst.strip().lower()
    if a in _TEMPS and b in _TEMPS:
        a, b = _TEMPS[a], _TEMPS[b]
        c = value if a == "c" else (value - 32) * 5 / 9 if a == "f" else value - 273.15
        return c if b == "c" else c * 9 / 5 + 32 if b == "f" else c + 273.15
    if a not in _UNITS or b not in _UNITS:
        raise PluginError(f"I don't know how to convert '{src}' to '{dst}'.")
    if _UNITS[a][0] != _UNITS[b][0]:
        raise PluginError(f"Can't convert {_UNITS[a][0]} ({src}) to {_UNITS[b][0]} ({dst}).")
    return value * _UNITS[a][1] / _UNITS[b][1]


WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow",
       75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "violent showers", 95: "thunderstorm", 96: "thunderstorm with hail",
       99: "thunderstorm with heavy hail"}

ZONE_ALIASES = {"london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin", "new york": "America/New_York",
                "nyc": "America/New_York", "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
                "chicago": "America/Chicago", "tokyo": "Asia/Tokyo", "delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
                "kolkata": "Asia/Kolkata", "india": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "dubai": "Asia/Dubai",
                "singapore": "Asia/Singapore", "sydney": "Australia/Sydney", "hong kong": "Asia/Hong_Kong",
                "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai", "moscow": "Europe/Moscow", "cairo": "Africa/Cairo",
                "sao paulo": "America/Sao_Paulo", "toronto": "America/Toronto", "utc": "UTC", "gmt": "UTC"}


def resolve_zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    key = (name or "").strip().lower()
    cand = ZONE_ALIASES.get(key) or name.strip().replace(" ", "_")
    try:
        return ZoneInfo(cand), cand
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if key == "utc":
            from datetime import timezone
            return timezone.utc, "UTC"
        raise PluginError(f"I don't know the time zone '{name}'. Try a city or an IANA name like Europe/London.")


class Utilities(Plugin):
    manifest = Manifest(
        id="utilities", name="Utilities", category="Utilities", version="1.0.0",
        description="Weather, date & time, world clocks, calculator, unit and currency conversion, connectivity check.",
        permissions=["network"], optional_python=["tzdata"])

    def health(self):
        return Health("ok", "weather & currency use free public APIs (Open-Meteo, Frankfurter) — no key needed")

    def _geocode(self, city: str) -> dict:
        r = self.ctx.http_get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "format": "json"})
        res = (r.json().get("results") or [])
        if not res:
            raise PluginError(f"I couldn't find a place called '{city}'.")
        return res[0]

    @command(description="Current weather and a 3-day forecast. Uses the configured home city when 'city' is blank.",
             params={"city": {"type": "string"}}, permissions=("network",), readonly=True)
    def weather(self, city=""):
        city = city or self.ctx.setting("location.city", "")
        lat, lon = self.ctx.setting("location.latitude", 0.0), self.ctx.setting("location.longitude", 0.0)
        place = city
        if city:
            g = self._geocode(city)
            lat, lon, place = g["latitude"], g["longitude"], f"{g['name']}, {g.get('country', '')}".strip(", ")
        elif not (lat or lon):
            raise PluginError("Tell me a city, or set your home city in Settings → Location.")
        try:
            r = self.ctx.http_get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
            j = r.json()
            c = j["current"]
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"The weather service didn't respond ({type(e).__name__}).")
        daily = [{"date": d, "max": mx, "min": mn, "rain_chance": rp, "summary": WMO.get(wc, "mixed")}
                 for d, mx, mn, rp, wc in zip(j["daily"]["time"], j["daily"]["temperature_2m_max"], j["daily"]["temperature_2m_min"],
                                              j["daily"]["precipitation_probability_max"], j["daily"]["weather_code"])]
        cur = {"temp": c["temperature_2m"], "feels_like": c["apparent_temperature"], "humidity": c["relative_humidity_2m"],
               "wind_kmh": c["wind_speed_10m"], "summary": WMO.get(c["weather_code"], "mixed")}
        msg = (f"{place or 'Your location'}: {cur['summary']}, {cur['temp']:.0f}°C (feels {cur['feels_like']:.0f}°C), "
               f"humidity {cur['humidity']}%. Today {daily[0]['min']:.0f}–{daily[0]['max']:.0f}°C, rain chance {daily[0]['rain_chance']}%. "
               f"[updated {c.get('time', '')} local, Open-Meteo]")
        return {"message": msg, "data": {"place": place, "current": cur, "daily": daily, "updated": c.get("time")}}

    @command(description="Current date and time, optionally in another city or time zone.", readonly=True,
             params={"zone": {"type": "string", "description": "City or IANA zone, blank = local"}})
    def time_now(self, zone=""):
        if not zone:
            now = datetime.now().astimezone()
            return {"message": now.strftime("It is %A %d %B %Y, %H:%M (%Z)."), "data": {"iso": now.isoformat()}}
        tz, name = resolve_zone(zone)
        now = datetime.now(tz)
        return {"message": f"In {zone.title()} it is {now.strftime('%A %d %B, %H:%M')} ({name}).", "data": {"iso": now.isoformat(), "zone": name}}

    @command(description="Times in several cities at once.", readonly=True,
             params={"zones": {"type": "array", "description": "List of cities / IANA zones"}})
    def world_clocks(self, zones=None):
        zones = zones or ["London", "New York", "Delhi", "Tokyo", "Sydney"]
        out = []
        for z in zones[:12]:
            tz, name = resolve_zone(z)
            n = datetime.now(tz)
            out.append({"zone": z, "name": name, "time": n.strftime("%H:%M"), "date": n.strftime("%a %d %b")})
        return {"message": "; ".join(f"{o['zone']} {o['time']}" for o in out), "data": {"clocks": out}}

    @command(description="Evaluate an arithmetic expression (supports + - * / ** % of, sqrt, sin, log, pi…).", readonly=True,
             params={"expression": {"type": "string", "required": True}})
    def calculate(self, expression):
        v = safe_eval(expression)
        shown = int(v) if isinstance(v, float) and v.is_integer() and abs(v) < 1e15 else round(v, 10) if isinstance(v, float) else v
        return {"message": f"{expression.strip()} = {shown}", "data": {"value": v}}

    @command(description="Convert between units (length, mass, volume, temperature, time, data, speed, area).", readonly=True,
             params={"value": {"type": "number", "required": True}, "from_unit": {"type": "string", "required": True},
                     "to_unit": {"type": "string", "required": True}})
    def convert(self, value, from_unit, to_unit):
        out = convert_units(float(value), from_unit, to_unit)
        return {"message": f"{value:g} {from_unit} = {out:.6g} {to_unit}", "data": {"value": out}}

    @command(description="Convert currency using European Central Bank reference rates (Frankfurter API).", readonly=True,
             permissions=("network",), params={"amount": {"type": "number", "required": True},
                                              "from_currency": {"type": "string", "required": True},
                                              "to_currency": {"type": "string", "required": True}})
    def currency(self, amount, from_currency, to_currency):
        a, b = from_currency.upper().strip(), to_currency.upper().strip()
        if a == b:
            return {"message": f"{amount:g} {a} = {amount:g} {b}", "data": {"value": amount, "rate": 1.0}}
        try:
            r = self.ctx.http_get("https://api.frankfurter.app/latest", params={"amount": amount, "from": a, "to": b})
            if r.status_code != 200:
                raise PluginError(f"Unknown currency code? ({a}/{b}) — the rate service answered {r.status_code}.")
            j = r.json()
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"The currency service didn't respond ({type(e).__name__}).")
        v = j["rates"][b]
        return {"message": f"{amount:g} {a} = {v:,.2f} {b} (ECB rate for {j['date']})",
                "data": {"value": v, "rate": v / amount if amount else None, "date": j["date"]}}

    @command(description="Check internet connectivity and DNS.", readonly=True, permissions=("network",))
    def connectivity(self):
        checks = {}
        for name, host, port in (("dns", "one.one.one.one", 443), ("google_dns", "8.8.8.8", 53)):
            t = time.time()
            try:
                socket.create_connection((host, port), timeout=3).close()
                checks[name] = {"ok": True, "ms": int((time.time() - t) * 1000)}
            except OSError as e:
                checks[name] = {"ok": False, "error": str(e)}
        online = any(c["ok"] for c in checks.values())
        return {"ok": True, "message": "Internet is reachable." if online else "No internet connection detected.",
                "data": {"online": online, "checks": checks}}

    @command(description="Calendar utility: weekday of a date, days between dates, or add days.", readonly=True,
             params={"date": {"type": "string", "description": "YYYY-MM-DD"}, "other_date": {"type": "string"},
                     "add_days": {"type": "integer"}})
    def date_math(self, date="", other_date="", add_days=None):
        try:
            d1 = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
            if other_date:
                d2 = datetime.strptime(other_date, "%Y-%m-%d")
                return f"{abs((d2 - d1).days)} days between {d1:%d %b %Y} and {d2:%d %b %Y}."
            if add_days is not None:
                r = d1 + timedelta(days=int(add_days))
                return f"{d1:%d %b %Y} {'+' if add_days >= 0 else '-'} {abs(add_days)} days = {r:%A %d %B %Y}."
            return f"{d1:%d %B %Y} is a {d1:%A} (week {d1.isocalendar()[1]})."
        except ValueError:
            raise PluginError("Dates must look like 2026-12-31.")


PLUGINS = [Utilities]
