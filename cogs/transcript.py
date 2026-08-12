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


def _all_roles(member: discord.Member | None) -> list[dict]:
    """All named roles (excl. @everyone) with color and icon URL, position desc."""
    if not member:
        return []
    out = []
    for role in sorted(member.roles, key=lambda r: r.position, reverse=True):
        if role.name == "@everyone":
            continue
        color = f"#{role.color.value:06x}" if role.color.value else "#949ba4"
        icon_url = str(role.icon.url) if role.icon else None
        out.append({"name": role.name, "color": color, "icon": icon_url})
    return out[:28]


def _status_color(member: discord.Member | None) -> str:
    if not member:
        return "#80848e"
    mapping = {
        discord.Status.online:    "#23a55a",
        discord.Status.idle:      "#f0b232",
        discord.Status.dnd:       "#f23f43",
        discord.Status.offline:   "#80848e",
        discord.Status.invisible: "#80848e",
    }
    return mapping.get(member.status, "#80848e")


def _avatar_url(user: discord.User | discord.Member) -> str:
    av = user.display_avatar
    return str(av.with_size(128).url) if av else ""


def _fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.month}/{dt.day}/{dt.year} {dt.strftime('%I:%M %p').lstrip('0')}"


def _fmt_ts_full(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year} {dt.strftime('%I:%M %p').lstrip('0')} UTC"


def _fmt_short_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%I:%M %p").lstrip("0")


def _date_label(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).date()
    d = dt.date()
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


# ── Markdown → HTML ────────────────────────────────────────────────────────────

_CB_RE     = re.compile(r'```(?:(\w+)\n)?([\s\S]*?)```', re.DOTALL)
_IC_RE     = re.compile(r'`([^`\n]+)`')
_BOLD_RE   = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
_UL_RE     = re.compile(r'__(.+?)__',     re.DOTALL)
_ITALIC_RE = re.compile(r'\*(.+?)\*|(?<!_)_(.+?)_(?!_)', re.DOTALL)
_STRIKE_RE = re.compile(r'~~(.+?)~~',     re.DOTALL)
_SPOIL_RE  = re.compile(r'\|\|(.+?)\|\|', re.DOTALL)
_QUOTE_RE  = re.compile(r'^&gt; (.+)$',   re.MULTILINE)
_USER_RE   = re.compile(r'&lt;@!?(\d+)&gt;')
_CHAN_RE   = re.compile(r'&lt;#(\d+)&gt;')
_ROLE_RE   = re.compile(r'&lt;@&amp;(\d+)&gt;')
_EMOJ_RE   = re.compile(r'&lt;(a?):(\w+):(\d+)&gt;')


def _markdown(text: str, guild: discord.Guild | None = None) -> str:
    slots: dict[str, str] = {}
    idx = 0

    def save(rendered: str) -> str:
        nonlocal idx
        key = f"\x00{idx}\x00"
        slots[key] = rendered
        idx += 1
        return key

    text = _CB_RE.sub(
        lambda m: save(
            f'<pre>{"<span class=lang>" + _esc(m.group(1)) + "</span>" if m.group(1) else ""}'
            f'<code>{_esc(m.group(2).strip())}</code></pre>'
        ), text
    )
    text = _IC_RE.sub(lambda m: save(f'<code class="inline">{_esc(m.group(1))}</code>'), text)

    result = _esc(text)

    def _umention(m: re.Match) -> str:
        uid = m.group(1)
        if guild:
            mem = guild.get_member(int(uid))
            name = mem.display_name if mem else uid
        else:
            name = uid
        return f'<span class="mention">@{_esc(name)}</span>'

    def _cmention(m: re.Match) -> str:
        cid = m.group(1)
        if guild:
            ch = guild.get_channel(int(cid))
            name = f"#{ch.name}" if ch else f"#{cid}"
        else:
            name = f"#{cid}"
        return f'<span class="mention">{_esc(name)}</span>'

    def _rmention(m: re.Match) -> str:
        rid = m.group(1)
        if guild:
            role = guild.get_role(int(rid))
            name = f"@{role.name}" if role else f"@{rid}"
        else:
            name = f"@{rid}"
        return f'<span class="mention">{_esc(name)}</span>'

    def _emojim(m: re.Match) -> str:
        anim, name, eid = m.group(1), m.group(2), m.group(3)
        ext = "gif" if anim else "webp"
        return f'<img src="https://cdn.discordapp.com/emojis/{eid}.{ext}?size=32" class="emoji" alt=":{name}:">'

    result = _USER_RE.sub(_umention, result)
    result = _CHAN_RE.sub(_cmention, result)
    result = _ROLE_RE.sub(_rmention, result)
    result = _EMOJ_RE.sub(_emojim,   result)

    result = _BOLD_RE.sub(  lambda m: f'<strong>{m.group(1)}</strong>', result)
    result = _UL_RE.sub(    lambda m: f'<u>{m.group(1)}</u>',           result)
    result = _ITALIC_RE.sub(lambda m: f'<em>{m.group(1) or m.group(2)}</em>', result)
    result = _STRIKE_RE.sub(lambda m: f'<s>{m.group(1)}</s>',           result)
    result = _SPOIL_RE.sub(
        lambda m: f'<span class="spoiler" onclick="this.classList.toggle(\'revealed\')">{m.group(1)}</span>',
        result
    )
    result = _QUOTE_RE.sub(lambda m: f'<blockquote>{m.group(1)}</blockquote>', result)
    result = result.replace('\n', '<br>')

    for key, val in slots.items():
        result = result.replace(_esc(key), val)
    return result


