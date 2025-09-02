import os
import io
import base64
import logging
from typing import Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ========= 環境変数 =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip().isdigit()]
DEFAULT_BG_IMAGE_URL = os.getenv("DEFAULT_BG_IMAGE_URL", "")
ALLOWED_ROLE_ID = int(os.getenv("ALLOWED_ROLE_ID", "0") or 0)
DEFAULT_PARTICIPANT_ROLE_ID = int(os.getenv("PARTICIPANT_ROLE_ID", "0") or 0)
DEFAULT_SPECTATOR_ROLE_ID   = int(os.getenv("SPECTATOR_ROLE_ID", "0") or 0)

# フォント（リポ同梱優先・URLフォールバック）
FONT_PATH = os.getenv("FONT_PATH", "")   # 例: fonts/NotoSansJP-VariableFont_wght.ttf
FONT_URL  = os.getenv("FONT_URL", "")    # 直リンク(.otf/.ttf)を使う場合のみ

# ========= 表示チューニング =========
FONT_SCALE = float(os.getenv("FONT_SCALE", "1.0"))  # 全体倍率 例: 1.1 / 1.2
TITLE_SIZE  = int(56 * FONT_SCALE)  # 旧48
LABEL_SIZE  = int(32 * FONT_SCALE)  # 旧28
VALUE_SIZE  = int(34 * FONT_SCALE)  # 旧30
NOTE_SIZE   = int(30 * FONT_SCALE)  # 旧28
FOOTER_SIZE = int(22 * FONT_SCALE)  # 旧20

# 黒フチ（外側）と白ストローク（内側＝太らせ用）
STROKE_TITLE = int(os.getenv("STROKE_TITLE", "5"))  # タイトルの黒フチ太さ
STROKE_BODY  = int(os.getenv("STROKE_BODY",  "4"))  # 本文などの黒フチ太さ
INLINE_STROKE_TITLE = int(os.getenv("INLINE_STROKE_TITLE", str(max(STROKE_TITLE-2, 1))))
INLINE_STROKE_BODY  = int(os.getenv("INLINE_STROKE_BODY",  str(max(STROKE_BODY-2,  1))))

# テキスト位置
LABEL_X = int(os.getenv("LABEL_X", "74"))
VALUE_X = int(os.getenv("VALUE_X", "360"))  # 値の列のX座標（380〜420などで微調整可）

# ========= ログ =========
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="(%(asctime)s) [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mysterybot")

# ========= Intents / Bot =========
intents = discord.Intents.default()
intents.guilds = True
intents.members = True          # ロール付与に必要
intents.message_content = True  # prefixコマンド(!)用
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ========= フォント取得 =========
_FONT_CACHE_PATH = "/tmp/mystery_font.ttf"

