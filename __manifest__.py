# ai_appointment_webhook/__manifest__.py
{
    "name": "AI Appointment Webhook",
    "summary": "Webhook endpoints for AI caller agent to check and book appointments",
    "version": "18.0.1.0.0",
    "author": "IPPBX Group",
    "website": "https://workforcesync.io",
    "category": "Tools",
    "license": "LGPL-3",
    "depends": ["base", "calendar"],
    "data": [
        "security/ir_model_access.csv",
    ],
    "installable": True,
    "application": False,
}
