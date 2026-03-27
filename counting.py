# counting.py
import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# -----------------------------
# 가격표
# -----------------------------
# 기본 5개 포함 단가
BASE_FUEL_TO_PAY = {
    16: 30_000_000,
    18: 32_500_000,
    20: 35_000_000,
    22: 37_500_000,
    24: 40_000_000,
}

# 기름통 추가 1개당 단가 (기본 5개 초과분만 적용)
EXTRA_CAN_TO_PAY = {
    16: 5_000_000,
    18: 5_500_000,
    20: 6_000_000,
    22: 6_500_000,
    24: 7_000_000,
}

POLISH_EXTRA_PAY = 10_000_000
MIN_FUEL_CAN_COUNT = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_week_key_kst() -> str:
    now = datetime.now(KST)
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-{iso_week:02d}"


def calculate_pay_per_count(fuel_price: int, fuel_can_count: int = 5, has_polish: bool = False) -> int:
    base_pay = BASE_FUEL_TO_PAY[fuel_price]
    extra_per_can = EXTRA_CAN_TO_PAY[fuel_price]

    extra_cans = max(fuel_can_count - MIN_FUEL_CAN_COUNT, 0)
    extra_pay = extra_cans * extra_per_can
    polish_pay = POLISH_EXTRA_PAY if has_polish else 0

    return base_pay + extra_pay + polish_pay


# -----------------------------
# DB init / migration
# -----------------------------
async def init_counting_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER PRIMARY KEY,
            allowed_channel_id INTEGER,
            fuel_price INTEGER,
            last_reset_week TEXT,
            updated_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            announcer_nick TEXT NOT NULL,
            total INTEGER NOT NULL,
            remaining INTEGER NOT NULL,
            fuel_can_count INTEGER NOT NULL DEFAULT 5,
            has_polish INTEGER NOT NULL DEFAULT 0,
            pay_per_count INTEGER NOT NULL,
            is_open INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            is_void INTEGER NOT NULL DEFAULT 0,
            voided_at TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            account_number TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        # 예전 DB 호환용 migration
        cur = await db.execute("PRAGMA table_info(config)")
        config_columns = [row[1] for row in await cur.fetchall()]

        if "last_reset_week" not in config_columns:
            await db.execute("ALTER TABLE config ADD COLUMN last_reset_week TEXT")

        cur = await db.execute("PRAGMA table_info(sessions)")
        session_columns = [row[1] for row in await cur.fetchall()]

        if "fuel_can_count" not in session_columns:
            await db.execute("ALTER TABLE sessions ADD COLUMN fuel_can_count INTEGER NOT NULL DEFAULT 5")

        if "has_polish" not in session_columns:
            await db.execute("ALTER TABLE sessions ADD COLUMN has_polish INTEGER NOT NULL DEFAULT 0")

        await db.commit()


# -----------------------------
# Config helpers
# -----------------------------
async def ensure_config_row(db_path: str, guild_id: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT OR IGNORE INTO config(
                guild_id,
                allowed_channel_id,
                fuel_price,
                last_reset_week,
                updated_at
            )
            VALUES(?, NULL, NULL, NULL, ?)
        """, (guild_id, now_iso()))
        await db.commit()


async def set_allowed_channel(db_path: str, guild_id: int, channel_id: int):
    await ensure_config_row(db_path, guild_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            UPDATE config
            SET allowed_channel_id=?, updated_at=?
            WHERE guild_id=?
        """, (channel_id, now_iso(), guild_id))
        await db.commit()


async def set_fuel_price(db_path: str, guild_id: int, fuel_price: int):
    await ensure_config_row(db_path, guild_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            UPDATE config
            SET fuel_price=?, updated_at=?
            WHERE guild_id=?
        """, (fuel_price, now_iso(), guild_id))
        await db.commit()


async def reset_fuel_price(db_path: str, guild_id: int, week_key: str):
    await ensure_config_row(db_path, guild_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            UPDATE config
            SET fuel_price=NULL,
                last_reset_week=?,
                updated_at=?
            WHERE guild_id=?
        """, (week_key, now_iso(), guild_id))
        await db.commit()


