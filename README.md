# lofi-bot 🎧

A small self-healing Discord bot that sits in one voice channel 24/7 and streams lo-fi
internet radio. No API keys, no yt-dlp, no scheduled downtime — just ffmpeg decoding a
handful of verified stations and a supervisor that puts the music back on when anything
falls over.

```
☕ Chill Area
  🔊 Lofi 24/7        streaming laut.fm lofi
```

* `discord.py 2.7.1` + `davey 0.1.7` (DAVE / E2EE voice)
* `ffmpeg` → PCM → Opus, one `AudioSource`, 50 frames/s
* systemd unit with `Restart=always`, survives reboots and kicks

---

## Features

- **24/7 playback** from five rotating lo-fi stations — no YouTube, so no token, no
  extractor breakage, no per-track API calls.
- **Self-healing supervisor** at three layers: `after=` callback when ffmpeg dies, a
  watchdog on frame counters, and `on_voice_state_update` when the bot is kicked or moved.
- **Station rotation** when a stream ends on its own, with a two-strike rule so a wedged
  ffmpeg pipe replays the same station instead of bouncing the channel around.
- **DAVE-aware voice handling**: waits for the MLS handshake before playing, detects a
  wedged session, and renegotiates by rejoining from scratch (rate limited).
- **Two command surfaces** with identical behaviour: slash commands and a `l!` prefix.
- **Diagnostics in Discord**: `/status` reports frames, E2EE state, listeners, RAM, uptime.

## Commands

Everything is available both as a slash command and as a `l!` prefix command.

| Command | What it does |
|---|---|
| `/help` · `l!help` | Command menu |
| `/station [name]` · `l!station <key>` | Switch station, or list them if no argument |
| `/now` · `l!now` | What is playing right now (with the spinning-vinyl embed) |
| `/next` · `l!next` | Jump to the next station |
| `/pause` · `/resume` | Pause / resume playback |
| `/volume <0-150>` | Live volume, applied to the running stream |
| `/status` · `l!status` | Diagnostics: frames, E2EE, voice state, listeners, RAM |
| `/join` · `/leave` | Join or leave the configured voice channel |

## Requirements

| | |
|---|---|
| Python | 3.10+ (tested on 3.14) |
| System libs | `ffmpeg`, `libopus0` |
| Network | outbound UDP (Discord voice), outbound HTTPS for the streams |

```bash
sudo apt update && sudo apt install -y ffmpeg libopus0 python3-venv
```

Behind a NAT without UDP egress the bot will connect to voice and stay silent — voice is
UDP, and no amount of retrying fixes a blocked port. Check before deploying.

## Quick start

