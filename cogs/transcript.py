"""Generates Discord-styled HTML transcripts for ticket channels."""

import re
import html as _html
import json
from datetime import datetime, timezone
import discord


# ── Helpers ────────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return _html.escape(str(text), quote=True)


def _role_color(member: discord.Member | None) -> str:
    if not member:
        return "#f2f3f5"
    for role in sorted(member.roles, key=lambda r: r.position, reverse=True):
        if role.color.value:
            return f"#{role.color.value:06x}"
    return "#f2f3f5"


def _top_role_name(member: discord.Member | None) -> str:
    if not member:
        return ""
    for role in sorted(member.roles, key=lambda r: r.position, reverse=True):
        if role.name != "@everyone":
            return role.name
    return ""


def _avatar_url(user: discord.User | discord.Member) -> str:
    av = user.display_avatar
    return str(av.with_size(128).url) if av else ""


def _fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%-m/%-d/%Y %-I:%M %p")


def _fmt_ts_full(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%A, %B %-d, %Y %-I:%M %p UTC")


def _fmt_short_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%-I:%M %p")


# ── Markdown → HTML ────────────────────────────────────────────────────────────

_CB_RE      = re.compile(r'```(?:(\w+)\n)?([\s\S]*?)```', re.DOTALL)
_IC_RE      = re.compile(r'`([^`\n]+)`')
_BOLD_RE    = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
_UL_RE      = re.compile(r'__(.+?)__',     re.DOTALL)
_ITALIC_RE  = re.compile(r'\*(.+?)\*|(?<!_)_(.+?)_(?!_)', re.DOTALL)
_STRIKE_RE  = re.compile(r'~~(.+?)~~',     re.DOTALL)
_SPOILER_RE = re.compile(r'\|\|(.+?)\|\|', re.DOTALL)
_QUOTE_RE   = re.compile(r'^&gt; (.+)$',   re.MULTILINE)
_USER_RE    = re.compile(r'&lt;@!?(\d+)&gt;')
_CHAN_RE    = re.compile(r'&lt;#(\d+)&gt;')
_ROLE_RE    = re.compile(r'&lt;@&amp;(\d+)&gt;')
_EMOJI_RE   = re.compile(r'&lt;(a?):(\w+):(\d+)&gt;')


def _markdown(text: str, guild: discord.Guild | None = None) -> str:
    # Protect code blocks with null-byte placeholders before escaping
    slots: dict[str, str] = {}
    idx = 0

    def save(rendered: str) -> str:
        nonlocal idx
        key = f"\x00{idx}\x00"
        slots[key] = rendered
        idx += 1
        return key

    def cb_repl(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2).strip()
        label = f'<span class="lang">{_esc(lang)}</span>' if lang else ""
        return save(f'<pre>{label}<code>{_esc(code)}</code></pre>')

    def ic_repl(m: re.Match) -> str:
        return save(f'<code class="inline">{_esc(m.group(1))}</code>')

    text = _CB_RE.sub(cb_repl, text)
    text = _IC_RE.sub(ic_repl, text)

    # Now HTML-escape the rest
    result = _esc(text)

    # Mentions / emoji (patterns match escaped HTML)
    def user_m(m: re.Match) -> str:
        uid = m.group(1)
        if guild:
            mem = guild.get_member(int(uid))
            name = mem.display_name if mem else uid
        else:
            name = uid
        return f'<span class="mention">@{_esc(name)}</span>'

    def chan_m(m: re.Match) -> str:
        cid = m.group(1)
        if guild:
            ch = guild.get_channel(int(cid))
            name = f"#{ch.name}" if ch else f"#{cid}"
        else:
            name = f"#{cid}"
        return f'<span class="mention">{_esc(name)}</span>'

    def role_m(m: re.Match) -> str:
        rid = m.group(1)
        if guild:
            role = guild.get_role(int(rid))
            name = f"@{role.name}" if role else f"@{rid}"
        else:
            name = f"@{rid}"
        return f'<span class="mention">{_esc(name)}</span>'

    def emoji_m(m: re.Match) -> str:
        animated, name, eid = m.group(1), m.group(2), m.group(3)
        ext = "gif" if animated else "webp"
        return f'<img src="https://cdn.discordapp.com/emojis/{eid}.{ext}?size=32" class="emoji" alt=":{name}:">'

    result = _USER_RE.sub(user_m, result)
    result = _CHAN_RE.sub(chan_m, result)
    result = _ROLE_RE.sub(role_m, result)
    result = _EMOJI_RE.sub(emoji_m, result)

    # Formatting (order: bold/underline before italic to avoid greed clashes)
    result = _BOLD_RE.sub(lambda m: f'<strong>{m.group(1)}</strong>', result)
    result = _UL_RE.sub(lambda m: f'<u>{m.group(1)}</u>', result)
    result = _ITALIC_RE.sub(lambda m: f'<em>{m.group(1) or m.group(2)}</em>', result)
    result = _STRIKE_RE.sub(lambda m: f'<s>{m.group(1)}</s>', result)
    result = _SPOILER_RE.sub(lambda m: f'<span class="spoiler" onclick="this.classList.toggle(\'revealed\')">{m.group(1)}</span>', result)
    result = _QUOTE_RE.sub(lambda m: f'<blockquote>{m.group(1)}</blockquote>', result)

    # Line breaks
    result = result.replace('\n', '<br>')

    # Restore code block slots
    for key, val in slots.items():
        result = result.replace(_esc(key), val)

    return result


# ── Embed renderer ─────────────────────────────────────────────────────────────

def _render_embed(emb: discord.Embed) -> str:
    color = f"#{emb.color.value:06x}" if (emb.color and emb.color.value) else "#4e5058"
    parts = [f'<div class="embed" style="border-left-color:{color}">']

    if emb.author and emb.author.name:
        av = f'<img src="{_esc(emb.author.icon_url)}" class="embed-author-icon">' if emb.author.icon_url else ""
        parts.append(f'<div class="embed-author">{av}<span>{_esc(emb.author.name)}</span></div>')

    if emb.title:
        url_open = f'<a href="{_esc(emb.url)}" target="_blank" class="embed-title-link">' if emb.url else ""
        url_close = "</a>" if emb.url else ""
        parts.append(f'<div class="embed-title">{url_open}{_esc(emb.title)}{url_close}</div>')

    if emb.description:
        parts.append(f'<div class="embed-desc">{_esc(emb.description)}</div>')

    if emb.fields:
        parts.append('<div class="embed-fields">')
        for f in emb.fields:
            cls = "embed-field-inline" if f.inline else "embed-field"
            parts.append(
                f'<div class="{cls}">'
                f'<div class="embed-field-name">{_esc(f.name)}</div>'
                f'<div class="embed-field-val">{_esc(f.value)}</div>'
                f'</div>'
            )
        parts.append('</div>')

    if emb.thumbnail and emb.thumbnail.url:
        parts.append(f'<img src="{_esc(emb.thumbnail.url)}" class="embed-thumbnail">')

    if emb.image and emb.image.url:
        parts.append(f'<img src="{_esc(emb.image.url)}" class="embed-image">')

    if emb.footer and emb.footer.text:
        fi = f'<img src="{_esc(emb.footer.icon_url)}" class="embed-footer-icon">' if emb.footer.icon_url else ""
        ts_txt = ""
        if emb.timestamp:
            ts_txt = f" • {_fmt_ts(emb.timestamp)}"
        parts.append(f'<div class="embed-footer">{fi}<span>{_esc(emb.footer.text)}{ts_txt}</span></div>')

    parts.append('</div>')
    return ''.join(parts)


# ── Main generator ─────────────────────────────────────────────────────────────

async def generate_transcript(
    channel: discord.TextChannel,
    guild: discord.Guild,
    ticket_id: str,
) -> bytes:
    """Fetch all messages and return a self-contained Discord-styled HTML file."""

    messages: list[discord.Message] = []
    async for msg in channel.history(limit=None, oldest_first=True):
        messages.append(msg)

    # ── Collect per-user metadata for profile popups ──────────────────────────
    users: dict[str, dict] = {}
    for msg in messages:
        uid = str(msg.author.id)
        if uid in users:
            continue
        member = guild.get_member(msg.author.id)
        color = _role_color(member)
        users[uid] = {
            "name":     msg.author.display_name,
            "tag":      str(msg.author),
            "avatar":   _avatar_url(msg.author),
            "color":    color,
            "top_role": _top_role_name(member),
            "joined":   member.joined_at.strftime("%b %-d, %Y") if (member and member.joined_at) else "",
            "created":  msg.author.created_at.strftime("%b %-d, %Y") if msg.author.created_at else "",
            "bot":      msg.author.bot,
        }

    # ── Render each message ───────────────────────────────────────────────────
    GROUP_SECS = 420  # 7 minutes — same as Discord
    parts: list[str] = []
    prev_id: int | None = None
    prev_dt: datetime | None = None

    for msg in messages:
        uid = str(msg.author.id)
        u = users[uid]
        dt = msg.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        grouped = (
            prev_id == msg.author.id
            and prev_dt is not None
            and (dt - prev_dt).total_seconds() < GROUP_SECS
            and not msg.reference
            and not msg.type in (discord.MessageType.pins_add, discord.MessageType.new_member)
        )
        prev_id = msg.author.id
        prev_dt = dt

        buf: list[str] = []

        if not grouped:
            buf.append(
                f'<div class="msg-group">'
                f'<img src="{_esc(u["avatar"])}" class="msg-avatar" onclick="showProfile(\'{_esc(uid)}\')" alt="">'
                f'<div class="msg-right">'
                f'<div class="msg-header">'
                f'<span class="msg-author" style="color:{u["color"]}" onclick="showProfile(\'{_esc(uid)}\')">'
                f'{_esc(u["name"])}</span>'
                + ('<span class="bot-badge">APP</span>' if u["bot"] else "")
                + f'<span class="msg-ts" title="{_esc(_fmt_ts_full(dt))}">{_esc(_fmt_ts(dt))}</span>'
                f'</div>'
            )
        else:
            buf.append(
                f'<div class="msg-cont">'
                f'<span class="cont-ts" title="{_esc(_fmt_ts_full(dt))}">{_esc(_fmt_short_time(dt))}</span>'
                f'<div class="msg-right">'
            )

        # Reply
        if msg.reference and isinstance(msg.reference.resolved, discord.Message):
            ref = msg.reference.resolved
            ref_mem = guild.get_member(ref.author.id)
            rc = _role_color(ref_mem)
            snippet = (ref.content[:80] + "…") if ref.content and len(ref.content) > 80 else (ref.content or "*[no text]*")
            buf.append(
                f'<div class="reply">'
                f'<div class="reply-line"></div>'
                f'<img src="{_esc(_avatar_url(ref.author))}" class="reply-av" alt="">'
                f'<span class="reply-name" style="color:{rc}">{_esc(ref.author.display_name)}</span>'
                f'<span class="reply-txt">{_esc(snippet)}</span>'
                f'</div>'
            )

        # Content
        if msg.content:
            buf.append(f'<div class="msg-text">{_markdown(msg.content, guild)}</div>')

        # System messages
        if msg.type == discord.MessageType.pins_add:
            buf.append('<div class="sys-msg">📌 A message was pinned.</div>')
        elif msg.type == discord.MessageType.new_member:
            buf.append(f'<div class="sys-msg">👋 {_esc(msg.author.display_name)} joined the server.</div>')

        # Attachments
        for att in msg.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                buf.append(f'<div class="att"><a href="{_esc(att.url)}" target="_blank"><img src="{_esc(att.url)}" class="att-img" alt="{_esc(att.filename)}"></a></div>')
            else:
                buf.append(f'<div class="att att-file"><a href="{_esc(att.url)}" target="_blank" class="att-link">📎 {_esc(att.filename)}</a><span class="att-size">{att.size // 1024} KB</span></div>')

        # Embeds
        for emb in msg.embeds:
            buf.append(_render_embed(emb))

        # Reactions
        if msg.reactions:
            rxns = []
            for r in msg.reactions:
                if hasattr(r.emoji, "id") and r.emoji.id:
                    ext = "gif" if r.emoji.animated else "webp"
                    em = f'<img src="https://cdn.discordapp.com/emojis/{r.emoji.id}.{ext}?size=32" class="emoji" alt=":{r.emoji.name}:">'
                else:
                    em = _esc(str(r.emoji))
                rxns.append(f'<div class="reaction">{em}<span>{r.count}</span></div>')
            buf.append(f'<div class="reactions">{"".join(rxns)}</div>')

        buf.append('</div>')  # close msg-right
        buf.append('</div>')  # close msg-group / msg-cont
        parts.append(''.join(buf))

    # ── Assemble HTML ─────────────────────────────────────────────────────────
    guild_icon_html = (
        f'<img src="{_esc(str(guild.icon.with_size(64).url))}" class="hdr-icon" alt="">'
        if guild.icon else
        f'<div class="hdr-icon-text">{"".join(w[0].upper() for w in guild.name.split()[:2])}</div>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transcript · #{_esc(channel.name)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="hdr">
  {guild_icon_html}
  <div class="hdr-info">
    <div class="hdr-server">{_esc(guild.name)}</div>
    <div class="hdr-channel"># {_esc(channel.name)}</div>
    <div class="hdr-meta">{len(messages)} messages &bull; Ticket {_esc(ticket_id)}</div>
  </div>
</header>
<div class="msgs">
{"".join(parts)}
</div>

<!-- Profile popup -->
<div class="overlay" id="overlay" onclick="closeCard()"></div>
<div class="card" id="card">
  <div class="card-banner" id="card-banner"></div>
  <div class="card-av-wrap">
    <img class="card-av" id="card-av" src="" alt="">
  </div>
  <div class="card-body">
    <div class="card-name" id="card-name"></div>
    <div class="card-tag"  id="card-tag"></div>
    <div class="card-div"></div>
    <div id="card-role-section">
      <div class="card-label">ROLES</div>
      <div class="card-roles" id="card-roles"></div>
      <div class="card-div" style="margin-top:12px"></div>
    </div>
    <div class="card-label">MEMBER SINCE</div>
    <div class="card-val"  id="card-joined"></div>
    <div class="card-label" style="margin-top:10px">DISCORD MEMBER SINCE</div>
    <div class="card-val"  id="card-created"></div>
  </div>
</div>

<script>
const U = {json.dumps(users, ensure_ascii=False)};
function esc(s) {{ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
function showProfile(uid) {{
  const u = U[uid];
  if (!u) return;
  document.getElementById('card-av').src = u.avatar;
  document.getElementById('card-name').textContent = u.name;
  document.getElementById('card-name').style.color = u.color;
  document.getElementById('card-tag').textContent = u.tag;
  document.getElementById('card-joined').textContent = u.joined || '—';
  document.getElementById('card-created').textContent = u.created || '—';
  document.getElementById('card-banner').style.background =
    'linear-gradient(135deg,' + u.color + '99, ' + u.color + '33)';
  const rs = document.getElementById('card-role-section');
  const rc = document.getElementById('card-roles');
  if (u.top_role) {{
    rc.innerHTML = '<span class="role-pill" style="border-color:' + u.color + '">'
      + '<span class="role-dot" style="background:' + u.color + '"></span>'
      + esc(u.top_role) + '</span>';
    rs.style.display = 'block';
  }} else {{
    rs.style.display = 'none';
  }}
  document.getElementById('overlay').classList.add('show');
  document.getElementById('card').classList.add('show');
}}
function closeCard() {{
  document.getElementById('overlay').classList.remove('show');
  document.getElementById('card').classList.remove('show');
}}
document.addEventListener('keydown', e => {{ if(e.key==='Escape') closeCard(); }});
</script>
</body>
</html>"""

    return html.encode("utf-8")


# ── CSS ────────────────────────────────────────────────────────────────────────

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  background:#313338;
  color:#dbdee1;
  font-family:"gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif;
  font-size:16px;
  line-height:1.375;
}

/* ── Header ── */
.hdr{
  background:#2b2d31;
  border-bottom:1px solid #1e1f22;
  padding:14px 20px;
  display:flex;
  align-items:center;
  gap:14px;
  position:sticky;top:0;z-index:5;
}
.hdr-icon,.hdr-icon-text{
  width:48px;height:48px;border-radius:50%;flex-shrink:0;object-fit:cover;
}
.hdr-icon-text{
  background:#5865f2;
  display:flex;align-items:center;justify-content:center;
  font-size:15px;font-weight:700;color:#fff;
}
.hdr-server{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#949ba4;font-weight:600}
.hdr-channel{font-size:18px;font-weight:700;color:#f2f3f5}
.hdr-meta{font-size:12px;color:#949ba4;margin-top:2px}

/* ── Messages ── */
.msgs{padding:16px 0 60px}

/* First message in an author run */
.msg-group{
  display:flex;gap:0;
  padding:2px 48px 2px 72px;
  position:relative;
  margin-top:17px;
}
.msg-group:hover,.msg-cont:hover{background:#2e3035}

.msg-avatar{
  width:40px;height:40px;border-radius:50%;
  position:absolute;left:16px;top:2px;
  cursor:pointer;object-fit:cover;
  transition:opacity .1s;
}
.msg-avatar:hover{opacity:.85}

/* Continuation messages (same author < 7 min) */
.msg-cont{
  display:flex;gap:0;
  padding:1px 48px 1px 72px;
  position:relative;
}
.cont-ts{
  position:absolute;left:16px;top:50%;transform:translateY(-50%);
  width:50px;text-align:right;
  font-size:11px;color:#949ba4;
  opacity:0;pointer-events:none;transition:opacity .1s;
}
.msg-cont:hover .cont-ts{opacity:1}

.msg-right{flex:1;min-width:0}
.msg-header{display:flex;align-items:baseline;gap:8px;margin-bottom:2px;flex-wrap:wrap}
.msg-author{
  font-size:16px;font-weight:500;cursor:pointer;
  transition:text-decoration .05s;
}
.msg-author:hover{text-decoration:underline}
.msg-ts{font-size:12px;color:#949ba4;flex-shrink:0}
.bot-badge{
  background:#5865f2;color:#fff;
  font-size:10px;font-weight:700;
  padding:1px 5px;border-radius:3px;
  text-transform:uppercase;letter-spacing:.02em;
}

/* Message content */
.msg-text{
  font-size:16px;color:#dbdee1;
  word-break:break-word;white-space:pre-wrap;
}
.msg-text strong{font-weight:700}
.msg-text em{font-style:italic}
.msg-text s{text-decoration:line-through}
.msg-text u{text-decoration:underline}
.msg-text code.inline{
  background:#2b2d31;border-radius:3px;
  padding:1px 5px;font-family:"Consolas",monospace;font-size:14px;
}
.msg-text pre{
  background:#2b2d31;border-radius:6px;
  padding:12px 16px;margin:4px 0;
  overflow-x:auto;max-width:100%;
}
.msg-text pre .lang{
  display:block;font-size:11px;text-transform:uppercase;
  color:#949ba4;margin-bottom:6px;letter-spacing:.04em;
}
.msg-text pre code{
  font-family:"Consolas",monospace;font-size:14px;color:#dbdee1;
}
.msg-text blockquote{
  border-left:4px solid #4e5058;
  padding-left:12px;margin:4px 0;
  color:#dbdee1;
}
.spoiler{
  background:#202225;color:transparent;border-radius:3px;
  cursor:pointer;transition:all .15s;padding:0 2px;
}
.spoiler.revealed{background:rgba(255,255,255,.1);color:#dbdee1}
.mention{
  background:rgba(88,101,242,.15);color:#c9cdfb;
  border-radius:3px;padding:0 3px;cursor:default;
}
.emoji{height:1.375em;width:auto;vertical-align:middle}
h3{font-size:18px;font-weight:700;color:#f2f3f5;margin:8px 0 4px}

/* ── Replies ── */
.reply{
  display:flex;align-items:center;gap:6px;
  font-size:13px;color:#949ba4;margin-bottom:4px;
}
.reply-line{
  width:38px;height:10px;
  border-top:2px solid #4e5058;border-left:2px solid #4e5058;
  border-radius:6px 0 0 0;flex-shrink:0;margin-left:4px;
}
.reply-av{width:16px;height:16px;border-radius:50%;object-fit:cover}
.reply-name{font-weight:500;cursor:pointer;flex-shrink:0}
.reply-name:hover{text-decoration:underline}
.reply-txt{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  max-width:380px;
}

/* ── Attachments ── */
.att{margin:4px 0}
.att-img{
  max-width:400px;max-height:300px;border-radius:4px;display:block;
  cursor:pointer;
}
.att-file{
  display:flex;align-items:center;gap:10px;
  background:#2b2d31;border-radius:8px;padding:10px 14px;
  max-width:380px;
}
.att-link{color:#00a8fc;text-decoration:none;font-size:14px}
.att-link:hover{text-decoration:underline}
.att-size{font-size:12px;color:#949ba4}

/* ── Embeds ── */
.embed{
  background:#2b2d31;border-left:4px solid #4e5058;
  border-radius:0 4px 4px 0;
  padding:12px 16px;margin:4px 0;
  max-width:520px;position:relative;
}
.embed-author{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:14px;font-weight:600}
.embed-author-icon{width:20px;height:20px;border-radius:50%}
.embed-title{font-size:16px;font-weight:700;color:#f2f3f5;margin-bottom:8px}
.embed-title-link{color:#00a8fc;text-decoration:none}
.embed-title-link:hover{text-decoration:underline}
.embed-desc{font-size:14px;color:#dbdee1;white-space:pre-wrap;margin-bottom:8px}
.embed-fields{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.embed-field{width:100%}
.embed-field-inline{flex:1 1 120px}
.embed-field-name{font-size:12px;font-weight:700;color:#f2f3f5;text-transform:uppercase;letter-spacing:.02em;margin-bottom:2px}
.embed-field-val{font-size:14px;color:#dbdee1}
.embed-thumbnail{
  position:absolute;top:12px;right:12px;
  width:80px;height:80px;border-radius:4px;object-fit:cover;
}
.embed-image{max-width:100%;border-radius:4px;margin-top:8px;display:block}
.embed-footer{display:flex;align-items:center;gap:8px;font-size:12px;color:#949ba4;margin-top:10px}
.embed-footer-icon{width:16px;height:16px;border-radius:50%}

/* ── Reactions ── */
.reactions{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.reaction{
  background:rgba(88,101,242,.15);
  border:1px solid rgba(88,101,242,.4);
  border-radius:8px;padding:2px 8px;
  font-size:13px;display:flex;align-items:center;gap:5px;
}
.reaction span{font-weight:600;color:#dbdee1}

/* ── System messages ── */
.sys-msg{
  font-size:14px;color:#949ba4;font-style:italic;
  padding:4px 0;
}

/* ── Profile popup ── */
.overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.7);z-index:50;
  animation:fadeIn .15s;
}
.overlay.show{display:block}
.card{
  display:none;position:fixed;
  z-index:51;top:50%;left:50%;
  transform:translate(-50%,-50%);
  width:300px;
  background:#111214;
  border-radius:10px;overflow:hidden;
  box-shadow:0 24px 64px rgba(0,0,0,.6);
  animation:popIn .15s;
}
.card.show{display:block}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes popIn{from{opacity:0;transform:translate(-50%,-48%)}to{opacity:1;transform:translate(-50%,-50%)}}
.card-banner{height:60px}
.card-av-wrap{padding:0 16px;margin-top:-28px;margin-bottom:6px}
.card-av{
  width:72px;height:72px;border-radius:50%;
  border:6px solid #111214;
  object-fit:cover;display:block;
}
.card-body{padding:6px 16px 16px}
.card-name{font-size:20px;font-weight:800;color:#f2f3f5;line-height:1.2}
.card-tag{font-size:14px;color:#949ba4;margin-top:2px;margin-bottom:10px}
.card-div{height:1px;background:#2b2d31;margin:10px 0}
.card-label{
  font-size:11px;font-weight:700;color:#949ba4;
  text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;
}
.card-val{font-size:14px;color:#dbdee1}
.card-roles{display:flex;flex-wrap:wrap;gap:6px}
.role-pill{
  border:1px solid;border-radius:4px;
  padding:2px 8px;font-size:12px;
  display:flex;align-items:center;gap:6px;
}
.role-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
"""
