# Tipsoi apps for Odoo

Odoo modules published by [Inovace Technologies](https://inovacetech.com/), the company
behind [Tipsoi](https://tipsoi.ai/).

One folder per app, at the root of this repository.

| App | What it does |
|---|---|
| [`tipsoi_connector`](tipsoi_connector/) | Brings Tipsoi people, devices and attendance into Odoo as native records |

## One system of record

Tipsoi comes in two shapes. Some customers run the full Tipsoi app; others run the Device
Portal alone. You choose which one the connector talks to, and it talks to that one only.

That restraint is the connector's central design rule rather than a limitation. If two
systems both believe they own a person record, employee IDs drift apart and attendance
ends up attached to the wrong people. So the connector refuses to be configured against
both, tells you when the mode you chose does not match what your Tipsoi project actually
has, and enforces the separation at the HTTP layer — with a test whose only job is to
assert that a backend in one mode issues zero requests to the other API.

| | Device Portal | Tipsoi app |
|---|---|---|
| **Attendance** | Raw punches, paired in Odoo — overnight shifts, breaks, duplicate reads and odd punch counts all handled here | The day rows Tipsoi has already computed, with shift context, late/early flags and overtime |
| **Employees** | Name, identifier and card. **Odoo owns** job title, department and manager — Tipsoi holds no such fields for this topology | Department, designation, shift group, workplace and subsidiary all come from Tipsoi, with the sync ids write-back needs |
| **Devices** | Reader list plus live MQTT connection state | Reader list with connection state included |
| **Write back** | Create and update people, allocate, revoke, upload photos, soft-delete | The same, through the Tipsoi app, which propagates to the devices itself |
| **Departures** | Soft delete — punch history keeps its subject | A real status change: resigned or terminated, with the date |

## What runs on a schedule

Nine scheduled jobs ship enabled, and each one selects only backends that have tested
successfully and asserts the mode it was written for — so nothing happens until a backend
is configured. Devices every 15 minutes; punches and pairing every 5; the Tipsoi app's
attendance window and day import every 15; employees, photo uploads and queued write-backs
hourly; and a nightly vacuum that drops staging rows and audit rows once they have
outlived their usefulness.

Every run is one row in **Sync Runs** with the window it asked for and counts of what was
fetched, created, updated, skipped and failed. It is the first place to look when a number
seems wrong.

## About Tipsoi

[Tipsoi](https://tipsoi.ai/) combines AI-powered biometric attendance devices with a
complete employee management platform — hardware and software as one system.
*"One system. Total control."*

The platform covers a centralized workforce dashboard, AI face recognition with sub-second
identification, shift scheduling and rostering, leave management, field-team GPS tracking,
biometric access control, visitor and parking management, notifications by SMS, email and
push, and payroll integration. Its devices use facial recognition, fingerprint and RFID,
with infrared and RGB cameras.

It runs everywhere from small businesses to multinationals — manufacturing, healthcare,
education, government, retail, financial services, agriculture, construction, hospitality.

These Odoo modules are for organisations already running Tipsoi who also run Odoo, and
want the two to agree about who their people are and when they worked.

## About Inovace Technologies

[Inovace Technologies](https://inovacetech.com/) builds Tipsoi. Contact:
support@tipsoi.ai

## Branches

One branch per Odoo series, named to match:

- `18.0` — Odoo 18.0
- `17.0` — Odoo 17.0

The Python, security and test layers are identical across branches; only the list-view
tag, the action `view_mode` and the manifest version differ.

## Installing

Clone the branch matching your Odoo series into your addons path, then update the app
list and install **Tipsoi Connector**. It needs Odoo's **Employees** and **Attendances**
apps, plus an active Tipsoi account with API credentials.

## Licence

LGPL-3. See [LICENSE](LICENSE).

## Support

support@tipsoi.ai · https://tipsoi.ai/