# ── Embed renderer ─────────────────────────────────────────────────────────────

def _render_embed(emb: discord.Embed) -> str:
    color = f"#{emb.color.value:06x}" if (emb.color and emb.color.value) else "#4e5058"
    parts = [f'<div class="embed" style="--ec:{color}">']

    if emb.author and emb.author.name:
        av = (f'<img src="{_esc(emb.author.icon_url)}" class="embed-author-icon">'
              if emb.author.icon_url else "")
        parts.append(f'<div class="embed-author">{av}<span>{_esc(emb.author.name)}</span></div>')

    if emb.title:
        link_o = f'<a href="{_esc(emb.url)}" target="_blank" class="embed-title-link">' if emb.url else ""
        link_c = "</a>" if emb.url else ""
        parts.append(f'<div class="embed-title">{link_o}{_esc(emb.title)}{link_c}</div>')

    if emb.description:
        parts.append(f'<div class="embed-desc">{_markdown(emb.description)}</div>')

    if emb.fields:
        parts.append('<div class="embed-fields">')
        for f in emb.fields:
            cls = "embed-field-inline" if f.inline else "embed-field"
            parts.append(
                f'<div class="{cls}">'
                f'<div class="embed-field-name">{_esc(f.name)}</div>'
                f'<div class="embed-field-val">{_esc(f.value)}</div></div>'
            )
        parts.append('</div>')

    if emb.thumbnail and emb.thumbnail.url:
        parts.append(f'<img src="{_esc(emb.thumbnail.url)}" class="embed-thumbnail" loading="lazy">')
    if emb.image and emb.image.url:
        parts.append(f'<img src="{_esc(emb.image.url)}" class="embed-image" loading="lazy">')

    if emb.footer and emb.footer.text:
        fi = (f'<img src="{_esc(emb.footer.icon_url)}" class="embed-footer-icon">'
              if emb.footer.icon_url else "")
        ts = f" • {_fmt_ts(emb.timestamp)}" if emb.timestamp else ""
        parts.append(f'<div class="embed-footer">{fi}<span>{_esc(emb.footer.text)}{ts}</span></div>')

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

    # ── Collect per-user metadata ─────────────────────────────────────────────
    users: dict[str, dict] = {}
    participants: list[dict] = []

    for msg in messages:
        uid = str(msg.author.id)
        if uid in users:
            continue
        member = guild.get_member(msg.author.id)
        color  = _role_color(member)
        roles  = _all_roles(member)
        users[uid] = {
            "name":         msg.author.display_name,
            "tag":          str(msg.author),
            "avatar":       _avatar_url(msg.author),
            "color":        color,
            "status_color": _status_color(member),
            "roles":        roles,
            "joined":       (f"{member.joined_at.strftime('%B')} {member.joined_at.day}, {member.joined_at.year}"
                             if member and member.joined_at else ""),
            "created":      (f"{msg.author.created_at.strftime('%B')} {msg.author.created_at.day}, {msg.author.created_at.year}"
                             if msg.author.created_at else ""),
            "bot":          msg.author.bot,
        }
        participants.append({"avatar": _avatar_url(msg.author), "name": msg.author.display_name,
                             "color": color, "uid": uid})

    # ── Render messages ───────────────────────────────────────────────────────
    GROUP_SECS = 420
    parts: list[str] = []
    prev_id: int | None = None
    prev_dt: datetime | None = None
    prev_date = None

    for msg in messages:
        uid = str(msg.author.id)
        u   = users[uid]
        dt  = msg.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if dt.date() != prev_date:
            prev_date = dt.date()
            prev_id   = None
            prev_dt   = None
            parts.append(
                f'<div class="day-sep"><div class="day-line"></div>'
                f'<div class="day-label">{_esc(_date_label(dt))}</div>'
                f'<div class="day-line"></div></div>'
            )

        grouped = (
            prev_id == msg.author.id
            and prev_dt is not None
            and (dt - prev_dt).total_seconds() < GROUP_SECS
            and not msg.reference
            and msg.type not in (discord.MessageType.pins_add, discord.MessageType.new_member)
        )
        prev_id = msg.author.id
        prev_dt = dt

        buf: list[str] = []

        if not grouped:
            buf.append(
                f'<div class="msg-group">'
                f'<img src="{_esc(u["avatar"])}" class="msg-avatar" loading="lazy" alt="" '
                f'onclick="showProfile(\'{_esc(uid)}\')">'
                f'<div class="msg-right">'
                f'<div class="msg-header">'
                f'<span class="msg-author" style="color:{u["color"]}" '
                f'onclick="showProfile(\'{_esc(uid)}\')">{_esc(u["name"])}</span>'
                + ('<span class="bot-badge">BOT</span>' if u["bot"] else "")
                + f'<span class="msg-ts" title="{_esc(_fmt_ts_full(dt))}">{_esc(_fmt_ts(dt))}</span>'
                f'</div>'
            )
        else:
            buf.append(
                f'<div class="msg-cont">'
                f'<span class="cont-ts" title="{_esc(_fmt_ts_full(dt))}">'
                f'{_esc(_fmt_short_time(dt))}</span>'
                f'<div class="msg-right">'
            )

        if msg.reference and isinstance(msg.reference.resolved, discord.Message):
            ref = msg.reference.resolved
            rc  = _role_color(guild.get_member(ref.author.id))
            snp = (ref.content[:80] + "…") if ref.content and len(ref.content) > 80 else (ref.content or "*[attachment]*")
            buf.append(
                f'<div class="reply">'
                f'<div class="reply-spine"></div>'
                f'<img src="{_esc(_avatar_url(ref.author))}" class="reply-av" loading="lazy" alt="">'
                f'<span class="reply-name" style="color:{rc}">{_esc(ref.author.display_name)}</span>'
                f'<span class="reply-txt">{_esc(snp)}</span>'
                f'</div>'
            )

        if msg.content:
            buf.append(f'<div class="msg-text">{_markdown(msg.content, guild)}</div>')

        if msg.type == discord.MessageType.pins_add:
            buf.append('<div class="sys-msg"><span class="sys-icon">📌</span> A message was pinned.</div>')
        elif msg.type == discord.MessageType.new_member:
            buf.append(f'<div class="sys-msg"><span class="sys-icon">👋</span> {_esc(msg.author.display_name)} joined the server.</div>')

        for att in msg.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                buf.append(f'<div class="att"><a href="{_esc(att.url)}" target="_blank"><img src="{_esc(att.url)}" class="att-img" loading="lazy" alt="{_esc(att.filename)}"></a></div>')
            else:
                buf.append(f'<div class="att att-file"><div class="att-file-icon">📎</div><div class="att-file-info"><a href="{_esc(att.url)}" target="_blank" class="att-link">{_esc(att.filename)}</a><div class="att-size">{att.size // 1024} KB</div></div></div>')

        for emb in msg.embeds:
            buf.append(_render_embed(emb))

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

        buf.append('</div></div>')
        parts.append(''.join(buf))

    # ── Header ────────────────────────────────────────────────────────────────
    guild_icon_html = (
        f'<img src="{_esc(str(guild.icon.with_size(64).url))}" class="hdr-icon" alt="">'
        if guild.icon else
        f'<div class="hdr-icon-text">{"".join(w[0].upper() for w in guild.name.split()[:2])}</div>'
    )
    shown = participants[:8]
    extra = max(0, len(participants) - 8)
    pax_html = "".join(
        f'<img src="{_esc(p["avatar"])}" class="pax-av" style="border-color:{p["color"]}" '
        f'title="{_esc(p["name"])}" onclick="showProfile(\'{_esc(p["uid"])}\')" loading="lazy" alt="">'
        for p in shown
    )
    if extra:
        pax_html += f'<div class="pax-more">+{extra}</div>'

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
  <div class="hdr-left">
    {guild_icon_html}
    <div class="hdr-info">
      <div class="hdr-server">{_esc(guild.name)}</div>
      <div class="hdr-channel"><span class="hdr-hash">#</span>{_esc(channel.name)}</div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="hdr-stats">
      <div class="hdr-stat"><div class="hdr-stat-val">{len(messages)}</div><div class="hdr-stat-lbl">Messages</div></div>
      <div class="hdr-sep"></div>
      <div class="hdr-stat"><div class="hdr-stat-val">{len(participants)}</div><div class="hdr-stat-lbl">Participants</div></div>
    </div>
    <div class="pax-row">{pax_html}</div>
  </div>
