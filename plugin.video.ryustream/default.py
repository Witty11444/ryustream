import sys
import os
import json
import time
import sqlite3
import hashlib
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
PROFILE_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
MEDIA_PATH = os.path.join(ADDON_PATH, 'resources', 'media')
if not xbmcvfs.exists(PROFILE_PATH):
    xbmcvfs.mkdirs(PROFILE_PATH)

DB_PATH = os.path.join(PROFILE_PATH, 'ryustream_cache.db')
DEFAULT_TMDB_KEY = "4a4c2835f4cb50a1fdb8cf2d5b16daa7"
RD_CLIENT_ID = "X245A4XAIBGVM"
PM_CLIENT_ID = "888228107"
TRAKT_CLIENT_ID = "N2uItt-lTlCfWS_6U2j7JlH64nFMzP4AN_zvB1i7bX4"
SIMKL_CLIENT_ID = "39e8640b6f1a60aaf60f3f3313475e830517badab8048a4e52ff2d10deb2b9b0"

SESSION = requests.Session()
_retries = Retry(total=3, connect=3, read=3, status=3, status_forcelist=(429, 500, 502, 503, 504), backoff_factor=0.3)
SESSION.mount('https://', HTTPAdapter(pool_maxsize=100, max_retries=_retries))

def get_tmdb_key():
    user_key = ADDON.getSetting('tmdb_apikey').strip()
    return user_key if user_key else DEFAULT_TMDB_KEY

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT, expires INTEGER)')
    conn.commit()
    conn.close()

def get_cache(key):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT data, expires FROM cache WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    if row and row[1] > int(time.time()):
        return json.loads(row[0])
    return None

def set_cache(key, data, ttl=86400):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('REPLACE INTO cache (key, data, expires) VALUES (?, ?, ?)', (key, json.dumps(data), int(time.time()) + ttl))
    conn.commit()
    conn.close()

def fetch_tmdb(endpoint, extra_params=None):
    tmdb_key = get_tmdb_key()
    if not tmdb_key:
        return list()
    raw_key = f"tmdb_v300_{endpoint}_{json.dumps(extra_params or dict())}"
    cache_key = hashlib.md5(raw_key.encode()).hexdigest()
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    params = {'api_key': tmdb_key, 'language': 'en-US'}
    if extra_params:
        params.update(extra_params)

    try:
        url = "https://" + "api." + "themoviedb.org/3/" + endpoint
        res = SESSION.get(url, params=params, timeout=10)
        if res.ok:
            results = res.json().get('results', list())
            if results:
                set_cache(cache_key, results)
            return results
        return list()
    except Exception:
        return list()

def get_imdb_id(tmdb_id, media_type='movie'):
    tmdb_key = get_tmdb_key()
    url = f"https://" + f"api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids"
    try:
        res = SESSION.get(url, params={'api_key': tmdb_key}, timeout=8).json()
        return res.get('imdb_id', '')
    except Exception:
        return ''
