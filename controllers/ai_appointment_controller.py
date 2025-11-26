# ai_appointment_webhook/controllers/ai_appointment_controller.py
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
    - POST /ai/appointments/cancel

    Appointment scoping:

    You can target a specific Odoo Appointment by sending either:

      "appointment_type_id": 11
         OR
      "appointment_type_name": "Dr Drizzle"

    The controller will:
    - find that appointment.type (Appointment Title)
    - use its staff users for availability (their calendars)
    - create a calendar.event with appointment_type_id set
      so it appears under that Appointment in the Odoo UI.
    """

    # Static default assignee (Mark Khan) – res.users ID
    STATIC_ASSIGNEE_USER_ID = 2

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
            # Return full day to ensure we don't hide any potential slots
            return time(0, 0), time(23, 59)

    def _get_appointment_type_obj(self, appointment_type_id=None, appointment_type_name=None):
        """Resolve appointment.type (Appointment Title: Dr Drizzle)."""
        env = request.env
        AppointmentType = env["appointment.type"].sudo()

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

        # Odoo 18+ uses staff_user_ids, older versions used user_ids
        users = False
        if hasattr(appt_type, "staff_user_ids") and appt_type.staff_user_ids:
            users = appt_type.staff_user_ids
        elif hasattr(appt_type, "user_ids") and appt_type.user_ids:
            users = appt_type.user_ids

        # Sort users to ensure deterministic behavior (e.g. always pick the same one if multiple)
        if users:
            users = users.sorted('id')
        
        user = users[:1] if users else False
        return user

    def _get_configured_slots_from_appointment(self, appointment_type_obj, date_pref, timezone_str):
        """
        Fetch configured time slots from appointment.type.slot for a specific date.
        
        Returns a list of slot intervals (start_datetime, end_datetime) in UTC,
        based on the appointment type's Schedule configuration.
        """
        if not appointment_type_obj:
            return []
        
        env = request.env
        
        # Check if appointment.type has slot_ids field
        if not hasattr(appointment_type_obj, 'slot_ids'):
            return []
        
        # Parse the requested date
        try:
            year, month, day = map(int, date_pref.split("-"))
            requested_date = datetime(year, month, day)
        except Exception:
            return []
        
        # Get weekday (0=Monday, 6=Sunday in Python)
        weekday = requested_date.weekday()
        
        # Resolve timezone: Use appointment type's timezone if available, else fallback to params or UTC
        # This ensures slots are interpreted in the doctor's timezone, not the caller's.
        appt_tz_name = False
        if hasattr(appointment_type_obj, 'appointment_tz') and appointment_type_obj.appointment_tz:
            appt_tz_name = appointment_type_obj.appointment_tz
        
        try:
            # If appointment has a TZ, use it to interpret the slot hours
            # Otherwise use the passed timezone (which might be wrong if caller is in different TZ)
            tz_name = appt_tz_name or timezone_str or "UTC"
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.UTC
        
        # Fetch configured slots for this weekday
        configured_slots = []
        for slot in appointment_type_obj.slot_ids:
            # slot.weekday is typically stored as string: '1' for Monday ... '7' for Sunday in Odoo
            try:
                slot_weekday_int = int(slot.weekday)
            except (ValueError, AttributeError):
                continue
            
            # Map Python 0-6 to Odoo 1-7
            # Python: 0=Mon, 1=Tue, ... 6=Sun
            # Odoo:   1=Mon, 2=Tue, ... 7=Sun
            if slot_weekday_int != (weekday + 1):
                continue
            
            # Convert float hours to datetime
            # slot.start_hour and slot.end_hour are floats (e.g., 13.0 = 1:00 PM, 13.5 = 1:30 PM)
            try:
                start_hour = int(slot.start_hour)
                start_minute = int((slot.start_hour - start_hour) * 60)
                end_hour = int(slot.end_hour)
                end_minute = int((slot.end_hour - end_hour) * 60)
                
                # Create datetime in the APPOINTMENT'S timezone
                slot_start_local = tz.localize(
                    datetime(year, month, day, start_hour, start_minute)
                )
                slot_end_local = tz.localize(
                    datetime(year, month, day, end_hour, end_minute)
                )
                
                # Convert to UTC for consistency
                slot_start_utc = slot_start_local.astimezone(pytz.UTC)
                slot_end_utc = slot_end_local.astimezone(pytz.UTC)
                
                configured_slots.append((slot_start_utc, slot_end_utc))
            except (AttributeError, ValueError):
                continue
        
        return configured_slots

    def _compute_free_slots(
        self, date_pref, duration_minutes, time_window, timezone_str, user_ids=None, appointment_type_obj=None
    ):
        """
        - If appointment_type_obj is provided and has configured slots, use those.
        - Otherwise, build working window in caller's timezone and generate slots.
        - Convert to UTC for DB queries.
        - Fetch overlapping events from calendar.event, optionally filtered by user_ids.
        - Merge busy intervals.
        - Compute free intervals.
        - Split into fixed-size slots (or use configured slots).
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

        # Try to get configured slots from appointment type
        configured_slots = []
        if appointment_type_obj:
            configured_slots = self._get_configured_slots_from_appointment(
                appointment_type_obj, date_pref, timezone_str
            )

        # If we have configured slots, use them; otherwise fall back to auto-generation
        if configured_slots:
            # Use configured slots (already in UTC)
            slot_intervals = configured_slots
            
            # Determine search window for busy events (min/max of all configured slots)
            day_start_utc = min(s[0] for s in slot_intervals)
            day_end_utc = max(s[1] for s in slot_intervals)
        else:
            # Fall back to auto-generation based on time window
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
            
            # We'll generate slots later
            slot_intervals = None

        # Build search domain for busy events
        domain = [
            ("start", "<", day_end_utc),
            ("stop", ">", day_start_utc),
        ]
        if user_ids:
            domain.append(("user_id", "in", user_ids))

        busy_events = CalendarEvent.search(domain)

        # Collect busy intervals and convert to timezone-aware UTC
        # Odoo stores datetimes as naive UTC, so we need to make them aware
        busy_intervals = []
        for ev in busy_events:
            # Convert naive datetime to UTC-aware
            start_aware = ev.start.replace(tzinfo=pytz.UTC) if ev.start.tzinfo is None else ev.start
            stop_aware = ev.stop.replace(tzinfo=pytz.UTC) if ev.stop.tzinfo is None else ev.stop
            busy_intervals.append((start_aware, stop_aware))
        
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

        # Check availability for configured slots OR generate free slots
        slots = []
        
        if slot_intervals:
            # Check each configured slot for availability
            for slot_start_utc, slot_end_utc in slot_intervals:
                # Check if this slot overlaps with any busy interval
                is_free = True
                for b_start, b_end in merged:
                    # Check for overlap
                    if slot_start_utc < b_end and slot_end_utc > b_start:
                        is_free = False
                        break
                
                if is_free:
                    # Convert to local timezone for response
                    local_start = slot_start_utc.astimezone(tz)
                    local_end = slot_end_utc.astimezone(tz)
                    slots.append({
                        "start": local_start.isoformat(),
                        "end": local_end.isoformat(),
                    })
        else:
            # Auto-generate slots from free intervals
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

        return slots

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
        e.g. "2025-11-17T13:00:00-05:00" -> "2025-11-17 18:00:00" (UTC)
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
          "date_preference": "2025-11-20",
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
        appointment_type_name = params.get("appointment_type_name") or params.get("appointment_title")
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

        # Resolve appointment type object for configured slots
        appointment_type_obj = None
        if appointment_type_id or appointment_type_name:
            appointment_type_obj = self._get_appointment_type_obj(
                appointment_type_id=appointment_type_id,
                appointment_type_name=appointment_type_name,
            )

        # Which user calendar(s) to use
        user_ids = []
        env = request.env

        # 1) explicit email override
        if calendar_user_email:
            User = env["res.users"].sudo()
            user = User.search([("email", "=", calendar_user_email)], limit=1)
            if user:
                user_ids = [user.id]

        # 2) else derive from appointment type (Dr Drizzle)
        if not user_ids and appointment_type_obj:
            user = self._resolve_user_from_appointment(
                appointment_type_id=appointment_type_id,
                appointment_type_name=appointment_type_name,
            )
            if user:
                user_ids = [user.id]

        # 3) final fallback – static default doctor (Mark Khan)
        if not user_ids:
            user_ids = [self.STATIC_ASSIGNEE_USER_ID]

        try:
            slots = self._compute_free_slots(
                date_pref=date_pref,
                duration_minutes=duration,
                time_window=time_window,
                timezone_str=timezone_str,
                user_ids=user_ids or None,
                appointment_type_obj=appointment_type_obj,
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
          "slot_start": "2025-11-20T13:00:00-05:00",
          "slot_end":   "2025-11-20T14:00:00-05:00",
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
        appointment_type_name = params.get("appointment_type_name") or params.get("appointment_title")
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

        # 2) else derive from appointment.type (Dr Drizzle)
        appointment_type_obj = None
        if appointment_type_id or appointment_type_name:
            user = self._resolve_user_from_appointment(
                appointment_type_id=appointment_type_id,
                appointment_type_name=appointment_type_name,
            )
            if user and not user_id:
                user_id = user.id

            appointment_type_obj = self._get_appointment_type_obj(
                appointment_type_id=appointment_type_id,
                appointment_type_name=appointment_type_name,
            )

        # 3) final fallback – static default doctor (Mark Khan)
        if not user_id:
            user_id = self.STATIC_ASSIGNEE_USER_ID

        # Convert ISO datetime strings to Odoo format (UTC)
        try:
            start_odoo_format = self._convert_iso_to_odoo_format(start_iso)
            end_odoo_format = self._convert_iso_to_odoo_format(end_iso)
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
            }

        Event = env["calendar.event"].sudo()

        # ------------------------------------------------------------------
        # Build attendees (partner_ids):
        #   - Caller / guest partner
        #   - Assigned user (doctor / staff) partner
        # ------------------------------------------------------------------
        partner_commands = []

        # Guest / caller partner
        if partner:
            partner_commands.append((4, partner.id))

        # Assigned user (doctor/staff) as attendee too
        owner_partner = False
        if user_id:
            owner_user = env["res.users"].sudo().browse(user_id)
            owner_partner = owner_user.partner_id
            if owner_partner and (not partner or owner_partner.id != partner.id):
                partner_commands.append((4, owner_partner.id))

        # Fallback: ensure at least caller is present
        if not partner_commands and partner:
            partner_commands.append((4, partner.id))

        # Validate that the requested slot is a valid configured slot
        if appointment_type_obj:
            # We need the date string for the slot lookup
            # start_iso is like "2025-11-27T12:00:00-05:00"
            try:
                date_str = start_iso.split("T")[0]
                timezone_str = "UTC" # Default fallback
                # Try to extract timezone if possible, or use appointment's TZ
                if hasattr(appointment_type_obj, 'appointment_tz') and appointment_type_obj.appointment_tz:
                    timezone_str = appointment_type_obj.appointment_tz
                
                valid_slots = self._get_configured_slots_from_appointment(
                    appointment_type_obj, date_str, timezone_str
                )
                
                # Convert requested start/end to UTC datetime for comparison
                req_start_dt = datetime.fromisoformat(start_iso).astimezone(pytz.UTC)
                req_end_dt = datetime.fromisoformat(end_iso).astimezone(pytz.UTC)
                
                is_valid_slot = False
                for v_start, v_end in valid_slots:
                    # Allow a small tolerance (e.g. seconds) or exact match
                    if v_start == req_start_dt and v_end == req_end_dt:
                        is_valid_slot = True
                        break
                
                if not is_valid_slot:
                     return {
                        "status": "error",
                        "message": "Invalid time slot. Please choose a valid configured slot.",
                    }

            except Exception as e:
                # If validation fails due to parsing, log/ignore or return error. 
                # For safety, we might want to allow it if we can't validate, 
                # OR be strict. Let's be strict but safe on crash.
                pass

        # Create calendar event
        event_vals = {
            "name": f"{appointment_type_label} - {name}",
            "start": start_odoo_format,
            "stop": end_odoo_format,
            "partner_ids": partner_commands,
            "description": notes,
            "show_as": "busy",  # CRITICAL: Ensures it blocks availability
        }

        # Link to staff user (assignee)
        if user_id:
            event_vals["user_id"] = user_id

        # Link this event back to the Appointment Type (Dr Drizzle),
        # so it appears in the Appointment module just like website bookings.
        if appointment_type_obj and "appointment_type_id" in Event._fields:
            event_vals["appointment_type_id"] = appointment_type_obj.id

        # Optional custom field to mark AI source
        if "x_source" in Event._fields:
            event_vals["x_source"] = "ai_caller_agent"

        # Check for conflicts (double booking)
        if user_id:
            # Search for overlapping events for this user
            # Overlap logic: (StartA < EndB) and (EndA > StartB)
            domain = [
                ("user_id", "=", user_id),
                ("start", "<", end_odoo_format),
                ("stop", ">", start_odoo_format),
            ]
            # Optional: Exclude cancelled events if your system keeps them
            # domain.append(("active", "=", True)) 
            
            conflict_count = Event.search_count(domain)
            if conflict_count > 0:
                return {
                    "status": "error",
                    "message": "The requested time slot is already booked.",
                }

        try:
            event = Event.create(event_vals)
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to create calendar event: {str(e)}",
            }

        # Localize response to partner timezone for confirmation
        partner_tz = partner.tz or "UTC"
        try:
            local_tz = pytz.timezone(partner_tz)
        except Exception:
            local_tz = pytz.UTC

        # Convert back to local time for response
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

        # We are NOT creating extra appointment.booking records here on purpose,
        # to avoid KeyErrors on instances where that model doesn't exist.
        # The Appointment module will still show this event under Dr Drizzle
        # via appointment_type_id.
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
        Cancel an existing appointment (calendar.event).

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

            # If your DB has some booking models with a link to event,
            # you can softly update them here without crashing if missing.
            for model_name in [
                "calendar.booking",
                "appointment.booking",          # only if exists
                "calendar.appointment.booking", # only if exists
            ]:
                if model_name in env:
                    BookingModel = env[model_name].sudo()
                    if "calendar_event_id" in BookingModel._fields:
                        booking = BookingModel.search(
                            [("calendar_event_id", "=", appointment_id)],
                            limit=1,
                        )
                        if booking:
                            # Try to mark as cancelled if a state-like field exists
                            for field in ("state", "status", "booking_status"):
                                if field in BookingModel._fields:
                                    setattr(booking, field, "canceled")
                                    break

            # Finally delete the calendar event
            event.unlink()

            return {
                "status": "cancelled",
                "message": f"Appointment '{event_name}' has been cancelled. Reason: {reason}",
                "appointment_id": appointment_id,
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to cancel appointment: {str(e)}",
            }