def _resolve_font_path() -> Optional[str]:
    candidates = []
    if FONT_PATH:
        candidates.append(FONT_PATH)
    candidates += [
        "fonts/NotoSansJP-VariableFont_wght.ttf",
        "fonts/NotoSansJP-Regular.otf",
        "fonts/NotoSansJP-Regular.ttf",
        "fonts/NotoSerifJP-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def get_font(size: int) -> ImageFont.ImageFont:
    local = _resolve_font_path()
    if local:
        try:
            return ImageFont.truetype(local, size=size)
        except Exception as e:
            log.warning(f"FONT_PATH 読込失敗: {e}")
    if FONT_URL:
        try:
            if not os.path.exists(_FONT_CACHE_PATH):
                r = requests.get(FONT_URL, timeout=15)
                r.raise_for_status()
                with open(_FONT_CACHE_PATH, "wb") as f:
                    f.write(r.content)
            return ImageFont.truetype(_FONT_CACHE_PATH, size=size)
        except Exception as e:
            log.warning(f"FONT_URL取得失敗。デフォルトにフォールバック: {e}")
    return ImageFont.load_default()  # 日本語は豆腐

# ========= 不可視ペイロード（ゼロ幅） =========
_ZW0, _ZW1, _ZWPREFIX = '\u200B', '\u200C', '\u200D'  # ZWSP/ZWNJ/ZWJ

def _hide_payload(s: str) -> str:
    b64 = base64.b64encode(s.encode('utf-8'))
    bits = ''.join(f'{b:08b}' for b in b64)
    return _ZWPREFIX + ''.join(_ZW1 if bit == '1' else _ZW0 for bit in bits)

def _reveal_payload(s: str) -> Optional[str]:
    if not s:
        return None
    if s.startswith(_ZWPREFIX):
        bits = ''.join('1' if ch == _ZW1 else '0' for ch in s if ch in (_ZW0, _ZW1))
        if len(bits) % 8 != 0:
            return None
        data = bytes(int(bits[i:i+8], 2) for i in range(0, len(bits), 8))
        try:
            return base64.b64decode(data).decode('utf-8')
        except Exception:
            return None
    if 'participant=' in s and 'spectator=' in s:  # 互換
        return s
    return None

# ========= テキスト描画（ダブルストローク：黒→白） =========
def draw_text(draw: ImageDraw.ImageDraw, xy: Tuple[int, int], text: str, font: ImageFont.ImageFont,
              fill=(255, 255, 255), outline=(0, 0, 0),
              outline_w: int = 4, inline_w: int = 2):
    # 外側：黒フチ（太め）
    if outline_w > 0:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=outline_w, stroke_fill=outline)
    # 内側：白ストローク（少し細く）→“太字化”＋黒フチを残す
    if inline_w > 0:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=inline_w, stroke_fill=fill)
    else:
        draw.text(xy, text, font=font, fill=fill)

def draw_multiline(draw: ImageDraw.ImageDraw, text: str, xy: Tuple[int, int], font: ImageFont.ImageFont,
                   fill=(255,255,255), max_width: int = 800, line_spacing: int = 6,
                   outline=(0,0,0), outline_w: int = 4, inline_w: int = 2):
    if not text:
        return 0
    def text_w(s: str) -> int:
        l, t, r, b = draw.textbbox((0, 0), s, font=font)
        return r - l
    lines, cur = [], ""
    for ch in text:
        test = cur + ch
        if text_w(test) <= max_width:
            cur = test
        else:
            lines.append(cur); cur = ch
    if cur: lines.append(cur)

    x, y = xy
    total_h = 0
    for line in lines:
        draw_text(draw, (x, y + total_h), line, font=font, fill=fill,
                  outline=outline, outline_w=outline_w, inline_w=inline_w)
        bbox = font.getbbox(line); lh = bbox[3] - bbox[1]
        total_h += lh + line_spacing
    return total_h

# ========= 画像取得 =========
def fetch_image(url: str) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        log.warning(f"画像取得失敗: {url} ({e})")
        return None

