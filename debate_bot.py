import os
import math
import asyncio
import time
import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# The bot allows only ONE active speech timer across all servers.
# Maximum total speech duration: 60 minutes.
MAX_DURATION_SECONDS = 60 * 60
UPDATE_INTERVAL = 1.0


# ------------------------------------------------------------
# Bot setup
# ------------------------------------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def format_time(total_seconds: float) -> str:
    """Format seconds as MM:SS, rounding up while a timer is running."""
    seconds = max(0, int(math.ceil(total_seconds)))
    minutes = seconds // 60
    remainder = seconds % 60
    return "{:02d}:{:02d}".format(minutes, remainder)


def format_duration(total_seconds: float) -> str:
    seconds = max(0, int(round(total_seconds)))
    minutes = seconds // 60
    remainder = seconds % 60
    if minutes == 0:
        return "{} seconds".format(remainder)
    if remainder == 0:
        return "{} minute{}".format(minutes, "" if minutes == 1 else "s")
    return "{} minute{} and {} second{}".format(
        minutes, "" if minutes == 1 else "s",
        remainder, "" if remainder == 1 else "s"
    )


def can_moderate(member: discord.Member) -> bool:
    permissions = member.guild_permissions
    return permissions.manage_messages or permissions.administrator


# ------------------------------------------------------------
# Timer state and controls
# ------------------------------------------------------------
active_timer = None


