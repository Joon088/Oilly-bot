# main.py
import os
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

from counting import init_counting_db, register_counting
import economy as econ_module
from economy import init_economy_db, register_economy
from purchase import register_purchase
from join_check import register_join

DB_PATH = os.getenv("OILLY_DB_PATH", "oilly_counts.db")

ACTIVE_PLAYERS: set[int] = set()
ACTIVE_GAMES: dict[int, dict] = {}

GAMBLE_COMMANDS = {
    "슬롯",
    "올인",
}

async def init_gamble_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS gamble_channel(
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        )
        """)
        await db.commit()

async def set_gamble_channel(guild_id: int, channel_id: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        if channel_id is None:
            await db.execute("DELETE FROM gamble_channel WHERE guild_id=?", (int(guild_id),))
        else:
            await db.execute("""
            INSERT INTO gamble_channel(guild_id, channel_id)
            VALUES(?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET channel_id=excluded.channel_id
            """, (int(guild_id), int(channel_id)))
        await db.commit()

async def get_gamble_channel(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT channel_id FROM gamble_channel WHERE guild_id=?",
            (int(guild_id),)
        )
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None


class GambleCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id:
            return True

        cmd_name = None
        try:
            if interaction.command:
                cmd_name = interaction.command.name
        except:
            cmd_name = None

        if not cmd_name:
            try:
                if isinstance(interaction.data, dict):
                    cmd_name = interaction.data.get("name")
            except:
                cmd_name = None

        if not cmd_name or cmd_name not in GAMBLE_COMMANDS:
            return True

        allowed = await get_gamble_channel(interaction.guild_id)

        if allowed is None:
            return True

        if interaction.channel_id == allowed:
            return True

        msg = f"🎰 도박은 <#{allowed}> 채널에서만 가능해!"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except:
            pass

        return False


class OillyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=GambleCommandTree,  # ✅ 여기 핵심 (tree 교체 X, 생성 시 장착)
        )

    async def setup_hook(self):
        await init_counting_db(DB_PATH)
        await init_economy_db(DB_PATH)
        await init_gamble_db()

        register_counting(self, DB_PATH)

        register_purchase(self, DB_PATH)

        register_economy(
            self,
            DB_PATH,
            active_players=ACTIVE_PLAYERS,
            active_games=ACTIVE_GAMES,
        )

        register_join(self, DB_PATH)


        @self.tree.command(name="도박채널설정", description="현재 채널을 도박 채널로 설정(미설정이면 전채널 허용)")
        async def gamble_set(interaction: discord.Interaction):
            if not interaction.guild_id:
                await interaction.response.send_message("서버에서만 가능", ephemeral=True)
                return
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("관리자만 가능", ephemeral=True)
                return
            await set_gamble_channel(interaction.guild_id, interaction.channel_id)
            await interaction.response.send_message("✅ 현재 채널을 도박 채널로 설정 완료")

        @self.tree.command(name="도박채널해제", description="도박 채널 제한 해제(전채널 허용)")
        async def gamble_unset(interaction: discord.Interaction):
            if not interaction.guild_id:
                await interaction.response.send_message("서버에서만 가능", ephemeral=True)
                return
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("관리자만 가능", ephemeral=True)
                return
            await set_gamble_channel(interaction.guild_id, None)
            await interaction.response.send_message("✅ 도박 채널 제한 해제 완료! (전채널 허용)")

        synced = await self.tree.sync()
        print(f"✅ synced: {len(synced)} commands")


def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("환경변수 DISCORD_BOT_TOKEN 에 봇 토큰을 넣어주세요.")

    bot = OillyBot()
    bot.run(token)


if __name__ == "__main__":
    main()
