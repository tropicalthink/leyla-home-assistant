# Leyla for Home Assistant

For [Leyla](https://leyla.media), the media server for the films and shows
you own.

Your Leyla TVs as media players you can browse, search and talk to, your
server's streams, library sizes, updates and problems in Home Assistant, and
an event when a library changes. It talks only to your own Leyla server, on
your network or at the address you give it. Nothing goes through any other
service.

The full guide is at **[leyla.media/home-assistant](https://leyla.media/home-assistant)**.

## Install

1. In HACS, open the menu, choose **Custom repositories**, add this
   repository with the type **Integration**, and install **Leyla**.
2. Restart Home Assistant.
3. A Leyla server on your network shows up under **Settings > Devices &
   services** as discovered: press **Add**. Otherwise choose **Add
   integration**, pick **Leyla** and enter the address you open Leyla at,
   for example `http://192.168.1.20:10001`.
4. Choose **Sign in with a code**. Open the link shown (your server's
   `/link` page) on a phone or computer where you are signed in to Leyla,
   enter the code and approve it. Home Assistant signs in by itself.

You can also paste a token from a device that is already signed in. The
code is better: Home Assistant gets its own sign-in, listed as "Leyla app on
Home Assistant" in Your devices, where you can sign it out at any time. If
the sign-in ends, Home Assistant asks you to sign in again. If the server
moves, **Reconfigure** takes the new address and keeps the sign-in.

## What you get

**A media player per TV.** Every TV that runs Leyla signed in to your
account (LG and Samsung, Android TV, Google TV and Fire TV) appears as a
device with a media player: what plays, the poster, position and length,
play, pause, stop, seek, next episode and, where the TV owns it, volume.

- **Browse** Continue watching and every library, down to seasons and
  episodes, from the media browser, and play any of it on the TV. Playing a
  series goes on where you left off.
- **Search** by title or by what it is about. Voice assistants use it:
  "play Arrival on the living room TV".
- **Audio** and **Subtitles** pickers switch the track of what plays, from
  the tracks the TV reports.
- `media_player.play_media` takes a title's id (what follows `/catalog/` in
  its address in Leyla), or `<title id>:<file id>` for one file.

A command from Home Assistant takes control of the TV only for that command
and releases it straight after. A TV that is switched off stays as
unavailable and comes back when it opens Leyla again; one that is gone for
good can be deleted from its device page.

**On the server's device:**

- **<library> titles**: how many films and series each library holds.
- **Library updated**: an event entity that fires when titles in a library
  are added, changed or removed (also on the bus as
  `leyla_library_updated`, with `library_id` and `library_name`).

**For an administrator's sign-in, also:**

- **Leyla server** update entity: the running version, the newer one, how
  far the fetch has got, and **Install** to update now. Leyla rehearses a
  release on your own library before it installs it.
- **Scan <library>** buttons.
- **Active streams** and **Active conversions** sensors, with the newest ten
  sessions as an attribute.
- **Repairs**: what Leyla's server says needs your attention (a full disk, a
  backup that stopped) appears in Home Assistant's Repairs, in Leyla's own
  words. **Set aside** there sets it aside in Leyla too.

## Diagnostics

**Download diagnostics** gives the link's state, the TVs' statuses, the
update state and the kinds of open findings, without the token, the server
address or account names.

## Removing it

Delete the integration under **Settings > Devices & services**. Then, in
Leyla, sign out "Leyla app on Home Assistant" under Your devices.

## Development

```sh
uv venv -p 3.14 ~/.cache/leyla-ha-venv
VIRTUAL_ENV=~/.cache/leyla-ha-venv uv pip install -r requirements_test.txt
~/.cache/leyla-ha-venv/bin/pytest -p no:xdist tests --cov=custom_components/leyla
~/.cache/leyla-ha-venv/bin/ruff check custom_components tests
~/.cache/leyla-ha-venv/bin/mypy
```
