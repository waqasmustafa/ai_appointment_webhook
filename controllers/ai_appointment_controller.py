from odoo import http
from odoo.http import request
from datetime import datetime, time, timedelta
import pytz
import json


class AiAppointmentController(http.Controller):
    """
    JSON webhooks for AI caller:

    - POST /ai/appointments/check
    - POST /ai/appointments/book

    Appointment scoping:

    You can target a specific Odoo Appointment (like /appointment/11 - "Dr Drizzle")
    by sending either:

      "appointment_id": 11
         OR
      "appointment_title": "Dr Drizzle"

    The controller will:
    - read appointment.appointment (Odoo's Appointment app)
    - take the first user in appointment.user_ids (Users field)
    - use that user's calendar (calendar.event.user_id) for availability & booking.

    Optional:
      "calendar_user_email" overrides the user coming from the appointment.
    """

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    def _get_window_hours(self, time_window):
        """Map time_window keyword to a working-hour interval."""
        if time_window == "morning":
            return time(9, 0), time(12, 0)
        elif time_window == "afternoon":
            return time(13, 0), time(17, 0)
        elif time_window == "evening":
            return time(17, 0), time(20, 0)
        else:  # "any" or unknown
            return time(9, 0), time(17, 0)

    def _resolve_user_from_appointment(self, appointment_id=None, appointment_title=None):
        """
        Use appointment.appointment to find which user calendar to use.

        Priority:
        1) appointment_id (e.g. 11 from /appointment/11)
        2) appointment_title (e.g. "Dr Drizzle")

        Returns a res.users record or False.
        """
        Appointment = request.env["appointment.appointment"].sudo()
        appt = False

        if appointment_id:
            # ID from /appointment/11
            try:
                appt = Appointment.browse(int(appointment_id))
            except Exception:
                appt = False
            if appt and not appt.exists():
                appt = False

        if not appt and appointment_title:
            appt = Appointment.search([("name", "=", appointment_title)], limit=1)

        if not appt:
            return False

        # Appointment "Users" m2m (typically field user_ids)
        user = appt.user_ids[:1]
        return user or False

    def _compute_free_slots(
        self, date_pref, duration_minutes, time_window, timezone_str, user_ids=None
    ):
        """
        - Build working window in caller's timezone.
        - Convert to UTC for DB queries.
        - Fetch overlapping events from calendar.event, optionally filtered by user_ids.
        - Merge busy intervals.
        - Compute free intervals.
        - Split into fixed-size slots.
        - Return up to 10 slots (start/end in caller timezone).
        """
        env = request.env
        CalendarEvent = env["calendar.event"].sudo()

        # Parse date
        try:
            year, month, day = map(int, date_pref.split("-"))
        except Exception:
            raise ValueError("Invalid date_preference format. Expected YYYY-MM-DD.")

        # Resolve timezone
        try:
            tz = pytz.timezone(timezone_str or "UTC")
        except Exception:
            tz = pytz.UTC

        # Working window in local time
        start_hour, end_hour = self._get_window_hours(time_window)
        day_start_local = tz.localize(
            datetime(year, month, day, start_hour.hour, start_hour.minute)
        )
        day_end_local = tz.localize(
            datetime(year, month, day, end_hour.hour, end_hour.minute)
        )

        # Convert to UTC for DB search
        day_start_utc = day_start_local.astimezone(pytz.UTC)
        day_end_utc = day_end_local.astimezone(pytz.UTC)

        # Build search domain
        domain = [
            ("start", "<", day_end_utc),
            ("stop", ">", day_start_utc),
        ]
        if user_ids:
            domain.append(("user_id", "in", user_ids))

        busy_events = CalendarEvent.search(domain)

        # Collect busy intervals
        busy_intervals = [(ev.start, ev.stop) for ev in busy_events]
        busy_intervals.sort(key=lambda x: x[0])

        # Merge overlapping intervals
        merged = []
        for interval in busy_intervals:
            if not merged:
                merged.append([interval[0], interval[1]])
            else:
                last = merged[-1]
                if interval[0] <= last[1]:
                    last[1] = max(last[1], interval[1])
                else:
                    merged.append([interval[0], interval[1]])

        # Compute free intervals
        free_intervals = []
        current_start = day_start_utc
        for b_start, b_end in merged:
            if b_start > current_start:
                free_intervals.append((current_start, b_start))
            current_start = max(current_start, b_end)
        if current_start < day_end_utc:
            free_intervals.append((current_start, day_end_utc))

        # Split free intervals into slots
        delta = timedelta(minutes=duration_minutes)
        slots = []
        for f_start, f_end in free_intervals:
            slot_start = f_start
            while slot_start + delta <= f_end:
                slot_end = slot_start + delta
                local_start = slot_start.astimezone(tz)
                local_end = slot_end.astimezone(tz)
                slots.append({
                    "start": local_start.isoformat(),
                    "end": local_end.isoformat(),
                })
                slot_start += delta

        return slots[:10]

    def _get_payload(self, params):
        """
        Safely get JSON body regardless of how Odoo wraps it.
        Priority:
        - explicit params (kwargs)
        - raw httprequest.data parsed as JSON
        - if that JSON has "params", use that dict
        """
        if params:
            return params

        data = {}
        try:
            raw = request.httprequest.data
        except Exception:
            raw = b""

        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {}

        # If JSON-RPC envelope: {"jsonrpc":"2.0","params":{...}}
        if isinstance(data, dict) and "params" in data and isinstance(data["params"], dict):
            data = data["params"]

        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------------
    # POST /ai/appointments/check
    # ---------------------------------------------------------------------
    @http.route(
        "/ai/appointments/check",
        type="json",
        auth="public",
        csrf=False,
        methods=["POST"],
    )
    def check_availability(self, **params):
        """
        Body example:

        {
          "appointment_id": 11,
          "appointment_title": "Dr Drizzle",
          "date_preference": "2025-11-17",
          "time_window": "afternoon",
          "timezone": "America/New_York",
          "duration_minutes": 60
        }
        """
        params = self._get_payload(params)

        date_pref = params.get("date_preference")
        if not date_pref:
            return {
                "status": "error",
                "message": "date_preference is required (YYYY-MM-DD).",
            }

        appointment_id = params.get("appointment_id")
        appointment_title = params.get("appointment_title")
        time_window = params.get("time_window", "any")
        timezone_str = params.get("timezone", "UTC")
        duration = params.get("duration_minutes", 30)
        calendar_user_email = params.get("calendar_user_email")

        try:
            duration = int(duration)
        except Exception:
            return {
                "status": "error",
                "message": "duration_minutes must be an integer.",
            }

        # Figure out which user calendar to use
        user_ids = []
        env = request.env

        # 1) explicit email override
        if calendar_user_email:
            User = env["res.users"].sudo()
            user = User.search([("email", "=", calendar_user_email)], limit=1)
            if user:
                user_ids = [user.id]

        # 2) else derive from appointment.appointment
        if not user_ids and (appointment_id or appointment_title):
            user = self._resolve_user_from_appointment(
                appointment_id=appointment_id,
                appointment_title=appointment_title,
            )
            if user:
                user_ids = [user.id]

        try:
            slots = self._compute_free_slots(
                date_pref=date_pref,
                duration_minutes=duration,
                time_window=time_window,
                timezone_str=timezone_str,
                user_ids=user_ids or None,
            )
        except Exception as e:
            return {"status": "error", "message": str(e)}

        return {
            "status": "ok",
            "slots": slots,
        }

    # ---------------------------------------------------------------------
    # POST /ai/appointments/book
    # ---------------------------------------------------------------------
    @http.route(
        "/ai/appointments/book",
        type="json",
        auth="public",
        csrf=False,
        methods=["POST"],
    )
    def book_appointment(self, **params):
        """
        Body example:

        {
          "appointment_id": 11,
          "appointment_title": "Dr Drizzle",
          "appointment_type": "Dr Drizzle - Online",
          "slot_start": "2025-11-17T13:00:00-05:00",
          "slot_end": "2025-11-17T14:00:00-05:00",
          "caller_name": "John Smith",
          "caller_phone": "+15551234567",
          "caller_email": "john@example.com",
          "notes": "Booked via AI caller."
        }
        """
        params = self._get_payload(params)

        name = params.get("caller_name") or "Unknown"
        phone = params.get("caller_phone")
        email = params.get("caller_email")
        start_iso = params.get("slot_start")
        end_iso = params.get("slot_end")
        appointment_type = params.get("appointment_type", "Consultation")
        notes = params.get("notes", "")

        appointment_id = params.get("appointment_id")
        appointment_title = params.get("appointment_title")
        calendar_user_email = params.get("calendar_user_email")

        if not (start_iso and end_iso):
            return {
                "status": "error",
                "message": "slot_start and slot_end are required.",
            }

        env = request.env

        # Partner (caller)
        Partner = env["res.partner"].sudo()
        partner = None
        if email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if not partner and phone:
            partner = Partner.search([("phone", "=", phone)], limit=1)

        if not partner:
            vals = {"name": name}
            if phone:
                vals["phone"] = phone
            if email:
                vals["email"] = email
            partner = Partner.create(vals)

        # Determine owner user_id (calendar owner)
        user_id = False

        # 1) explicit email override
        if calendar_user_email:
            User = env["res.users"].sudo()
            user = User.search([("email", "=", calendar_user_email)], limit=1)
            if user:
                user_id = user.id

        # 2) else derive from appointment.appointment
        if not user_id and (appointment_id or appointment_title):
            user = self._resolve_user_from_appointment(
                appointment_id=appointment_id,
                appointment_title=appointment_title,
            )
            if user:
                user_id = user.id

        # Create calendar event
        Event = env["calendar.event"].sudo()
        event_vals = {
            "name": f"{appointment_type} - {name}",
            "start": start_iso,
            "stop": end_iso,
            "partner_ids": [(4, partner.id)],
            "description": notes,
        }
        if user_id:
            event_vals["user_id"] = user_id

        # Optional custom field
        if "x_source" in Event._fields:
            event_vals["x_source"] = "ai_caller_agent"

        event = Event.create(event_vals)

        # Localize response to partner timezone
        partner_tz = partner.tz or "UTC"
        try:
            local_tz = pytz.timezone(partner_tz)
        except Exception:
            local_tz = pytz.UTC

        start_local = event.start.astimezone(local_tz)
        end_local = event.stop.astimezone(local_tz)

        return {
            "status": "confirmed",
            "appointment_id": event.id,
            "final_start": start_local.isoformat(),
            "final_end": end_local.isoformat(),
            "partner_id": partner.id,
        }
