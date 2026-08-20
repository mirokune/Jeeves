"""
Auto Restart Extension
Handles scheduled server restarts with countdown notifications.

A scheduled restart is skipped when the server was rebooted less than
RESTART_SKIP_WINDOW_MINUTES ago — a mod update at 12:30 should not be chased
by the routine 13:00 restart. The decision is made at the 10 minute mark so
the countdown never announces a restart that will not happen, and re-checked
on the hour in case the bot was not running at the 10 minute mark.

The same applies for HORDE_RESTART_GRACE_MINUTES after a horde night ends —
the existing horde guard only defers the countdown, which lands the restart
about ten minutes after the event finishes, exactly when players are still
collecting their loot.
"""

import datetime
import time
from typing import List
from discord.ext import commands, tasks

import lua_bridge

UTC = datetime.timezone.utc


def _schedule(hours: List[int], minute: int, second: int = 0) -> List[datetime.time]:
    return [datetime.time(hour=h, minute=minute, second=second, tzinfo=UTC) for h in hours]


# Restart at 01:00, 05:00, 09:00, 13:00, 17:00, 21:00 UTC
RESTART_HOURS = [1, 5, 9, 13, 17, 21]
# Notifications fire 1 hour before
NOTIFY_HOURS = [0, 4, 8, 12, 16, 20]


def _smallest_gap_minutes(hours: List[int]) -> int:
    """Shortest gap between consecutive restart slots, wrapping midnight."""
    ordered = sorted(hours)
    gaps = [(b - a) * 60 for a, b in zip(ordered, ordered[1:])]
    gaps.append((ordered[0] + 24 - ordered[-1]) * 60)
    return min(gaps)