# ========= パネル生成（値右寄せ／ダブルストローク／黒幕なし） =========
def make_panel(
    bg_url: str,
    corner_image_url: str,
    title: str,
    date_time: str,
    players: int,
    duration: str,
    note: str,
    canvas_size=(1200, 650),
    bg_alpha: int = 255,    # 255=減光なし / 180で少し暗く
    panel_opacity: int = 0, # 0=幕なし / 110で半透明板
) -> bytes:
    W, H = canvas_size
    base = Image.new("RGBA", (W, H), (20, 22, 28, 255))

    # 背景
    bg = fetch_image(bg_url) if bg_url else None
    if bg:
        bg = ImageOps.fit(bg, (W, H), method=Image.Resampling.LANCZOS)
        bg = bg.copy()
        bg.putalpha(max(0, min(255, bg_alpha)))
        base = Image.alpha_composite(base, bg)

    # 左の金ライン
    base.alpha_composite(Image.new("RGBA", (18, H), (212, 175, 55, 255)), (0, 0))

    # 右上コーナー画像
    corner = fetch_image(corner_image_url) if corner_image_url else None
    if corner:
        thumb_w, thumb_h = 340, 340
        corner = ImageOps.fit(corner, (thumb_w, thumb_h), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (thumb_w, thumb_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, thumb_w, thumb_h], radius=28, fill=255)
        base.paste(corner, (W - thumb_w - 28, 28), mask)

    # 半透明パネル（既定は非表示）
    if panel_opacity > 0:
        panel = Image.new("RGBA", (W - 80, H - 80), (0, 0, 0, panel_opacity))
        base.alpha_composite(panel, (40, 40))

    draw = ImageDraw.Draw(base)

    # タイトル
    font_title = get_font(TITLE_SIZE)
    draw_text(draw, (70, 60), title, font=font_title,
              outline_w=STROKE_TITLE, inline_w=INLINE_STROKE_TITLE)

    # ラベル＆値（値だけ右へ）
    font_label = get_font(LABEL_SIZE)
    font_text  = get_font(VALUE_SIZE)
    y = 140
    line_gap = 16

    def put(label: str, value: str):
        nonlocal y
        draw_text(draw, (LABEL_X, y), label, font=font_label, fill=(220,220,220),
                  outline_w=STROKE_BODY, inline_w=INLINE_STROKE_BODY)
        draw_text(draw, (VALUE_X, y-2), value, font=font_text,
                  outline_w=STROKE_BODY, inline_w=INLINE_STROKE_BODY)
        y += (font_text.size + line_gap)

    put("開催予定日", date_time)
    put("プレイヤー数", f"{players} 名")
    put("想定プレイ時間", duration)

    # 一言
    draw_text(draw, (LABEL_X, y), "一言", font=font_label, fill=(220,220,220),
              outline_w=STROKE_BODY, inline_w=INLINE_STROKE_BODY)
    y += font_label.size + 10
    y += draw_multiline(draw, note, (LABEL_X, y), font=get_font(NOTE_SIZE),
                        fill=(245,245,245), max_width=W - LABEL_X - 380,
                        outline_w=STROKE_BODY, inline_w=INLINE_STROKE_BODY)

    # 署名
    draw_text(draw, (70, H - 40), "マーダーミステリー開催のお知らせ",
              font=get_font(FOOTER_SIZE), fill=(200,200,200),
              outline_w=STROKE_BODY, inline_w=INLINE_STROKE_BODY)

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()

# ========= 永続View（参加/観戦トグル） =========
class MysterySignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="参加希望", style=discord.ButtonStyle.success, custom_id="mystery_join")
    async def on_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, role_kind="participant")

    @discord.ui.button(label="観戦希望", style=discord.ButtonStyle.primary, custom_id="mystery_watch")
    async def on_watch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, role_kind="spectator")

    async def _toggle_role(self, interaction: discord.Interaction, role_kind: str):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("ギルド外では操作できません。", ephemeral=True)
        try:
            msg = await interaction.channel.fetch_message(interaction.message.id)
            embed = msg.embeds[0] if msg.embeds else None
            footer_raw = embed.footer.text if embed and embed.footer else ""

            payload = _reveal_payload(footer_raw) or ""
            participant_id = spectator_id = None
            for part in payload.split("|"):
                if part.startswith("participant="):
                    participant_id = int(part.split("=", 1)[1])
                elif part.startswith("spectator="):
                    spectator_id = int(part.split("=", 1)[1])

            target_role_id = participant_id if role_kind == "participant" else spectator_id
            if not target_role_id:
                return await interaction.response.send_message("ロールIDが設定されていません。パネル作成時の設定をご確認ください。", ephemeral=True)

            role = guild.get_role(target_role_id)
            if role is None:
                return await interaction.response.send_message("ロールが見つかりません。", ephemeral=True)

            member = interaction.user
            if role in member.roles:
                await member.remove_roles(role, reason="Mystery panel toggle off")
                return await interaction.response.send_message(f"✅ {role.name} を解除しました。", ephemeral=True)
            else:
                await member.add_roles(role, reason="Mystery panel toggle on")
                return await interaction.response.send_message(f"✅ {role.name} を付与しました。", ephemeral=True)
        except Exception:
            log.exception("ロール切替エラー")
            return await interaction.response.send_message("処理中にエラーが発生しました。", ephemeral=True)

