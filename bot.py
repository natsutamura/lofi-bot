#!/usr/bin/env python3
"""24/7 lo-fi radio bot for one Discord voice channel.

Streams internet radio and self-heals: reconnects the gateway/voice socket, rejoins
when kicked or moved, rotates stations when a stream dies or stalls.

Controls (prefix ``l!`` and slash ``/lofi``):
    help · station [name] · now · next · pause · resume · volume [0-150] · status · join · leave
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import time
from dataclasses import dataclass

import discord
from discord import app_commands

log = logging.getLogger("lofi")

GUILD_ID = int(os.environ["GUILD_ID"])
VOICE_CHANNEL_ID = int(os.environ["VOICE_CHANNEL_ID"])
TOKEN = os.environ["DISCORD_TOKEN"]
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
PREFIX = os.environ.get("BOT_PREFIX", "l!")
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
VINYL_GIF = os.path.join(ASSETS, "vinyl.gif")


@dataclass
class Payload:
    """An embed plus an optional attachment (the spinning vinyl thumbnail)."""
    embed: discord.Embed
    file: discord.File | None = None

# key -> (display name, stream url). Keys are what users type; keep them short.
STATIONS: dict[str, tuple[str, str]] = {
    "lautfm":   ("laut.fm lofi",        "https://stream.laut.fm/lofi"),
    "isekoi":   ("ISEKOI chill (320k)", "https://public.isekoi-radio.com/listen/chill/radio.mp3"),
    "litt":     ("LITT Live lofi",      "https://das-sa39.cdnstream1.com/5582_128"),
    "chillhop": ("I Love Chillhop",     "https://ilm.stream18.radiohost.de/ilm_ilovechillhop_mp3-192"),
    "moezeno":  ("MoE Lofi",            "http://stream.zeno.fm/3u1qndyk8rhvv"),
}
ORDER = list(STATIONS)

FFMPEG_BEFORE = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-analyzeduration 10M -probesize 10M"
)
FFMPEG_OPTS = "-vn -loglevel error"

CHECK_SECONDS = 10.0
SILENT_RESTART_SECONDS = 45
DAVE_STUCK_SECONDS = 30   # can_encrypt False this long while playing -> nothing is audible
FRAME_STALL_SECONDS = 30  # source.frames frozen while vc.is_playing() still says True


def load_opus() -> None:
    if discord.opus.is_loaded():
        return
    for path in ("/usr/lib/x86_64-linux-gnu/libopus.so.0", "libopus.so.0", "libopus.so"):
        try:
            discord.opus.load_opus(path)
            if discord.opus.is_loaded():
                log.info("opus loaded (%s)", path)
                return
        except Exception as exc:  # noqa: BLE001
            log.debug("opus load failed for %s: %s", path, exc)
    raise RuntimeError("libopus could not be loaded — voice would not work")


def apply_voice_compat() -> None:
    """Force legacy (non-DAVE) voice when asked via VOICE_DAVE=legacy.

    Kept as an escape hatch: Discord rejects protocol 0 on E2EE channels with close
    code 4017, so the default stays ``native``.
    """
    if os.environ.get("VOICE_DAVE", "native").lower() == "native":
        return
    from discord import voice_state as vs

    vs.VoiceConnectionState.max_dave_protocol_version = property(lambda self: 0)

    async def _noop(self) -> None:  # pragma: no cover
        return None

    vs.VoiceConnectionState.reinit_dave_session = _noop
    log.warning("DAVE forced to legacy (expect 4017 on E2EE channels)")


class RadioSource(discord.AudioSource):
    """FFmpeg PCM -> volume control + frame counting (proof that audio flows)."""

    def __init__(self, url: str, volume: float) -> None:
        self._pcm = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS, executable=FFMPEG_BIN
            ),
            volume=volume,
        )
        self.frames = 0

    def read(self) -> bytes:
        data = self._pcm.read()
        if data:
            self.frames += 1
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._pcm.cleanup()

    @property
    def volume(self) -> float:
        return self._pcm.volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._pcm.volume = value


class LofiBot(discord.Client):
    def __init__(self, *, use_prefix: bool = True) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.message_content = use_prefix   # privileged: needs the portal toggle
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.use_prefix = use_prefix
        self.station_key: str = ORDER[0]
        self.volume: float = float(os.environ.get("DEFAULT_VOLUME", 0.8))
        self._skip = False
        self._paused = False
        self._source: RadioSource | None = None
        self._task: asyncio.Task | None = None
        self._rejoin = asyncio.Event()
        self._last_ready: bool | None = None
        self._stalls: int = 0
        self._last_rejoin_try: float = 0.0
        self._dave_warned: bool = False
        self._tearing_down: bool = False   # our own disconnect is not a kick
        self._rejoins: list[float] = []    # rejoin timestamps, for storm backoff
        self.started_at: float = time.time()
        self._stream_started: float = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        self._register_slash()
        self.tree.on_error = self._on_app_error
        guild = discord.Object(id=GUILD_ID)
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d slash commands to guild %s", len(synced), GUILD_ID)
        except Exception as exc:  # noqa: BLE001
            log.error("slash sync failed (bot invited without applications.commands?): %s", exc)

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, self.user.id)
        if not self.use_prefix:
            log.warning("prefix commands disabled — enable Message Content in the dev portal to use %s", PREFIX)
        await self._set_presence()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervisor(), name="supervisor")

    async def _set_presence(self) -> None:
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=os.environ.get("BOT_ACTIVITY", "Lo-fi Radio 24/7"),
                ),
                status=discord.Status.online,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set presence: %s", exc)

    async def on_voice_state_update(self, member, before, after) -> None:
        if not self.user or member.id != self.user.id:
            return
        # Our own teardown makes the channel go None too. Reacting to it sets the very
        # event that triggers the next teardown, i.e. a rejoin loop every ~15 s.
        if after.channel is None and not self._tearing_down:
            log.warning(
                "voice connection lost (was in %s, now None) — will rejoin",
                getattr(before.channel, "name", None),
            )
            self._rejoin.set()

    async def _respond(self, interaction: discord.Interaction, **kw) -> None:
        """Ack fast: an attachment upload can take longer than Discord's 3 s window."""
        if kw.get("file") is not None:
            await interaction.response.defer(thinking=False)
            await interaction.followup.send(**kw)
        else:
            await interaction.response.send_message(**kw)

    async def _on_app_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Never leave a slash command hanging on 'application did not respond'."""
        original = getattr(error, "original", error)
        log.error("slash command failed: %s: %s", type(original).__name__, original)
        text = f"⚠️ {type(original).__name__}: {original}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    # ── prefix command router ─────────────────────────────────────────────────
    async def on_message(self, message: discord.Message) -> None:
        if not self.use_prefix or message.author.bot or message.guild is None:
            return
        if not message.content.startswith(PREFIX):
            return
        try:
            argv = shlex.split(message.content[len(PREFIX):])
        except ValueError:
            argv = message.content[len(PREFIX):].split()
        if not argv:
            return
        cmd, args = argv[0].lower(), argv[1:]
        log.info("prefix command from %s: %s %s", message.author.display_name, cmd, args)
        try:
            text = await self._dispatch(cmd, args, prefix=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed", cmd)
            text = f"⚠️ {exc}"
        if isinstance(text, Payload):
            kw = {"embed": text.embed, "mention_author": False}
            if text.file:
                kw["file"] = text.file
            await message.reply(**kw)
        elif isinstance(text, discord.Embed):
            await message.reply(embed=text, mention_author=False)
        elif text:
            await message.reply(text, mention_author=False)

    async def _dispatch(self, cmd: str, args: list[str], *, prefix: bool) -> str | discord.Embed | Payload | None:
        """Shared command table for prefix and slash surfaces."""
        if cmd in ("help", "menu", "commands"):
            return self.help_embed()
        if cmd in ("station", "stasiun"):
            if not args:
                return self.station_embed()
            return await self.switch_station(" ".join(args))
        if cmd in ("now", "np", "nowplaying"):
            return self.now_embed()
        if cmd in ("next", "skip"):
            return await self.do_next()
        if cmd == "pause":
            return await self.do_pause()
        if cmd in ("resume", "play"):
            return await self.do_resume()
        if cmd in ("volume", "vol"):
            try:
                pct = int(args[0]) if args else 80
            except ValueError:
                return "usage: volume <0-150>"
            return await self.do_volume(pct)
        if cmd in ("status", "diag"):
            return await self.status_embed()
        if cmd == "join":
            vc = await self._ensure_joined()
            return "✅ joined" if vc else "❌ could not join"
        if cmd == "leave":
            guild = self.get_guild(GUILD_ID)
            vc = guild.voice_client if guild else None
            if vc:
                await vc.disconnect(force=True)
                return "👋 left the channel"
            return "not connected"
        return f"unknown command `{cmd}` — try `{PREFIX}help`" if prefix else "unknown command"

    # ── command actions ───────────────────────────────────────────────────────
    def _vinyl_file(self) -> discord.File | None:
        """Fresh File per message (a File object is consumed on send)."""
        try:
            return discord.File(VINYL_GIF, filename="vinyl.gif")
        except Exception:  # noqa: BLE001
            return None

    def station_embed(self) -> Payload:
        e = discord.Embed(
            title="📻 Stasiun Lo-fi",
            description="\n".join(
                f"{'▶️' if k == self.station_key else '◦'}  **{STATIONS[k][0]}**\n"
                f"     `{k}`"
                for k in ORDER
            ),
            colour=0x5865F2,
        )
        e.set_footer(text=f"ganti: {PREFIX}station <key> · /station name:<key>")
        return Payload(e)

    def now_embed(self) -> Payload:
        title = STATIONS[self.station_key][0]
        stream_up = int(time.time() - self._stream_started) if self._stream_started else 0
        m, s = divmod(stream_up, 60)
        h, m = divmod(m, 60)
        up = f"{h}j {m}m {s}s" if h else f"{m}m {s}s"

        guild = self.get_guild(GUILD_ID)
        vc = guild.voice_client if guild else None
        listeners = [mem.display_name for mem in vc.channel.members if mem.id != self.user.id] if vc else []
        filled = round(self.volume / 1.5 * 10)

        e = discord.Embed(
            title="🎵 Sekarang diputar",
            description=f"**{title}**\n`{self.station_key}`",
            colour=0x5865F2,
        )
        e.add_field(name="⏯", value=("⏸ paused" if self._paused else "▶️ playing"), inline=True)
        e.add_field(name="🔊", value=f"{int(self.volume * 100)}%", inline=True)
        e.add_field(name="⏱", value=up, inline=True)
        e.add_field(name="👥", value=f"{len(listeners)} pendengar" if listeners else "—", inline=True)
        e.add_field(name="📶", value=f"{'▓' * filled}{'░' * (10 - filled)}", inline=False)
        e.set_thumbnail(url="attachment://vinyl.gif")
        e.set_footer(text="Lo-fi Radio 24/7")
        return Payload(e, self._vinyl_file())

    async def switch_station(self, name: str) -> str:
        key = name.strip().lower().lstrip("/")
        key = {"laut.fm": "lautfm", "laut": "lautfm", "chill": "chillhop", "moe": "moezeno"}.get(key, key)
        if key not in STATIONS:
            return f"unknown station `{name}` — options: " + ", ".join(f"`{k}`" for k in ORDER)
        self.station_key = key
        self._skip = True
        self._stalls = 0
        guild = self.get_guild(GUILD_ID)
        vc = guild.voice_client if guild else None
        if vc and (vc.is_playing() or vc.is_paused()):
            self._paused = False
            vc.stop()
        return f"🔀 switching to **{STATIONS[key][0]}**"

    async def do_pause(self) -> str:
        guild = self.get_guild(GUILD_ID)
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_playing():
            return "nothing is playing"
        vc.pause()
        self._paused = True
        return "⏸ paused"

    async def do_resume(self) -> str:
        guild = self.get_guild(GUILD_ID)
        vc = guild.voice_client if guild else None
        if not vc:
            return "not connected to voice"
        if vc.is_paused():
            vc.resume()
            self._paused = False
        return "▶️ resumed"

    async def do_next(self) -> str:
        idx = (ORDER.index(self.station_key) + 1) % len(ORDER)
        return await self.switch_station(ORDER[idx])

    async def do_volume(self, pct: int) -> str:
        pct = max(0, min(150, pct))
        self.volume = pct / 100.0
        if self._source:
            self._source.volume = self.volume
        return f"🔊 volume {pct}%"

    async def status_embed(self) -> discord.Embed:
        guild = self.get_guild(GUILD_ID)
        vc = guild.voice_client if guild else None
        if not vc:
            return discord.Embed(title="🎧 Lo-fi Radio", description="❌ Tidak terhubung ke voice channel",
                                 colour=0xED4245)
        conn = getattr(vc, "_connection", None)
        frames = self._source.frames if self._source else 0
        stream_up = int(time.time() - self._stream_started) if self._stream_started else 0

        def dur(sec: int) -> str:
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            return f"{h}j {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

        sh, rem = divmod(int(time.time() - self.started_at), 3600)
        sm, ss = divmod(rem, 60)
        bot_up = f"{sh}j {sm}m" if sh else f"{sm}m {ss}s"

        listeners = [m.display_name for m in vc.channel.members if m.id != self.user.id]
        dave_ok = bool(getattr(getattr(conn, "dave_session", None), "ready", False))
        state_ok = "connected" in str(getattr(conn, "state", None))
        rss_mb = 0
        try:
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) // 1024
                    break
        except Exception:  # noqa: BLE001
            pass

        filled = round(self.volume / 1.5 * 10)
        bar = "▓" * filled + "░" * (10 - filled)
        title = STATIONS[self.station_key][0]

        e = discord.Embed(title="🎧 Lo-fi Radio — status", colour=0x5865F2)
        e.add_field(name="🎵 Stasiun", value=f"**{title}**\n`{self.station_key}`", inline=True)
        e.add_field(name="⏯ State", value=("⏸ paused" if self._paused else "▶️ playing"), inline=True)
        e.add_field(name="🔊 Volume", value=f"`{bar}` {int(self.volume * 100)}%", inline=True)
        e.add_field(name="👥 Listener", value=(", ".join(listeners) if listeners else "—") +
                    f"\n{len(listeners)} orang", inline=True)
        e.add_field(name="📡 Frames", value=f"{frames:,} (~50 fps)\nstream {dur(stream_up)}", inline=True)
        e.add_field(name="🔐 E2EE", value=("✅ DAVE ready" if dave_ok else "⚠️ belum ready"), inline=True)
        e.add_field(name="🔌 Voice", value=("✅ connected" if state_ok else f"⚠️ {getattr(conn, 'state', None)}"),
                    inline=True)
        e.add_field(name="🧠 RAM", value=f"{rss_mb} MB", inline=True)
        e.add_field(name="⏱ Uptime bot", value=bot_up, inline=True)
        e.set_footer(text="Lo-fi Radio 24/7 · auto-recover aktif")
        e.timestamp = discord.utils.utcnow()
        return e

    def help_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🎧 Lo-fi Radio — commands",
            description=f"Slash `/lofi …` atau prefix `{PREFIX}…` (dua-duanya sama)",
            colour=0x5865F2,
        )
        rows = [
            ("help", "menu ini"),
            ("station <nama>", "ganti stasiun · tanpa argumen = daftar"),
            ("now", "stasiun yang jalan sekarang"),
            ("next / skip", "stasiun berikutnya"),
            ("pause · resume", "jeda · lanjut"),
            ("volume <0-150>", "atur volume"),
            ("status", "diagnosa: frame, E2EE, listener"),
            ("join · leave", "masuk · keluar voice channel"),
        ]
        for cmd, desc in rows:
            base = cmd.split(" ")[0]
            e.add_field(name=f"`{PREFIX}{cmd}` · `/{base}`", value=desc, inline=False)
        e.set_footer(text="Daftar station: lautfm · isekoi · litt · chillhop · moezeno")
        return e

    # ── slash commands ────────────────────────────────────────────────────────
    def _register_slash(self) -> None:
        """Flat commands: /station, /now, /next, /volume, /status, /help … (no submenu)."""
        tree = self.tree

        @tree.command(name="help", description="Menu perintah radio")
        async def help_(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=self.help_embed())

        @tree.command(name="station", description="Ganti stasiun lofi (kosongkan untuk daftar)")
        @app_commands.describe(name="key stasiun, mis. lautfm")
        async def station(interaction: discord.Interaction, name: str | None = None) -> None:
            if not name:
                p = self.station_embed()
                kw = {"embed": p.embed}
                if p.file:
                    kw["file"] = p.file
                await self._respond(interaction, **kw)
                return
            await interaction.response.send_message(await self.switch_station(name))

        @station.autocomplete("name")
        async def station_ac(interaction: discord.Interaction, current: str):
            return [
                app_commands.Choice(name=f"{STATIONS[k][0]} ({k})", value=k)
                for k in ORDER if current.lower() in k
            ]

        @tree.command(name="now", description="Stasiun yang jalan sekarang")
        async def now(interaction: discord.Interaction) -> None:
            p = self.now_embed()
            kw = {"embed": p.embed}
            if p.file:
                kw["file"] = p.file
            await self._respond(interaction, **kw)

        @tree.command(name="next", description="Pindah ke stasiun berikutnya")
        async def next_(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(await self.do_next())

        @tree.command(name="pause", description="Jeda playback")
        async def pause(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(await self.do_pause())

        @tree.command(name="resume", description="Lanjutkan playback")
        async def resume(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(await self.do_resume())

        @tree.command(name="volume", description="Atur volume 0-150%")
        async def volume(interaction: discord.Interaction, pct: app_commands.Range[int, 0, 150]) -> None:
            await interaction.response.send_message(await self.do_volume(int(pct)))

        @tree.command(name="status", description="Diagnosa: frame, E2EE, listener")
        async def status(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=await self.status_embed())

        @tree.command(name="join", description="Masuk ke voice channel")
        async def join(interaction: discord.Interaction) -> None:
            vc = await self._ensure_joined()
            await interaction.response.send_message("✅ joined" if vc else "❌ gagal join")

        @tree.command(name="leave", description="Keluar dari voice channel")
        async def leave(interaction: discord.Interaction) -> None:
            guild = self.get_guild(GUILD_ID)
            vc = guild.voice_client if guild else None
            if vc:
                await vc.disconnect(force=True)
                await interaction.response.send_message("👋 keluar dari channel")
            else:
                await interaction.response.send_message("belum connect")

    # ── voice plumbing ────────────────────────────────────────────────────────
    def _channel(self):
        guild = self.get_guild(GUILD_ID)
        return guild.get_channel(VOICE_CHANNEL_ID) if guild else None

    async def _ensure_joined(self):
        channel = self._channel()
        if channel is None:
            log.error("channel %s not found in guild %s", VOICE_CHANNEL_ID, GUILD_ID)
            return None
        vc = channel.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            return vc
        try:
            vc = await channel.connect(timeout=30, reconnect=True, self_deaf=True)
            log.info("joined voice channel '%s'", channel.name)
            conn = vc._connection
            # Frames sent before the MLS session is ready go out unencrypted and every
            # modern client drops them — audible as silence. Wait for the handshake.
            waited = 0
            while waited < 30 and not getattr(conn, "can_encrypt", False):
                await asyncio.sleep(1)
                waited += 1
            log.info(
                "voice state: dave_proto=%s dave_ready=%s can_encrypt=%s (waited %ss)",
                getattr(conn, "dave_protocol_version", "?"),
                getattr(getattr(conn, "dave_session", None), "ready", None),
                getattr(conn, "can_encrypt", None),
                waited,
            )
            return vc
        except Exception as exc:  # noqa: BLE001
            log.error("voice connect failed: %s", exc)
            return None

    async def _hard_rejoin(self) -> None:
        """Tear the voice session down completely so DAVE keys are renegotiated."""
        now = time.time()
        self._last_rejoin_try = now
        self._rejoins = [t for t in self._rejoins if now - t < 300]
        self._rejoins.append(now)
        self._tearing_down = True
        try:
            guild = self.get_guild(GUILD_ID)
            vc = guild.voice_client if guild else None
            if vc is not None:
                try:
                    await vc.disconnect(force=True)
                    log.info("tore down voice session for a clean rejoin")
                except Exception as exc:  # noqa: BLE001
                    log.warning("disconnect while reconnecting failed: %s", exc)
            await asyncio.sleep(3)
        finally:
            # The disconnect above emits our own voice state update; drop the event it
            # produced so it cannot request yet another rejoin.
            self._rejoin.clear()
            self._tearing_down = False
        if len(self._rejoins) > 5:
            log.warning("rejoin storm: %d in 5 min — backing off 60 s", len(self._rejoins))
            self._rejoins.clear()
            await asyncio.sleep(60)

    async def _stream_once(self, vc) -> str:
        key = self.station_key
        title, url = STATIONS[key]
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()

        def _after(err) -> None:
            if not done.done():
                loop.call_soon_threadsafe(done.set_result, err)

        self._skip = False
        self._rejoin.clear()  # any rejoin request before this point is stale
        if vc.is_playing():
            vc.stop()
        source = RadioSource(url, self.volume)
        self._source = source
        try:
            vc.play(source, after=_after)
        except Exception as exc:  # noqa: BLE001
            return f"play() raised: {exc}"
        self._paused = False
        self._last_ready = None
        self._stream_started = time.time()
        log.info("streaming %s (%s)", title, url)

        silent_for = 0.0
        dave_bad_for = 0.0
        stall_for = 0.0
        last_frames = 0
        ticks = 0
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(done), timeout=CHECK_SECONDS)
                return f"stream ended ({done.result()})"
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            ticks += 1
            conn = getattr(vc, "_connection", None)
            ready = bool(getattr(conn, "can_encrypt", False))
            if ready != self._last_ready:
                log.info("dave can_encrypt=%s", ready)
                self._last_ready = ready
            if source.frames != last_frames:
                last_frames = source.frames
                stall_for = 0.0
            elif not self._paused:
                stall_for += CHECK_SECONDS
            dave_bad_for = 0.0 if ready else dave_bad_for + CHECK_SECONDS
            others = max(0, len(getattr(vc.channel, "members", ())) - 1)
            if ticks % 6 == 0:  # every ~60 s: one line of proof life is flowing
                log.info(
                    "heartbeat: station=%s frames=%d can_encrypt=%s state=%s paused=%s",
                    key, source.frames, ready, getattr(conn, "state", None), self._paused,
                )
            if self._skip:
                return "skip requested"
            if self._rejoin.is_set():
                self._rejoin.clear()
                return "rejoin requested"
            if vc is None or not vc.is_connected():
                return "voice disconnected"
            if conn is not None and "connected" not in str(getattr(conn, "state", None)):
                return f"voice state {getattr(conn, 'state', None)}"
            if self._paused:
                silent_for = 0.0
                dave_bad_for = 0.0
                continue
            # The MLS group only forms once a human client is in the channel: alone,
            # the server sends no welcome/proposals and ready stays False, so frames
            # are dropped — but nobody is listening either. Never rejoin-thrash for
            # that case; only a sustained False *with* a listener means a wedged
            # session worth renegotiating (rate limited).
            if others <= 0:
                if dave_bad_for >= DAVE_STUCK_SECONDS and not self._dave_warned:
                    self._dave_warned = True
                    log.info("dave idle: alone in channel, MLS group not established "
                             "(frames dropped until someone joins — expected)")
            if dave_bad_for >= DAVE_STUCK_SECONDS and others > 0:
                if time.time() - self._last_rejoin_try > 120:
                    return f"dave not ready for {int(dave_bad_for)}s with {others} listener(s)"
            if ready and self._dave_warned:
                self._dave_warned = False
            if ready and stall_for >= FRAME_STALL_SECONDS:
                return f"frames stalled for {int(stall_for)}s"
            if not vc.is_playing():
                silent_for += CHECK_SECONDS
                if silent_for >= SILENT_RESTART_SECONDS:
                    return "no audio for too long"
            else:
                silent_for = 0.0

    async def _rotate(self) -> None:
        self.station_key = ORDER[(ORDER.index(self.station_key) + 1) % len(ORDER)]
        log.warning("rotating station -> %s", self.station_key)

    async def _supervisor(self) -> None:
        backoff = 2.0
        while not self.is_closed():
            try:
                vc = await self._ensure_joined()
                if vc is None:
                    await asyncio.sleep(min(backoff, 30))
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 2.0
                reason = await self._stream_once(vc)
                log.warning("stream stopped (%s)", reason)
                if reason.startswith(("voice", "rejoin", "dave")):
                    await self._hard_rejoin()
                elif reason.startswith("frames stalled") and not self._skip:
                    # ffmpeg or the PCM pipe wedged: replay the same station, and only
                    # rotate when the exact same station wedges twice in a row.
                    self._stalls += 1
                    if self._stalls >= 2:
                        self._stalls = 0
                        await self._rotate()
                    else:
                        log.warning("stream wedged — replaying the same station")
                    await asyncio.sleep(2)
                else:
                    if reason.startswith(("stream ended", "no audio")) and not self._skip:
                        # station died on its own -> rotate, but keep a user-picked one stable
                        self._stalls = 0
                        await self._rotate()
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("supervisor error: %s", exc)
                await asyncio.sleep(5)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )
    load_opus()
    apply_voice_compat()
    want_prefix = os.environ.get("PREFIX_ENABLED", "1") != "0"
    try:
        LofiBot(use_prefix=want_prefix).run(TOKEN, log_handler=None, reconnect=True)
    except discord.PrivilegedIntentsRequired:
        if not want_prefix:
            raise
        log.warning("Message Content intent is not enabled in the dev portal — "
                    "running slash-only until it is turned on")
        LofiBot(use_prefix=False).run(TOKEN, log_handler=None, reconnect=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())