"""
Server Status Dashboard Extension

Maintains a single auto-updating Discord embed in a dedicated channel showing:
  - Server status (Online/Offline)
  - Active player count
  - Next scheduled restart (countdown)
  - In-game time and date with day/night indicator
  - Server age (day count)
  - Weather conditions, wind, and temperature
  - Next horde night (days remaining)

Resilience features:
  - Grace period: 3 consecutive RCON failures before showing offline
  - Lua file freshness: treats recently-written bridge files as a secondary
    online signal even if RCON is momentarily unresponsive
  - Last-known-good data: retains and displays cached world/horde data during
    brief outages instead of blanking the panel
  - Horde status: shows event count and current day alongside next horde info

Config:
  STATUS_CHANNEL_ID=  (Discord channel ID for the status embed)
"""

import os
import sys
import time
import asyncio
import datetime
import discord
from discord.ext import commands, tasks

import lua_bridge

# ============================================================================
# Constants
# ============================================================================

ICON_URL = "https://cdn.discordapp.com/attachments/1160323630773842010/1479184189835317430/jeeves_icon_128.png"
IMAGE_URL = "https://cdn.discordapp.com/attachments/1160323630773842010/1479186567758086164/status_bunker_banner_v2.png"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

RESTART_HOURS_UTC = [1, 5, 9, 13, 17, 21]

WEATHER_EMOJI = {
    "Clear": "\u2600\ufe0f",
    "Partly Cloudy": "\u26c5",
    "Overcast": "\u2601\ufe0f",
    "Light Rain": "\U0001f326\ufe0f",
    "Rain": "\U0001f327\ufe0f",
    "Heavy Rain": "\u26c8\ufe0f",
    "Foggy": "\U0001f32b\ufe0f",
    "Snowing": "\u2744\ufe0f",
}

# How many consecutive RCON failures before declaring offline
OFFLINE_GRACE_COUNT = 3

# How old (seconds) a Lua bridge file can be and still count as "fresh"
# (i.e., the server was writing data recently even if RCON timed out)
BRIDGE_FRESHNESS_SECONDS = 120

# How old (seconds) world data can be before the dashboard stops presenting it
# as live. Deliberately much looser than BRIDGE_FRESHNESS_SECONDS so a slow
# writer isn't mistaken for a dead one — but with an in-game day lasting about
# an hour, anything older than this is a stopped bridge, not a late update.
WORLD_STALE_SECONDS = 900

# ============================================================================
# Data helpers
# ============================================================================

def _next_restart_str(skip_active):
    if skip_active:
        return "Skipped"
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for h in RESTART_HOURS_UTC:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        candidates.append(t)
    nxt = min(candidates)
    delta = nxt - now
    total_minutes = int(delta.total_seconds() // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_time(hour, minutes):
    period = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minutes:02d} {period}"


def _temp_f(celsius):
    return f"{celsius * 9 / 5 + 32:.0f}\u00b0F"


def _data_age(data):
    """Seconds since a Lua bridge dict was written, or None if unknown."""
    if not data:
        return None
    ts = data.get("timestamp")
    if ts is None:
        return None
    try:
        return time.time() - float(ts)
    except (TypeError, ValueError):
        return None


def _format_age(seconds):
    """Human-readable age for the stale-bridge warning."""
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _bridge_file_is_fresh(data, max_age=BRIDGE_FRESHNESS_SECONDS):
    """Check if a Lua bridge dict has a recent timestamp."""
    if not data:
        return False
    ts = data.get("timestamp")
    if ts is None:
        return False
    try:
        age = time.time() - float(ts)
        return age < max_age
    except (TypeError, ValueError):
        return False


# ============================================================================
# Server INI
# ============================================================================

# (path, mtime, value_or_error). The dashboard asks every 30 seconds; keying the
# cache on mtime means an admin editing the INI is picked up without re-reading
# the file on every tick.
_pause_empty_cache = None