def resolve_debrid_magnet(magnet_uri):
    ad_key = ADDON.getSetting('ad_apikey').strip()
    rd_key = ADDON.getSetting('rd_apikey').strip()
    pm_key = ADDON.getSetting('pm_apikey').strip()
    tb_key = ADDON.getSetting('tb_apikey').strip()

    # 1. AllDebrid Resolver (URL เต็มครบถ้วน)
    if ad_key:
        try:
            u1 = "https://" + "api." + "alldebrid.com" + "/v4/magnet/upload"
            r1 = SESSION.get(u1, params={'agent': 'RyuStream', 'apikey': ad_key, 'magnets[]': magnet_uri}, timeout=12).json()
            mags = r1.get('data', {}).get('magnets', list())
            if mags:
                mag_id = mags[0].get('id')
                u2 = "https://" + "api." + "alldebrid.com" + "/v4/magnet/status"
                r2 = SESSION.get(u2, params={'agent': 'RyuStream', 'apikey': ad_key, 'id': mag_id}, timeout=12).json()
                links = r2.get('data', {}).get('magnets', {}).get('links', list())
                if links:
                    best_link = max(links, key=lambda x: x.get('size', 0)).get('link')
                    u3 = "https://" + "api." + "alldebrid.com" + "/v4/link/unlock"
                    r3 = SESSION.get(u3, params={'agent': 'RyuStream', 'apikey': ad_key, 'link': best_link}, timeout=12).json()
                    stream_url = r3.get('data', {}).get('link')
                    if stream_url:
                        return stream_url
        except Exception as e:
            xbmcgui.Dialog().notification('AllDebrid Error', str(e)[:60], xbmcgui.NOTIFICATION_ERROR, 4000)

    # 2. Real-Debrid Resolver (แก้ไข Path เต็ม + เลือกไฟล์วิดีโอใหญ่สุดอัตโนมัติ)
    if rd_key:
        try:
            headers = {'Authorization': f'Bearer {rd_key}'}
            rd_base = "https://" + "api." + "real-debrid.com" + "/rest/1.0"
            r1 = SESSION.post(f"{rd_base}/torrents/addMagnet", data={'magnet': magnet_uri}, headers=headers, timeout=12).json()
            tid = r1.get('id')
            if not tid:
                err_msg = r1.get('error', 'Invalid Token or Magnet')
                xbmcgui.Dialog().notification('Real-Debrid', str(err_msg) + ' - Trying next Debrid...', xbmcgui.NOTIFICATION_WARNING, 3000)

            # ดึงรายชื่อไฟล์ในทอร์เรนต์เพื่อเลือกเฉพาะไฟล์วิดีโอขนาดใหญ่สุด (ป้องกันไฟล์ซับ/ตัวอย่างติดมา)
            info = SESSION.get(f"{rd_base}/torrents/info/{tid}", headers=headers, timeout=12).json()
            files = info.get('files', list())
            vid_files = [f for f in files if f.get('path', '').lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.ts'))]
            if vid_files:
                best_file = max(vid_files, key=lambda x: x.get('bytes', 0))
                file_ids = str(best_file['id'])
            else:
                file_ids = 'all'

            SESSION.post(f"{rd_base}/torrents/selectFiles/{tid}", data={'files': file_ids}, headers=headers, timeout=12)

            # รอตรวจสอบสถานะลิงก์พร้อมเล่น (สูงสุด 5 วินาทีสำหรับไฟล์ Cached)
            for _ in range(5):
                xbmc.sleep(1000)
                info = SESSION.get(f"{rd_base}/torrents/info/{tid}", headers=headers, timeout=12).json()
                links = info.get('links', list())
                if links:
                    unres = SESSION.post(f"{rd_base}/unrestrict/link", data={'link': links[0]}, headers=headers, timeout=12).json()
                    if unres.get('download'):
                        return unres['download']
                if info.get('status') in ('downloading', 'queued', 'magnet_conversion'):
                    xbmcgui.Dialog().notification('Real-Debrid', 'Not cached yet - Trying next Debrid...', xbmcgui.NOTIFICATION_WARNING, 3000)
                    break
        except Exception as e:
            xbmcgui.Dialog().notification('Real-Debrid Error', str(e)[:60], xbmcgui.NOTIFICATION_ERROR, 5000)

    # 3. Premiumize Resolver
    if pm_key:
        try:
            r1 = SESSION.post("https://" + "www.premiumize.me/api/transfer/directdl", data={'apikey': pm_key, 'src': magnet_uri}, timeout=15).json()
            content = r1.get('content', list())
            if content:
                best = max(content, key=lambda x: x.get('size', 0))
                if best.get('link'):
                    return best['link']
        except Exception:
            pass

    # 4. TorBox Resolver
    if tb_key:
        try:
            headers = {'Authorization': f'Bearer {tb_key}'}
            r1 = SESSION.post("https://" + "api.torbox.app/v1/api/torrents/createtorrent", data={'magnet': magnet_uri}, headers=headers, timeout=12).json()
            tid = r1.get('data', {}).get('torrent_id')
            if tid:
                xbmc.sleep(1000)
                r2 = SESSION.get("https://" + "api.torbox.app/v1/api/torrents/requestdl", params={'token': tb_key, 'torrent_id': tid, 'file_id': 0, 'zip_link': 'false'}, timeout=12).json()
                if r2.get('data'):
                    return r2['data']
        except Exception:
            pass

    return None

def search_internal_streams(imdb_id, media_type='movie', season=1, episode=1, quality_filter='All'):
    if not imdb_id:
        return list()
    strem_type = 'series' if media_type == 'tv' else 'movie'
    strem_id = f"{imdb_id}:{season}:{episode}" if media_type == 'tv' else imdb_id

    sources = list()
    scrapers = (
        ("Torrentio", "https://" + f"torrentio.strem.fun/stream/{strem_type}/{strem_id}.json"),
        ("MediaFusion", "https://" + f"elfhosted.com{strem_type}/{strem_id}.json")
    )

    for provider, url in scrapers:
        try:
            res = SESSION.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8).json()
            for s in res.get('streams', list()):
                title_raw = s.get('title') or s.get('name') or 'Unknown Stream'
                info_hash = s.get('infoHash', '')
                if not info_hash:
                    continue

                # กรองความละเอียดตามปุ่ม Quality Filter บนหน้าจอ
                low_t = title_raw.lower()
                if '1080p & 720p' in quality_filter and ('2160p' in low_t or '4k' in low_t):
                    continue
                elif '1080p Full HD' in quality_filter and '1080p' not in low_t:
                    continue
                elif '4K UHD' in quality_filter and ('2160p' not in low_t and '4k' not in low_t):
                    continue
                elif '720p HD' in quality_filter and '720p' not in low_t:
                    continue

                clean_line = title_raw.replace('\n', ' | ')[:95]
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(clean_line[:40])}"
                sources.append({
                    'label': f"[{provider}] {clean_line}",
                    'magnet': magnet
                })
        except Exception:
            continue

    return sources[:30]

