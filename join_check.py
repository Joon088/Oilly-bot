import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiosqlite

JOINS = {}


# =========================
# 계좌 가져오기
# =========================
async def get_user_account(db_path: str, user_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT account_number FROM accounts WHERE user_id=?",
            (int(user_id),)
        )
        row = await cur.fetchone()
        return row[0] if row else "미등록"


# =========================
# 진행중 embed 갱신
# =========================
def build_progress_embed(title: str, count: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 {title} 참여 인원 체크",
        description="아래 버튼으로 참여 여부를 눌러주세요.\n⏳ 5분 후 자동 마감됩니다.",
        color=0x2ecc71
    )
    embed.add_field(name="상태", value="진행중", inline=False)
    embed.add_field(name="현재 참여 인원", value=f"{count}명", inline=False)
    return embed


# =========================
# 마감 embed 생성
# =========================
def build_final_embed(title: str, result_text: str, total_count: int) -> discord.Embed:
    final_embed = discord.Embed(
        title=f"📋 {title} 참여 인원 체크",
        description="아래는 최종 참여 인원입니다.",
        color=0x95a5a6
    )
    final_embed.add_field(name="상태", value="마감", inline=False)
    final_embed.add_field(name="총 참여 인원", value=f"{total_count}명", inline=False)
    final_embed.add_field(name="참여자 목록", value=result_text, inline=False)
    return final_embed


# =========================
# View
# =========================
class JoinView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id

    async def refresh_message(self, interaction: discord.Interaction, title: str, count: int):
        embed = build_progress_embed(title, count)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="참여", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = JOINS.get(self.message_id)

        if not data:
            await interaction.response.send_message("이미 마감됐어요.", ephemeral=True)
            return

        user_id = interaction.user.id

        if user_id in data["user_set"]:
            await interaction.response.send_message("이미 참여했어요.", ephemeral=True)
            return

        data["user_set"].add(user_id)
        data["user_list"].append(user_id)

        await self.refresh_message(
            interaction,
            data["title"],
            len(data["user_list"])
        )

    @discord.ui.button(label="참여취소", style=discord.ButtonStyle.danger)
    async def cancel_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = JOINS.get(self.message_id)

        if not data:
            await interaction.response.send_message("이미 마감됐어요.", ephemeral=True)
            return

        user_id = interaction.user.id

        if user_id not in data["user_set"]:
            await interaction.response.send_message("참여한 기록이 없어요.", ephemeral=True)
            return

        data["user_set"].remove(user_id)

        try:
            data["user_list"].remove(user_id)
        except ValueError:
            pass

        await self.refresh_message(
            interaction,
            data["title"],
            len(data["user_list"])
        )


# =========================
# 명령어
# =========================
def register_join(bot: commands.Bot, db_path: str):

    @bot.tree.command(name="참여", description="인원 체크")
    @app_commands.describe(제목="예: 기업전쟁")
    async def join_cmd(interaction: discord.Interaction, 제목: str):
        embed = build_progress_embed(제목, 0)
        view = JoinView(0)

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        JOINS[msg.id] = {
            "title": 제목,
            "user_set": set(),
            "user_list": []
        }

        view.message_id = msg.id

        # 5분 대기
        await asyncio.sleep(300)

        data = JOINS.pop(msg.id, None)
        if not data:
            return

        user_list = data["user_list"]

        result_lines = []
        for i, uid in enumerate(user_list, 1):
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"User({uid})"
            account = await get_user_account(db_path, uid)
            result_lines.append(f"{i}. {name} - {account}")

        result_text = "\n".join(result_lines) if result_lines else "참여자 없음"

        final_embed = build_final_embed(
            title=제목,
            result_text=result_text,
            total_count=len(user_list)
        )

        # 버튼 완전 제거
        await msg.edit(embed=final_embed, view=None)
