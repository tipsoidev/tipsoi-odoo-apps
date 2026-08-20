# -*- coding: utf-8 -*-
{
    "name": "Tipsoi Connector",
    "summary": "Bring Tipsoi people, devices and attendance into Odoo as native records",
    "description": """
Tipsoi Connector
================

Pulls devices, people and attendance out of Tipsoi over REST into native Odoo records.
Direct API integration -- no Kafka, no middleware.

One backend record per client, and its type selects the whole pipeline:

* **Device Portal only** -- raw punches from the device API, paired here.
* **Tipsoi app (HRM)** -- employees, masters and attendance from the HRM API.

The two never mix, so person records and employee identifiers are managed in exactly one
place -- which is what stops the two systems disagreeing about who someone is.

Learn more at https://tipsoi.ai/
""",
    "version": "17.0.1.0.0",
    "category": "Human Resources/Attendances",
    "author": "Inovace Technologies",
    "website": "https://tipsoi.ai/",
    "support": "support@tipsoi.ai",
    "images": ["static/description/cover.png"],
    "license": "LGPL-3",
    "depends": ["base", "hr", "hr_attendance"],
    "external_dependencies": {"python": ["requests", "pytz"]},
    "data": [
        "security/tipsoi_security.xml",
        "security/ir.model.access.csv",
        "views/tipsoi_backend_views.xml",
        "views/tipsoi_sync_run_views.xml",
        "views/tipsoi_menus.xml",
    ],
    "installable": True,
    "application": True,
}