1. **Create the Discord application.** [discord.com/developers/applications](https://discord.com/developers/applications)
   → *New Application* → *Bot* → *Reset Token*. Copy the token.
   Do **not** reuse an existing bot's token — two gateway connections for the same identity
   evict each other.

2. **Invite it** with `bot` + `applications.commands` scopes and the permissions it needs:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=3263488
   ```

   `3263488` = View Channel, Send Messages, Embed Links, Attach Files, Read Message History,
   Connect, Speak. (`applications.commands` is required for slash commands to sync.)

3. **Clone and install.**

   ```bash
   git clone https://github.com/YOU/lofi-bot.git /opt/lofi-bot
   cd /opt/lofi-bot
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

4. **Configure.**

   ```bash
   cp .env.example .env
   chmod 600 .env          # it holds the bot token
   $EDITOR .env            # token, guild id, voice channel id
   ```

   Enable *Message Content Intent* in the dev portal if you want the `l!` prefix commands;
   without it the bot automatically falls back to slash-only (and says so in the log).

5. **Run it.**

   ```bash
   set -a; . ./.env; set +a
   .venv/bin/python bot.py
   ```

   You should see `logged in as …`, `joined voice channel …` and a `heartbeat: … frames=…`
   line every minute. Press `Ctrl-C`, then set it up as a service.

## Run as a service

```bash
sudo cp lofi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lofi-bot
journalctl -u lofi-bot -f
```

The unit reads `/opt/lofi-bot/lofi.env` and restarts on any exit. Adjust the paths in the
unit if you cloned somewhere else, and drop `User=root` down to a dedicated user.

## Configuration

All configuration is environment variables, read from the env file by systemd.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | — | Bot token |
| `GUILD_ID` | yes | — | Server (guild) ID |
| `VOICE_CHANNEL_ID` | yes | — | Voice channel to sit in |
| `BOT_PREFIX` | no | `l!` | Prefix for text commands |
| `PREFIX_ENABLED` | no | `1` | `0` = slash only (never asks for Message Content) |
| `DEFAULT_VOLUME` | no | `0.8` | `VolumeTransformer` multiplier, 0.0–1.5 |
| `BOT_ACTIVITY` | no | `Lo-fi Radio 24/7` | "Listening to …" presence text |
| `FFMPEG_BIN` | no | `/usr/bin/ffmpeg` | ffmpeg binary |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` logs the raw voice socket frames |
| `VOICE_DAVE` | no | `native` | Escape hatch; `legacy` is known to fail with close code `4017` |

## Stations

Stations live in `STATIONS` at the top of `bot.py` — a key, a display name and a stream URL:

```python
STATIONS = {
    "lautfm": ("laut.fm lofi", "https://stream.laut.fm/lofi"),
    ...
}
```

Keys are what users type, `ORDER` is the rotation order. Test any URL before trusting it:

```bash
python3 tools/check_stations.py                # every station in bot.py
python3 tools/check_stations.py URL [URL ...]  # specific URLs
python3 tools/check_stations.py --search       # candidate stations from radio-browser.info
```

A healthy stream decodes to ~`960000` bytes for 5 s of 48 kHz stereo s16. Stations rot: a
URL that works today can return `502` tomorrow, so re-run the check when the channel goes
quiet instead of assuming the bot is broken.

## Troubleshooting

**The bot is in the channel, the ring is green, nobody hears anything.**
Check `/status` → `🔐 E2EE`. Discord negotiates DAVE (E2EE) on voice; while
`can_encrypt` is `false` the frames go out unencrypted and every modern client drops them
silently. Two cases:

- **Nobody else in the channel.** Expected. Discord only forms the MLS group when a human
  client is present, so `can_encrypt` stays `false` until someone joins, and the handshake
  completes about a second later. This is why the watchdog refuses to rejoin-thrash when
  the channel is empty.
- **A listener is present and it stays `false`.** The MLS session is wedged; the supervisor
  notices after ~30 s and rejoins from scratch to renegotiate the keys.

**`davey` matters.** `davey 0.2.0` is newer than `discord.py 2.7.1` and leaves the MLS
session permanently `ready=False` — the bot looks like it is talking, and nobody hears it.
Pin `davey==0.1.7`. Do not "fix" it by forcing `max_dave_protocol_version=0`: on E2EE
channels the voice socket then closes with code `4017`.

**Slash commands do not appear.** The bot was invited with `scope=bot` only. It needs
`bot applications.commands`; guild-scoped sync is instant, global sync lags behind.

**`PrivilegedIntentsRequired` at startup.** Message Content is off in the dev portal. Flip it
on, or set `PREFIX_ENABLED=0` and use slash commands only.

**The stream stops and the log says nothing.** ffmpeg runs with `-loglevel error`; reproduce
manually with `ffmpeg -v info -i URL -t 5 -f null -` to see the real reason (404, 403,
timeout). Usually the station died and the rotation already moved on.

**The bot joins and leaves voice every ~15 seconds.** That is a rejoin loop, and the cause is
self-inflicted: `_hard_rejoin()` disconnects, the bot's own voice state then reads
`channel=None`, and if `on_voice_state_update` treats that as a kick it requests *another*
rejoin — each cycle killing the stream. Two guards are in place: the teardown sets
`_tearing_down` so its own disconnect is ignored, and the rejoin event is cleared after the
teardown. `tools/test_rejoin_loop.py` is the regression test for exactly this. Keep both if
you rewrite the voice plumbing.

**Choppy audio, `can_encrypt` toggling.** Normal during a DAVE re-key (someone joins or
leaves). It recovers in a few seconds; the bot deliberately does not restart the station
for it.

## Tests

```bash
.venv/bin/python -m unittest tools/test_rejoin_loop.py -v   # rejoin-loop regression
python3 tools/check_stations.py                             # every station decodes audio
journalctl -u lofi-bot -f | grep -E "heartbeat|stopped"     # live: 50 fps means healthy
```

## Project layout

```
bot.py                    bot: source, supervisor, commands, embeds
requirements.txt          pinned discord.py + davey
lofi-bot.service          hardened systemd unit
.env.example              configuration template (copy to .env)
tools/check_stations.py   decode-test stream URLs before trusting them
tools/test_rejoin_loop.py regression test for the rejoin feedback loop
tools/make_vinyl.py       regenerate assets/vinyl.gif (Pillow), build-time script
assets/vinyl.gif          spinning-vinyl thumbnail for /now
```

## License

MIT. The audio is third-party internet radio — this project only plays it, and you are
responsible for respecting each station's terms.
