# kindle-dash

An e-ink wall panel for the Bellevue office: a Kindle Paperwhite fetching pre-rendered
frames over plain HTTPS from a public GitHub repo. Seven panels rotate once a minute.

## How it fits together

    Kindle (office, any wifi)  polls dash.conf, fetches one PNG per minute, paints it
             |
             |  HTTPS GET, anonymous - no credentials on the device
             v
    public repo `varunr89/kindle-dash`
      main  = the frames (dash-*.png) + dash.conf, the device's config
      code  = this branch: the renderer and the publisher, fetched by the mini each minute
             ^
             |  git fetch every minute
             |
    Mac mini (at home, always on)  renders the current slot, publishes it, force-pushes
                                   main to a single orphan commit so the repo stays small

The Kindle only ever reads a public URL, so its network is irrelevant: office wifi, a
hotspot, anything with outbound HTTPS. Nothing needs to reach in, and home and office never
talk to each other. The mini needs power and internet, nothing else.

## Changing panels from anywhere

Edit this branch on GitHub. The mini fetches it every minute, and if the revision changed it
renders and publishes **every** frame immediately, so the change is on the wall within a
minute or two. No SSH, no USB, no local checkout.

    add a panel     1. write data_<name>() and body_<name>() in render_panels.py
                    2. register it in BUILDERS and append "<name>" to PANELS
                    3. commit to this branch
                    The launcher republishes all frames and regenerates dash.conf, which is
                    derived from PANELS - so the device's rotation picks it up too.

    edit a panel    change its builder, commit. Only that panel's frame changes.
    remove a panel  take it out of PANELS, commit. Its URL leaves dash.conf with it.
    reorder         reorder PANELS. Order is the rotation order on both ends.
    change cadence  ROTATE_MINUTES. dash.conf's DASH_INTERVAL follows it automatically.

`PANELS` and `ROTATE_MINUTES` are the only knobs for the rotation. Everything else -
the device's URL list, the frame filenames, the interval - is derived from them, because the
device picks its panel from the same clock the publisher does: if the two lists drifted, the
device would fetch a frame nobody had refreshed that minute.

### Why at least six panels

`raw.githubusercontent.com` serves `max-age=300` per path and ignores cache-busters. The
device fetches each path once per rotation, so with `len(PANELS) * ROTATE_MINUTES` under 300
seconds a fetch would be served the cached copy and the panel would silently show a frame up
to five minutes old. At a 1-minute cadence that means six panels minimum. `publish-slot`
prints a warning to `publish.log` if the list gets short enough to break this.

## Credentials

`~/.hermes/kindle-dash.env` on the mini, chmod 600, never in this repo:

    GH_TOKEN=<fine-grained PAT, Contents: write, scoped to this one repo>
    GH_REPO=varunr89/kindle-dash
    GH_MIRROR=dash.png    # optional: also publish a copy at dash.png

Worth being explicit about the trade-off: the mini fetches and runs the code on this branch,
so write access here is code execution on the mini. That is the price of editing panels from
a browser. The token is repo-scoped and lives in a 600 file; if that is not a trade you want,
move this branch to a private repo and have the mini pull it with a read-only deploy key,
leaving the public repo to hold frames only.

## Running it

    ~/bin/kindle-dash-loop.sh --loop     # what launchd runs; install from deploy/
    ~/bin/kindle-dash-loop.sh            # one-shot: pull, refresh if changed, publish slot

The launcher lives outside this checkout on purpose - see the comment at the top of
`deploy/kindle-dash-loop.sh`. After editing it here, re-install it by hand.

Other entry points:

    render_panels.py --panel menu --out /tmp/x.png     render one panel locally
    render_panels.py --publish-slot                    what the scheduler calls each minute
    render_panels.py --publish-all                     render and publish every panel
    render_panels.py --conf                            print the device config
    render_panels.py --urls                            just the frame URLs

Each publish logs `rev=<sha>` so `publish.log` shows which revision drew a frame.

## The device

Kindle Paperwhite 5, jailbroken, SSH as `root` on port 2222 (a self-contained dropbear that
`dashboard.sh` starts on 2223 to survive KOReader shutting down). `dashboard.sh` re-reads
`dash.conf` from `main` every cycle and adopts changes, which is why the panel set, order and
cadence can all change without touching the device.

    dashboard.sh      the device-side loop
    restore.sh        kills the loop and the dropbear
    device-deploy.sh  push device files over USB (writes /mnt/us, which mounts as /Volumes/Kindle)
