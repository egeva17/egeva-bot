#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Egeva Bot Cloud Edition (Render / 24-7)
- TempVoice: Otomatik kişisel ses kanalı oluşturma, kullanıcıyı taşıma ve oda boşaldığında silme.
- Discord Log: Silinen ve düzenlenen mesajların loglanması, fotoğrafların kalıcı arşivlenmesi.
- Gelen / Giden: #gelen-giden kanalında davet takibi, hesap açılış ve katılma zamanları.
- Çevrimdışı Tarayıcı (Catchup): Başlangıçta kaçan üyeleri tespit eder.
- Web Sunucu: Render'ın 7/24 uyanık kalması için HTTP portunu dinler ve kendini uyanık tutar.
"""

import asyncio
from collections import OrderedDict
import datetime
import json
import logging
import os
import re
import sys
import time
import httpx
import websockets
from aiohttp import web

# --- Yapılandırma ---
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
GUILD_ID = '1402730772112216094'
LOG_CHANNEL_ID = '1569474522095296685'           # 📋・log
GELEN_GIDEN_CHANNEL_ID = '1402736641214382262'   # 🤪・gelen-giden
TRIGGER_CHANNEL_ID = '1403757221136957490'       # ➕・Kendi Ses Kanalını Oluştur
CATEGORY_ID = '1403757883866484768'              # 🎙️・KİŞİSEL SES KANALLARI
PORT = int(os.environ.get('PORT', 10000))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cached_media')
os.makedirs(CACHE_DIR, exist_ok=True)

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=[
    logging.StreamHandler(sys.stdout)
])

FULL_INTENTS = 1 | 2 | 128 | 512 | 32768
FALLBACK_INTENTS = 1 | 128 | 512 | 32768

cached_invites = {}
cached_vanity = {'uses': 0}
MAX_CACHE_SIZE = 30000
message_cache = OrderedDict()
active_temp_channels = set()
voice_states = {}

def get_avatar_url(user_or_author):
    aid = user_or_author.get('id')
    avatar = user_or_author.get('avatar')
    if aid and avatar:
        ext = 'gif' if str(avatar).startswith('a_') else 'png'
        return f"https://cdn.discordapp.com/avatars/{aid}/{avatar}.{ext}?size=256"
    return "https://cdn.discordapp.com/embed/avatars/0.png"

def get_account_created_timestamp(snowflake_id):
    try:
        ms = (int(snowflake_id) >> 22) + 1420070400000
        return int(ms // 1000)
    except Exception:
        return int(time.time())

def cache_message(msg):
    mid = msg.get('id')
    cid = msg.get('channel_id')
    gid = msg.get('guild_id', GUILD_ID)
    if cid == LOG_CHANNEL_ID:
        return

    author = msg.get('author', {})
    raw_ts = msg.get('timestamp')
    created_ts = int(time.time())
    if raw_ts:
        try:
            dt = datetime.datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
            created_ts = int(dt.timestamp())
        except Exception:
            pass

    attachments = []
    for att in msg.get('attachments', []):
        attachments.append({
            'id': att.get('id'),
            'filename': att.get('filename'),
            'url': att.get('url'),
            'proxy_url': att.get('proxy_url'),
            'content_type': att.get('content_type', ''),
            'size': att.get('size', 0)
        })

    if len(message_cache) >= MAX_CACHE_SIZE:
        old_id, _ = message_cache.popitem(last=False)
        clean_cached_media_for_msg(old_id)

    message_cache[mid] = {
        'id': mid,
        'channel_id': cid,
        'guild_id': gid,
        'author': {
            'id': author.get('id'),
            'username': author.get('username', 'Bilinmeyen'),
            'global_name': author.get('global_name'),
            'avatar': author.get('avatar'),
            'bot': author.get('bot', False)
        },
        'content': msg.get('content', ''),
        'attachments': attachments,
        'timestamp': created_ts
    }

def clean_cached_media_for_msg(mid):
    try:
        for fname in os.listdir(CACHE_DIR):
            if fname.startswith(f"{mid}_"):
                try:
                    os.remove(os.path.join(CACHE_DIR, fname))
                except Exception:
                    pass
    except Exception:
        pass

async def precache_attachments(mid, attachments, http_client: httpx.AsyncClient):
    for att in attachments:
        ctype = att.get('content_type', '')
        fname = att.get('filename', '')
        size = att.get('size', 0)
        if size > 15 * 1024 * 1024:
            continue
        if not (ctype.startswith('image/') or fname.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp'))):
            continue
        url = att.get('url') or att.get('proxy_url')
        if not url:
            continue
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', fname)
        file_path = os.path.join(CACHE_DIR, f"{mid}_{safe_name}")
        try:
            resp = await http_client.get(url, timeout=10.0)
            if resp.status_code == 200:
                with open(file_path, 'wb') as f:
                    f.write(resp.content)
        except Exception:
            pass

async def refresh_invites(http_client: httpx.AsyncClient):
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    try:
        inv_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/invites", headers=headers)
        if inv_resp.status_code == 200:
            for inv in inv_resp.json():
                cached_invites[inv.get('code')] = {
                    'uses': inv.get('uses', 0),
                    'inviter': inv.get('inviter', {})
                }
        vanity_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/vanity-url", headers=headers)
        if vanity_resp.status_code == 200:
            v_data = vanity_resp.json()
            cached_vanity['uses'] = v_data.get('uses', 0)
            cached_vanity['code'] = v_data.get('code', 'egeva')
    except Exception as e:
        logging.error(f"Invite error: {e}")

async def warm_cache(http_client: httpx.AsyncClient):
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    try:
        resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/channels", headers=headers)
        if resp.status_code != 200:
            return
        channels = resp.json()
        text_channels = [c for c in channels if c.get('type') == 0 and c.get('id') != LOG_CHANNEL_ID]
        for ch in text_channels:
            cid = ch['id']
            try:
                m_resp = await http_client.get(f"https://discord.com/api/v10/channels/{cid}/messages?limit=50", headers=headers)
                if m_resp.status_code == 200:
                    for m in m_resp.json():
                        cache_message(m)
            except Exception:
                pass
            await asyncio.sleep(0.08)
    except Exception as e:
        logging.error(f"Cache warming error: {e}")

async def cleanup_orphaned_voice_channels(http_client: httpx.AsyncClient):
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    try:
        resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/channels", headers=headers)
        if resp.status_code != 200:
            return
        for ch in resp.json():
            if ch.get('parent_id') == CATEGORY_ID and ch.get('id') != TRIGGER_CHANNEL_ID:
                cid = ch['id']
                occupied = sum(1 for uid, vcid in voice_states.items() if vcid == cid)
                if occupied == 0:
                    await http_client.delete(f"https://discord.com/api/v10/channels/{cid}", headers=headers)
    except Exception as e:
        logging.error(f"Voice cleanup error: {e}")

async def create_temp_voice(user_id, member_data, http_client: httpx.AsyncClient):
    user = member_data.get('user', {})
    display_name = member_data.get('nick') or user.get('global_name') or user.get('username') or 'Kullanıcı'
    channel_name = f"🔊・{display_name}'in Odası"
    payload = {
        "name": channel_name,
        "type": 2,
        "parent_id": CATEGORY_ID,
        "user_limit": 4,
        "permission_overwrites": [
            {"id": GUILD_ID, "type": 0, "allow": "1048576", "deny": "0"},
            {"id": user_id, "type": 1, "allow": "30408720", "deny": "0"}
        ]
    }
    headers = {'Authorization': f'Bot {BOT_TOKEN}', 'Content-Type': 'application/json'}
    resp = await http_client.post(f"https://discord.com/api/v10/guilds/{GUILD_ID}/channels", headers=headers, json=payload)
    if resp.status_code in (200, 201):
        new_cid = resp.json()['id']
        active_temp_channels.add(new_cid)
        await http_client.patch(
            f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}",
            headers=headers,
            json={"channel_id": new_cid}
        )
        return new_cid
    return None

async def check_and_delete_empty_channel(channel_id, http_client: httpx.AsyncClient):
    if channel_id == TRIGGER_CHANNEL_ID:
        return
    await asyncio.sleep(2)
    count = sum(1 for uid, cid in voice_states.items() if cid == channel_id)
    if count > 0:
        return
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    try:
        ch_resp = await http_client.get(f"https://discord.com/api/v10/channels/{channel_id}", headers=headers)
        if ch_resp.status_code == 200 and ch_resp.json().get('parent_id') == CATEGORY_ID:
            await http_client.delete(f"https://discord.com/api/v10/channels/{channel_id}", headers=headers)
            active_temp_channels.discard(channel_id)
    except Exception:
        pass

async def handle_member_join(data, http_client: httpx.AsyncClient):
    user = data.get('user', {})
    uid = user.get('id', '0')
    username = user.get('username', 'Bilinmeyen')
    display_name = data.get('nick') or user.get('global_name') or username
    avatar_url = get_avatar_url(user)
    
    joined_raw = data.get('joined_at')
    joined_ts = int(time.time())
    if joined_raw:
        try:
            dt = datetime.datetime.fromisoformat(joined_raw.replace('Z', '+00:00'))
            joined_ts = int(dt.timestamp())
        except Exception:
            pass
            
    created_ts = get_account_created_timestamp(uid)
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    used_invite_info = "🌟 **Özel Sunucu Bağlantısı** (`discord.gg/egeva`)"
    
    try:
        new_inv_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/invites", headers=headers)
        new_vanity_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/vanity-url", headers=headers)
        found = False
        if new_inv_resp.status_code == 200:
            for inv in new_inv_resp.json():
                code = inv.get('code')
                cur_uses = inv.get('uses', 0)
                old_uses = cached_invites.get(code, {}).get('uses', 0)
                if cur_uses > old_uses:
                    inviter = inv.get('inviter', {})
                    inviter_name = inviter.get('global_name') or inviter.get('username') or 'Kullanıcı'
                    used_invite_info = f"📨 <@{inviter.get('id')}> (`{inviter_name}`)\n└ Davet Kodu: `{code}` • Toplam Davet: **{cur_uses}**"
                    found = True
                    cached_invites[code] = {'uses': cur_uses, 'inviter': inviter}
                    break
                else:
                    cached_invites[code] = {'uses': cur_uses, 'inviter': inv.get('inviter', {})}

        if not found and new_vanity_resp.status_code == 200:
            v_data = new_vanity_resp.json()
            cur_vanity_uses = v_data.get('uses', 0)
            used_invite_info = f"🌟 **Özel Sunucu Bağlantısı** (`discord.gg/{v_data.get('code', 'egeva')}`)\n└ Toplam Kullanım: **{cur_vanity_uses}**"
            cached_vanity['uses'] = cur_vanity_uses
    except Exception as e:
        logging.error(f"Invite check error: {e}")

    member_count = "?"
    try:
        g_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}?with_counts=true", headers=headers)
        if g_resp.status_code == 200:
            member_count = g_resp.json().get('approximate_member_count', '?')
    except Exception:
        pass

    embed = {
        'title': '👋 Aramıza Hoş Geldin!',
        'color': 0x57F287,
        'thumbnail': {'url': avatar_url},
        'description': f"Aramıza hoş geldin <@{uid}>! Seninle birlikte **{member_count}** kişi olduk! 🎉",
        'fields': [
            {'name': '👤 Kullanıcı:', 'value': f"<@{uid}> (`{display_name}`)", 'inline': True},
            {'name': '🆔 Kullanıcı ID:', 'value': f"`{uid}`", 'inline': True},
            {'name': '🕒 Katılma Zamanı:', 'value': f"<t:{joined_ts}:f>\n(<t:{joined_ts}:R>)", 'inline': True},
            {'name': '📅 Hesap Oluşturulma:', 'value': f"<t:{created_ts}:f>\n(<t:{created_ts}:R>)", 'inline': True},
            {'name': '🔗 Davet Eden & Bağlantı:', 'value': used_invite_info, 'inline': False}
        ],
        'footer': {'text': f"Egeva Topluluğu • Üye #{member_count}"},
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    await http_client.post(f"https://discord.com/api/v10/channels/{GELEN_GIDEN_CHANNEL_ID}/messages", headers=headers, json={'embeds': [embed]})

async def handle_member_leave(data, http_client: httpx.AsyncClient):
    user = data.get('user', {})
    uid = user.get('id', '0')
    display_name = user.get('global_name') or user.get('username', 'Bilinmeyen')
    avatar_url = get_avatar_url(user)
    now_ts = int(time.time())
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    member_count = "?"
    try:
        g_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}?with_counts=true", headers=headers)
        if g_resp.status_code == 200:
            member_count = g_resp.json().get('approximate_member_count', '?')
    except Exception:
        pass

    embed = {
        'title': '🚪 Bir Üye Sunucudan Ayrıldı',
        'color': 0xED4245,
        'thumbnail': {'url': avatar_url},
        'description': f"<@{uid}> (`{display_name}`) sunucudan ayrıldı. Kalan üye sayısı: **{member_count}**",
        'fields': [
            {'name': '👤 Kullanıcı:', 'value': f"<@{uid}> (`{display_name}`)", 'inline': True},
            {'name': '🆔 Kullanıcı ID:', 'value': f"`{uid}`", 'inline': True},
            {'name': '🕒 Ayrılma Saati:', 'value': f"<t:{now_ts}:f> (<t:{now_ts}:R>)", 'inline': False}
        ],
        'footer': {'text': f"Egeva Topluluğu • Kalan Üye: {member_count}"},
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    await http_client.post(f"https://discord.com/api/v10/channels/{GELEN_GIDEN_CHANNEL_ID}/messages", headers=headers, json={'embeds': [embed]})

async def catchup_missed_joins(http_client: httpx.AsyncClient):
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    try:
        msgs_resp = await http_client.get(f"https://discord.com/api/v10/channels/{GELEN_GIDEN_CHANNEL_ID}/messages?limit=35", headers=headers)
        welcomed_ids = set()
        if msgs_resp.status_code == 200:
            for m in msgs_resp.json():
                for e in m.get('embeds', []):
                    for f in e.get('fields', []):
                        if 'ID' in f.get('name', ''):
                            welcomed_ids.add(f.get('value', '').replace('`', '').strip())
                    for match in re.findall(r'<@(\d+)>', e.get('description', '')):
                        welcomed_ids.add(match)

        audit_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/audit-logs?action_type=25&limit=15", headers=headers)
        if audit_resp.status_code != 200:
            return

        missed_users = []
        for e in audit_resp.json().get('audit_log_entries', []):
            if e.get('reason') == 'AutoRole':
                target_id = e.get('target_id')
                if target_id and target_id not in welcomed_ids and target_id not in missed_users:
                    missed_users.append(target_id)

        for uid in reversed(missed_users):
            m_resp = await http_client.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{uid}", headers=headers)
            if m_resp.status_code == 200:
                await handle_member_join(m_resp.json(), http_client)
                welcomed_ids.add(uid)
                await asyncio.sleep(1.0)
    except Exception as ex:
        logging.error(f"Catchup scanner error: {ex}")

async def handle_message_delete(data, http_client: httpx.AsyncClient):
    mid = data.get('id')
    cid = data.get('channel_id')
    if cid == LOG_CHANNEL_ID:
        return

    cached = message_cache.pop(mid, None)
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if cached:
        author = cached['author']
        author_name = author.get('global_name') or author.get('username') or 'Kullanıcı'
        content = cached.get('content', '')
        sent_ts = cached.get('timestamp', int(time.time()))
        attachments = cached.get('attachments', [])

        embed = {
            'title': '🗑️ Bir Mesaj Silindi',
            'color': 0xED4245,
            'author': {'name': f"{author_name} (@{author.get('username')})", 'icon_url': get_avatar_url(author)},
            'description': f"👤 **Kullanıcı:** <@{author.get('id')}>\n📍 **Kanal:** <#{cid}>\n🕒 **Gönderilme:** <t:{sent_ts}:R> (<t:{sent_ts}:f>)",
            'fields': [{'name': '💬 Silinen Mesaj İçeriği:', 'value': f">>> {content[:1015]}" if content else "*Metin yok*", 'inline': False}],
            'footer': {'text': f"Mesaj ID: {mid} • Egeva Log Sistemi"},
            'timestamp': now_iso
        }

        found_files = []
        if attachments:
            att_lines = []
            for idx, att in enumerate(attachments):
                fname = att.get('filename', f'file_{idx}')
                att_lines.append(f"• `{fname}` ({round(att.get('size', 0)/1024, 1)} KB)")
                safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', fname)
                fpath = os.path.join(CACHE_DIR, f"{mid}_{safe_name}")
                file_bytes = None
                if os.path.exists(fpath):
                    with open(fpath, 'rb') as f:
                        file_bytes = f.read()
                elif att.get('url'):
                    try:
                        r = await http_client.get(att.get('url'), timeout=5.0)
                        if r.status_code == 200:
                            file_bytes = r.content
                    except Exception:
                        pass
                if file_bytes and len(found_files) < 4:
                    found_files.append((f"silinen_{safe_name}", file_bytes, att.get('content_type', 'image/png') or 'image/png'))

            embed['fields'].append({'name': f"📎 Silinen Ek Dosyalar ({len(attachments)}):", 'value': '\n'.join(att_lines), 'inline': False})

        if found_files:
            main_fn, _, _ = found_files[0]
            embed['image'] = {'url': f"attachment://{main_fn}"}
            payload_att = [{'id': i, 'filename': fn, 'description': 'Silinen Medya'} for i, (fn, _, _) in enumerate(found_files)]
            files_dict = {f'files[{i}]': (fn, fb, fm) for i, (fn, fb, fm) in enumerate(found_files)}
            await http_client.post(
                f"https://discord.com/api/v10/channels/{LOG_CHANNEL_ID}/messages",
                headers=headers,
                data={'payload_json': json.dumps({'embeds': [embed], 'attachments': payload_att})},
                files=files_dict
            )
        else:
            await http_client.post(f"https://discord.com/api/v10/channels/{LOG_CHANNEL_ID}/messages", headers=headers, json={'embeds': [embed]})
        clean_cached_media_for_msg(mid)

async def handle_message_update(data, http_client: httpx.AsyncClient):
    mid = data.get('id')
    cid = data.get('channel_id')
    if cid == LOG_CHANNEL_ID or data.get('content') is None:
        return
    cached = message_cache.get(mid)
    if not cached or cached.get('content') == data.get('content'):
        if not cached:
            cache_message(data)
        return

    author = cached.get('author', {})
    author_name = author.get('global_name') or author.get('username') or 'Kullanıcı'
    embed = {
        'title': '✏️ Bir Mesaj Düzenlendi',
        'color': 0xFEE75C,
        'author': {'name': f"{author_name} (@{author.get('username')})", 'icon_url': get_avatar_url(author)},
        'description': f"👤 **Kullanıcı:** <@{author.get('id')}>\n📍 **Kanal:** <#{cid}> • [Mesaja Git ➔](https://discord.com/channels/{GUILD_ID}/{cid}/{mid})",
        'fields': [
            {'name': '🔴 Eski Hali:', 'value': f">>> {cached.get('content', '')[:1015]}" or "*Boş*", 'inline': False},
            {'name': '🟢 Yeni Hali:', 'value': f">>> {data.get('content', '')[:1015]}" or "*Boş*", 'inline': False}
        ],
        'footer': {'text': f"Mesaj ID: {mid} • Egeva Log Sistemi"},
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    await http_client.post(f"https://discord.com/api/v10/channels/{LOG_CHANNEL_ID}/messages", headers=headers, json={'embeds': [embed]})
    cached['content'] = data.get('content')

# --- GTA 6 Geri Sayım Döngüsü (Her 5 Dakikada Bir) ---

async def gta6_countdown_loop(http_client: httpx.AsyncClient):
    """Her 5 dakikada bir GTA 6 sayacını kontrol edip emirfurkannn'ın nickini günceller."""
    logging.info("Starting GTA 6 countdown loop...")
    headers = {'Authorization': f'Bot {BOT_TOKEN}'}
    target_ts = 1795035600 # 19 Kasım 2026 00:00:00 UTC+3
    user_id = '852605079466803232'
    panel_msg_id = '1570118813066862687'

    await asyncio.sleep(5) # Başlangıçta 5 saniye bekle

    while True:
        try:
            state_enabled = True
            try:
                r = await http_client.get(f"https://egeva.tr/api/gta6_countdown.json?t={int(time.time())}", timeout=5.0)
                if r.status_code == 200:
                    data = r.json()
                    state_enabled = data.get('enabled', True)
                    user_id = data.get('user_id', user_id)
                    panel_msg_id = data.get('panel_message_id', panel_msg_id)
            except Exception as e:
                logging.debug(f"GTA6 state fetch error: {e}")

            if not state_enabled:
                logging.info("GTA 6 countdown is currently paused/disabled.")
            else:
                now = time.time()
                diff = target_ts - now
                days = max(0, int(diff // 86400))
                hours = max(0, int((diff % 86400) // 3600))
                minutes = max(0, int((diff % 3600) // 60))

                if diff <= 0:
                    new_nick = "GTA 6 Çıktı!"
                    rem_text = "Süre Doldu!"
                else:
                    new_nick = f"GTA 6'ya {days}g {hours}sa {minutes}dk kaldı"
                    rem_text = f"{days} Gün {hours} Saat {minutes} Dakika"

                # Nick güncelle
                patch_resp = await http_client.patch(
                    f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}",
                    headers=headers,
                    json={'nick': new_nick}
                )
                if patch_resp.status_code in (200, 204):
                    logging.info(f"GTA 6 Nick auto-updated to: {new_nick}")

                # Log kanalındaki paneli de güncelle
                panel_payload = {
                    'embeds': [{
                        'title': '⏱️ GTA 6 Sayaç Kontrol Paneli',
                        'color': 0x57F287,
                        'description': (
                            f"👤 **Hedef Kullanıcı:** <@{user_id}> (`emirfurkannn`)\n"
                            "🎯 **Hedef:** 19 Kasım 2026 00:00 (GTA 6)\n"
                            "📊 **Durum:** 🟢 **AKTİF** (Her 5 dakikada bir otomatik güncelleniyor)\n"
                            f"🏷️ **Güncel Nick:** `{new_nick}`\n"
                            f"⏳ **Kalan Süre:** `{rem_text}`\n\n"
                            "*(Aşağıdaki butonları kullanarak otomatik sayacı istediğiniz an durdurabilir veya yeniden başlatabilirsiniz.)*"
                        ),
                        'footer': {'text': 'Egeva Bot • GTA 6 Otomatik Nick Sayacı'},
                        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }],
                    'components': [{
                        'type': 1,
                        'components': [
                            {'type': 2, 'style': 4, 'label': '🔴 Sayacı Durdur', 'custom_id': 'gta6_stop'},
                            {'type': 2, 'style': 3, 'label': '🟢 Sayacı Başlat', 'custom_id': 'gta6_start'},
                            {'type': 2, 'style': 2, 'label': '🔄 Şimdi Güncelle', 'custom_id': 'gta6_sync'}
                        ]
                    }]
                }
                await http_client.patch(
                    f"https://discord.com/api/v10/channels/{LOG_CHANNEL_ID}/messages/{panel_msg_id}",
                    headers=headers,
                    json=panel_payload
                )

        except Exception as e:
            logging.error(f"GTA 6 loop error: {e}")

        await asyncio.sleep(300) # Her 5 dakika (300 saniye)

# --- Render Uyanık Tutucu Web Sunucusu ---

async def handle_health_check(request):
    return web.Response(text="Egeva Bot is running 24/7 on Render! Status: OK")

async def keep_awake_pinger():
    """Render free tier'ın uykuya dalmasını engellemek için her 9 dakikada bir HTTP isteği atar."""
    await asyncio.sleep(60) # Başlangıçta 1 dk bekle
    while True:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get("https://egeva-bot.onrender.com/health")
                logging.info(f"⏰ Keep-Alive Ping gönderildi: {r.status_code} (Render aktif tutuluyor)")
        except Exception as e:
            logging.debug(f"Keep-alive ping error: {e}")
        await asyncio.sleep(540) # Her 9 dakikada bir (15 dk dolmadan)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    app.router.add_get('/health', handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"🌐 Health Check Web Server running on port {PORT} for Render!")
    asyncio.create_task(keep_awake_pinger())

# --- Ana Gateway Döngüsü ---

async def gateway_loop():
    uri = "wss://gateway.discord.gg/?v=10&encoding=json"
    
    # 1. Web sunucusunu başlat (Render sağlık kontrolü için)
    await start_web_server()

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        await warm_cache(http_client)
        await refresh_invites(http_client)
        await cleanup_orphaned_voice_channels(http_client)
        await catchup_missed_joins(http_client)

        # GTA 6 5 dakikalık geri sayım döngüsü
        asyncio.create_task(gta6_countdown_loop(http_client))

        current_intents = FULL_INTENTS
        backoff = 2

        while True:
            try:
                logging.info(f"Connecting to Discord Gateway WebSocket (Intents: {current_intents})...")
                async with websockets.connect(uri, max_size=25_000_000, ping_interval=20, ping_timeout=20) as ws:
                    hello = await ws.recv()
                    hello_data = json.loads(hello)
                    heartbeat_interval = hello_data['d']['heartbeat_interval'] / 1000.0

                    identify = {
                        "op": 2,
                        "d": {
                            "token": BOT_TOKEN,
                            "intents": current_intents,
                            "properties": {"os": "linux", "browser": "egeva_cloud", "device": "egeva_cloud"}
                        }
                    }
                    await ws.send(json.dumps(identify))
                    backoff = 2

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(heartbeat_interval)
                            await ws.send(json.dumps({"op": 1, "d": None}))

                    hb_task = asyncio.create_task(heartbeat())
                    try:
                        async for raw_msg in ws:
                            event = json.loads(raw_msg)
                            op, t, d = event.get('op'), event.get('t'), event.get('d')
                            if op == 0:
                                if t == 'READY':
                                    logging.info(f"✅ Gateway READY as {d.get('user', {}).get('username')} (ID: {d.get('user', {}).get('id')})")
                                elif t == 'GUILD_CREATE':
                                    for vs in d.get('voice_states', []):
                                        voice_states[vs['user_id']] = vs['channel_id']
                                    for ch in d.get('channels', []):
                                        if ch.get('parent_id') == CATEGORY_ID and ch['id'] != TRIGGER_CHANNEL_ID:
                                            active_temp_channels.add(ch['id'])
                                elif t == 'GUILD_MEMBER_ADD':
                                    asyncio.create_task(handle_member_join(d, http_client))
                                elif t == 'GUILD_MEMBER_REMOVE':
                                    asyncio.create_task(handle_member_leave(d, http_client))
                                elif t in ('INVITE_CREATE', 'INVITE_DELETE'):
                                    asyncio.create_task(refresh_invites(http_client))
                                elif t == 'VOICE_STATE_UPDATE':
                                    uid, new_cid = d.get('user_id'), d.get('channel_id')
                                    old_cid = voice_states.get(uid)
                                    voice_states[uid] = new_cid
                                    if new_cid == TRIGGER_CHANNEL_ID:
                                        asyncio.create_task(create_temp_voice(uid, d.get('member', {}), http_client))
                                    if old_cid and old_cid != new_cid:
                                        rem = sum(1 for u, c in voice_states.items() if c == old_cid)
                                        if rem == 0 and (old_cid in active_temp_channels or old_cid != TRIGGER_CHANNEL_ID):
                                            asyncio.create_task(check_and_delete_empty_channel(old_cid, http_client))
                                elif t == 'MESSAGE_CREATE':
                                    cache_message(d)
                                    if d.get('attachments'):
                                        asyncio.create_task(precache_attachments(d['id'], d['attachments'], http_client))
                                elif t == 'MESSAGE_UPDATE':
                                    asyncio.create_task(handle_message_update(d, http_client))
                                elif t == 'MESSAGE_DELETE':
                                    asyncio.create_task(handle_message_delete(d, http_client))
                    finally:
                        hb_task.cancel()

            except websockets.exceptions.ConnectionClosedError as cce:
                if cce.rcvd and cce.rcvd.code == 4014:
                    if current_intents == FULL_INTENTS:
                        current_intents = FALLBACK_INTENTS
                        await asyncio.sleep(1)
                        continue
                logging.error(f"Gateway closed [{cce}]. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30)
            except Exception as e:
                logging.error(f"Gateway error: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30)

if __name__ == '__main__':
    logging.info("Starting Egeva Bot Cloud Edition...")
    try:
        asyncio.run(gateway_loop())
    except KeyboardInterrupt:
        pass
