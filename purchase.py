import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
import aiosqlite

# =========================
# 자동삭제 시간 설정
# =========================
EPHEMERAL_DELETE_AFTER = 3
SELECT_DELETE_AFTER = 10

# =========================
# 품목 가격표
# =========================
ITEMS = {
    "회복제": 1_500_000,
    "수리키트": 7_000_000,
    "스테이크": 2_000_000,
    "딸바": 2_000_000,
    "방탄복": 2_500_000,
    "소음기": 1_500_000,
    "손잡이": 2_000_000,
    "조준경": 2_000_000,
    "확장탄창": 2_000_000,
    "SMG": 40_000_000,
    "특총": 30_000_000,
}

# 유저별 임시 작성중 청구서
ACTIVE_CLAIMS: dict[int, list[dict]] = {}

# 제출된 청구서 원본 데이터 (수정요청 시 다시 불러오기용)
SUBMITTED_CLAIMS: dict[int, dict] = {}


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
        return row[0] if row else None


# =========================
# 공통 유틸
# =========================
def build_claim_embed(user: discord.abc.User, claim_items: list[dict], account_number: str | None):
    total = 0
    desc_lines = []

    for item in claim_items:
        subtotal = int(item["qty"]) * int(item["price"])
        total += subtotal
        desc_lines.append(f"- {item['name']} x{item['qty']} = {subtotal:,}원")

    embed = discord.Embed(
        title="📄 구매 청구서",
        description="\n".join(desc_lines) if desc_lines else "품목 없음",
        color=0x2ecc71
    )
    embed.add_field(name="총합", value=f"{total:,}원", inline=False)
    embed.add_field(name="계좌", value=account_number or "미등록", inline=False)
    embed.add_field(name="상태", value="대기중", inline=False)
    embed.set_footer(text=f"신청자: {user.display_name} | 신청자ID: {user.id}")
    return embed


def clone_embed(embed: discord.Embed) -> discord.Embed:
    new_embed = discord.Embed(
        title=embed.title,
        description=embed.description,
        color=embed.color
    )

    for field in embed.fields:
        new_embed.add_field(name=field.name, value=field.value, inline=field.inline)

    if embed.footer:
        new_embed.set_footer(text=embed.footer.text, icon_url=embed.footer.icon_url)

    if embed.author:
        new_embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url)

    return new_embed


def finalize_embed(
    original_embed: discord.Embed,
    status_text: str,
    status_color: int,
    manager_name: str,
    reason_text: str | None = None
) -> discord.Embed:
    embed = clone_embed(original_embed)
    embed.color = status_color

    kept_fields = []
    for f in embed.fields:
        if f.name not in {"상태", "처리자", "처리시간", "사유"}:
            kept_fields.append((f.name, f.value, f.inline))

    embed.clear_fields()

    for name, value, inline in kept_fields:
        embed.add_field(name=name, value=value, inline=inline)

    embed.add_field(name="상태", value=status_text, inline=False)
    embed.add_field(name="처리자", value=manager_name, inline=False)
    embed.add_field(
        name="처리시간",
        value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        inline=False
    )

    # 지급완료는 사유 필드 없음
    if status_text != "지급완료":
        embed.add_field(name="사유", value=reason_text or "-", inline=False)

    return embed


def disable_view(view: discord.ui.View):
    for child in view.children:
        child.disabled = True


def extract_user_id_from_embed(embed: discord.Embed) -> int | None:
    if not embed.footer or not embed.footer.text:
        return None

    text = embed.footer.text
    marker = "신청자ID:"
    if marker not in text:
        return None

    try:
        return int(text.split(marker, 1)[1].strip())
    except Exception:
        return None


