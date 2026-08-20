# Tipsoi apps for Odoo

Odoo modules published by [Inovace Technologies](https://tipsoi.ai/).

One folder per app, at the root of this repository.

| App | What it does |
|---|---|
| [`tipsoi_connector`](tipsoi_connector/) | Brings Tipsoi people, devices and attendance into Odoo as native records |

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