async def get_config(db_path: str, guild_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT allowed_channel_id, fuel_price, last_reset_week
            FROM config
            WHERE guild_id=?
        """, (guild_id,))
        row = await cur.fetchone()
        if not row:
            return (None, None, None)
        allowed_channel_id, fuel_price, last_reset_week = row
        return (allowed_channel_id, fuel_price, last_reset_week)


# -----------------------------
# Account helpers
# -----------------------------
async def set_account(db_path: str, guild_id: int, user_id: int, account_number: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT INTO accounts (guild_id, user_id, account_number, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET
                account_number=excluded.account_number,
                updated_at=excluded.updated_at
        """, (guild_id, user_id, account_number, now_iso()))
        await db.commit()


async def get_account(db_path: str, guild_id: int, user_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT account_number
            FROM accounts
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        return await cur.fetchone()


async def get_accounts_map(db_path: str, guild_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT user_id, account_number
            FROM accounts
            WHERE guild_id=?
        """, (guild_id,))
        rows = await cur.fetchall()
    return {user_id: account_number for user_id, account_number in rows}


# -----------------------------
# Session/log helpers
# -----------------------------
async def get_open_session(db_path: str, guild_id: int, channel_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT id, total, remaining, fuel_can_count, has_polish, pay_per_count
            FROM sessions
            WHERE guild_id=? AND channel_id=? AND is_open=1
            ORDER BY id DESC
            LIMIT 1
        """, (guild_id, channel_id))
        return await cur.fetchone()
        # (sid, total, remaining, fuel_can_count, has_polish, pay_per_count)


async def update_remaining(db_path: str, session_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT COALESCE(SUM(qty), 0)
            FROM logs
            WHERE session_id=? AND is_void=0
        """, (session_id,))
        used = (await cur.fetchone())[0]

        cur2 = await db.execute("SELECT total FROM sessions WHERE id=?", (session_id,))
        total_row = await cur2.fetchone()
        if not total_row:
            return 0, 0, 0

        total = total_row[0]
        remaining = max(total - used, 0)

        await db.execute("UPDATE sessions SET remaining=? WHERE id=?", (remaining, session_id))
        await db.commit()
        return total, used, remaining


async def get_totals(db_path: str, session_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT user_id, COALESCE(SUM(qty), 0) AS s
            FROM logs
            WHERE session_id=? AND is_void=0
            GROUP BY user_id
            ORDER BY s DESC
        """, (session_id,))
        return await cur.fetchall()


async def add_log(db_path: str, session_id: int, user_id: int, qty: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT INTO logs(session_id, user_id, qty, created_at, is_void)
            VALUES(?,?,?,?,0)
        """, (session_id, user_id, qty, now_iso()))
        await db.commit()


async def void_last_log(db_path: str, session_id: int, user_id: int):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("""
            SELECT id, qty
            FROM logs
            WHERE session_id=? AND user_id=? AND is_void=0
            ORDER BY id DESC
            LIMIT 1
        """, (session_id, user_id))
        row = await cur.fetchone()
        if not row:
            return None

        log_id, qty = row
        await db.execute("""
            UPDATE logs
            SET is_void=1, voided_at=?
            WHERE id=?
        """, (now_iso(), log_id))
        await db.commit()
        return (log_id, qty)


async def close_session(db_path: str, session_id: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            UPDATE sessions
            SET is_open=0, closed_at=?
            WHERE id=?
        """, (now_iso(), session_id))
        await db.commit()


# -----------------------------
# Forum / 채널 허용 체크
# -----------------------------
async def ensure_allowed_channel(db_path: str, interaction: discord.Interaction):
    allowed_id, _fuel, _last_reset_week = await get_config(db_path, interaction.guild_id)
    if not allowed_id:
        await interaction.response.send_message(
            "먼저 `/채널설정`으로 기록할 Forum 채널을 지정해줘요!",
            ephemeral=True
        )
        return None

    ch = interaction.channel
    ok = False

    if interaction.channel_id == allowed_id:
        ok = True
    elif isinstance(ch, discord.Thread) and ch.parent_id == allowed_id:
        ok = True

    if not ok:
        allowed_ch = interaction.guild.get_channel(allowed_id)
        mention = allowed_ch.mention if allowed_ch else f"(ID: {allowed_id})"
        await interaction.response.send_message(
            f"이 명령은 지정된 Forum(또는 그 게시글)에서만 가능해요 → {mention}",
            ephemeral=True
        )
        return None

    return allowed_id


def format_status(
    total: int,
    used: int,
    remaining: int,
    totals_rows,
    guild: discord.Guild,
    pay_per_count: int,
    accounts_map=None,
    fuel_can_count: int = 5,
    has_polish: bool = False
):
    accounts_map = accounts_map or {}

    lines = []
    lines.append(f"총량: **{total}** | 사용: **{used}** | 남음: **{remaining}**")
    lines.append(
        f"구성: **기름통 {fuel_can_count}개** | **세차광택 {'포함' if has_polish else '미포함'}**"
    )
    lines.append(f"🏆 총 정산금: **{(used * pay_per_count):,}원**")
    lines.append("")

    if not totals_rows:
        lines.append("아직 기록이 없습니다.")
    else:
        lines.append("**사람별 누적/정산:**")
        for uid, s in totals_rows:
            member = guild.get_member(uid)
            name = member.display_name if member else f"User({uid})"
            pay = s * pay_per_count
            account_number = accounts_map.get(uid, "-")
            lines.append(f"- {name}: **{s}회** | 💰 **{pay:,}원** | **{account_number}**")

    return "\n".join(lines)


# -----------------------------
# Register (commands + on_message + weekly reset)
# -----------------------------
def register_counting(bot, db_path: str):
    reset_state = {"last_run_minute": None}

    @tasks.loop(seconds=30)
    async def weekly_fuel_reset_loop():
        now = datetime.now(KST)
        minute_key = now.strftime("%Y-%m-%d %H:%M")

        if now.weekday() != 0 or now.hour != 0 or now.minute != 0:
            return

        if reset_state["last_run_minute"] == minute_key:
            return

        reset_state["last_run_minute"] = minute_key
        week_key = current_week_key_kst()

        for guild in bot.guilds:
            try:
                await ensure_config_row(db_path, guild.id)
                _allowed_id, _fuel_price, last_reset_week = await get_config(db_path, guild.id)

                if last_reset_week == week_key:
                    continue

                await reset_fuel_price(db_path, guild.id, week_key)
                print(f"[weekly_fuel_reset_loop] guild={guild.id} fuel reset done ({week_key})")

            except Exception as e:
                print(f"[weekly_fuel_reset_loop] guild={guild.id} error={e}")

    @weekly_fuel_reset_loop.before_loop
    async def before_weekly_fuel_reset_loop():
        await bot.wait_until_ready()

    if not weekly_fuel_reset_loop.is_running():
        weekly_fuel_reset_loop.start()

    @bot.tree.command(name="채널설정", description="(관리자) 기록을 받을 Forum 채널을 지정합니다.")
    @app_commands.describe(채널="Forum 채널 선택")
    async def channel_set(interaction: discord.Interaction, 채널: discord.abc.GuildChannel):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "관리자(서버 관리 권한)만 설정할 수 있어요.",
                ephemeral=True
            )
            return

        if not isinstance(채널, discord.ForumChannel):
            await interaction.response.send_message(
                "Forum 채널만 설정할 수 있어요.",
                ephemeral=True
            )
            return

        await set_allowed_channel(db_path, interaction.guild_id, 채널.id)
        await interaction.response.send_message(f"✅ 기록 위치(Forum)가 {채널.mention} 로 설정됐어요!")

    @bot.tree.command(name="채널확인", description="현재 설정된 기록 Forum 채널을 확인합니다.")
    async def channel_check(interaction: discord.Interaction):
        allowed_id, _fuel, _last_reset_week = await get_config(db_path, interaction.guild_id)
        if not allowed_id:
            await interaction.response.send_message(
                "아직 기록 위치가 설정되지 않았어요. `/채널설정` 해줘요!",
                ephemeral=True
            )
            return

        ch = interaction.guild.get_channel(allowed_id)
        mention = ch.mention if ch else f"(ID: {allowed_id})"
        await interaction.response.send_message(f"✅ 현재 기록 Forum: {mention}")

    @bot.tree.command(name="기름가격", description="(관리자) 이번 주 기름가격(만원)을 드롭다운으로 선택합니다.")
    @app_commands.choices(가격=[
        app_commands.Choice(name="16만원", value=16),
        app_commands.Choice(name="18만원", value=18),
        app_commands.Choice(name="20만원", value=20),
        app_commands.Choice(name="22만원", value=22),
        app_commands.Choice(name="24만원", value=24),
    ])
    async def fuel_price_cmd(interaction: discord.Interaction, 가격: app_commands.Choice[int]):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("관리자만 기름가격을 설정할 수 있어요.", ephemeral=True)
            return

        fuel = 가격.value
        base_pay = BASE_FUEL_TO_PAY[fuel]
        extra_pay = EXTRA_CAN_TO_PAY[fuel]

        await set_fuel_price(db_path, interaction.guild_id, fuel)
        await interaction.response.send_message(
            f"✅ 기름가격 설정: **{fuel}만원** | 기본단가 **{base_pay:,}원** | 추가기름통 **{extra_pay:,}원/개** | 세차광택 **+{POLISH_EXTRA_PAY:,}원**"
        )

    @bot.tree.command(name="기름가격확인", description="현재 설정된 기름가격/단가를 확인합니다.")
    async def fuel_price_check(interaction: discord.Interaction):
        _allowed_id, fuel, _last_reset_week = await get_config(db_path, interaction.guild_id)

        if fuel is None:
            await interaction.response.send_message(
                "⛽ 이번 주 기름가격은 아직 등록되지 않았어요. 관리자분이 `/기름가격`으로 등록해주세요."
            )
            return

        base_pay = BASE_FUEL_TO_PAY[fuel]
        extra_pay = EXTRA_CAN_TO_PAY[fuel]
        await interaction.response.send_message(
            f"⛽ 현재 기름가격: **{fuel}만원** | 기본단가 **{base_pay:,}원** | 추가기름통 **{extra_pay:,}원/개** | 세차광택 **+{POLISH_EXTRA_PAY:,}원**"
        )

    @bot.tree.command(name="내계좌등록", description="내 게임 계좌번호를 등록하거나 수정합니다.")
    @app_commands.describe(번호="예: 123456")
    async def my_account_register(interaction: discord.Interaction, 번호: str):
        account_number = (번호 or "").strip()
        if not account_number:
            await interaction.response.send_message("계좌번호는 비워둘 수 없어요.", ephemeral=True)
            return

        await set_account(db_path, interaction.guild_id, interaction.user.id, account_number)
        await interaction.response.send_message(
            f"✅ 내 계좌번호 등록 완료: **{account_number}**",
            ephemeral=True
        )

    @bot.tree.command(name="내계좌확인", description="내 등록된 게임 계좌번호를 확인합니다.")
    async def my_account_check(interaction: discord.Interaction):
        row = await get_account(db_path, interaction.guild_id, interaction.user.id)
        if not row:
            await interaction.response.send_message(
                "등록된 계좌번호가 없어요. `/내계좌등록` 해주세요.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(f"💳 내 계좌번호: **{row[0]}**", ephemeral=True)

    @bot.tree.command(name="계좌등록", description="(관리자) 특정 유저의 게임 계좌번호를 등록하거나 수정합니다.")
    @app_commands.describe(대상="계좌를 등록할 유저", 번호="예: 123456")
    async def account_register(interaction: discord.Interaction, 대상: discord.Member, 번호: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("관리자만 사용할 수 있어요.", ephemeral=True)
            return

        account_number = (번호 or "").strip()
        if not account_number:
            await interaction.response.send_message("계좌번호는 비워둘 수 없어요.", ephemeral=True)
            return

        await set_account(db_path, interaction.guild_id, 대상.id, account_number)
        await interaction.response.send_message(
            f"✅ {대상.display_name} 님 계좌번호 등록 완료: **{account_number}**",
            ephemeral=True
        )

    @bot.tree.command(name="계좌확인", description="(관리자) 특정 유저의 등록된 계좌번호를 확인합니다.")
    @app_commands.describe(대상="확인할 유저")
    async def account_check(interaction: discord.Interaction, 대상: discord.Member):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("관리자만 사용할 수 있어요.", ephemeral=True)
            return

        row = await get_account(db_path, interaction.guild_id, 대상.id)
        if not row:
            await interaction.response.send_message(
                f"{대상.display_name} 님은 등록된 계좌번호가 없어요.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"💳 {대상.display_name} 님 계좌번호: **{row[0]}**",
            ephemeral=True
        )

    @bot.tree.command(name="시작", description="골든벨 작업 시작 (Forum 게시글 안에서 실행)")
    @app_commands.describe(
        그룹명="예: 시민/경찰/조직/오일리",
        닉네임="골든벨 울린 사람",
        총량="기본 50",
        기름통개수="기본 5개, 더 추가 가능",
        세차광택="세차 광택 포함 여부"
    )
    @app_commands.choices(세차광택=[
        app_commands.Choice(name="미포함", value=0),
        app_commands.Choice(name="포함", value=1),
    ])
    async def start(
        interaction: discord.Interaction,
        그룹명: str,
        닉네임: str,
        총량: int = 50,
        기름통개수: int = 5,
        세차광택: app_commands.Choice[int] = None
    ):
        allowed = await ensure_allowed_channel(db_path, interaction)
        if not allowed:
            return

        if 총량 <= 0:
            await interaction.response.send_message("총량은 1 이상이어야 해요.", ephemeral=True)
            return

        if 기름통개수 < MIN_FUEL_CAN_COUNT:
            await interaction.response.send_message(
                f"기름통 개수는 최소 {MIN_FUEL_CAN_COUNT}개부터 가능해요.",
                ephemeral=True
            )
            return

        existing = await get_open_session(db_path, interaction.guild_id, interaction.channel_id)
        if existing:
            _sid, total, remaining, _fuel_can_count, _has_polish, _pay = existing
            await interaction.response.send_message(
                f"이미 진행 중인 작업이 있어요. (총량 {total}, 남음 {remaining})",
                ephemeral=True
            )
            return

        group_name = (그룹명 or "").strip()
        announcer_nick = (닉네임 or "").strip()
        if not group_name or not announcer_nick:
            await interaction.response.send_message("그룹명/닉네임은 비워둘 수 없어요.", ephemeral=True)
            return

        _allowed_id, fuel_price, _last_reset_week = await get_config(db_path, interaction.guild_id)
        if fuel_price is None:
            await interaction.response.send_message(
                "이번 주 기름가격이 아직 등록되지 않았어요. 관리자분이 `/기름가격` 먼저 설정해주세요.",
                ephemeral=True
            )
            return

        has_polish = bool(세차광택.value) if 세차광택 else False
        pay_per_count = calculate_pay_per_count(fuel_price, 기름통개수, has_polish)

        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                INSERT INTO sessions(
                    guild_id, channel_id, group_name, announcer_nick,
                    total, remaining, fuel_can_count, has_polish,
                    pay_per_count, is_open, created_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,1,?)
            """, (
                interaction.guild_id,
                interaction.channel_id,
                group_name,
                announcer_nick,
                총량,
                총량,
                기름통개수,
                1 if has_polish else 0,
                pay_per_count,
                now_iso()
            ))
            await db.commit()

        polish_text = " + 세차광택" if has_polish else ""

        msg = (
            f"&1 {group_name} [{announcer_nick}]님께서 ^9⛽오일리⛽ ^3🔔골든벨🔔 ^0울려주셨습니다!  코팅제1개, "
            f"기름통{기름통개수}개{polish_text}! 모든분들 오셔서 받아가세요! ^8(0/{총량})\n"
        )
        await interaction.response.send_message(msg)

    @bot.tree.command(name="현황", description="현재 작업 현황(사람별 누적/정산 포함)")
    async def status(interaction: discord.Interaction):
        allowed = await ensure_allowed_channel(db_path, interaction)
        if not allowed:
            return

        open_sess = await get_open_session(db_path, interaction.guild_id, interaction.channel_id)
        if not open_sess:
            await interaction.response.send_message(
                "진행 중인 작업이 없어요. 이 게시글에서 `/시작` 먼저!",
                ephemeral=True
            )
            return

        sid, _, _, fuel_can_count, has_polish, pay_per_count = open_sess
        total, used, remaining = await update_remaining(db_path, sid)
        totals_rows = await get_totals(db_path, sid)
        accounts_map = await get_accounts_map(db_path, interaction.guild_id)

        await interaction.response.send_message(
            format_status(
                total,
                used,
                remaining,
                totals_rows,
                interaction.guild,
                pay_per_count,
                accounts_map,
                fuel_can_count,
                bool(has_polish)
            )
        )

    @bot.tree.command(name="되돌리기", description="내 마지막 기록 1건을 취소합니다.")
    async def undo(interaction: discord.Interaction):
        allowed = await ensure_allowed_channel(db_path, interaction)
        if not allowed:
            return

        open_sess = await get_open_session(db_path, interaction.guild_id, interaction.channel_id)
        if not open_sess:
            await interaction.response.send_message("진행 중인 작업이 없어요.", ephemeral=True)
            return

        sid, _, _, fuel_can_count, has_polish, pay_per_count = open_sess
        undone = await void_last_log(db_path, sid, interaction.user.id)
        if not undone:
            await interaction.response.send_message("취소할 내 기록이 없어요.", ephemeral=True)
            return

        total, used, remaining = await update_remaining(db_path, sid)
        totals_rows = await get_totals(db_path, sid)
        accounts_map = await get_accounts_map(db_path, interaction.guild_id)

        _log_id, qty = undone
        msg = format_status(
            total,
            used,
            remaining,
            totals_rows,
            interaction.guild,
            pay_per_count,
            accounts_map,
            fuel_can_count,
            bool(has_polish)
        )
        await interaction.response.send_message(f"↩️ 되돌림 완료: **-{qty}**\n\n{msg}")

    @bot.tree.command(name="마감", description="현재 작업을 수동 마감하고 최종 집계를 출력합니다.")
    async def close(interaction: discord.Interaction):
        allowed = await ensure_allowed_channel(db_path, interaction)
        if not allowed:
            return

        open_sess = await get_open_session(db_path, interaction.guild_id, interaction.channel_id)
        if not open_sess:
            await interaction.response.send_message("진행 중인 작업이 없어요.", ephemeral=True)
            return

        sid, _, _, fuel_can_count, has_polish, pay_per_count = open_sess
        total, used, remaining = await update_remaining(db_path, sid)
        totals_rows = await get_totals(db_path, sid)
        accounts_map = await get_accounts_map(db_path, interaction.guild_id)

        await close_session(db_path, sid)

        msg = format_status(
            total,
            used,
            remaining,
            totals_rows,
            interaction.guild,
            pay_per_count,
            accounts_map,
            fuel_can_count,
            bool(has_polish)
        )
        await interaction.response.send_message(f"✅ 작업 마감! (수동)\n\n{msg}")

    # -----------------------------
    # 숫자만 치면 자동 기록
    # -----------------------------
    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        if not message.guild:
            return

        allowed_id, _fuel, _last_reset_week = await get_config(db_path, message.guild.id)
        if not allowed_id:
            return

        if message.channel.id == allowed_id:
            pass
        elif isinstance(message.channel, discord.Thread) and message.channel.parent_id == allowed_id:
            pass
        else:
            return

        content = (message.content or "").strip()
        if not content.isdigit():
            return

        qty = 1
        open_sess = await get_open_session(db_path, message.guild.id, message.channel.id)
        if not open_sess:
            return

        sid, _t, _r, fuel_can_count, has_polish, pay_per_count = open_sess

        await add_log(db_path, sid, message.author.id, qty)
        total, used, remaining = await update_remaining(db_path, sid)

        try:
            await message.add_reaction("✅")
        except Exception:
            pass

        if remaining <= 0:
            totals_rows = await get_totals(db_path, sid)
            accounts_map = await get_accounts_map(db_path, message.guild.id)
            status_msg = format_status(
                total,
                used,
                remaining,
                totals_rows,
                message.guild,
                pay_per_count,
                accounts_map,
                fuel_can_count,
                bool(has_polish)
            )

            await close_session(db_path, sid)
            await message.channel.send(
                "🚨 **남은 수량이 0이어서 자동 마감됐어요! (최종 집계)**\n\n" + status_msg
            )