# =========================
# 품목 선택
# =========================
class ItemSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=name) for name in ITEMS.keys()]
        super().__init__(
            placeholder="품목 선택",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(QuantityModal(self.values[0]))


class QuantityModal(discord.ui.Modal, title="수량 입력"):
    qty = discord.ui.TextInput(
        label="수량",
        placeholder="숫자만 입력",
        required=True
    )

    def __init__(self, item_name: str):
        super().__init__()
        self.item_name = item_name

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        try:
            qty = int(str(self.qty.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "수량은 숫자로 입력해주세요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        if qty <= 0:
            await interaction.response.send_message(
                "수량은 1 이상이어야 해요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        ACTIVE_CLAIMS.setdefault(user_id, [])
        ACTIVE_CLAIMS[user_id].append({
            "name": self.item_name,
            "qty": qty,
            "price": ITEMS[self.item_name],
        })

        subtotal = qty * ITEMS[self.item_name]
        await interaction.response.send_message(
            f"✅ {self.item_name} x{qty} 추가됨 ({subtotal:,}원)",
            ephemeral=True,
            delete_after=EPHEMERAL_DELETE_AFTER
        )


# =========================
# 기타 입력
# =========================
class CustomModal(discord.ui.Modal, title="기타 품목 입력"):
    name = discord.ui.TextInput(
        label="내용",
        placeholder="예: 응급상자",
        required=True
    )
    qty = discord.ui.TextInput(
        label="수량",
        placeholder="숫자만 입력",
        required=True
    )
    price = discord.ui.TextInput(
        label="개당 금액",
        placeholder="숫자만 입력",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        try:
            qty = int(str(self.qty.value).strip())
            price = int(str(self.price.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "수량/금액은 숫자로 입력해주세요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        if qty <= 0 or price <= 0:
            await interaction.response.send_message(
                "수량/금액은 1 이상이어야 해요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        item_name = str(self.name.value).strip()
        if not item_name:
            await interaction.response.send_message(
                "기타 내용은 비워둘 수 없어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        ACTIVE_CLAIMS.setdefault(user_id, [])
        ACTIVE_CLAIMS[user_id].append({
            "name": f"기타({item_name})",
            "qty": qty,
            "price": price,
        })

        subtotal = qty * price
        await interaction.response.send_message(
            f"✅ 기타({item_name}) x{qty} 추가됨 ({subtotal:,}원)",
            ephemeral=True,
            delete_after=EPHEMERAL_DELETE_AFTER
        )


# =========================
# 수정요청 후 재작성 버튼
# =========================
class RevisionRequestView(discord.ui.View):
    def __init__(self, db_path: str, owner_id: int, source_message_id: int):
        super().__init__(timeout=None)
        self.db_path = db_path
        self.owner_id = int(owner_id)
        self.source_message_id = int(source_message_id)

    @discord.ui.button(label="수정해서 다시 작성", style=discord.ButtonStyle.primary)
    async def revise_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "청구한 직원 본인만 수정할 수 있어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        claim_data = SUBMITTED_CLAIMS.get(self.source_message_id)
        if not claim_data:
            await interaction.response.send_message(
                "이전 청구 데이터를 찾을 수 없어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        ACTIVE_CLAIMS[self.owner_id] = [
            {
                "name": item["name"],
                "qty": int(item["qty"]),
                "price": int(item["price"]),
            }
            for item in claim_data["items"]
        ]

        await interaction.response.send_message(
            "📝 기존 청구 항목을 불러왔어요. 아래 버튼으로 수정 후 다시 제출해주세요.",
            view=ClaimView(self.db_path),
            ephemeral=True
        )


# =========================
# 관리자 사유 입력 모달
# =========================
class AdminReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="사유",
        style=discord.TextStyle.paragraph,
        placeholder="사유를 입력해주세요.",
        required=True,
        max_length=1000
    )

    def __init__(self, parent_view: "AdminView", action: str, source_message: discord.Message, db_path: str):
        self.parent_view = parent_view
        self.action = action
        self.source_message = source_message
        self.db_path = db_path

        title_map = {
            "rejected": "반려 사유 입력",
            "revision": "수정요청 사유 입력",
        }
        super().__init__(title=title_map[action])

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "관리자만 처리할 수 있어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        if not self.source_message.embeds:
            await interaction.response.send_message(
                "청구서 메시지를 찾을 수 없어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        original_embed = self.source_message.embeds[0]
        reason_text = str(self.reason.value).strip()
        owner_id = extract_user_id_from_embed(original_embed)

        if self.action == "rejected":
            new_embed = finalize_embed(
                original_embed=original_embed,
                status_text="반려",
                status_color=0xe74c3c,
                manager_name=interaction.user.display_name,
                reason_text=reason_text
            )
            disable_view(self.parent_view)
            await self.source_message.edit(embed=new_embed, view=self.parent_view)
            await interaction.response.send_message(
                "❌ 반려 처리됐어요. 티켓은 유지되고, 이 청구서는 종료 상태로 잠겼어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )

        else:
            new_embed = finalize_embed(
                original_embed=original_embed,
                status_text="수정요청",
                status_color=0xf1c40f,
                manager_name=interaction.user.display_name,
                reason_text=reason_text
            )

            revision_view = RevisionRequestView(
                db_path=self.db_path,
                owner_id=owner_id or 0,
                source_message_id=self.source_message.id
            )

            await self.source_message.edit(embed=new_embed, view=revision_view)
            await interaction.response.send_message(
                "📝 수정요청 처리됐어요. 청구한 직원이 `수정해서 다시 작성` 버튼으로 다시 제출할 수 있어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )


# =========================
# 청구 작성 버튼
# =========================
class ClaimView(discord.ui.View):
    def __init__(self, db_path: str):
        super().__init__(timeout=None)
        self.db_path = db_path

    @discord.ui.button(label="품목 추가", style=discord.ButtonStyle.primary)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(ItemSelect())
        await interaction.response.send_message(
            "추가할 품목을 선택해주세요.",
            view=view,
            ephemeral=True,
            delete_after=SELECT_DELETE_AFTER
        )

    @discord.ui.button(label="기타 추가", style=discord.ButtonStyle.secondary)
    async def add_custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomModal())

    @discord.ui.button(label="현재목록 보기", style=discord.ButtonStyle.secondary)
    async def preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        items = ACTIVE_CLAIMS.get(user_id, [])

        if not items:
            await interaction.response.send_message(
                "아직 추가된 품목이 없어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        lines = []
        total = 0
        for item in items:
            subtotal = int(item["qty"]) * int(item["price"])
            total += subtotal
            lines.append(f"- {item['name']} x{item['qty']} = {subtotal:,}원")

        await interaction.response.send_message(
            "📦 현재 청구 목록\n" + "\n".join(lines) + f"\n\n총합: **{total:,}원**",
            ephemeral=True,
            delete_after=EPHEMERAL_DELETE_AFTER
        )

    @discord.ui.button(label="초기화", style=discord.ButtonStyle.danger)
    async def reset_claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ACTIVE_CLAIMS[interaction.user.id] = []
        await interaction.response.send_message(
            "🗑 청구 목록을 초기화했어요.",
            ephemeral=True,
            delete_after=EPHEMERAL_DELETE_AFTER
        )

    @discord.ui.button(label="청구서 제출", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        items = ACTIVE_CLAIMS.get(user_id, [])

        if not items:
            await interaction.response.send_message(
                "❌ 품목이 없어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        account = await get_user_account(self.db_path, user_id)
        embed = build_claim_embed(interaction.user, items, account)

        await interaction.response.defer(ephemeral=True)

        admin_view = AdminView(self.db_path)
        sent_msg = await interaction.followup.send(embed=embed, view=admin_view, wait=True)

        SUBMITTED_CLAIMS[sent_msg.id] = {
            "owner_id": user_id,
            "items": [
                {
                    "name": item["name"],
                    "qty": int(item["qty"]),
                    "price": int(item["price"]),
                }
                for item in items
            ]
        }

        ACTIVE_CLAIMS[user_id] = []

        try:
            await interaction.delete_original_response()
        except Exception:
            pass

        await interaction.followup.send(
            "✅ 청구서가 제출됐어요.",
            ephemeral=True,
        )


# =========================
# 관리자 처리 버튼
# =========================
class AdminView(discord.ui.View):
    def __init__(self, db_path: str):
        super().__init__(timeout=None)
        self.db_path = db_path

    @discord.ui.button(label="지급완료", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "관리자만 가능해요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message(
                "청구서를 찾을 수 없어요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return

        original_embed = interaction.message.embeds[0]
        new_embed = finalize_embed(
            original_embed=original_embed,
            status_text="지급완료",
            status_color=0x3498db,
            manager_name=interaction.user.display_name,
            reason_text=None
        )

        disable_view(self)
        await interaction.response.edit_message(embed=new_embed, view=self)

    @discord.ui.button(label="반려", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "관리자만 가능해요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return
        await interaction.response.send_modal(
            AdminReasonModal(self, "rejected", interaction.message, self.db_path)
        )

    @discord.ui.button(label="수정요청", style=discord.ButtonStyle.secondary)
    async def revise(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "관리자만 가능해요.",
                ephemeral=True,
                delete_after=EPHEMERAL_DELETE_AFTER
            )
            return
        await interaction.response.send_modal(
            AdminReasonModal(self, "revision", interaction.message, self.db_path)
        )


# =========================
# 명령어 등록
# =========================
def register_purchase(bot: commands.Bot, db_path: str):
    @bot.tree.command(name="청구시작", description="구매 청구서 작성을 시작합니다.")
    async def start_claim(interaction: discord.Interaction):
        ACTIVE_CLAIMS.setdefault(interaction.user.id, [])
        await interaction.response.send_message(
            "📦 아래 버튼으로 청구서를 작성해주세요.",
            view=ClaimView(db_path),
            ephemeral=True
        )