class QRAuthDialog(xbmcgui.WindowDialog):
    def __init__(self, title, qr_target_url, display_url, pin_code):
        super().__init__()
        self.cancelled = False
        white_tex = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', 'media', 'white.png')
        qr_img_url = "https://" + "api." + "qrserver.com" + "/v1/create-qr-code/?size=320x320&data=" + urllib.parse.quote(qr_target_url)

        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, white_tex, colorDiffuse='CC000000'))
        self.addControl(xbmcgui.ControlImage(340, 80, 600, 560, white_tex, colorDiffuse='FF0F172A'))
        self.addControl(xbmcgui.ControlLabel(360, 100, 560, 40, f"[B]{title}[/B]", font='font14', textColor='FF10B981', alignment=2))
        self.addControl(xbmcgui.ControlLabel(360, 140, 560, 30, "Scan QR Code with your phone camera", font='font12', textColor='FFE2E8F0', alignment=2))
        self.addControl(xbmcgui.ControlImage(480, 180, 320, 320, qr_img_url))
        self.addControl(xbmcgui.ControlLabel(360, 515, 560, 30, f"URL: [COLOR cyan]{display_url}[/COLOR]", font='font12', textColor='FFFFFFFF', alignment=2))
        self.addControl(xbmcgui.ControlLabel(360, 545, 560, 35, f"CODE: [COLOR lime][B]{pin_code}[/B][/COLOR]", font='font14', textColor='FFFFFFFF', alignment=2))
        self.addControl(xbmcgui.ControlLabel(360, 595, 560, 30, "Press ESC or Back to Close", font='font12', textColor='FF94A3B8', alignment=2))

    def onAction(self, action):
        if action.getId() in (10, 13, 92, 110, 122) or action.getButtonCode() in (27, 61467, 61448):
            self.cancelled = True
            self.close()

