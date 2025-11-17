# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
from datetime import datetime, time, timedelta
import pytz
import json


class AiAppointmentController(http.Controller):

    # ==========================================================================
    # SAFE PAYLOAD PARSER  (Handles JSON, JSON-RPC, Postman etc)
    # ==========================================================================
    def _get_payload(self, params):
        if params:
            return params

        data = {}
        try:
            raw = request.httprequest.data
            if raw:
                data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}

        # JSON-RPC envelope?
        if isinstance(data, dict) and "params" in data and isinstance(data["params"], dict):
            data = data["params"]

        return data

    # ==========================================================================
    # GET APPOINTMENT TYPE (Model: appointment.type)
    # ==========================================================================
    def _get_appt_type(self, appt_id=None, appt_name=None):
        AppointmentType = request.env["appointment.type"].sudo()

        rec = False

        if appt_id:
            try:
                rec = AppointmentType.browse(int(appt_id))
                if not rec.exists():
                    rec = False
            except:
                rec = False

        if not rec and appt_name:
            rec = AppointmentType.search([("name", "=", appt_name)], limit=1)

        return rec

    # ==========================================================================
    # DETERMINE STAFF USER (appointment.type.staff_user_ids)
    # ==========================================================================
    def _resolve_staff_user(self, appt_type):
        if not appt_type:
            return False

        if hasattr(appt_type, "staff_user_ids") and appt_type.staff_user_ids:
            return appt_type.staff_user_ids[:1]

        return False

    # ==========================================================================
    # CONVERT WORK WINDOW BASED ON TIME WINDOW
    # ==========================================================================
    def _get_window_hours(self, window):
        if window == "morning":
            return (time(9, 0), time(12, 0))
        elif window == "afternoon":
            return (time(13, 0), time(17, 0))
        return (time(9, 0), time(17, 0))  # default

    # ==========================================================================
    # COMPUTE FREE SLOTS
    # ==========================================================================
    def _compute_free_slots(self, date_pref, duration, time_window, tz, user_id):

        CalendarEvent = request.env["calendar.event"].sudo()

        year, month, day = map(int, date_pref.split("-"))
        try:
            tz_local = pytz.timezone(tz)
        except:
            tz_local = pytz.UTC

        wh_start, wh_end = self._get_window_hours(time_window)

        day_start_local = tz_local.localize(datetime(year, month, day, wh_start.hour, wh_start.minute))
        day_end_local = tz_local.localize(datetime(year, month, day, wh_end.hour, wh_end.minute))

        day_start_utc = day_start_local.astimezone(pytz.UTC)
        day_end_utc = day_end_local.astimezone(pytz.UTC)

        domain = [
            ("start", "<", day_end_utc),
            ("stop", ">", day_start_utc),
        ]
        if user_id:
            domain.append(("user_id", "=", user_id))

        busy = CalendarEvent.search(domain)

        # Merge intervals
        busy_intervals = sorted([(ev.start, ev.stop) for ev in busy], key=lambda x: x[0])

        merged = []
        for rng in busy_intervals:
            if not merged:
                merged.append([rng[0], rng[1]])
            else:
                last = merged[-1]
                if rng[0] <= last[1]:
                    last[1] = max(last[1], rng[1])
                else:
                    merged.append([rng[0], rng[1]])

        # Find free blocks
        free = []
        current = day_start_utc
        for b in merged:
            if b[0] > current:
                free.append((current, b[0]))
            current = max(current, b[1])
        if current < day_end_utc:
            free.append((current, day_end_utc))

        # Split into slots
        delta = timedelta(minutes=duration)
        slots = []
        for f in free:
            start = f[0]
            while start + delta <= f[1]:
                end = start + delta
                slots.append({
                    "start": start.astimezone(tz_local).isoformat(),
                    "end": end.astimezone(tz_local).isoformat(),
                })
                start += delta

        return slots[:10]

    # ==========================================================================
    # CHECK AVAILABILITY
    # ==========================================================================
    @http.route("/ai/appointments/check", type="json", auth="public", csrf=False, methods=["POST"])
    def check_availability(self, **params):

        params = self._get_payload(params)

        date_pref = params.get("date_preference")
        if not date_pref:
            return {"status": "error", "message": "date_preference is required"}

        appt_id = params.get("appointment_type_id")
        appt_name = params.get("appointment_type_name")
        time_window = params.get("time_window", "any")
        tz = params.get("timezone", "UTC")
        duration = int(params.get("duration_minutes", 30))

        appt_type = self._get_appt_type(appt_id, appt_name)
        if not appt_type:
            return {"status": "error", "message": "Appointment type not found"}

        staff = self._resolve_staff_user(appt_type)
        if not staff:
            return {"status": "error", "message": "No staff assigned to this appointment type"}

        slots = self._compute_free_slots(date_pref, duration, time_window, tz, staff.id)

        return {"status": "ok", "slots": slots}

    # ==========================================================================
    # BOOK APPOINTMENT
    # ==========================================================================
    @http.route("/ai/appointments/book", type="json", auth="public", csrf=False, methods=["POST"])
    def book_appointment(self, **params):

        params = self._get_payload(params)

        name = params.get("caller_name", "Unknown")
        email = params.get("caller_email")
        phone = params.get("caller_phone")
        notes = params.get("notes", "")

        start_iso = params.get("slot_start")
        end_iso = params.get("slot_end")
        if not start_iso or not end_iso:
            return {"status": "error", "message": "slot_start and slot_end required"}

        appt_id = params.get("appointment_type_id")
        appt_name = params.get("appointment_type_name")

        appt_type = self._get_appt_type(appt_id, appt_name)
        if not appt_type:
            return {"status": "error", "message": "Appointment type not found"}

        staff = self._resolve_staff_user(appt_type)

        # Convert slot ISO → UTC → Odoo datetime
        start_dt = datetime.fromisoformat(start_iso).astimezone(pytz.UTC)
        end_dt = datetime.fromisoformat(end_iso).astimezone(pytz.UTC)

        start_odoo = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_odoo = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Create or find partner
        Partner = request.env["res.partner"].sudo()
        partner = False
        if email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if not partner and phone:
            partner = Partner.search([("phone", "=", phone)], limit=1)

        if not partner:
            partner = Partner.create({
                "name": name,
                "email": email,
                "phone": phone,
            })

        # Create Calendar Event
        Event = request.env["calendar.event"].sudo()
        event = Event.create({
            "name": f"{appt_type.name} - {name}",
            "start": start_odoo,
            "stop": end_odoo,
            "user_id": staff.id,
            "partner_ids": [(4, partner.id)],
            "appointment_type_id": appt_type.id,
            "description": notes,
        })

        # Create Appointment Booking
        Booking = request.env["appointment.booking"].sudo()
        booking = Booking.create({
            "appointment_type_id": appt_type.id,
            "calendar_event_id": event.id,
            "partner_id": partner.id,
            "start": start_odoo,
            "stop": end_odoo,
            "state": "booked",
        })

        # Trigger Odoo confirmation email
        try:
            template = request.env.ref("appointment.mail_template_data_app_appointment")
            template.sudo().send_mail(booking.id, force_send=True)
        except:
            pass  # email is optional

        return {
            "status": "confirmed",
            "appointment_id": event.id,
            "booking_id": booking.id,
            "final_start": start_dt.isoformat(),
            "final_end": end_dt.isoformat(),
            "partner_id": partner.id,
            "appointment_name": event.name,
            "message": "Appointment successfully booked",
        }

    # ==========================================================================
    # CANCEL APPOINTMENT
    # ==========================================================================
    @http.route("/ai/appointments/cancel", type="json", auth="public", csrf=False, methods=["POST"])
    def cancel_appointment(self, **params):

        params = self._get_payload(params)

        appt_id = params.get("appointment_id")
        if not appt_id:
            return {"status": "error", "message": "appointment_id required"}

        Event = request.env["calendar.event"].sudo()
        event = Event.browse(int(appt_id))
        if not event.exists():
            return {"status": "error", "message": "Appointment not found"}

        # Cancel booking
        Booking = request.env["appointment.booking"].sudo()
        booking = Booking.search([("calendar_event_id", "=", event.id)], limit=1)
        if booking:
            booking.state = "cancelled"

        event.unlink()

        return {"status": "cancelled", "appointment_id": appt_id}
