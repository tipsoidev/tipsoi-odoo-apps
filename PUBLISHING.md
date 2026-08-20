# Publishing to Odoo Apps

Internal note. Not part of any module.

## Why GitHub and not our GitLab

`gitlab.inovacebd.com` resolves to a public IP and serves HTTPS, but **no SSH port is
exposed** (22, 2222, 2022, 8022 and 222 all closed/filtered as of 2026-08-21). Odoo Apps
registers repositories in `ssh://` form, so it cannot reach our GitLab without opening a
port on our infrastructure. We are not doing that, so this repository lives on GitHub
instead.

Development can stay on GitLab; GitLab push-mirroring can keep the GitHub copy in step
(Settings → Repository → Mirroring repositories). Nothing sensitive is in here — no
internal file references, no security notes, no planning docs. Those stay in
`tipsoi-odoo-connector/`.

## One-time setup

Create `tipsoi-odoo-apps` under https://github.com/tipsoidev, then:

```sh
cd tipsoi-odoo-apps
git push -u origin 18.0
git push    origin 17.0
```

The remote is already configured as:

```
ssh://git@github.com/tipsoidev/tipsoi-odoo-apps.git
```

### Pushing from the HRM box

Outbound tcp/22 is blocked from that machine, but GitHub's alternative SSH endpoint on
443 is reachable. Add to `~/.ssh/config`:

```
Host github.com
  Hostname ssh.github.com
  Port 443
  User git
```

Or push over HTTPS with a personal access token instead. This only affects pushing from
that host — Odoo fetches from its own servers and is unaffected.

## Registering with Odoo

At https://apps.odoo.com/apps/upload, register **once per series**, with the branch name
matching the series exactly:

```
ssh://git@github.com/tipsoidev/tipsoi-odoo-apps.git#18.0
ssh://git@github.com/tipsoidev/tipsoi-odoo-apps.git#17.0
```

**If the GitHub repository is private**, Odoo shows a deploy key during registration —
add it under Settings → Deploy keys (read access is enough). If public, no key is needed.

## Checklist before registering

- [x] One folder per app at the repository root (`tipsoi_connector/`)
- [x] Branch names match the series (`18.0`, `17.0`)
- [x] `static/description/icon.png` present (512×512) — otherwise Apps shows a white cube
- [x] `static/description/index.html` present
- [x] `LICENSE` present and matching the manifest's `license` (LGPL-3)
- [x] Manifest carries `name`, `summary`, `version`, `author`, `website`, `support`, `category`
- [x] Installs clean and tests pass on both series
- [ ] The module actually syncs something — **currently the connection layer only**

That last box is unticked on purpose. Phases 2–7 add the employee, device, punch,
attendance and photo jobs. A listing published before then installs and does nothing.

## Re-verifying before a release

```sh
docker run -d --name pg --network v -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo \
  -e POSTGRES_DB=postgres postgres:16-alpine

for V in 18 17; do
  docker run --rm --network v -v "$PWD:/mnt/extra-addons:ro" \
    -e HOST=pg -e USER=odoo -e PASSWORD=odoo odoo:$V \
    odoo -d t$V -i tipsoi_connector \
      --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons \
      --without-demo=all --test-enable --test-tags=/tipsoi_connector --stop-after-init
done
```

Use `--test-tags=/tipsoi_connector`; plain `--test-enable` runs the whole dependency
chain and takes over ten minutes. Check out the `17.0` branch for the 17 run.