class RyuHomeWindow(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loaded = False
        self.row1_mode = 'catalog'
        self.quality_mode = ADDON.getSetting('quality_filter') or 'All (4K / 1080p / 720p)'

    def onInit(self):
        xbmc.executebuiltin('Dialog.Close(busydialog)')
        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        self.update_quality_button()
        if not self.loaded:
            self.loaded = True
            self.load_default_rows()

    def update_quality_button(self):
        short_label = self.quality_mode.split(' ')[0][0]
        self.getControl(906).setLabel(f"Quality: {short_label}")

    def onAction(self, action):
        if action.getId() in (10, 13, 92, 110, 122) or action.getButtonCode() in (27, 61467, 61448):
            self.close()

    def populate_row(self, panel_id, label_id, endpoint, title, extra_params=None):
        if panel_id == 5001:
            self.row1_mode = 'catalog'
        self.getControl(label_id).setLabel(f"[B]{title}[/B]")
        items = fetch_tmdb(endpoint, extra_params)
        panel = self.getControl(panel_id)
        panel.reset()
        list_items = list()
        for item in items:
            m_title = item.get('title') or item.get('name') or 'Unknown'
            year = (item.get('release_date') or item.get('first_air_date') or '')[:4]
            rating = str(round(float(item.get('vote_average', 0)), 1))
            plot = item.get('overview') or 'No overview available.'
            p_path = item.get('poster_path')
            b_path = item.get('backdrop_path')
            poster = ("https://" + "image." + "tmdb.org/t/p/w342" + p_path) if p_path else ''
            fanart = ("https://" + "image." + "tmdb.org/t/p/w1280" + b_path) if b_path else ''
            m_type = item.get('media_type') or ('tv' if 'tv' in endpoint else 'movie')

            li = xbmcgui.ListItem(label=m_title)
            li.setArt({'poster': poster, 'thumb': '', 'icon': poster, 'fanart': fanart})
            li.setProperty('subtitle', f"TMDb {rating}   |   {year}")
            li.setProperty('tmdb_id', str(item.get('id', '')))
            li.setProperty('media_type', m_type)
            li.setInfo('video', {'title': m_title, 'year': int(year) if year.isdigit() else 0, 'rating': float(rating), 'plot': plot})
            list_items.append(li)

        if list_items:
            panel.addItems(list_items)

    def load_default_rows(self):
        self.populate_row(5001, 103, 'trending/all/week', 'TRENDING NOW')
        self.populate_row(5002, 104, 'movie/popular', 'POPULAR MOVIES')
        self.populate_row(5003, 105, 'discover/tv', 'POPULAR ANIME & SERIES', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'})
        self.setFocusId(5001)

    def load_authorize_cards(self):
        self.row1_mode = 'auth'
        self.getControl(103).setLabel("[B]DEBRID CLOUD, AUTHORIZE SERVICES & SUPPORT (CLICK TO SCAN QR)[/B]")
        panel = self.getControl(5001)
        panel.reset()
        services = (
            ('alldebrid', 'AllDebrid', 'alldebrid.png', 'ad_apikey', 'Scan QR Code with your phone to authorize AllDebrid instantly.'),
            ('realdebrid', 'Real-Debrid', 'realdebrid.png', 'rd_apikey', 'Scan QR Code with your phone to authorize Real-Debrid instantly.'),
            ('premiumize', 'Premiumize', 'premiumize.png', 'pm_apikey', 'Scan QR Code with your phone to authorize Premiumize.me cloud.'),
            ('torbox', 'TorBox', 'torbox.png', 'tb_apikey', 'Scan QR Code to open TorBox settings and paste your API Key.'),
            ('trakt', 'Trakt.tv', 'trakt.png', 'trakt_token', 'Scan QR Code to link your Trakt watchlist & history.'),
            ('simkl', 'Simkl.com', 'simkl.png', 'simkl_token', 'Scan QR Code to sync Anime & TV tracking with Simkl.'),
            ('mdblist', 'MDBList.com', 'mdblist.png', 'mdblist_apikey', 'Scan QR Code to open MDBList preferences and paste your API Key.'),
            ('support', 'Support RyuStream', 'support.png', '', 'Enjoying RyuStream? Scan QR Code to support ongoing updates via Buy Me a Coffee!')
        )
        list_items = list()
        for srv_id, name, img_file, setting_id, desc in services:
            if srv_id == 'support':
                status_text = "[COLOR cyan]BUY ME A COFFEE[/COLOR]"
            else:
                is_linked = bool(ADDON.getSetting(setting_id).strip())
                status_text = "[COLOR lime]CONNECTED[/COLOR]" if is_linked else "[COLOR gold]CLICK TO SCAN QR[/COLOR]"
            icon_path = os.path.join(MEDIA_PATH, img_file)
            li = xbmcgui.ListItem(label=name)
            li.setArt({'poster': '', 'thumb': icon_path, 'icon': icon_path, 'fanart': ''})
            li.setProperty('subtitle', status_text)
            li.setProperty('srv_id', srv_id)
            li.setProperty('setting_id', setting_id)
            li.setInfo('video', {'title': name, 'plot': desc})
            list_items.append(li)

        panel.addItems(list_items)
        self.setFocusId(5001)

    def handle_auth_click(self, srv_id, name, setting_id):
        try:
            if srv_id == 'support':
                coffee_url = "https://" + "buymeacoffee.com" + "/ryustream"
                qr_dlg = QRAuthDialog('Support RyuStream Developer', coffee_url, 'buymeacoffee.com', 'THANK YOU!')
                qr_dlg.show()
                while not qr_dlg.cancelled:
                    xbmc.sleep(500)
                qr_dlg.close()

            elif srv_id == 'alldebrid':
                url = "https://" + "api." + "alldebrid.com" + "/v4/pin/get?agent=RyuStream"
                res = SESSION.get(url, headers={'User-Agent': 'RyuStream/3.0.0'}, timeout=10).json()
                data = res.get('data', dict())
                pin = data.get('pin', '')
                check = data.get('check', '')
                user_url = data.get('user_url') or ("https://" + "alldebrid.com" + "/pin/?pin=" + pin)
                base_url = data.get('base_url') or ("https://" + "alldebrid.com" + "/pin")
                if not pin:
                    return
                qr_dlg = QRAuthDialog('Authorize AllDebrid', user_url, base_url, pin)
                qr_dlg.show()
                for _ in range(60):
                    if qr_dlg.cancelled:
                        break
                    xbmc.sleep(2000)
                    chk_url = "https://" + "api." + "alldebrid.com" + f"/v4/pin/check?agent=RyuStream&check={check}&pin={pin}"
                    chk = SESSION.get(chk_url, headers={'User-Agent': 'RyuStream/3.0.0'}, timeout=8).json()
                    if chk.get('data', dict()).get('activated'):
                        ADDON.setSetting('ad_apikey', chk.get('data', dict()).get('apikey', ''))
                        qr_dlg.close()
                        xbmcgui.Dialog().notification('AllDebrid', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                        self.load_authorize_cards()
                        return
                qr_dlg.close()

            elif srv_id == 'realdebrid':
                url = "https://" + "api." + "real-debrid.com" + f"/oauth/v2/device/code?client_id={RD_CLIENT_ID}&new_credentials=yes"
                res = SESSION.get(url, timeout=10).json()
                dev_code = res.get('device_code', '')
                user_code = res.get('user_code', '')
                ver_url = res.get('verification_url') or ("https://" + "real-debrid.com" + "/device")
                direct_url = res.get('direct_verification_url') or ver_url
                if not dev_code:
                    return
                qr_dlg = QRAuthDialog('Authorize Real-Debrid', direct_url, ver_url, user_code)
                qr_dlg.show()
                for _ in range(60):
                    if qr_dlg.cancelled:
                        break
                    xbmc.sleep(2500)
                    cred_url = "https://" + "api." + "real-debrid.com" + f"/oauth/v2/device/credentials?client_id={RD_CLIENT_ID}&code={dev_code}"
                    cred_res = SESSION.get(cred_url, timeout=8)
                    if cred_res.status_code == 200:
                        creds = cred_res.json()
                        tok_url = "https://" + "api." + "real-debrid.com" + "/oauth/v2/token"
                        tok_data = {
                            'client_id': creds['client_id'],
                            'client_secret': creds['client_secret'],
                            'code': dev_code,
                            'grant_type': 'http://' + 'oauth.net/grant_type/device/1.0'
                        }
                        tok_res = SESSION.post(tok_url, data=tok_data, timeout=8).json()
                        if tok_res.get('access_token'):
                            ADDON.setSetting('rd_apikey', tok_res['access_token'])
                            qr_dlg.close()
                            xbmcgui.Dialog().notification('Real-Debrid', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                            self.load_authorize_cards()
                            return
                qr_dlg.close()

            elif srv_id == 'premiumize':
                url = "https://" + "www.premiumize.me/token"
                res = SESSION.post(url, data={'client_id': PM_CLIENT_ID, 'response_type': 'device_code'}, timeout=10).json()
                dev_code = res.get('device_code', '')
                user_code = res.get('user_code', '')
                ver_url = res.get('verification_uri') or ("https://" + "www.premiumize.me/device")
                if not dev_code:
                    return
                qr_dlg = QRAuthDialog('Authorize Premiumize', f"{ver_url}?user_code={user_code}", ver_url, user_code)
                qr_dlg.show()
                for _ in range(60):
                    if qr_dlg.cancelled:
                        break
                    xbmc.sleep(2500)
                    tok_res = SESSION.post(url, data={'client_id': PM_CLIENT_ID, 'code': dev_code, 'grant_type': 'device_code'}, timeout=8).json()
                    if tok_res.get('access_token'):
                        ADDON.setSetting('pm_apikey', tok_res['access_token'])
                        qr_dlg.close()
                        xbmcgui.Dialog().notification('Premiumize', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                        self.load_authorize_cards()
                        return
                qr_dlg.close()

            elif srv_id == 'trakt':
                t_headers = {'Content-Type': 'application/json', 'trakt-api-version': '2', 'trakt-api-key': TRAKT_CLIENT_ID}
                url = "https://" + "api.trakt.tv/oauth/device/code"
                r = SESSION.post(url, json={'client_id': TRAKT_CLIENT_ID}, headers=t_headers, timeout=10)
                if not r.ok:
                    return
                res = r.json()
                dev_code = res.get('device_code', '')
                user_code = res.get('user_code', '')
                ver_url = res.get('verification_url') or ("https://" + "trakt.tv/activate")
                if not dev_code:
                    return
                qr_dlg = QRAuthDialog('Authorize Trakt.tv', f"{ver_url}?code={user_code}", ver_url, user_code)
                qr_dlg.show()
                for _ in range(60):
                    if qr_dlg.cancelled:
                        break
                    xbmc.sleep(3000)
                    tok_url = "https://" + "api.trakt.tv/oauth/device/token"
                    tok_res = SESSION.post(tok_url, json={'code': dev_code, 'client_id': TRAKT_CLIENT_ID}, headers=t_headers, timeout=8)
                    if tok_res.status_code == 200:
                        ADDON.setSetting('trakt_token', tok_res.json().get('access_token', ''))
                        qr_dlg.close()
                        xbmcgui.Dialog().notification('Trakt.tv', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                        self.load_authorize_cards()
                        return
                qr_dlg.close()

            elif srv_id == 'simkl':
                url = "https://" + "api." + "simkl.com" + f"/oauth/pin?client_id={SIMKL_CLIENT_ID}"
                res = SESSION.get(url, timeout=10).json()
                user_code = res.get('user_code', '')
                ver_url = res.get('verification_url') or ("https://" + "simkl.com" + "/pin")
                if not user_code:
                    return
                qr_dlg = QRAuthDialog('Authorize Simkl', f"{ver_url}/{user_code}", ver_url, user_code)
                qr_dlg.show()
                for _ in range(60):
                    if qr_dlg.cancelled:
                        break
                    xbmc.sleep(3000)
                    chk_url = "https://" + "api." + "simkl.com" + f"/oauth/pin/{user_code}?client_id={SIMKL_CLIENT_ID}"
                    chk = SESSION.get(chk_url, timeout=8).json()
                    if chk.get('result') == 'OK' and chk.get('access_token'):
                        ADDON.setSetting('simkl_token', chk['access_token'])
                        qr_dlg.close()
                        xbmcgui.Dialog().notification('Simkl', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                        self.load_authorize_cards()
                        return
                qr_dlg.close()

            elif srv_id in ('torbox', 'mdblist'):
                web_url = "https://" + ("torbox.app/settings" if srv_id == 'torbox' else "mdblist.com")
                qr_dlg = QRAuthDialog(f"{name} API Key", web_url, web_url, "SCAN QR -> COPY API KEY")
                qr_dlg.show()
                while not qr_dlg.cancelled:
                    xbmc.sleep(500)
                qr_dlg.close()
                current = ADDON.getSetting(setting_id)
                kb = xbmc.Keyboard(current, f"Paste {name} API Key")
                kb.doModal()
                if kb.isConfirmed():
                    ADDON.setSetting(setting_id, kb.getText().strip())
                    xbmcgui.Dialog().notification(name, 'Saved Successfully!', xbmcgui.NOTIFICATION_INFO, 3000)
                    self.load_authorize_cards()

        except Exception as e:
            xbmcgui.Dialog().notification(f'{name} Error', str(e)[:60], xbmcgui.NOTIFICATION_ERROR, 5000)

    def play_media_item(self, li):
        m_title = li.getLabel()
        tmdb_id = li.getProperty('tmdb_id')
        m_type = li.getProperty('media_type') or 'movie'

        # ตรวจสอบว่าเชื่อมต่อ Debrid อย่างน้อย 1 บริการหรือยัง
        has_debrid = any(ADDON.getSetting(k).strip() for k in ('ad_apikey', 'rd_apikey', 'pm_apikey', 'tb_apikey'))
        if not has_debrid:
            xbmcgui.Dialog().ok('Debrid Required', 'Please authorize AllDebrid, Real-Debrid, Premiumize, or TorBox in "Debrid Cloud (Authorize)" first.')
            self.load_authorize_cards()
            return

        season = 1
        episode = 1
        if m_type == 'tv':
            s_in = xbmcgui.Dialog().numeric(0, f'{m_title} - Season Number', '1')
            if not s_in:
                return
            e_in = xbmcgui.Dialog().numeric(0, f'{m_title} - Episode Number', '1')
            if not e_in:
                return
            season = int(s_in)
            episode = int(e_in)

        pDlg = xbmcgui.DialogProgress()
        pDlg.create('RyuStream Internal Scraper', f'Searching streams for {m_title}...')
        pDlg.update(30)
        imdb_id = get_imdb_id(tmdb_id, m_type)
        pDlg.update(60, f'Querying Torrentio & MediaFusion ({self.quality_mode.split(" ")[0][0]})...')
        streams = search_internal_streams(imdb_id, m_type, season, episode, self.quality_mode)
        pDlg.close()

        if not streams:
            xbmcgui.Dialog().notification('No Streams Found', 'Try changing Quality Filter to "All"', xbmcgui.NOTIFICATION_WARNING, 4000)
            return

        labels = [s['label'] for s in streams]
        sel = xbmcgui.Dialog().select(f'Select Stream ({len(streams)} found)', labels)
        if sel < 0:
            return

        pDlg.create('RyuStream Debrid Engine', 'Resolving high-speed Debrid stream link...')
        pDlg.update(50)
        stream_url = resolve_debrid_magnet(streams[sel]['magnet'])
        pDlg.close()

        if not stream_url:
            xbmcgui.Dialog().notification('Debrid Error', 'Could not resolve stream link (Check Debrid account)', xbmcgui.NOTIFICATION_ERROR, 5000)
            return

        play_li = xbmcgui.ListItem(label=m_title, path=stream_url)
        play_li.setInfo('video', {'title': m_title})
        xbmc.Player().play(stream_url, play_li)

    def select_quality_filter(self):
        options = (
            'All (4K / 1080p / 720p)',
            '1080p & 720p Only (No 4K - Low RAM TV)',
            '1080p Full HD Only',
            '4K UHD / HDR Only',
            '720p HD Only (Fastest)'
        )
        idx = xbmcgui.Dialog().select('Select Preferred Video Quality', list(options))
        if idx >= 0:
            self.quality_mode = options[idx]
            ADDON.setSetting('quality_filter', self.quality_mode)
            self.update_quality_button()
            xbmcgui.Dialog().notification('Quality Filter', self.quality_mode, xbmcgui.NOTIFICATION_INFO, 3000)

    def onClick(self, controlId):
        if controlId == 909:
            self.close()
        elif controlId == 906:
            self.select_quality_filter()
        elif controlId == 901:
            kb = xbmc.Keyboard('', 'Search Movies, TV Shows & Anime')
            kb.doModal()
            if kb.isConfirmed() and kb.getText().strip():
                q = kb.getText().strip()
                self.populate_row(5001, 103, 'search/multi', f'SEARCH RESULTS: {q.upper()}', {'query': q})
                self.setFocusId(5001)
        elif controlId == 904:
            cats = ('Reset Home (3 Rows)', 'Anime (Popular Japanese)', 'Anime Movies', 'Top Rated Movies', 'Popular TV Shows')
            idx = xbmcgui.Dialog().select('Select Catalog', list(cats))
            if idx == 0:
                self.load_default_rows()
            elif idx == 1:
                self.populate_row(5001, 103, 'discover/tv', 'POPULAR ANIME', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'})
                self.setFocusId(5001)
            elif idx == 2:
                self.populate_row(5001, 103, 'discover/movie', 'ANIME MOVIES', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'})
                self.setFocusId(5001)
            elif idx == 3:
                self.populate_row(5001, 103, 'movie/top_rated', 'TOP RATED MOVIES')
                self.setFocusId(5001)
            elif idx == 4:
                self.populate_row(5001, 103, 'tv/popular', 'POPULAR TV SHOWS')
                self.setFocusId(5001)
        elif controlId == 902:
            self.load_authorize_cards()
        elif controlId in (5001, 5002, 5003):
            li = self.getControl(controlId).getSelectedItem()
            if not li:
                return
            if controlId == 5001 and self.row1_mode == 'auth':
                self.handle_auth_click(li.getProperty('srv_id'), li.getLabel(), li.getProperty('setting_id'))
            else:
                self.play_media_item(li)

def router():
    xbmc.executebuiltin('Dialog.Close(busydialog)')
    xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
    ui = RyuHomeWindow('ryu_home.xml', ADDON_PATH, 'default', '1080i')
    ui.doModal()
    del ui

if __name__ == '__main__':
    router()
