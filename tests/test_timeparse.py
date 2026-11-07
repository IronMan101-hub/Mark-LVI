from datetime import datetime
from galahad.timeparse import parse_when, parse_recurrence, next_occurrence

NOW = datetime(2026, 11, 7, 10, 0)   # a Saturday


def test_relative():
    assert parse_when("in 10 minutes", NOW) == datetime(2026, 11, 7, 10, 10)
    assert parse_when("remind me in 2 hours", NOW) == datetime(2026, 11, 7, 12, 0)
    assert parse_when("in half an hour", NOW) == datetime(2026, 11, 7, 10, 30)
    assert parse_when("in three days", NOW) == datetime(2026, 11, 10, 10, 0)


def test_absolute():
    assert parse_when("tomorrow at 5pm", NOW) == datetime(2026, 11, 8, 17, 0)
    assert parse_when("today 18:30", NOW) == datetime(2026, 11, 7, 18, 30)
    assert parse_when("at 7am", NOW) == datetime(2026, 11, 8, 7, 0)       # already past -> next day
    assert parse_when("at 11pm", NOW) == datetime(2026, 11, 7, 23, 0)
    assert parse_when("2026-12-01 14:30", NOW) == datetime(2026, 12, 1, 14, 30)
    assert parse_when("on 15 March", NOW) == datetime(2027, 3, 15, 9, 0)
    assert parse_when("December 3rd at 4pm", NOW) == datetime(2026, 12, 3, 16, 0)


def test_weekdays():
    assert parse_when("monday 9am", NOW) == datetime(2026, 11, 9, 9, 0)
    assert parse_when("next saturday", NOW) == datetime(2026, 11, 14, 9, 0)
    assert parse_when("tonight", NOW) == datetime(2026, 11, 7, 20, 0)


def test_unrecognised():
    assert parse_when("sometime soon-ish", NOW) is None
    assert parse_when("", NOW) is None


def test_recurrence():
    assert parse_recurrence("every day at 8am") == "daily"
    assert parse_recurrence("every weekday") == "weekdays"
    assert parse_recurrence("every 3 hours") == "every:3:hours"
    assert parse_recurrence("every monday") == "weekly"
    assert parse_recurrence("once") == ""


def test_next_occurrence_skips_missed():
    due = datetime(2026, 11, 1, 8, 0)
    assert next_occurrence(due, "daily", NOW) == datetime(2026, 11, 8, 8, 0)
    assert next_occurrence(datetime(2026, 11, 6, 8, 0), "weekdays", NOW) == datetime(2026, 11, 9, 8, 0)
    assert next_occurrence(datetime(2026, 1, 31, 8, 0), "monthly", datetime(2026, 1, 31, 9, 0)) == datetime(2026, 2, 28, 8, 0)
    assert next_occurrence(due, "", NOW) is None