# ========= 起動時 =========
@bot.event
async def on_ready():
    try:
        bot.add_view(MysterySignupView())  # 永続View登録
    except Exception:
        pass
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

    # スラコマ即時同期
    try:
        if GUILD_IDS:
            for gid in GUILD_IDS:
                await tree.sync(guild=discord.Object(id=gid))
            log.info(f"Synced commands to guilds: {GUILD_IDS}")
        else:
            await tree.sync()
            log.info("Synced commands globally")
    except Exception as e:
        log.warning(f"Slash command sync failed: {e}")

# ========= 権限ヘルパ =========
def is_allowed(interaction: discord.Interaction) -> bool:
    if ALLOWED_ROLE_ID == 0:
        return True
    return discord.utils.get(interaction.user.roles, id=ALLOWED_ROLE_ID) is not None

def _is_admin_or_allowed(member: discord.Member) -> bool:
    return (
        member.guild_permissions.administrator or
        (ALLOWED_ROLE_ID and discord.utils.get(member.roles, id=ALLOWED_ROLE_ID))
    )

# ========= 強制同期/可視化/修復（prefix） =========
@bot.command(name="sync_here")
async def sync_here(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not _is_admin_or_allowed(ctx.author):
        return await ctx.reply("権限がありません。", mention_author=False)
    try:
        await tree.sync(guild=ctx.guild)
        await ctx.reply("✅ このサーバーにスラッシュコマンドを同期しました。", mention_author=False)
    except Exception as e:
        await ctx.reply(f"❌ 同期失敗: {e}", mention_author=False)

@bot.command(name="clear_and_sync")
async def clear_and_sync(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not _is_admin_or_allowed(ctx.author):
        return await ctx.reply("権限がありません。", mention_author=False)
    try:
        tree.clear_commands(guild=ctx.guild)
        await tree.sync(guild=ctx.guild)  # 空を同期
        await tree.sync(guild=ctx.guild)  # 再同期
        await ctx.reply("🧹→🔁 ギルドコマンドをクリアして再同期しました。", mention_author=False)
    except Exception as e:
        await ctx.reply(f"❌ クリア＆同期失敗: {e}", mention_author=False)

@bot.command(name="list_cmds")
async def list_cmds(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not _is_admin_or_allowed(ctx.author):
        return
    try:
        cmds = tree.get_commands(guild=ctx.guild)
        names = ", ".join([c.name for c in cmds]) or "(なし)"
        await ctx.reply(f"このギルドの登録コマンド: {names}", mention_author=False)
    except Exception as e:
        await ctx.reply(f"❌ 取得失敗: {e}", mention_author=False)

@bot.command(name="debug_sync")
async def debug_sync(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        return await ctx.reply("権限がありません。", mention_author=False)
    local = tree.get_commands(guild=ctx.guild)
    local_names = [c.name for c in local]
    remote_guild = await tree.fetch_commands(guild=ctx.guild)
    remote_global = await tree.fetch_commands()
    msg = (
        "【ローカル】" + (", ".join(local_names) or "(なし)") + "\n"
        f"【リモートGuild】{len(remote_guild)} 件\n"
        f"【リモートGlobal】{len(remote_global)} 件"
    )
    await ctx.reply(msg, mention_author=False)

@bot.command(name="repair_sync")
async def repair_sync(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        return await ctx.reply("権限がありません。", mention_author=False)
    try:
        remote_guild = await tree.fetch_commands(guild=ctx.guild)
        if len(remote_guild) == 0:
            tree.clear_commands(guild=ctx.guild)
            await tree.sync(guild=ctx.guild)     # 空同期
            if GUILD_IDS:
                for gid in GUILD_IDS:
                    await tree.sync(guild=discord.Object(id=gid))
            else:
                await tree.sync()
        local_after = [c.name for c in tree.get_commands(guild=ctx.guild)]
        remote_after = await tree.fetch_commands(guild=ctx.guild)
        await ctx.reply(
            "修復完了\n"
            f"【ローカル】{', '.join(local_after) or '(なし)'}\n"
            f"【リモートGuild】{len(remote_after)} 件",
            mention_author=False
        )
    except Exception as e:
        await ctx.reply(f"❌ 修復中エラー: {e}", mention_author=False)

# ========= スラッシュコマンド =========
@tree.command(name="create_mystery_panel", description="マーダーミステリー開催パネルを生成します。")
@app_commands.describe(
    title="パネル上部に表示するタイトル（例：マダミス開催告知）",
    date_time="開催予定日（例：2025年9月12日）",
    players="プレイヤー数（例：6）",
    duration="想定プレイ時間（例：2～3時間）",
    note="一言コメント（改行可）",
    bg_image_url="背景画像URL（未指定なら既定を使用）",
    corner_image_url="右上に表示する作品画像URL",
    participant_role="参加希望で付与するロール（未指定なら環境変数）",
    spectator_role="観戦希望で付与するロール（未指定なら環境変数）",
)
@app_commands.guilds(*[discord.Object(id=g) for g in GUILD_IDS] if GUILD_IDS else [])
async def create_mystery_panel(
    interaction: discord.Interaction,
    title: str,
    date_time: str,
    players: int,
    duration: str,
    note: str,
    bg_image_url: Optional[str] = None,
    corner_image_url: Optional[str] = None,
    participant_role: Optional[discord.Role] = None,
    spectator_role: Optional[discord.Role] = None,
):
    if not is_allowed(interaction):
        return await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)

    await interaction.response.defer(thinking=True, ephemeral=False)

    pr_id = participant_role.id if participant_role else (DEFAULT_PARTICIPANT_ROLE_ID or 0)
    sp_id = spectator_role.id if spectator_role else (DEFAULT_SPECTATOR_ROLE_ID or 0)
    if pr_id == 0 or sp_id == 0:
        return await interaction.followup.send(
            "❗ 参加/観戦ロールIDが未設定です。環境変数（PARTICIPANT_ROLE_ID / SPECTATOR_ROLE_ID）を設定するか、コマンド引数でロール指定してください。",
            ephemeral=True,
        )

    panel_png = make_panel(
        bg_url=bg_image_url or DEFAULT_BG_IMAGE_URL,
        corner_image_url=corner_image_url or "",
        title=title,
        date_time=date_time,
        players=players,
        duration=duration,
        note=note,
    )
    file = discord.File(io.BytesIO(panel_png), filename="mystery_panel.png")

    embed = discord.Embed(
        title="マーダーミステリー開催！",
        description="下のボタンから「参加希望 / 観戦希望」を選べます。",
        color=discord.Color.gold(),
    )
    embed.set_image(url="attachment://mystery_panel.png")
    embed.set_footer(text=_hide_payload(f"participant={pr_id}|spectator={sp_id}"))  # UIには表示されない

    view = MysterySignupView()
    await interaction.followup.send(file=file, embed=embed, view=view)

@tree.command(name="ping", description="疎通確認")
@app_commands.guilds(*[discord.Object(id=g) for g in GUILD_IDS] if GUILD_IDS else [])
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

# ========= エラーハンドラ =========
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Slash command error")
    try:
        await interaction.response.send_message(f"エラー: {error}", ephemeral=True)
    except:
        await interaction.followup.send(f"エラー: {error}", ephemeral=True)

# ========= 実行 =========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN が未設定です。")
    bot.run(DISCORD_TOKEN)