def _format_delta(delta: datetime.timedelta) -> str:
    """Human-readable age for the skip notice."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


class AutoRestartCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._horde_defer_active = False

        # A skip window wider than the gap between restart slots means the
        # slot after a reboot or horde is skipped and the one after that runs
        # late rather than never — worth saying out loud all the same.
        gap = _smallest_gap_minutes(RESTART_HOURS)
        for name in ('RESTART_SKIP_WINDOW_MINUTES', 'HORDE_RESTART_GRACE_MINUTES'):
            value = getattr(bot.config, name, 0)
            if value >= gap:
                print(f"[AutoRestart] WARNING: {name}={value} is at least as long as the "
                      f"{gap}m gap between scheduled restarts — expect restarts to be "
                      f"skipped more often than they run.")

        for task in (self.auto_restart, self.notify_10m, self.notify_5m, self.notify_1m, self.notify_10s):
            task.start()

    def cog_unload(self):
        for task in (self.auto_restart, self.notify_10m, self.notify_5m, self.notify_1m, self.notify_10s):
            task.cancel()

    # ---- Recent-boot skip ----

    def _recent_boot_reason(self):
        """Why this scheduled restart should be skipped, or None to proceed.

        Only bot-initiated boots count: last_boot_at is None when the bot
        found the server already running, and inventing a boot time there
        would suppress a legitimate restart.
        """
        window_minutes = getattr(self.bot.config, 'RESTART_SKIP_WINDOW_MINUTES', 0)
        if window_minutes <= 0:
            return None

        booted = self.bot.state.last_boot_at
        if booted is None:
            return None

        window = datetime.timedelta(minutes=window_minutes)
        since = datetime.datetime.now(UTC) - booted
        if since >= window:
            return None

        return (f"the server was rebooted {_format_delta(since)} ago, "
                f"within the {_format_delta(window)} restart skip window")

    def _recent_horde_reason(self):
        """Why this restart should be skipped for a just-finished horde night.

        The mod writes phase="ended" with a timestamp the moment the event
        concludes, so no separate bookkeeping is needed. That file keeps
        saying "ended" until the next event, which is why the age matters
        and the phase alone does not.
        """
        grace_minutes = getattr(self.bot.config, 'HORDE_RESTART_GRACE_MINUTES', 0)
        if grace_minutes <= 0:
            return None

        horde = lua_bridge.read_horde_status()
        if not horde or horde.get("phase") != "ended":
            return None

        ended_at = horde.get("timestamp")
        if ended_at is None:
            return None

        try:
            since = datetime.timedelta(seconds=time.time() - float(ended_at))
        except (TypeError, ValueError):
            return None

        grace = datetime.timedelta(minutes=grace_minutes)
        if since < datetime.timedelta(0) or since >= grace:
            return None

        return (f"horde night ended {_format_delta(since)} ago \u2014 leaving the "
                f"{_format_delta(grace)} looting window alone")

    def _skip_reason(self):
        """Why the upcoming scheduled restart should not run, or None."""
        return self._recent_boot_reason() or self._recent_horde_reason()

    # ---- Scheduled tasks ----

    @tasks.loop(time=_schedule(RESTART_HOURS, minute=0))
    async def auto_restart(self):
        # Decided at the 10 minute mark, but re-checked here so a bot that
        # was not running then still honours the window.
        reason = self._skip_reason()
        if self.bot.state.auto_skip_active or reason:
            import discord
            self.bot.state.auto_skip_active = False
            detail = reason or "a restart happened recently"
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Scheduled restart skipped \u2014 {detail}.",
                discord.Colour.yellow()
            )
            print(f"[AutoRestart] Restart skipped: {detail}")
            return

        if self.bot.state.skip_next_restart:
            import discord
            self.bot.state.skip_next_restart = False
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Scheduled restart was skipped. Next restart will proceed as normal.",
                discord.Colour.yellow()
            )
            print("[AutoRestart] Restart skipped.")
            return

        if self._horde_defer_active:
            import discord, asyncio
            # Already deferred at the 10m mark. Wait for horde to clear.
            print("[AutoRestart] Restart deferred for horde, polling until clear...")
            while True:
                await asyncio.sleep(300)
                blocked, reason = self.bot._is_horde_blocking_restart()
                if not blocked:
                    self._horde_defer_active = False

                    # The horde has just finished, which is the worst possible
                    # moment to take the server away — players are still
                    # picking the field clean. Drop the deferred restart and
                    # let the next scheduled one handle it.
                    grace = self._recent_horde_reason()
                    if grace:
                        print(f"[AutoRestart] Deferred restart dropped: {grace}")
                        await self.bot.send_notification(
                            f"{self.bot.Emojis.JEEVES} Deferred restart skipped \u2014 {grace}.",
                            discord.Colour.yellow()
                        )
                        await self.bot.rcon.broadcast(
                            "Horde night is over — the restart has been skipped. "
                            "Go collect your loot."
                        )
                        return

                    print("[AutoRestart] Horde cleared, running deferred countdown.")
                    await self._announce("10 Minutes", self.bot.Emojis.SPIFFO_WAVE)
                    await self.bot.rcon.broadcast(
                        "Horde event concluded. Server will restart in 10 minutes!"
                    )
                    await asyncio.sleep(300)  # 5 minutes
                    await self._announce("5 Minutes", self.bot.Emojis.SPIFFO_EDUCATE)
                    await asyncio.sleep(240)  # 4 minutes
                    await self._announce("1 Minute", self.bot.Emojis.SPIFFO_KATANA)
                    await asyncio.sleep(50)   # 50 seconds
                    await self._announce("10 Seconds", self.bot.Emojis.SPIFFO_STOP)
                    await asyncio.sleep(10)
                    break

        import discord
        self.bot.state.auto_restart_pending = True
        await self.bot.send_notification(
            f"{self.bot.Emojis.SPIFFO_POP} Automatic Restart Initiated, this may take several minutes...",
            discord.Colour.yellow()
        )
        await self.bot.restart_server()

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=50))
    async def notify_10m(self):
        if self.bot.state.skip_next_restart:
            return

        # Decide before announcing anything: a countdown for a restart that
        # will not happen is worse than no countdown at all.
        reason = self._skip_reason()
        if reason:
            import discord
            self.bot.state.auto_skip_active = True
            print(f"[AutoRestart] Skipping upcoming restart: {reason}")
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Next scheduled restart will be skipped \u2014 {reason}.",
                discord.Colour.yellow()
            )
            return
        # Horde guard: check current AND projected in-game time.
        # With 1 day = 2 hours, 10 real minutes = 2 in-game hours.
        # If a horde is active or the window will be active at restart
        # time, defer the entire countdown.
        blocked, reason = self.bot._is_horde_blocking_restart(real_minutes_ahead=10)
        if blocked:
            import discord
            self._horde_defer_active = True
            print(f"[AutoRestart] Horde guard at 10m: {reason}")
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Scheduled restart deferred — {reason}. Will restart after the event.",
                discord.Colour.orange()
            )
            await self.bot.rcon.broadcast(
                "Scheduled restart deferred — horde night approaching. Server will restart after the event."
            )
            return
        self._horde_defer_active = False
        await self._announce("10 Minutes", self.bot.Emojis.SPIFFO_WAVE)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=55))
    async def notify_5m(self):
        if (not self.bot.state.skip_next_restart and not self._horde_defer_active
                and not self.bot.state.auto_skip_active):
            await self._announce("5 Minutes", self.bot.Emojis.SPIFFO_EDUCATE)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=59))
    async def notify_1m(self):
        if (not self.bot.state.skip_next_restart and not self._horde_defer_active
                and not self.bot.state.auto_skip_active):
            await self._announce("1 Minute", self.bot.Emojis.SPIFFO_KATANA)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=59, second=50))
    async def notify_10s(self):
        if (not self.bot.state.skip_next_restart and not self._horde_defer_active
                and not self.bot.state.auto_skip_active):
            await self._announce("10 Seconds", self.bot.Emojis.SPIFFO_STOP)

    async def _announce(self, label: str, emoji: str):
        import discord
        msg = f"Server will automatically restart in {label}!"
        await self.bot.send_notification(f"{emoji} {msg}", discord.Colour.yellow())
        await self.bot.rcon.broadcast(msg)

    # ---- Wait for ready ----

    @auto_restart.before_loop
    async def _wait_auto(self):
        await self.bot.wait_until_ready()

    @notify_10m.before_loop
    async def _wait_10m(self):
        await self.bot.wait_until_ready()

    @notify_5m.before_loop
    async def _wait_5m(self):
        await self.bot.wait_until_ready()

    @notify_1m.before_loop
    async def _wait_1m(self):
        await self.bot.wait_until_ready()

    @notify_10s.before_loop
    async def _wait_10s(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(AutoRestartCog(bot))