</header>

<div class="msgs">{"".join(parts)}</div>

<!-- Profile popup -->
<div class="overlay" id="overlay" onclick="closeCard()"></div>
<div class="card" id="card">
  <div class="card-banner" id="card-banner"></div>
  <div class="card-av-area">
    <div class="card-av-wrap">
      <img class="card-av" id="card-av" src="" alt="">
      <div class="card-status" id="card-status"></div>
    </div>
  </div>
  <div class="card-body">
    <div class="card-name" id="card-name"></div>
    <div class="card-tag"  id="card-tag"></div>
    <div class="card-divider"></div>
    <div id="card-role-section">
      <div class="card-label">ROLES</div>
      <div class="card-roles" id="card-roles"></div>
      <div class="card-divider"></div>
    </div>
    <div class="card-label">SERVER MEMBER SINCE</div>
    <div class="card-val" id="card-joined"></div>
    <div class="card-label" style="margin-top:12px">DISCORD MEMBER SINCE</div>
    <div class="card-val" id="card-created"></div>
  </div>
</div>

<script>
const U={json.dumps(users, ensure_ascii=False)};
function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function h2r(h){{
  const r=parseInt(h.slice(1,3),16),g=parseInt(h.slice(3,5),16),b=parseInt(h.slice(5,7),16);
  return [r,g,b];
}}
function rgba(h,a){{const [r,g,b]=h2r(h);return 'rgba('+r+','+g+','+b+','+a+')';}}

