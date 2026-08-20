# Tipsoi apps for Odoo

Odoo modules published by [Inovace Technologies](https://inovacetech.com/), the company
behind [Tipsoi](https://tipsoi.ai/).

One folder per app, at the root of this repository.

| App | What it does |
|---|---|
| [`tipsoi_connector`](tipsoi_connector/) | Brings Tipsoi people, devices and attendance into Odoo as native records |

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
