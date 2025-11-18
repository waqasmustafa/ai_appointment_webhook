from odoo import http, fields
from odoo.http import request
from datetime import datetime, time, timedelta
import pytz
import json


class AiAppointmentController(http.Controller):
    """
    JSON webhooks for AI caller:

    - POST /ai/appointments/check
    - POST /ai/appointments/book
    - POST /ai/appointments/cancel

    Appointment scoping:

    You can target a specific Odoo Appointment by sending either:

      "appointment_type_id": 11
         OR
      "appointment_type_name": "Dr Drizzle"

    The controller will use that appointment type's staff members.
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

    def _get_appointment_type_obj(self, appointment_type_id=None, appointment_type_name=None):
        """Get appointment type object for booking creation."""
        env = request.env

        # Try different possible model names for Odoo 18
        possible_models = [
            "appointment.type",
            "calendar.appointment.type",
            "appointment.appointment.type",
        ]

        AppointmentType = None
        for model_name in possible_models:
            if model_name in env:
                AppointmentType = env[model_name].sudo()
                break

        if not AppointmentType:
            return False

        appt_type = False

        if appointment_type_id:
            try:
                appt_type = AppointmentType.browse(int(appointment_type_id))
            except Exception:
                appt_type = False
            if appt_type and not appt_type.exists():
                appt_type = False

        if not appt_type and appointment_type_name:
            appt_type = AppointmentType.search([("name", "=", appointment_type_name)], limit=1)

        return appt_type

    def _get_staff_users_from_type(self, appt_type):
        """Return ALL staff users linked to an appointment type."""
        if not appt_type:
            return request.env["res.users"].sudo().browse()

        user_field_names = ["staff_user_ids", "user_ids", "resource_ids"]
        users = request.env["res.users"].sudo().browse()

        for field_name in user_field_names:
            if field_name in appt_type._fields:
                val = getattr(appt_type, field_name)
                if val:
                    users = val
                    break

        return users

    def _resolve_user_from_appointment(self, appointment_type_id=None, appointment_type_name=None):
        """
        Use appointment.type to find which user calendar to use.

        Priority:
        1) appointment_type_id
        2) appointment_type_name

        Returns a res.users record or False.
        """
        appt_type = self._get_appointment_type_obj(
            appointment_type_id=appointment_type_id,
            appointment_type_name=appointment_type_name,
        )

        if not appt_type:
            return False

        users = self._get_staff_users_from_type(appt_type)
        user = users[:1] if users else False
        return user

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
                slots.append(
                    {
                        "start": local_start.isoformat(),
                        "end": local_end.isoformat(),
                    }
                )
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

    def _convert_iso_to_odoo_format(self, iso_datetime_str):
        """
        Convert ISO datetime string with timezone to Odoo's datetime format.

        Args:
            iso_datetime_str (str): ISO format like "2025-11-17T13:00:00-05:00"

        Returns:
            str: "YYYY-MM-DD HH:MM:SS" in UTC
        """
        try:
            dt_with_tz = datetime.fromisoformat(iso_datetime_str)
            dt_utc = dt_with_tz.astimezone(pytz.UTC)
            return dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            raise ValueError(f"Invalid datetime format '{iso_datetime_str}': {str(e)}")

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
          "appointment_type_id": 11,
          "appointment_type_name": "Dr Drizzle",
          "date_preference": "2025-11-18",
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

        appointment_type_id = params.get("appointment_type_id") or params.get("appointment_id")
        appointment_type_name = params.get("appointment_type_name") or params.get(
            "appointment_title"
        )
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

        # 2) else derive from appointment type
        if not user_ids and (appointment_type_id or appointment_type_name):
            user = self._resolve_user_from_appointment(
                appointment_type_id=appointment_type_id,
                appointment_type_name=appointment_type_name,
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
          "appointment_type_id": 11,
          "appointment_type_name": "Dr Drizzle",
          "appointment_type": "Dr Drizzle - Online",
          "slot_start": "2025-11-18T13:00:00-05:00",
          "slot_end": "2025-11-18T14:00:00-05:00",
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
        appointment_type_label = params.get("appointment_type", "Consultation")
        notes = params.get("notes", "")

        appointment_type_id = params.get("appointment_type_id") or params.get("appointment_id")
        appointment_type_name = params.get("appointment_type_name") or params.get(
            "appointment_title"
        )
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

        # Get appointment type & staff users
        appointment_type_obj = self._get_appointment_type_obj(
            appointment_type_id=appointment_type_id,
            appointment_type_name=appointment_type_name,
        )
        staff_users = self._get_staff_users_from_type(appointment_type_obj)
        staff_partner_ids = staff_users.mapped("partner_id").ids if staff_users else []

        # Determine owner user_id (calendar owner)
        user_id = False

        # 1) explicit email override
        if calendar_user_email:
            User = env["res.users"].sudo()
            user = User.search([("email", "=", calendar_user_email)], limit=1)
            if user:
                user_id = user.id

        # 2) else derive from appointment type
        if not user_id and staff_users:
            user_id = staff_users[:1].id

        # Convert ISO datetime strings to Odoo format
        try:
            start_odoo_format = self._convert_iso_to_odoo_format(start_iso)
            end_odoo_format = self._convert_iso_to_odoo_format(end_iso)
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
            }

        # Build attendees list: caller + staff users' partners
        attendee_partner_ids = [partner.id] + staff_partner_ids
        # Deduplicate
        attendee_partner_ids = list(set(attendee_partner_ids))

        # Create calendar event
        Event = env["calendar.event"].sudo()
        event_vals = {
            "name": f"{appointment_type_label} - {name}",
            "start": start_odoo_format,
            "stop": end_odoo_format,
            # ALL attendees: caller + staff
            "partner_ids": [(6, 0, attendee_partner_ids)],
            "description": notes,
        }
        if user_id:
            event_vals["user_id"] = user_id

        # Optional custom field
        if "x_source" in Event._fields:
            event_vals["x_source"] = "ai_caller_agent"

        try:
            event = Event.create(event_vals)
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to create calendar event: {str(e)}",
            }

        # Link to calendar.booking if that model exists
        booking = None
        if "calendar.booking" in env and appointment_type_obj:
            Booking = env["calendar.booking"].sudo()

            booking_vals = {
                "name": event.name,
                "calendar_event_id": event.id,
                "start": event.start,
                "stop": event.stop,
            }

            # Appointment type relation
            if "appointment_type_id" in Booking._fields:
                booking_vals["appointment_type_id"] = appointment_type_obj.id
            elif "appointment_id" in Booking._fields:
                booking_vals["appointment_id"] = appointment_type_obj.id

            # Booked-by partner
            if "partner_id" in Booking._fields:
                booking_vals["partner_id"] = partner.id

            # Appointment resources / staff (best-effort)
            if staff_users:
                # some DBs use appointment_resource_ids (to appointment.resource)
                # others might use staff_user_ids or user_ids
                if "appointment_resource_ids" in Booking._fields:
                    # best-effort: if appointment_resource_ids is related to users,
                    # you might need mapping; here we just skip to avoid crash
                    pass
                elif "staff_user_ids" in Booking._fields:
                    booking_vals["staff_user_ids"] = [(6, 0, staff_users.ids)]
                elif "user_ids" in Booking._fields:
                    booking_vals["user_ids"] = [(6, 0, staff_users.ids)]

            try:
                booking = Booking.create(booking_vals)
            except Exception:
                # don't block API if booking creation fails
                booking = None

        # Localize response to partner timezone for confirmation
        partner_tz = partner.tz or "UTC"
        try:
            local_tz = pytz.timezone(partner_tz)
        except Exception:
            local_tz = pytz.UTC

        start_local = event.start.astimezone(local_tz)
        end_local = event.stop.astimezone(local_tz)

        response = {
            "status": "confirmed",
            "appointment_id": event.id,
            "final_start": start_local.isoformat(),
            "final_end": end_local.isoformat(),
            "partner_id": partner.id,
            "appointment_name": event.name,
            "message": "Calendar event created and linked to appointment type (if available)",
        }

        if booking:
            response["booking_id"] = booking.id

        return response

    # ---------------------------------------------------------------------
    # POST /ai/appointments/cancel
    # ---------------------------------------------------------------------
    @http.route(
        "/ai/appointments/cancel",
        type="json",
        auth="public",
        csrf=False,
        methods=["POST"],
    )
    def cancel_appointment(self, **params):
        """
        Cancel an existing appointment.

        Body example:
        {
          "appointment_id": 123,
          "reason": "Patient rescheduled"
        }
        """
        params = self._get_payload(params)

        appointment_id = params.get("appointment_id")
        reason = params.get("reason", "Cancelled via AI caller")

        if not appointment_id:
            return {
                "status": "error",
                "message": "appointment_id is required.",
            }

        env = request.env
        Event = env["calendar.event"].sudo()

        try:
            appointment_id = int(appointment_id)
        except ValueError:
            return {
                "status": "error",
                "message": "appointment_id must be a valid integer.",
            }

        event = Event.browse(appointment_id)
        if not event.exists():
            return {
                "status": "error",
                "message": f"Appointment with ID {appointment_id} not found.",
            }

        try:
            event_name = event.name

            # Also try to cancel calendar.booking if exists
            if "calendar.booking" in env:
                Booking = env["calendar.booking"].sudo()
                if "calendar_event_id" in Booking._fields:
                    booking = Booking.search(
                        [("calendar_event_id", "=", appointment_id)], limit=1
                    )
                    if booking:
                        if "state" in booking._fields:
                            booking.state = "cancelled"
                        elif "status" in booking._fields:
                            booking.status = "cancelled"

            event.message_post(body=f"Cancelled via AI caller: {reason}")
            event.unlink()

            return {
                "status": "cancelled",
                "message": f"Appointment '{event_name}' has been cancelled.",
                "appointment_id": appointment_id,
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to cancel appointment: {str(e)}",
            }