function showProfile(uid){{
  const u=U[uid];
  if(!u)return;
  const card=document.getElementById('card');
  document.getElementById('card-av').src=u.avatar;
  document.getElementById('card-name').textContent=u.name;
  document.getElementById('card-name').style.color=u.color;
  document.getElementById('card-tag').textContent=u.tag;
  document.getElementById('card-joined').textContent=u.joined||'—';
  document.getElementById('card-created').textContent=u.created||'—';
  const st=document.getElementById('card-status');
  st.style.background=u.status_color;
  const banner=document.getElementById('card-banner');
  banner.style.background='linear-gradient(160deg,'+rgba(u.color,.25)+' 0%,'+rgba(u.color,.08)+' 60%,transparent 100%)';
  const rs=document.getElementById('card-role-section');
  const rc=document.getElementById('card-roles');
  if(u.roles&&u.roles.length){{
    rc.innerHTML=u.roles.map(r=>{{
      const bdr=rgba(r.color,.4);
      const bg=rgba(r.color,.08);
      const icon=r.icon?'<img class="role-icon" src="'+esc(r.icon)+'" loading="lazy" alt="">':'';
      return '<span class="role-pill" style="border-color:'+bdr+';background:'+bg+'">'
        +'<span class="role-dot" style="background:'+r.color+'"></span>'
        +icon+'<span class="role-name">'+esc(r.name)+'</span></span>';
    }}).join('');
    rs.style.display='block';
  }}else{{
    rs.style.display='none';
  }}
  document.getElementById('overlay').classList.add('show');
  card.classList.add('show');
}}
function closeCard(){{
  document.getElementById('overlay').classList.remove('show');
  document.getElementById('card').classList.remove('show');
}}
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeCard();}});
</script>
</body>
</html>"""

    return html.encode("utf-8")


# ── CSS ────────────────────────────────────────────────────────────────────────

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  background:#313338;color:#dbdee1;
  font-family:"gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif;
  font-size:16px;line-height:1.375;
  -webkit-font-smoothing:antialiased;
}

/* ── Header ── */
.hdr{
  background:#2b2d31;border-bottom:1px solid #1e1f22;
  padding:12px 20px;display:flex;align-items:center;
  justify-content:space-between;gap:16px;
  position:sticky;top:0;z-index:10;
}
.hdr-left{display:flex;align-items:center;gap:14px}
.hdr-icon{width:44px;height:44px;border-radius:50%;object-fit:cover;flex-shrink:0}
.hdr-icon-text{width:44px;height:44px;border-radius:50%;background:#5865f2;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:#fff}
.hdr-server{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#949ba4;font-weight:700}
.hdr-channel{font-size:17px;font-weight:700;color:#f2f3f5;display:flex;align-items:center;gap:4px}
.hdr-hash{color:#949ba4;font-weight:400;font-size:20px;line-height:1}
.hdr-right{display:flex;align-items:center;gap:20px;flex-shrink:0}
.hdr-stats{display:flex;align-items:center;gap:16px}
.hdr-stat{text-align:right}
.hdr-stat-val{font-size:16px;font-weight:700;color:#f2f3f5;line-height:1}
.hdr-stat-lbl{font-size:11px;color:#949ba4;text-transform:uppercase;letter-spacing:.04em;margin-top:1px}
.hdr-sep{width:1px;height:32px;background:#3b3d44}
.pax-row{display:flex;align-items:center}
.pax-av{width:30px;height:30px;border-radius:50%;border:2px solid;object-fit:cover;
  cursor:pointer;margin-left:-8px;position:relative;z-index:1;transition:transform .15s,z-index 0s}
.pax-av:first-child{margin-left:0}
.pax-av:hover{transform:scale(1.15);z-index:10}
.pax-more{width:30px;height:30px;border-radius:50%;background:#3b3d44;border:2px solid #2b2d31;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:700;color:#949ba4;margin-left:-8px}

/* ── Day separators ── */
.day-sep{display:flex;align-items:center;gap:10px;padding:16px 16px 4px}
.day-line{flex:1;height:1px;background:#3b3d44}
.day-label{font-size:12px;font-weight:600;color:#949ba4;white-space:nowrap;padding:0 4px}

/* ── Messages ── */
.msgs{padding:4px 0 60px}
.msg-group{display:flex;padding:2px 48px 2px 72px;position:relative;margin-top:14px;border-radius:3px;transition:background .05s}
.msg-group:hover,.msg-cont:hover{background:#2e3035}
.msg-avatar{width:40px;height:40px;border-radius:50%;position:absolute;left:16px;top:2px;
  cursor:pointer;object-fit:cover;transition:opacity .1s,transform .1s}
.msg-avatar:hover{opacity:.9;transform:scale(1.05)}
.msg-cont{display:flex;padding:1px 48px 1px 72px;position:relative;border-radius:3px;transition:background .05s}
.cont-ts{position:absolute;left:18px;top:50%;transform:translateY(-50%);width:46px;text-align:right;
  font-size:10px;color:#949ba4;opacity:0;pointer-events:none;transition:opacity .1s}
.msg-cont:hover .cont-ts{opacity:1}
.msg-right{flex:1;min-width:0}
.msg-header{display:flex;align-items:baseline;gap:8px;margin-bottom:2px;flex-wrap:wrap}
.msg-author{font-size:16px;font-weight:500;cursor:pointer}
.msg-author:hover{text-decoration:underline}
.msg-ts{font-size:11px;color:#949ba4;flex-shrink:0}
.bot-badge{background:#5865f2;color:#fff;font-size:10px;font-weight:700;padding:1px 5px;
  border-radius:3px;text-transform:uppercase;letter-spacing:.02em;vertical-align:middle}
.msg-text{font-size:16px;color:#dbdee1;word-break:break-word}
.msg-text strong{font-weight:700}
.msg-text em{font-style:italic}
.msg-text s{text-decoration:line-through}
.msg-text u{text-decoration:underline}
.msg-text code.inline{background:#1e1f22;border-radius:3px;padding:1px 6px;
  font-family:"Consolas","Courier New",monospace;font-size:85%;color:#e3e5e8}
.msg-text pre{background:#1e1f22;border-radius:6px;padding:12px 16px;margin:6px 0;
  overflow-x:auto;max-width:100%;border:1px solid #2b2d31}
.msg-text pre .lang{display:block;font-size:11px;text-transform:uppercase;color:#5c6370;
  margin-bottom:8px;letter-spacing:.06em;font-family:"Consolas","Courier New",monospace}
.msg-text pre code{font-family:"Consolas","Courier New",monospace;font-size:14px;color:#abb2bf;line-height:1.5}
.msg-text blockquote{border-left:4px solid #4e5058;padding-left:12px;margin:4px 0;color:#dbdee1}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px;cursor:pointer;padding:0 2px;transition:all .15s}
.spoiler.revealed{background:rgba(255,255,255,.07);color:#dbdee1}
.mention{background:rgba(88,101,242,.15);color:#c9cdfb;border-radius:3px;padding:0 3px;font-weight:500}
.emoji{height:1.375em;width:auto;vertical-align:middle}

/* ── Replies ── */
.reply{display:flex;align-items:center;gap:6px;font-size:13px;color:#949ba4;margin-bottom:3px}
.reply-spine{width:34px;height:10px;border-top:2px solid #4e5058;border-left:2px solid #4e5058;
  border-radius:8px 0 0 0;flex-shrink:0;margin-left:2px}
.reply-av{width:16px;height:16px;border-radius:50%;object-fit:cover;flex-shrink:0}
.reply-name{font-weight:600;cursor:pointer;flex-shrink:0}
.reply-name:hover{text-decoration:underline;color:#dbdee1}
.reply-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:400px}

/* ── Attachments ── */
.att{margin:4px 0}
.att-img{max-width:420px;max-height:320px;border-radius:6px;display:block;transition:opacity .1s}
.att-img:hover{opacity:.9}
.att-file{display:flex;align-items:center;gap:12px;background:#2b2d31;border:1px solid #1e1f22;
  border-radius:8px;padding:10px 14px;max-width:380px}
.att-file-icon{font-size:24px;flex-shrink:0}
.att-file-info{min-width:0}
.att-link{color:#00a8fc;text-decoration:none;font-size:14px;font-weight:500;
  display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.att-link:hover{text-decoration:underline}
.att-size{font-size:12px;color:#949ba4;margin-top:2px}

/* ── Embeds ── */
.embed{background:#2b2d31;border-left:4px solid var(--ec,#4e5058);border-radius:0 6px 6px 0;
  padding:12px 16px 16px;margin:4px 0;max-width:520px;position:relative;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.04)}
.embed-author{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:14px;font-weight:600}
.embed-author-icon{width:20px;height:20px;border-radius:50%}
.embed-title{font-size:16px;font-weight:700;color:#f2f3f5;margin-bottom:8px;line-height:1.3}
.embed-title-link{color:#00a8fc;text-decoration:none}
.embed-title-link:hover{text-decoration:underline}
.embed-desc{font-size:14px;color:#dbdee1;white-space:pre-wrap;margin-bottom:8px;line-height:1.5}
.embed-fields{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.embed-field{width:100%}
.embed-field-inline{flex:1 1 120px}
.embed-field-name{font-size:12px;font-weight:700;color:#f2f3f5;text-transform:uppercase;letter-spacing:.02em;margin-bottom:3px}
.embed-field-val{font-size:14px;color:#dbdee1;line-height:1.4}
.embed-thumbnail{position:absolute;top:12px;right:12px;width:80px;height:80px;border-radius:6px;object-fit:cover}
.embed-image{max-width:100%;border-radius:6px;margin-top:8px;display:block}
.embed-footer{display:flex;align-items:center;gap:8px;font-size:12px;color:#949ba4;margin-top:10px}
.embed-footer-icon{width:16px;height:16px;border-radius:50%}

/* ── Reactions ── */
.reactions{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.reaction{background:rgba(88,101,242,.12);border:1px solid rgba(88,101,242,.35);
  border-radius:8px;padding:3px 8px;font-size:13px;display:flex;align-items:center;gap:5px}
.reaction span{font-weight:700;color:#c9cdfb;font-size:12px}

/* ── System messages ── */
.sys-msg{font-size:13px;color:#949ba4;padding:4px 0;display:flex;align-items:center;gap:6px}
.sys-icon{font-size:15px}

/* ══════════════════════════════════════════════
   PROFILE POPUP — matches Discord's profile card
   ══════════════════════════════════════════════ */
.overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.7);z-index:50;
  backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
}
.overlay.show{display:block;animation:fadein .15s ease}

.card{
  display:none;
  position:fixed;z-index:51;top:50%;left:50%;
  transform:translate(-50%,-50%) scale(.9);
  width:290px;
  background:#232428;
  border-radius:8px;overflow:hidden;
  box-shadow:0 16px 40px rgba(0,0,0,.9),0 4px 12px rgba(0,0,0,.5);
}
.card.show{
  display:block;
  animation:popin .2s cubic-bezier(.34,1.26,.64,1) forwards;
}
@keyframes fadein{from{opacity:0}to{opacity:1}}
@keyframes popin{
  from{opacity:0;transform:translate(-50%,-50%) scale(.88)}
  to  {opacity:1;transform:translate(-50%,-50%) scale(1)}
}

/* banner */
.card-banner{height:60px;background:#111214}

/* avatar area sits between banner and body */
.card-av-area{
  padding:0 12px;
  margin-top:-40px;
  height:40px;          /* half of avatar sticks into banner */
  display:flex;
  align-items:flex-start;
  position:relative;
}
.card-av-wrap{position:relative;width:80px;height:80px;flex-shrink:0}
.card-av{
  width:80px;height:80px;border-radius:50%;object-fit:cover;
  border:6px solid #232428;display:block;
}
/* status dot — bottom-right of avatar, same as Discord */
.card-status{
  position:absolute;bottom:5px;right:5px;
  width:16px;height:16px;border-radius:50%;
  border:3px solid #232428;
}

/* body */
.card-body{padding:12px 16px 16px}
.card-name{
  font-size:20px;font-weight:800;color:#f2f3f5;
  line-height:1.2;letter-spacing:-.01em;margin-bottom:2px;
}
.card-tag{font-size:13px;color:#949ba4}
.card-divider{height:1px;background:#3b3d44;margin:12px 0}
.card-label{
  font-size:11px;font-weight:700;color:#949ba4;
  text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;
}
.card-val{font-size:14px;color:#dbdee1}

/* roles grid — 2 columns, matching Discord */
.card-roles{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:6px;
  max-height:140px;
  overflow-y:auto;
  scrollbar-width:thin;scrollbar-color:#3b3d44 transparent;
}
.card-roles::-webkit-scrollbar{width:4px}
.card-roles::-webkit-scrollbar-track{background:transparent}
.card-roles::-webkit-scrollbar-thumb{background:#3b3d44;border-radius:2px}

.role-pill{
  display:flex;align-items:center;gap:5px;
  border:1px solid;border-radius:4px;
  padding:4px 7px;font-size:12px;color:#dbdee1;
  overflow:hidden;min-width:0;
}
.role-dot{
  width:12px;height:12px;border-radius:50%;flex-shrink:0;
}
.role-icon{
  width:16px;height:16px;border-radius:50%;
  object-fit:cover;flex-shrink:0;
}
.role-name{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:12px;font-weight:500;
}
"""
