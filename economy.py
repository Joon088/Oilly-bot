# economy.py
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import random
import asyncio

MIN_BET = 1000
DAILY_AMOUNT = 50_000
DAILY_COOLDOWN_SECONDS = 60 * 60 * 24  # 24시간

# =========================================================
# 🎰 슬롯 확률표
# =========================================================
SLOT_TABLE = [
    {"key": "jackpot", "prob": 1,  "mult": 10, "text": "💎 잭팟!! x10"},
    {"key": "win",     "prob": 24, "mult": 2,  "text": "🎉 성공 x2"},
    {"key": "draw",    "prob": 25, "mult": 0,  "text": "😐 무승부"},
    {"key": "lose",    "prob": 40, "mult": -1, "text": "❌ 실패"},
    {"key": "fail",    "prob": 10, "mult": -2, "text": "💀 대실패"},
]

SLOT_REELS = {
    "jackpot": ["💎", "💎", "💎"],
    "win":     ["⭐", "⭐", "🍒"],
    "draw":    ["🍋", "🔔", "🍇"],
    "lose":    ["🍒", "🍋", "🍇"],
    "fail":    ["💀", "💀", "💀"],
}
SLOT_EMOJIS = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎", "💀"]


# =========================
# 시간/유틸
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seconds_left(then_iso: str, cooldown_sec: int) -> int:
    if not then_iso:
        return 0
    try:
        then = datetime.fromisoformat(then_iso)
        now = datetime.now(timezone.utc)
        diff = (now - then).total_seconds()
        left = int(cooldown_sec - diff)
        return max(left, 0)
    except Exception:
        return 0


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
    except Exception:
        pass


def _slot_total_prob() -> int:
    return max(sum(int(x.get("prob", 0)) for x in SLOT_TABLE), 0)


def pick_slot():
    total = _slot_total_prob()
    if total <= 0:
        return {"key": "lose", "prob": 1, "mult": -1, "text": "❌ 실패"}

    r = random.randint(1, total)
    acc = 0
    for item in SLOT_TABLE:
        p = int(item.get("prob", 0))
        if p <= 0:
            continue
        acc += p
        if r <= acc:
            return item
    return SLOT_TABLE[-1]


# =========================
# DB
# =========================
async def init_economy_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=10000;")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS money (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            last_daily TEXT
        )
        """)
        await db.commit()


async def ensure_user(db_path: str, user_id: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT OR IGNORE INTO money(user_id, balance, last_daily)
            VALUES(?, 0, NULL)
        """, (int(user_id),))
        await db.commit()