class DebateTimer:
    def __init__(self, channel: discord.abc.Messageable, guild_id: int,
                 starter_id: int, duration_seconds: int):
        self.channel = channel
        self.guild_id = guild_id
        self.starter_id = starter_id
        self.duration_seconds = duration_seconds
        self.started_at = None
        self.message = None
        self.task = None
        self.started = False
        self.finished = False
        self.poi_opened = False
        self.poi_closed = False
        self.warning_sent = False
        self.view = TimerControls(self)
        self.view.stop_button.disabled = True

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

    def start(self):
        if not self.started and not self.finished:
            self.started = True
            self.started_at = time.monotonic()
            self.view.start_button.disabled = True
            self.view.stop_button.disabled = False
            self.task = asyncio.create_task(self.run())

    def remaining(self) -> float:
        return max(0.0, self.duration_seconds - self.elapsed())

    def make_embed(self, status: str = "Running") -> discord.Embed:
        elapsed = self.elapsed()
        remaining = self.remaining()

        if self.finished:
            colour = discord.Colour.dark_grey()
        elif remaining <= 30:
            colour = discord.Colour.orange()
        else:
            colour = discord.Colour.green()

        embed = discord.Embed(
            title="Debate Speech Timer",
            description="**{}**".format(format_time(remaining)),
            colour=colour
        )
        if not self.started and not self.finished:
            status = "Ready — press Start"

        embed.add_field(
            name="Status",
            value=status,
            inline=True
        )
        embed.add_field(
            name="Scheduled duration",
            value=format_time(self.duration_seconds),
            inline=True
        )
        embed.add_field(
            name="Elapsed",
            value=format_time(elapsed),
            inline=True
        )

        if self.poi_closed:
            poi_status = "Closed"
        elif self.poi_opened:
            poi_status = "Open"
        elif self.duration_seconds <= 120:
            poi_status = "No POI window (speech is 2 minutes or shorter)"
        else:
            poi_status = "Not open yet"

        embed.add_field(name="Points of Information", value=poi_status, inline=False)
        embed.set_footer(text="Only the speaker who started the timer or a moderator can control it.")
        return embed

    async def announce(self, content: str):
        try:
            await self.channel.send(content)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def update_message(self, status: str = "Running"):
        if self.message is None:
            return
        try:
            await self.message.edit(
                embed=self.make_embed(status),
                view=self.view
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def run(self):
        global active_timer
        last_shown = None

        try:
            while not self.finished:
                elapsed = self.elapsed()
                remaining = self.duration_seconds - elapsed

                # POIs are permitted from 1 minute elapsed until 1 minute
                # before the currently scheduled end. A speech of 2 minutes
                # or less has no POI window.
                if self.duration_seconds > 120:
                    if elapsed >= 60 and not self.poi_opened:
                        self.poi_opened = True
                        await self.announce(
                            "🟢 **POIs are now OPEN.** Points of Information may be offered."
                        )

                    if (self.poi_opened and not self.poi_closed
                            and remaining <= 60):
                        self.poi_closed = True
                        await self.announce(
                            "🔴 **POIs are now CLOSED.** No further Points of Information."
                        )

                if remaining <= 30 and not self.warning_sent:
                    self.warning_sent = True
                    await self.announce(
                        "⏳ **30 seconds remaining!**"
                    )

                # Refresh the countdown once per displayed second.
                displayed = format_time(remaining)
                if displayed != last_shown:
                    await self.update_message("Running")
                    last_shown = displayed

                if remaining <= 0:
                    self.finished = True
                    await self.finish(manual=False)
                    break

                await asyncio.sleep(UPDATE_INTERVAL)

        except asyncio.CancelledError:
            raise
        finally:
            if self.finished and active_timer is self:
                active_timer = None

    async def finish(self, manual: bool):
        global active_timer

        if self.finished is False:
            self.finished = True

        actual_seconds = self.elapsed()
        scheduled_seconds = self.duration_seconds

        for item in self.view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        if manual:
            status = "Stopped manually"
            announcement = (
                "⏹️ **Speech timer stopped manually.**\n"
                "Final speaking time: **{}**.".format(format_time(actual_seconds))
            )
        else:
            status = "Time expired"
            announcement = (
                "🔔 **Time is up!**\n"
                "Final speaking time: **{}**.".format(format_time(actual_seconds))
            )

        await self.update_message(status)
        await self.announce(announcement)

        if active_timer is self:
            active_timer = None


class TimerControls(discord.ui.View):
    def __init__(self, timer: DebateTimer):
        super().__init__(timeout=None)
        self.timer = timer

    async def can_control(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "These controls can only be used inside a server.",
                ephemeral=True
            )
            return False
        if member.id != self.timer.starter_id and not can_moderate(member):
            await interaction.response.send_message(
                "Only the person who created this timer or a moderator can control it.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Start",
        style=discord.ButtonStyle.success,
        emoji="▶️",
        custom_id="debate_timer_start"
    )
    async def start_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if not await self.can_control(interaction):
            return
        if self.timer.started or self.timer.finished:
            await interaction.response.send_message(
                "This timer has already been started or finished.",
                ephemeral=True
            )
            return

        self.timer.start()
        await interaction.response.defer()
        await self.timer.update_message("Running")

    @discord.ui.button(
        label="Stop",
        style=discord.ButtonStyle.danger,
        emoji="⏹️",
        custom_id="debate_timer_stop"
    )
    async def stop_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        if not await self.can_control(interaction):
            return

        if not self.timer.started:
            await interaction.response.send_message(
                "Start the timer before stopping it.",
                ephemeral=True
            )
            return

        if self.timer.finished:
            await interaction.response.send_message(
                "This timer has already finished.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        self.timer.finished = True

        if self.timer.task is not None:
            self.timer.task.cancel()

        await self.timer.finish(manual=True)


# ------------------------------------------------------------
# Slash commands
# ------------------------------------------------------------
@bot.tree.command(
    name="time",
    description="Start a debate speech timer (defaults to 8 minutes)."
)
@app_commands.describe(
    minutes="Speech duration in minutes (default: 8).",
    seconds="Additional seconds (default: 0).",
    adjustment="Add or subtract seconds from the duration (e.g. 30 or -60)."
)
async def time_command(
    interaction: discord.Interaction,
    minutes: app_commands.Range[int, 0, 60] = 8,
    seconds: app_commands.Range[int, 0, 59] = 0,
    adjustment: app_commands.Range[int, -3599, 3599] = 0
):
    global active_timer

    if interaction.guild is None:
        await interaction.response.send_message(
            "The debate timer can only be used in a server.",
            ephemeral=True
        )
        return

    requested_duration = (minutes * 60) + seconds + adjustment

    if requested_duration <= 0:
        await interaction.response.send_message(
            "The final duration must be at least 1 second.",
            ephemeral=True
        )
        return

    if requested_duration > MAX_DURATION_SECONDS:
        await interaction.response.send_message(
            "The maximum speech duration is 60 minutes.",
            ephemeral=True
        )
        return

    if active_timer is not None and not active_timer.finished:
        await interaction.response.send_message(
            "A timer is already running in {}. Only one timer can run at a time.".format(
                getattr(active_timer.channel, "mention", "another channel")
            ),
            ephemeral=True
        )
        return

    timer = DebateTimer(
        channel=interaction.channel,
        guild_id=interaction.guild.id,
        starter_id=interaction.user.id,
        duration_seconds=requested_duration
    )

    # Reserve the single global timer before awaiting the Discord API.
    active_timer = timer

    try:
        await interaction.response.send_message(
            content="⏱️ **Speech timer is ready. Press Start when the speaker begins.**",
            embed=timer.make_embed(),
            view=timer.view
        )
        timer.message = await interaction.original_response()
        timer.task = asyncio.create_task(timer.run())
    except Exception:
        if active_timer is timer:
            active_timer = None
        raise


@bot.tree.command(
    name="timeadd",
    description="Add time to the currently running debate timer."
)
@app_commands.describe(
    seconds="Number of seconds to add to the running timer."
)
async def timeadd_command(
    interaction: discord.Interaction,
    seconds: app_commands.Range[int, 1, 3600]
):
    global active_timer

    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used by a server moderator.",
            ephemeral=True
        )
        return

    if not can_moderate(interaction.user):
        await interaction.response.send_message(
            "You need the Manage Messages permission (or Administrator) to add time.",
            ephemeral=True
        )
        return

    if active_timer is None or active_timer.finished or not active_timer.started:
        await interaction.response.send_message(
            "There is no running speech timer. Start the timer first.",
            ephemeral=True
        )
        return

    if interaction.guild.id != active_timer.guild_id:
        await interaction.response.send_message(
            "The active timer belongs to another server.",
            ephemeral=True
        )
        return

    if interaction.channel_id != getattr(active_timer.channel, "id", None):
        await interaction.response.send_message(
            "Please add time in the channel where the timer is running.",
            ephemeral=True
        )
        return

    if active_timer.duration_seconds + seconds > MAX_DURATION_SECONDS:
        await interaction.response.send_message(
            "That would exceed the 60-minute maximum.",
            ephemeral=True
        )
        return

    active_timer.duration_seconds += seconds
    await active_timer.update_message("Running — time added")

    await interaction.response.send_message(
        "➕ Added **{}** to the speech. New scheduled duration: **{}**.".format(
            format_duration(seconds),
            format_time(active_timer.duration_seconds)
        ),
        ephemeral=False
    )


# ------------------------------------------------------------
# Startup
# ------------------------------------------------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print("Synced {} slash command(s).".format(len(synced)))
    except Exception as error:
        print("Slash command sync failed: {}".format(error))

    print("Logged in as {}".format(bot.user))


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Set the DISCORD_BOT_TOKEN environment variable before starting the bot."
        )

    bot.run(TOKEN)
