from __future__ import annotations

import asyncio
import logging
import os
import json

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
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "hardware_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "hardwareitems")



logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
mongo_client: MongoClient | None = MongoClient(MONGODB_URI) if MONGODB_URI else None


def _hardware_collection() -> Collection:
	if mongo_client is None:
		raise RuntimeError("MONGODB_URI is not set. Set it in your environment or .env.")
	return mongo_client[MONGODB_DATABASE][MONGODB_COLLECTION]


def _get_available_quantity(name: str) -> int | float:
	"""Find one hardware item by its exact name and return availableQty."""
	item = _hardware_collection().find_one({"name": name})
	if item is None:
		raise LookupError(f"No hardware item named '{name}' was found.")

	item_json = json.loads(bson_dumps(item))
	if "availableQty" not in item_json:
		raise LookupError(f"Hardware item '{name}' has no availableQty value.")
	return item_json["availableQty"]


@bot.event
async def on_ready() -> None:
	logging.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
	guild_id = os.getenv("DISCORD_GUILD_ID")
	try:
		if guild_id:
			guild = discord.Object(id=int(guild_id))
			bot.tree.copy_global_to(guild=guild)
			commands_synced = await bot.tree.sync(guild=guild)
			logging.info("Synced %d app commands to guild ID %s", len(commands_synced), guild_id)
		else:
			commands_synced = await bot.tree.sync()
			logging.info("Synced %d global app commands", len(commands_synced))
	except (ValueError, discord.DiscordException):
		logging.exception("Failed to sync app commands")


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
	except LookupError as error:
		await interaction.response.send_message(str(error), ephemeral=True)
		return
	except (PyMongoError, RuntimeError) as error:
		logging.exception("MongoDB search failed")
		await interaction.response.send_message(
			f"Unable to search the hardware inventory: {error}",
			ephemeral=True,
		)
		return

	await interaction.response.send_message(
		f"**{name}** has **{available_quantity}** available."
	)


@bot.tree.command(name="hardwarelist", description="Get the hardware inventory link")
async def hardwarelist(interaction: discord.Interaction) -> None:
	"""Send a link to the hardware inventory/resource list."""
	await interaction.response.send_message("HI")


def main() -> None:
	if not TOKEN:
		raise RuntimeError("DISCORD_TOKEN not set.")
	if not MONGODB_URI:
		raise RuntimeError("MONGODB_URI not set. Set it in your environment or .env.")
	bot.run(TOKEN)


if __name__ == "__main__":
	main()
