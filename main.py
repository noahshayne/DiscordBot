from __future__ import annotations

import asyncio
import logging
import os
import json
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from bson.json_util import dumps as bson_dumps
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
MONGODB_DATABASE = "test"
HARDWARE_ITEMS = "hardwareitems"
COLLECTION_CHECKOUTS = "hardwarecheckouts"
USER_COLLECTION = "users"
EVENT_TIMEZONE = ZoneInfo(os.getenv("EVENT_TIMEZONE", "America/New_York"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
mongo_client: MongoClient | None = MongoClient(MONGODB_URI) if MONGODB_URI else None


def _hardware_collection() -> Collection:
    if mongo_client is None:
        raise RuntimeError(
            "MONGODB_URI is not set. Set it in your environment or .env."
        )
    return mongo_client[MONGODB_DATABASE][HARDWARE_ITEMS]


def checkIfDiscordLinked(discordIDArg: str) -> object | None:
    # DiscordIDArg is argument which comes from a user typing in a discord command (interaction.user.id)
    """Return the MongoDB _id for the user document associated which has a given discordId value."""
    if mongo_client is None:
        raise RuntimeError(
            "MONGODB_URI is not set. Set it in your environment or .env."
        )

    # The first  find_one argument filters for finding a document; the second filters the key-value output of document returned
    user = mongo_client[MONGODB_DATABASE][USER_COLLECTION].find_one(
        {"discordId": discordIDArg},
        {"_id": 1},
    )
    # Return the document ID, or None when no user is linked.
    return user["_id"] if user is not None else None


def _get_checked_out_item_names(user_id: object) -> list[str]:
    """Return the names of all items checked out by a dashboard user."""
    if mongo_client is None:
        raise RuntimeError(
            "MONGODB_URI is not set. Set it in your environment or .env."
        )

    checkouts = mongo_client[MONGODB_DATABASE][COLLECTION_CHECKOUTS].find(
        {"user": user_id}
    )
    item_collection = mongo_client[MONGODB_DATABASE][HARDWARE_ITEMS]
    item_names: list[str] = []

    for checkout in checkouts:
        item_id = checkout.get("item")
        if item_id is None:
            raise LookupError("A hardware checkout is missing its item value.")

        item = item_collection.find_one({"_id": item_id}, {"name": 1})
        if item is None:
            raise LookupError(
                f"No hardware item was found for checkout item ID '{item_id}'."
            )

        item_name = item.get("name")
        if not isinstance(item_name, str):
            raise LookupError(f"Hardware item '{item_id}' has no valid name value.")
        item_names.append(item_name)

    return item_names


@dataclass(frozen=True)
class ScheduledEvent:
    start: datetime
    end: datetime
    name: str
    location: str


def _load_event_schedule(raw: str) -> list[ScheduledEvent]:
    """Parse EVENT_SCHEDULE lines of the form 'start | end | name | location'."""

    def parse_event_time(value: str) -> datetime:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(
            tzinfo=EVENT_TIMEZONE
        )

    events: list[ScheduledEvent] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            logging.warning(
                "Skipping malformed EVENT_SCHEDULE line %d: %r", line_number, line
            )
            continue
        start_text, end_text, name = parts[0], parts[1], parts[2]
        location = parts[3] if len(parts) > 3 else ""
        try:
            start = parse_event_time(start_text)
            end = parse_event_time(end_text)
        except ValueError:
            logging.warning(
                "Skipping EVENT_SCHEDULE line %d with a bad time: %r", line_number, line
            )
            continue
        events.append(ScheduledEvent(start, end, name, location))
    return sorted(events, key=lambda event: event.start)


EVENT_SCHEDULE = _load_event_schedule(os.getenv("EVENT_SCHEDULE", ""))


def _format_event(event: ScheduledEvent) -> str:
    start_ts = int(event.start.timestamp())
    end_ts = int(event.end.timestamp())
    line = f"- **{event.name}** (<t:{start_ts}:t> - <t:{end_ts}:t>)"
    if event.location:
        line += f" @ {event.location}"
    return line


def _get_available_quantity(name: str) -> int | float:
    """Find one hardware item by its exact name and return availableQty."""
    item = _hardware_collection().find_one({"name": name})
    if item is None:
        raise LookupError(f"No hardware item named '{name}' was found.")

    item_json = json.loads(bson_dumps(item))
    if "availableQty" not in item_json:
        raise LookupError(f"Hardware item '{name}' has no availableQty value.")
    return item_json["availableQty"]


def _get_name_suggestions(current: str) -> list[str]:
    """Find hardware names that start with the user's autocomplete input."""
    search_text = current.strip()
    name_filter = {"name": {"$regex": f"^{re.escape(search_text)}", "$options": "i"}}
    names = (
        _hardware_collection()
        .find(name_filter, {"_id": 0, "name": 1})
        .sort("name", 1)
        .limit(3)
    )
    return [name for item in names if isinstance(name := item.get("name"), str)]


@bot.event
async def on_ready() -> None:
    logging.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    guild_id = os.getenv("DISCORD_GUILD_ID")
    try:
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild)
            commands_synced = await bot.tree.sync(guild=guild)
            logging.info(
                "Synced %d app commands to guild ID %s", len(commands_synced), guild_id
            )
        else:
            commands_synced = await bot.tree.sync()
            logging.info("Synced %d global app commands", len(commands_synced))
    except (ValueError, discord.DiscordException):
        logging.exception("Failed to sync app commands")


# Inventory command
@bot.tree.command(name="inventory", description="Get available hardware quantity")
@app_commands.describe(name="Exact hardware item name")
async def inventory(interaction: discord.Interaction, name: str) -> None:
    """Look up a hardware item after the user submits the slash command."""
    name = name.strip()
    if not name:
        await interaction.response.send_message(
            "Please enter a hardware item to search for.",
            ephemeral=True,
        )
        return

    try:
        available_quantity = await asyncio.to_thread(_get_available_quantity, name)
    except LookupError:
        await interaction.response.send_message("Error", ephemeral=True)
        return
    except (PyMongoError, RuntimeError):
        logging.exception("MongoDB search failed")
        await interaction.response.send_message(
            f"Unable to search the hardware inventory",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"**{name}** has **{available_quantity}** available.",
        ephemeral=True,
    )  # Discoird output of inventory command is only visible to user who ran it


# Autocomplete for inventory command
@inventory.autocomplete("name")
async def inventory_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Provide hardware name suggestions while the user types."""
    try:
        names = await asyncio.to_thread(_get_name_suggestions, current)
    except (PyMongoError, RuntimeError):
        logging.exception("MongoDB autocomplete search failed")
        return []

    return [app_commands.Choice(name=name, value=name) for name in names]


@bot.tree.command(name="checkedout", description="List the hardware checked out to you")
async def checkedout(interaction: discord.Interaction) -> None:
    """List the names of all hardware items checked out by the Discord user."""

    try:
        user_id = await asyncio.to_thread(
            checkIfDiscordLinked, str(interaction.user.id)
        )  # get mongodb user ID associated with current discordID
        if user_id is None:
            await interaction.response.send_message(
                "Your Discord account isn't linked to the HackUMass dashboard account. Link it via the dashboard, then try again",
                ephemeral=True,
            )
            return

        item_names = await asyncio.to_thread(_get_checked_out_item_names, user_id)
    except LookupError:
        await interaction.response.send_message("Error", ephemeral=True)
        return
    except (PyMongoError, RuntimeError):
        logging.exception("MongoDB checked-out items search failed")
        await interaction.response.send_message(
            f"Unable to search checked-out items",
            ephemeral=True,
        )
        return

    if not item_names:
        await interaction.response.send_message(
            "You do not have any hardware checked out.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Hardware checked out to you:\n"
        + "\n".join(f"- {name}" for name in item_names),
        ephemeral=True,
    )


@bot.tree.command(
    name="currently-playing", description="Show what's happening at HackUMass right now"
)
async def currently_playing(interaction: discord.Interaction) -> None:
    """Show the events currently in progress, or the next one if nothing is on."""
    if not EVENT_SCHEDULE:
        await interaction.response.send_message(
            "Whoops, there was an error. Contact an organizer!", ephemeral=True
        )
        return

    now = datetime.now(EVENT_TIMEZONE)
    current = [event for event in EVENT_SCHEDULE if event.start <= now < event.end]
    upcoming = [event for event in EVENT_SCHEDULE if event.start > now]

    if current:
        message = "**Happening now:**\n" + "\n".join(
            _format_event(event) for event in current
        )
        if upcoming:
            next_start = upcoming[0].start
            next_events = [event for event in upcoming if event.start == next_start]
            message += (
                "\n\n**Up next** (<t:" + str(int(next_start.timestamp())) + ":R>):\n"
            )
            message += "\n".join(_format_event(event) for event in next_events)
    elif upcoming:
        next_start = upcoming[0].start
        next_events = [event for event in upcoming if event.start == next_start]
        message = (
            "Nothing is happening right now.\n\n**Up next** (<t:"
            + str(int(next_start.timestamp()))
            + ":R>):\n"
            + "\n".join(_format_event(event) for event in next_events)
        )
    else:
        message = "HackUMass is over. Thanks for coming!"

    await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set.")
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI not set. Set it in your environment or .env.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