async def get_balance(db_path: str, guild_id: int, user_id: int) -> int:
    await ensure_user(db_path, user_id)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT balance FROM money WHERE user_id=?", (int(user_id),))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def add_balance(db_path: str, guild_id: int, user_id: int, delta: int) -> int:
    await ensure_user(db_path, user_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE money SET balance = MAX(balance + ?, 0) WHERE user_id=?",
            (int(delta), int(user_id))
        )
        await db.commit()
        cur = await db.execute("SELECT balance FROM money WHERE user_id=?", (int(user_id),))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def get_last_daily(db_path: str, guild_id: int, user_id: int):
    await ensure_user(db_path, user_id)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT last_daily FROM money WHERE user_id=?", (int(user_id),))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_last_daily(db_path: str, guild_id: int, user_id: int, iso: str):
    await ensure_user(db_path, user_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE money SET last_daily=? WHERE user_id=?", (iso, int(user_id)))
        await db.commit()


# =========================
# 🎰 슬롯
# =========================
async def animate_slot(interaction: discord.Interaction, db_path: str, bet: int):
    if interaction.guild_id is None:
        await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어!", ephemeral=True)
        return

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    bal = await get_balance(db_path, guild_id, user_id)

    if bet < MIN_BET:
        await interaction.response.send_message(f"최소배팅은 **{MIN_BET:,}원**!", ephemeral=True)
        return
    if bal < bet:
        await interaction.response.send_message("❌ 돈 부족!", ephemeral=True)
        return

    picked = pick_slot()
    key = picked["key"]
    mult = int(picked["mult"])
    reels = SLOT_REELS.get(key, ["❔", "❔", "❔"])
    result = picked.get("text", "결과")

    await safe_defer(interaction)

    msg = await interaction.followup.send("🎰 슬롯 시작...", wait=True)

    try:
        for _ in range(6):
            await asyncio.sleep(0.25)
            spin = [random.choice(SLOT_EMOJIS) for _ in range(3)]
            await msg.edit(content=f"🎰 [{spin[0]}][{spin[1]}][{spin[2]}]")

        await asyncio.sleep(0.25)

        delta = bet * mult
        new_bal = await add_balance(db_path, guild_id, user_id, delta)

        await msg.edit(content=(
            f"🎰 [{reels[0]}][{reels[1]}][{reels[2]}]\n"
            f"{result}\n"
            f"배팅: **{bet:,}원**\n"
            f"변동: **{delta:+,}원**\n"
            f"잔액: **{new_bal:,}원**"
        ))
    except Exception as e:
        try:
            await interaction.followup.send(f"⚠️ 슬롯 연출 실패: `{type(e).__name__}`", ephemeral=True)
        except Exception:
            pass


# =========================
# Commands 등록
# =========================
def register_economy(
    bot: commands.Bot,
    db_path: str,
    active_players: set[int] | None = None,
    active_games: dict[int, dict] | None = None,
):
    @bot.tree.command(name="잔액", description="내 돈 잔액 확인(상대도 조회 가능)")
    @app_commands.describe(대상="(선택) 잔액을 확인할 사람")
    async def balance(interaction: discord.Interaction, 대상: discord.Member | None = None):
        if interaction.guild_id is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어!", ephemeral=True)
            return

        target = 대상 or interaction.user
        bal = await get_balance(db_path, interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"💰 {target.mention} 잔액: **{bal:,}원**",
            ephemeral=True
        )

    @bot.tree.command(name="돈", description="기본 자금 50,000원 지급 (24시간 쿨타임)")
    async def daily_money(interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어!", ephemeral=True)
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        last = await get_last_daily(db_path, guild_id, user_id)
        left = seconds_left(last, DAILY_COOLDOWN_SECONDS)
        if left > 0:
            h = left // 3600
            m = (left % 3600) // 60
            s = left % 60
            await interaction.response.send_message(
                f"⏰ 아직 24시간 안 지났어! 남은시간 {h}시간 {m}분 {s}초",
                ephemeral=True
            )
            return

        await add_balance(db_path, guild_id, user_id, DAILY_AMOUNT)
        await set_last_daily(db_path, guild_id, user_id, now_iso())
        bal = await get_balance(db_path, guild_id, user_id)
        await interaction.response.send_message(
            f"💰 기본 자금 **{DAILY_AMOUNT:,}원** 지급 완료! 현재 잔액: **{bal:,}원**"
        )

    @bot.tree.command(name="슬롯", description="슬롯머신")
    @app_commands.describe(배팅="최소 1,000원")
    async def slot(interaction: discord.Interaction, 배팅: int):
        await animate_slot(interaction, db_path, int(배팅))

    @bot.tree.command(name="올인", description="전재산 올인 슬롯")
    async def allin(interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어!", ephemeral=True)
            return

        bal = await get_balance(db_path, interaction.guild_id, interaction.user.id)
        if bal < MIN_BET:
            await interaction.response.send_message(
                "돈이 너무 없어서 올인 불가… 최소 1,000원 필요!",
                ephemeral=True
            )
            return

        await animate_slot(interaction, db_path, bal)

    @bot.tree.command(name="돈랭킹", description="서버 돈 랭킹 TOP10")
    async def money_rank(interaction: discord.Interaction):
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어!", ephemeral=True)
            return

        guild = interaction.guild
        member_ids = {m.id for m in guild.members if not m.bot}

        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT user_id, balance FROM money ORDER BY balance DESC LIMIT 2000"
            )
            rows = await cur.fetchall()

        picked = []
        for uid_, bal in rows:
            if int(uid_) in member_ids:
                picked.append((int(uid_), int(bal)))
                if len(picked) >= 10:
                    break

        if not picked:
            await interaction.response.send_message("데이터 없음")
            return

        txt = "🏆 **돈 랭킹 TOP10**\n"
        for i, (uid_, bal) in enumerate(picked, 1):
            member = guild.get_member(int(uid_))
            name = member.display_name if member else f"User({uid_})"
            txt += f"{i}. {name} - **{int(bal):,}원**\n"

        await interaction.response.send_message(txt)

    @bot.tree.command(name="송금", description="돈 보내기")
    @app_commands.describe(대상="받는 사람", 금액="보낼 금액")
    async def send_money(interaction: discord.Interaction, 대상: discord.Member, 금액: int):
        if interaction.guild_id is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있어!", ephemeral=True)
            return

        guild_id = interaction.guild_id
        sender = interaction.user

        if 대상.bot:
            await interaction.response.send_message("봇에게 송금 불가", ephemeral=True)
            return
        if 대상.id == sender.id:
            await interaction.response.send_message("자기 자신에게 송금 불가", ephemeral=True)
            return
        if 금액 <= 0:
            await interaction.response.send_message("금액은 1원 이상!", ephemeral=True)
            return

        sender_bal = await get_balance(db_path, guild_id, sender.id)
        if sender_bal < 금액:
            await interaction.response.send_message("돈 부족", ephemeral=True)
            return

        await add_balance(db_path, guild_id, sender.id, -금액)
        await add_balance(db_path, guild_id, 대상.id, 금액)

        await interaction.response.send_message(
            f"💸 송금완료!\n"
            f"{sender.mention} ➜ {대상.mention}\n"
            f"금액: **{금액:,}원**"
        )