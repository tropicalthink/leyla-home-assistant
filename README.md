# Leyla for Home Assistant

For [Leyla](https://leyla.media), the media server for the films and shows
you own.

Your Leyla TVs as media players, your server's streams as sensors, and an
event when a library changes. It talks only to your own Leyla server, on
your network or at the address you give it. Nothing goes through any other
service.

## Install

1. In HACS, open the menu, choose **Custom repositories**, add this
   repository with the type **Integration**, and install **Leyla**.
2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and pick
   **Leyla**.
4. Enter the address you open Leyla at, for example
   `http://192.168.1.20:10001`.
5. Choose **Sign in with a code**. Open the link shown (your server's
   `/link` page) on a phone or computer where you are signed in to Leyla,
   enter the code and approve it. Home Assistant signs in by itself.

You can also paste a token from a device that is already signed in. The
code is better: Home Assistant gets its own sign-in, listed as "Leyla app on
Home Assistant" in Your devices, where you can sign it out at any time.

If the sign-in ends (signed out, or unused for 30 days), Home Assistant asks
you to sign in again.

## What you get

**A media player per TV.** Every TV that runs Leyla signed in to your
account (LG and Samsung, Android TV and Fire TV) appears as a device with a
media player. It shows what plays, the poster, the position and length,
and lets you play, pause, stop, seek, skip to the next episode and, where
the TV owns its volume, set and mute the volume. A TV that is switched off
or closes Leyla stays in Home Assistant as unavailable and comes back when
it opens Leyla again.

`media_player.play_media` starts a title on the TV: `media_content_id` is
the title's id (what follows `/catalog/` in the address of its page in Leyla), or
`<title id>:<file id>` to pick one file. The content type is ignored.

A command from Home Assistant takes control of the TV only for that
command: it releases the TV straight after, so the TV does not keep saying
it is controlled from Home Assistant and your phone takes over again with
its next command.

**Streams sensors (administrators only).** When you sign in as an
administrator you also get **Active streams** and **Active conversions**.
A conversion is a stream whose picture the server re-encodes; a stream it
only repackages counts as a stream. The sensors carry the newest ten sessions as an
attribute (who, which device, what, how).

**An event when a library changes.** `leyla_library_updated` fires when
titles in a library are added, changed or removed, at most once every few
seconds per library:

```yaml
event_type: leyla_library_updated
data:
  config_entry_id: 0123abcd...
  library_id: 5b1e...      # null when changes were missed and the server cannot say where
  library_name: Films       # null when unknown
```

For example, to announce new films:

```yaml
triggers:
  - trigger: event
    event_type: leyla_library_updated
    event_data:
      library_name: Films
actions:
  - action: notify.notify
    data:
      message: Something changed in Films.
```

## Diagnostics

**Download diagnostics** on the integration gives the link's state (connected
or not, why it last closed, the TVs' statuses) without the token, the
server address or account names.

## Development

```sh
uv venv -p 3.14 ~/.cache/leyla-ha-venv
VIRTUAL_ENV=~/.cache/leyla-ha-venv uv pip install -r requirements_test.txt
~/.cache/leyla-ha-venv/bin/pytest -p no:xdist tests
~/.cache/leyla-ha-venv/bin/ruff check custom_components tests
```
