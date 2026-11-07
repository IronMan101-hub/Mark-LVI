"""Small, dependency-free natural-language date/time parser.

Understands: "in 10 minutes", "in 2 hours", "tomorrow 5pm", "today at 18:30",
"at 7pm", "next monday 9am", "friday", "on 15 march [2027] 10:00",
"2026-12-01 14:30", "tonight", "this evening", "end of day", "in 3 days".
Returns None if nothing recognisable is found — callers must handle that and ask.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
         "week": 604800}
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
              "half": 0.5}


def _time_of_day(text: str) -> Optional[tuple[int, int]]:
    m = re.search(r"\b(?:at\s+)?(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", text)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = re.search(r"\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b", text)
        if not m:
            m = re.search(r"\bat\s+(\d{1,2})\b(?!\s*(?:st|nd|rd|th|%|\d))", text)
            if not m:
                if "noon" in text:
                    return 12, 0
                if "midnight" in text:
                    return 0, 0
                return None
            h, mi, ap = int(m.group(1)), 0, None
        else:
            h, mi, ap = int(m.group(1)), 0, m.group(2)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_when(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    t = (text or "").lower().strip()
    if not t:
        return None

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[ t]+(\d{1,2}):(\d{2}))?", t)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 9), int(m[5] or 0))
        except ValueError:
            return None

    m = re.search(r"\bin\s+(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|half)\s*"
                  r"(?:and a half\s+)?(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b", t)
    if m:
        n = _NUM_WORDS.get(m[1]) if not m[1][0].isdigit() else float(m[1])
        unit = UNITS[m[2].rstrip("s")]
        return now + timedelta(seconds=n * unit)
    m = re.search(r"\bin\s+half an?\s+hour\b", t)
    if m:
        return now + timedelta(minutes=30)

    tod = _time_of_day(t)
    day: Optional[datetime] = None
    if "tomorrow" in t:
        day = now + timedelta(days=1)
    elif re.search(r"\b(today|tonight|this (evening|afternoon|morning))\b", t):
        day = now
    else:
        m = re.search(r"\b(next\s+|this\s+|on\s+)?(" + "|".join(WEEKDAYS) + r")\b", t)
        if m:
            target = WEEKDAYS.index(m[2])
            delta = (target - now.weekday()) % 7
            if delta == 0 and (m[1] or "").strip() == "next":
                delta = 7
            elif delta == 0 and tod and (tod[0], tod[1]) <= (now.hour, now.minute):
                delta = 7
            day = now + timedelta(days=delta)
        else:
            m = re.search(r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", t) or \
                re.search(r"\b(?:on\s+)?(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", t)
            if m:
                a, b, y = m[1], m[2], m[3]
                d_, mo = (int(a), MONTHS.index(b) + 1) if a.isdigit() else (int(b), MONTHS.index(a) + 1)
                year = int(y) if y else now.year
                try:
                    day = datetime(year, mo, d_)
                    if not y and day.date() < now.date():
                        day = datetime(year + 1, mo, d_)
                except ValueError:
                    return None
    if "tonight" in t and not tod:
        tod = (20, 0)
    if "this evening" in t and not tod:
        tod = (18, 0)
    if "this morning" in t and not tod:
        tod = (9, 0)
    if "this afternoon" in t and not tod:
        tod = (15, 0)
    if "end of day" in t or "eod" in t:
        tod = tod or (17, 0)
        day = day or now

    if day is None and tod is None:
        return None
    if day is None:
        day = now
        cand = day.replace(hour=tod[0], minute=tod[1])
        return cand if cand > now else cand + timedelta(days=1)
    if tod is None:
        tod = (9, 0)
    return day.replace(hour=tod[0], minute=tod[1], second=0, microsecond=0)


def parse_recurrence(text: str) -> str:
    """Returns '', 'daily', 'weekdays', 'weekly', 'monthly', 'hourly' or 'every:N:unit'."""
    t = (text or "").lower()
    m = re.search(r"\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b", t)
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m[2][0]]
        return f"every:{int(m[1])}:{unit}"
    if re.search(r"\bevery\s+(weekday|work ?day)s?\b|\bweekdays\b", t):
        return "weekdays"
    if re.search(r"\b(every\s+day|daily|each day|every morning|every evening|every night)\b", t):
        return "daily"
    if re.search(r"\b(every\s+week|weekly)\b|\bevery\s+(" + "|".join(WEEKDAYS) + r")\b", t):
        return "weekly"
    if re.search(r"\b(every\s+month|monthly)\b", t):
        return "monthly"
    if re.search(r"\b(every\s+hour|hourly)\b", t):
        return "hourly"
    return ""


def next_occurrence(due: datetime, recurrence: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """The first occurrence strictly after `now` (skips ones missed while the app was closed)."""
    now = now or datetime.now()
    if not recurrence:
        return None
    cur = due
    for _ in range(100000):
        if recurrence == "daily":
            cur += timedelta(days=1)
        elif recurrence == "weekdays":
            cur += timedelta(days=1)
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
        elif recurrence == "weekly":
            cur += timedelta(weeks=1)
        elif recurrence == "monthly":
            y, mo = cur.year + (cur.month // 12), cur.month % 12 + 1
            day = min(cur.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1])
            cur = cur.replace(year=y, month=mo, day=day)
        elif recurrence == "hourly":
            cur += timedelta(hours=1)
        elif recurrence.startswith("every:"):
            _, n, unit = recurrence.split(":")
            cur += timedelta(**{unit: int(n)})
        else:
            return None
        if cur > now:
            return cur
    return None