def _read_pause_empty(ini_path):
    """Whether the server pauses the simulation when nobody is online.

    Reads the PauseEmpty line from the Project Zomboid server INI — the option
    the admin panel labels "Pause When Empty". No RCON command reports whether
    the world is paused right now, so this setting is the only way to know
    whether an empty server explains a bridge file that stopped being written.

    Raises OSError if the file can't be read and KeyError if the key isn't in
    it. Either means SERVER_INI_PATH isn't pointing at the server's INI, which
    is a bigger problem than a quiet bridge file — worth saying out loud rather
    than papering over with PZ's own default.
    """
    global _pause_empty_cache

    if not ini_path:
        raise OSError("SERVER_INI_PATH is not set")

    # Raises before the cache is consulted, so an INI that goes missing is
    # noticed on the tick it goes missing rather than on the next edit.
    mtime = os.path.getmtime(ini_path)

    if _pause_empty_cache is not None:
        cached_path, cached_mtime, cached = _pause_empty_cache
        if cached_path == ini_path and cached_mtime == mtime:
            # A cached miss is stored as its reason, not as the exception
            # object: re-raising one exception grows its traceback on every
            # raise, and this path runs every 30 seconds indefinitely.
            if isinstance(cached, str):
                raise KeyError(cached)
            return cached

    value = None
    # utf-8-sig, not utf-8: a BOM on a Windows-written INI would otherwise
    # ride along on the first key and stop it matching.
    with open(ini_path, 'r', encoding='utf-8-sig', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, raw = line.partition('=')
            if sep and key.strip().lower() == 'pauseempty':
                value = raw.strip().lower() == 'true'
                break

    if value is None:
        reason = f"PauseEmpty not found in {ini_path}"
        _pause_empty_cache = (ini_path, mtime, reason)
        raise KeyError(reason)

    _pause_empty_cache = (ini_path, mtime, value)
    return value


# ============================================================================
# Embed builder
# ============================================================================

def build_embed(server_online, world, horde, skip_active, stale=False,
                live_player_count=None, rcon_ok=False, pause_empty=None,
                pause_empty_error=None):
    """Build the status dashboard embed.

    Args:
        server_online: Whether the server is confirmed online.
        world: World data dict (may be cached/stale).
        horde: Horde data dict (may be cached/stale).
        skip_active: Whether next restart is being skipped.
        stale: If True, data is cached from a previous poll (server may be
               temporarily unreachable but we're within the grace period).
        live_player_count: Player count from the last successful RCON poll,
               or None if that poll failed and the count is unknown.
        rcon_ok: Whether RCON answered on *this* tick. Distinct from
               server_online, which stays True through the grace period.
        pause_empty: Server's PauseEmpty setting, or None if it couldn't be
               read from the INI.
        pause_empty_error: Why PauseEmpty couldn't be read, for the panel.
    """

    # World data is only presented as live when the mod wrote it recently.
    # Without this the panel happily renders a months-old snapshot as the
    # current in-game time, date and weather.
    world_age = _data_age(world)
    world_fresh = bool(world) and world_age is not None and world_age < WORLD_STALE_SECONDS

    # ...except when the server pauses on an empty world. With PauseEmpty the
    # simulation halts, OnTick stops firing and the mod stops writing — so the
    # last values are not stale, they are the frozen present. Show them, and
    # say the world is paused rather than crying broken bridge.
    #
    # Every term here has to be something we know rather than something we
    # assume, because world_paused doesn't just silence a warning — it feeds
    # world_live, which publishes the frozen file as the current state of the
    # world. Guessing wrong prints stale weather as fact.
    #
    #   rcon_ok      — not server_online, which stays True through the grace
    #                  period. Jeeves and the server share a machine, so a
    #                  failed RCON means the game isn't running and nothing is
    #                  paused.
    #   pause_empty  — read from the INI. A server with PauseEmpty=false and an
    #                  empty world has a genuinely broken bridge.
    #   count == 0   — a confirmed zero from this tick's own RCON call. The
    #                  earlier `not live_player_count` also passed None, so an
    #                  unknown count read as paused and the panel claimed a
    #                  pause through outages where players were online.
    world_paused = (bool(world) and not world_fresh and rcon_ok
                    and pause_empty is True and live_player_count == 0)
    world_live = world_fresh or world_paused

    if server_online and not stale:
        embed = discord.Embed(colour=discord.Colour.green())
    elif server_online and stale:
        # Within grace period — show amber/yellow to hint at instability
        embed = discord.Embed(colour=discord.Colour.orange())
    else:
        embed = discord.Embed(colour=discord.Colour.red())

    embed.set_author(name="SERVER STATUS", icon_url=ICON_URL)
    embed.set_thumbnail(url=ICON_URL)
    embed.set_image(url=IMAGE_URL)

    # Fully offline with no cached data at all
    if not server_online and not world:
        embed.add_field(name="\u200b", value="\U0001f534 **Server Offline**", inline=False)
        embed.set_footer(text="Updates every 30 seconds")
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        return embed

    # --- Row 1: Status | Players | Restart ---
    if server_online:
        embed.add_field(name="\U0001f4e1 Status", value="\U0001f7e2 Online", inline=True)
    else:
        embed.add_field(name="\U0001f4e1 Status", value="\U0001f534 Offline", inline=True)

    if world_fresh and world.get("playerCount") is not None:
        player_count = str(world["playerCount"])
    elif live_player_count is not None:
        player_count = str(live_player_count)
    else:
        # No trustworthy source: the world file is frozen and RCON is not
        # answering. "0" would be a guess dressed up as a fact.
        player_count = "\u2014"
    embed.add_field(name="\u2b50 Players", value=player_count, inline=True)

    restart_str = _next_restart_str(skip_active)
    embed.add_field(name="\u23f0 Restart", value=restart_str, inline=True)

    # --- Row 2: Time | Date | Age ---
    if world_live:
        hour = world.get("hour", 0)
        mins = world.get("minutes", 0)
        is_night = world.get("isNight", False)
        time_icon = "\U0001f319" if is_night else "\u2600\ufe0f"
        embed.add_field(name=f"{time_icon} Time", value=_format_time(hour, mins), inline=True)

        # PZ's GameTime getDay()/getMonth() are both zero-based, and the mod
        # passes them through untouched. The month lookup already accounts for
        # that; the day has to be shifted to match the in-game calendar.
        month = world.get("month")
        day = world.get("day")
        if month is None or day is None:
            date_str = "\u2014"
        else:
            month_name = MONTH_NAMES[month][:3] if 0 <= month < 12 else "???"
            date_str = f"{month_name} {int(day) + 1}"
        embed.add_field(name="\U0001f4c5 Date", value=date_str, inline=True)

        # Age comes from whichever day counter the mod reports. If neither is
        # present, show a dash — inventing "Day 1" hides a broken bridge file.
        elapsed = world.get("elapsedDays")
        age_raw = world.get("worldAgeDays")
        if elapsed is not None and elapsed > 0:
            age_str = f"Day {int(elapsed) + 1}"
        elif age_raw is not None and age_raw > 0:
            age_str = f"Day {int(age_raw) + 1}"
        else:
            age_str = "\u2014"
        embed.add_field(name="\U0001f4c6 Age", value=age_str, inline=True)
    else:
        embed.add_field(name="\U0001f552 Time", value="\u2014", inline=True)
        embed.add_field(name="\U0001f4c5 Date", value="\u2014", inline=True)
        embed.add_field(name="\U0001f4c6 Age", value="\u2014", inline=True)

    # --- Row 3: Weather | Wind | Cycle ---
    if world_live:
        weather = world.get("weather", "Clear")
        temp = world.get("temperature", 0)
        w_emoji = WEATHER_EMOJI.get(weather, "\u2600\ufe0f")
        embed.add_field(name=f"{w_emoji} Weather", value=f"{weather}, {_temp_f(temp)}", inline=True)

        wind_speed = world.get("windSpeed", 0)
        if wind_speed > 0.6:
            wind_desc = "Strong"
        elif wind_speed > 0.3:
            wind_desc = "Moderate"
        elif wind_speed > 0.05:
            wind_desc = "Light"
        else:
            wind_desc = "Calm"
        embed.add_field(name="\U0001f4a8 Wind", value=wind_desc, inline=True)

        is_night = world.get("isNight", False)
        if is_night:
            embed.add_field(name="\U0001f319 Cycle", value="Night", inline=True)
        else:
            embed.add_field(name="\u2600\ufe0f Cycle", value="Day", inline=True)
    else:
        embed.add_field(name="\u2601\ufe0f Weather", value="\u2014", inline=True)
        embed.add_field(name="\U0001f4a8 Wind", value="\u2014", inline=True)
        embed.add_field(name="\u2600\ufe0f Cycle", value="\u2014", inline=True)

    # --- Row 4: Horde Day | Horde Status | Completed ---
    horde_day, horde_status, horde_completed = _horde_fields(horde)
    embed.add_field(name="\U0001f31a Horde", value=horde_day, inline=True)
    embed.add_field(name="\U0001f9df Status", value=horde_status, inline=True)
    embed.add_field(name="\U0001f3c6 Completed", value=horde_completed, inline=True)

    # At most one warning field — name the root cause, not every symptom.
    if pause_empty is None:
        # Can't read the INI, so a paused world and a dead bridge look alike.
        # Say that, rather than picking one and asserting it.
        embed.add_field(
            name="\u26a0\ufe0f Server Config",
            value=(f"Can't read `PauseEmpty` from the server INI — "
                   f"{pause_empty_error or 'reason unknown'}. Check "
                   f"`SERVER_INI_PATH`; until it resolves, a paused world "
                   f"can't be told apart from a broken bridge."),
            inline=False,
        )
    elif rcon_ok and not world_live:
        # rcon_ok gates this: with the server down, a mod that isn't writing is
        # not news on top of an already-red panel.
        if world:
            detail = f"No update in {_format_age(world_age)}"
        else:
            detail = "File missing"
        embed.add_field(
            name="\u26a0\ufe0f World Bridge",
            value=(f"{detail} — the Jeeves server mod isn't writing "
                   f"`{lua_bridge.WORLD_STATUS_FILE}.txt`, so in-game time, "
                   f"date and weather are unavailable."),
            inline=False,
        )

    footer = "Updates every 30 seconds"
    if world_paused:
        footer = "Updates every 30 seconds \u2022 World paused \u2014 no players online"
    elif stale:
        footer = "Updates every 30 seconds \u2022 Data may be stale"
    embed.set_footer(text=footer)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def _horde_fields(horde):
    """Return (horde_day, horde_status, completed) for three inline fields."""
    if not horde:
        return ("—", "Idle", "0")

    phase = horde.get("phase", "")
    event_count = horde.get("eventCount")
    next_day = horde.get("nextHordeDay")

    # Horde day — the Lua scheduler's day counter runs 1 ahead of what
    # players see in-game. Subtract 1 to match the in-game display.
    if next_day is not None:
        horde_day = f"Day {next_day - 1}"
    else:
        horde_day = "—"

    # Status
    if phase == "active":
        horde_status = "\u26a0\ufe0f **ACTIVE**"
    elif phase == "ended":
        horde_status = "Idle"
    elif phase == "scheduled":
        horde_status = "Scheduled"
    elif phase == "status":
        horde_status = "Idle"
    elif phase:
        horde_status = phase.capitalize()
    else:
        horde_status = "Idle"

    # Completed count
    horde_completed = str(event_count) if event_count is not None else "0"

    return (horde_day, horde_status, horde_completed)


# ============================================================================
# Discord Cog
# ============================================================================

def _env_channel_id(name: str) -> int:
    """Read a Discord channel ID from the environment.

    Returns 0 when unset or when the value is a placeholder / non-numeric,
    so a bad config disables the feature instead of raising at load time.
    """
    raw = (os.getenv(name) or '').strip()
    try:
        return int(raw)
    except ValueError:
        if raw:
            print(f"[Config] {name}='{raw}' is not a channel ID — feature disabled.")
        return 0


class ServerStatusCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._channel_id = _env_channel_id('STATUS_CHANNEL_ID')
        self._message_id = None
        self._channel = None

        # Grace period state
        self._rcon_fail_count = 0

        # Last known good data (retained across brief outages)
        self._last_world = None
        self._last_horde = None

        # Last (value, error) printed for PauseEmpty. The empty tuple never
        # equals a real pair, so the first read always logs; after that only
        # changes do, since this is consulted every 30 seconds.
        self._pause_empty_logged = ()

        if not self._channel_id:
            print("[ServerStatus] WARNING: STATUS_CHANNEL_ID not set. Dashboard disabled.")
        else:
            print(f"[ServerStatus] Dashboard channel: {self._channel_id}")
            self.status_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()

    def _pause_empty(self):
        """(value, error) for the server's PauseEmpty setting.

        value is True/False, or None when the INI couldn't be read — in which
        case error says why, in words short enough for the panel.
        """
        ini_path = getattr(self.bot.config, 'SERVER_INI_PATH', None)
        try:
            value, error = _read_pause_empty(ini_path), None
        except KeyError as e:
            value, error = None, str(e.args[0])
        except OSError as e:
            value, error = None, f"{type(e).__name__}: {e}"

        if (value, error) != self._pause_empty_logged:
            if error:
                print(f"[ServerStatus] Cannot determine PauseEmpty: {error}")
            else:
                print(f"[ServerStatus] PauseEmpty={value} (from {ini_path})")
            self._pause_empty_logged = (value, error)

        return value, error

    def _restart_skipped(self) -> bool:
        """True when the next scheduled restart will not happen — either the
        admin ran /skip, or AutoRestartCog is skipping it because the server
        rebooted too recently."""
        return (self.bot.state.skip_next_restart
                or getattr(self.bot.state, 'auto_skip_active', False))

    async def _get_channel(self):
        if self._channel:
            return self._channel
        if not self._channel_id:
            return None
        ch = self.bot.get_channel(self._channel_id)
        if not ch:
            try:
                ch = await self.bot.fetch_channel(self._channel_id)
            except (discord.NotFound, discord.Forbidden) as e:
                print(f"[ServerStatus] Could not access channel {self._channel_id}: {e}")
                return None
        self._channel = ch
        return ch

    async def _send_or_edit(self, embed):
        channel = await self._get_channel()
        if not channel:
            return

        # Try to edit existing message
        if self._message_id:
            try:
                msg = await channel.fetch_message(self._message_id)
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.HTTPException):
                self._message_id = None

        # Search for our previous status message to reuse
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds:
                    for e in msg.embeds:
                        if e.author and e.author.name and "SERVER STATUS" in e.author.name:
                            self._message_id = msg.id
                            await msg.edit(embed=embed)
                            print(f"[ServerStatus] Found existing status message: {msg.id}")
                            return
        except discord.HTTPException:
            pass

        # Send new
        try:
            msg = await channel.send(embed=embed)
            self._message_id = msg.id
            print(f"[ServerStatus] Created status message: {msg.id}")
        except discord.HTTPException as e:
            print(f"[ServerStatus] Failed to send status: {e}")

    @tasks.loop(seconds=30)
    async def status_loop(self):
        try:
            # --- 1. Probe RCON ---
            # fetch_players() is a blocking socket call (up to 5s), so run it
            # off the event loop — otherwise every tick stalls chat relay,
            # slash commands and the gateway heartbeat.
            #
            # It runs the same 'players' command the old is_server_online()
            # probe ran, but keeps the answer, so liveness and the count come
            # from one connection at one instant. state.player_count is written
            # by a 60s heartbeat that returns early during restarts, so through
            # a restart it reports a frozen number as a current one.
            players_raw = None
            if self.bot.state.server_ready:
                players_raw = await asyncio.to_thread(self.bot.rcon.fetch_players)
            rcon_ok = players_raw is not None

            # parse_players reads its count from the "Players connected (N)"
            # header and its names from the "-name" lines. If that header is
            # ever absent or reworded the count parses as 0 while the names are
            # still there — and a false zero is the one error that matters
            # here, since it asserts a pause with players online. Take
            # whichever source says more.
            tick_count = None
            if rcon_ok:
                names, header_count = self.bot.rcon.parse_players(players_raw)
                tick_count = max(header_count, len(names))

            pause_empty, pause_empty_error = self._pause_empty()

            # --- 2. Read Lua bridge files ---
            world = lua_bridge.read_world_status()
            horde = lua_bridge.read_horde_status()

            # --- 3. Determine online status with grace period ---
            world_fresh = _bridge_file_is_fresh(world)
            horde_fresh = _bridge_file_is_fresh(horde)
            bridge_fresh = world_fresh or horde_fresh

            if rcon_ok:
                # RCON succeeded — reset fail counter, update cache
                self._rcon_fail_count = 0
                if world:
                    self._last_world = world
                if horde:
                    self._last_horde = horde

                embed = build_embed(True, world or self._last_world,
                                    horde or self._last_horde,
                                    self._restart_skipped(), stale=False,
                                    live_player_count=tick_count,
                                    rcon_ok=True, pause_empty=pause_empty,
                                    pause_empty_error=pause_empty_error)
            elif bridge_fresh:
                # RCON failed but bridge files are fresh — server is likely busy.
                #
                # live_player_count is None in this branch and the two below it:
                # RCON failed *this tick*, so there is no current count. The old
                # fallback read state.player_count whenever state.last_rcon_ok
                # was True, but that flag is written by a 60s heartbeat which
                # returns early during restarts — so through a restart it stayed
                # True over a frozen count, and the panel showed the players who
                # had been kicked. None renders as an em dash, which is the
                # honest answer.
                self._rcon_fail_count += 1
                if world:
                    self._last_world = world
                if horde:
                    self._last_horde = horde

                embed = build_embed(True, world or self._last_world,
                                    horde or self._last_horde,
                                    self._restart_skipped(), stale=True,
                                    live_player_count=None,
                                    rcon_ok=False, pause_empty=pause_empty,
                                    pause_empty_error=pause_empty_error)
            elif self._rcon_fail_count < OFFLINE_GRACE_COUNT:
                # RCON failed, bridge stale, but still within grace period
                self._rcon_fail_count += 1

                embed = build_embed(True, self._last_world, self._last_horde,
                                    self._restart_skipped(), stale=True,
                                    live_player_count=None,
                                    rcon_ok=False, pause_empty=pause_empty,
                                    pause_empty_error=pause_empty_error)
            else:
                # Fully offline: RCON failed repeatedly, bridge stale
                # Still show last known data rather than blanking
                embed = build_embed(False, self._last_world, self._last_horde,
                                    self._restart_skipped(), stale=False,
                                    live_player_count=None,
                                    rcon_ok=False, pause_empty=pause_empty,
                                    pause_empty_error=pause_empty_error)

            await self._send_or_edit(embed)

        except Exception as e:
            print(f"[ServerStatus] Error in status loop: {e}")

    @status_loop.before_loop
    async def _before_status(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        print("[ServerStatus] Dashboard started")


async def setup(bot):
    await bot.add_cog(ServerStatusCog(bot))
