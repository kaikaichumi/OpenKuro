"""Calendar tools: read and write local ICS calendar.

Uses a local .ics file at ~/.kuro/calendar.ics for storing events.
No OAuth2 or external API required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import get_kuro_home
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


def _get_calendar_path() -> Path:
    """Get path to the local ICS calendar file."""
    return get_kuro_home() / "calendar.ics"


def _ensure_calendar() -> Path:
    """Ensure the calendar file exists with a valid VCALENDAR structure."""
    path = _get_calendar_path()
    if not path.exists():
        from icalendar import Calendar

        cal = Calendar()
        cal.add("prodid", "-//Kuro AI Assistant//EN")
        cal.add("version", "2.0")
        cal.add("calscale", "GREGORIAN")
        cal.add("x-wr-calname", "Kuro Calendar")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cal.to_ical())

    return path


def _load_calendar():
    """Load and parse the ICS calendar file."""
    from icalendar import Calendar

    path = _ensure_calendar()
    with open(path, "rb") as f:
        return Calendar.from_ical(f.read())


def _save_calendar(cal) -> None:
    """Save the calendar back to the ICS file."""
    path = _get_calendar_path()
    path.write_bytes(cal.to_ical())


def _parse_date(date_str: str) -> datetime:
    """Parse a date string into a datetime object.

    Supports: YYYY-MM-DD, YYYY-MM-DD HH:MM, YYYY-MM-DDTHH:MM
    """
    date_str = date_str.strip()

    for fmt in [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    raise ValueError(
        f"Cannot parse date '{date_str}'. "
        "Use format: YYYY-MM-DD or YYYY-MM-DD HH:MM"
    )


def _format_event(event) -> str:
    """Format a VEVENT component into a readable string."""
    summary = str(event.get("summary", "Untitled"))
    dtstart = event.get("dtstart")
    dtend = event.get("dtend")
    description = event.get("description", "")
    location = event.get("location", "")
    uid = str(event.get("uid", ""))

    start_str = str(dtstart.dt) if dtstart else "unknown"
    end_str = str(dtend.dt) if dtend else ""

    lines = [f"  {summary}"]
    if end_str:
        lines.append(f"    Time: {start_str} ~ {end_str}")
    else:
        lines.append(f"    Time: {start_str}")
    if location:
        lines.append(f"    Location: {location}")
    if description:
        desc_preview = str(description)[:200]
        lines.append(f"    Note: {desc_preview}")
    lines.append(f"    ID: {uid[:8]}...")

    return "\n".join(lines)


class CalendarReadTool(BaseTool):
    """Read events from the local calendar."""

    name = "calendar_read"
    description = (
        "List calendar events for today or a specific date range. "
        "Reads from the local ICS calendar file. "
        "Use this to check the user's schedule."
    )
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": (
                    "Date to show events for (YYYY-MM-DD). "
                    "Default: today."
                ),
            },
            "start_date": {
                "type": "string",
                "description": "Start date for range query (YYYY-MM-DD)",
            },
            "end_date": {
                "type": "string",
                "description": "End date for range query (YYYY-MM-DD)",
            },
            "days": {
                "type": "integer",
                "description": "Number of days to show from today (default: 1)",
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            from icalendar import Calendar

            now = datetime.now()

            # Determine date range
            if params.get("start_date") and params.get("end_date"):
                range_start = _parse_date(params["start_date"])
                range_end = _parse_date(params["end_date"])
                # Make end_date inclusive (end of day)
                if range_end.hour == 0 and range_end.minute == 0:
                    range_end = range_end.replace(hour=23, minute=59, second=59)
            elif params.get("date"):
                range_start = _parse_date(params["date"])
                range_end = range_start.replace(hour=23, minute=59, second=59)
            else:
                days = params.get("days", 1)
                range_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                range_end = range_start + timedelta(days=days) - timedelta(seconds=1)

            cal = _load_calendar()

            # Collect matching events
            events = []
            for component in cal.walk():
                if component.name != "VEVENT":
                    continue

                dtstart = component.get("dtstart")
                if not dtstart:
                    continue

                event_dt = dtstart.dt
                # Convert date to datetime for comparison
                if not isinstance(event_dt, datetime):
                    event_dt = datetime.combine(event_dt, datetime.min.time())

                # Remove timezone info for naive comparison
                if event_dt.tzinfo is not None:
                    event_dt = event_dt.replace(tzinfo=None)

                if range_start <= event_dt <= range_end:
                    events.append((event_dt, component))

            # Sort by start time
            events.sort(key=lambda x: x[0])

            if not events:
                date_desc = (
                    f"{range_start.strftime('%Y-%m-%d')}"
                    if range_start.date() == range_end.date()
                    else f"{range_start.strftime('%Y-%m-%d')} ~ {range_end.strftime('%Y-%m-%d')}"
                )
                return ToolResult.ok(
                    f"No events found for {date_desc}.",
                    count=0,
                )

            # Format output
            lines = [f"Calendar Events ({len(events)} found):"]
            current_date = None
            for i, (dt, event) in enumerate(events, 1):
                event_date = dt.strftime("%Y-%m-%d (%A)")
                if event_date != current_date:
                    current_date = event_date
                    lines.append(f"\n[{current_date}]")
                lines.append(f"{i}. {_format_event(event)}")

            return ToolResult.ok(
                "\n".join(lines),
                count=len(events),
            )

        except ImportError as e:
            return ToolResult.fail(
                f"Calendar dependency not installed: {e}. "
                "Install with: pip install icalendar"
            )
        except Exception as e:
            return ToolResult.fail(f"Calendar read error: {e}")


class CalendarWriteTool(BaseTool):
    """Add a new event to the local calendar."""

    name = "calendar_write"
    description = (
        "Create a new calendar event in the local ICS calendar. "
        "Use this when the user wants to schedule something."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Event title/summary",
            },
            "start": {
                "type": "string",
                "description": "Start date/time (YYYY-MM-DD HH:MM or YYYY-MM-DD)",
            },
            "end": {
                "type": "string",
                "description": (
                    "End date/time (YYYY-MM-DD HH:MM). "
                    "Default: 1 hour after start."
                ),
            },
            "description": {
                "type": "string",
                "description": "Event description/notes",
            },
            "location": {
                "type": "string",
                "description": "Event location",
            },
        },
        "required": ["summary", "start"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        summary = params.get("summary", "")
        start_str = params.get("start", "")
        end_str = params.get("end")
        description = params.get("description", "")
        location = params.get("location", "")

        if not summary:
            return ToolResult.fail("Event summary is required")
        if not start_str:
            return ToolResult.fail("Start date/time is required")

        try:
            from icalendar import Calendar, Event

            start_dt = _parse_date(start_str)

            if end_str:
                end_dt = _parse_date(end_str)
            else:
                # Default: 1 hour after start for timed events, same day for all-day
                if start_dt.hour == 0 and start_dt.minute == 0 and " " not in start_str and "T" not in start_str:
                    # All-day event
                    end_dt = start_dt + timedelta(days=1)
                else:
                    end_dt = start_dt + timedelta(hours=1)

            # Create event
            event = Event()
            event_uid = str(uuid.uuid4())
            event.add("uid", event_uid)
            event.add("summary", summary)
            event.add("dtstart", start_dt)
            event.add("dtend", end_dt)
            event.add("dtstamp", datetime.now(timezone.utc))
            event.add("created", datetime.now(timezone.utc))

            if description:
                event.add("description", description)
            if location:
                event.add("location", location)

            # Load existing calendar and add event
            cal = _load_calendar()
            cal.add_component(event)
            _save_calendar(cal)

            return ToolResult.ok(
                f"Event created: {summary}\n"
                f"Time: {start_dt} ~ {end_dt}\n"
                f"ID: {event_uid[:8]}...",
                uid=event_uid,
                summary=summary,
            )

        except ImportError as e:
            return ToolResult.fail(
                f"Calendar dependency not installed: {e}. "
                "Install with: pip install icalendar"
            )
        except ValueError as e:
            return ToolResult.fail(str(e))
        except Exception as e:
            return ToolResult.fail(f"Calendar write error: {e}")
