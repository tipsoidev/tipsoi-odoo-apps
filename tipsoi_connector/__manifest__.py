# -*- coding: utf-8 -*-
{
    "name": "Tipsoi Connector",
    "summary": "Bring Tipsoi people, devices and attendance into Odoo as native records",
    "description": """
Tipsoi Connector
================

Brings Tipsoi people, devices and attendance into Odoo as native records, over REST.
Direct API integration -- no middleware to run, nothing extra to keep alive.

About Tipsoi
------------

Tipsoi combines AI-powered biometric attendance devices with a complete employee
management platform -- hardware and software as one system. "One system. Total control."

The platform covers a centralized workforce dashboard, AI face recognition with sub-second
identification, shift scheduling and rostering, leave management, field-team GPS tracking,
biometric access control, visitor and parking management, notifications by SMS, email and
push, and payroll integration. Its devices use facial recognition, fingerprint and RFID,
with infrared and RGB cameras. It runs from small businesses to multinationals, across
manufacturing, healthcare, education, government, retail, financial services, agriculture,
construction and hospitality.

This module is for organisations already running Tipsoi who also run Odoo.

One system of record
--------------------

One backend record per client, and its type selects the whole pipeline:

* Device Portal only -- raw punches from the device API, paired in Odoo.
* Tipsoi app (HRM) -- employees, org masters and attendance from the HRM API.

The two never mix, so person records and employee identifiers are managed in exactly one
place. That is what stops the two systems disagreeing about who someone is.

Links
-----

* Tipsoi: https://tipsoi.ai/
* Inovace Technologies, who build Tipsoi: https://inovacetech.com/
* Support: support@tipsoi.ai
""",
    "version": "16.0.1.0.0",
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
        # Order matters here, and not only stylistically: the backend form carries stat
        # buttons that resolve other actions' xml ids with %(...)d at data-load time, so
        # every action it points at has to exist before it is parsed. Menus come last for
        # the same reason.
        "views/tipsoi_device_views.xml",
        "views/tipsoi_punch_log_views.xml",
        "views/tipsoi_day_attendance_views.xml",
        "views/hr_employee_views.xml",
        "views/tipsoi_sync_run_views.xml",
        "views/tipsoi_backend_views.xml",
        "views/tipsoi_wizard_views.xml",
        "views/tipsoi_menus.xml",
        "data/ir_cron.xml",
    ],
    "installable": True,
    "application": True,
}
