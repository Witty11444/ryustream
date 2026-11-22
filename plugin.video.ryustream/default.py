import os
import sys
import json
import time
import hashlib
import sqlite3
import urllib.parse
import itertools
import requests
from requests.adapters import HTTPAdapter
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_PATH = ADDON.getAddonInfo('path')

# ดึงค่า sys.argv แบบปลอดภัย 100% ไม่พึ่งพาตัวเลขในวงเล็บ
_argv_iter = iter(sys.argv)
BASE_URL = next(_argv_iter, '')
HANDLE = int(next(_argv_iter, '-1'))
PARAM_STR = next(_argv_iter, '').lstrip('?')
AGENT_NAME = "RyuStreamKodi"

# =====================================================================
# ตั้งค่าข้อมูลร้านค้า & API Keys เริ่มต้นของคุณ
# =====================================================================
TH_SHOP_CONTACT = "LINE ID: @631llmic"
GLOBAL_DONATE_URL = "https://buymeacoffee.com"

DEFAULT_TMDB_KEY = "4a4c2835f4cb50a1fdb8cf2d5b16daa7"       # <--- ใส่ TMDb API Key (v3) 32 หลักของคุณตรงนี้
TRAKT_CLIENT_ID = ""        # สมัครที่ trakt.tv/oauth/applications
TRAKT_CLIENT_SECRET = ""
SIMKL_CLIENT_ID = ""        # สมัครที่ ://simkl.com
# =====================================================================

SESSION = requests.Session()
ADAPTER = HTTPAdapter(pool_connections=15, pool_maxsize=15, max_retries=1)
SESSION.mount('https://', ADAPTER)
SESSION.mount('http://', ADAPTER)

PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
if not os.path.exists(PROFILE_DIR):
    os.makedirs(PROFILE_DIR)
CACHE_DB = os.path.join(PROFILE_DIR, 'ryu_v22_cache.db')

def init_db():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT, expires INTEGER)')
    conn.commit()
    conn.close()

def get_cache(key):
    try:
        conn = sqlite3.connect(CACHE_DB)
        cur = conn.execute('SELECT data, expires FROM cache WHERE key = ?', (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            data_str, expires_at = row
            if expires_at > time.time():
                return json.loads(data_str)
    except Exception:
        pass
    return None

def set_cache(key, data, ttl=14400):
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.execute('REPLACE INTO cache (key, data, expires) VALUES (?, ?, ?)', (key, json.dumps(data), int(time.time() + ttl)))
        conn.commit()
        conn.close()
    except Exception:
        pass

init_db()

def is_thai():
    return False

def txt(th_text, en_text):
    return th_text if is_thai() else en_text

def get_tmdb_key():
    return ADDON.getSetting('tmdb_apikey').strip() or DEFAULT_TMDB_KEY

def build_url(query):
    return BASE_URL + '?' + urllib.parse.urlencode(query)
def fetch_submaker_subs(imdb_id, media_type='movie', season='', episode=''):
    sub_url = ADDON.getSetting('submaker_url').strip()
    if not sub_url or not imdb_id:
        return []
    base_sub_url = sub_url.replace('/manifest.json', '').rstrip('/')
    if media_type == 'tv' and season and episode:
        endpoint = f"{base_sub_url}/subtitles/series/{imdb_id}:{season}:{episode}.json"
    else:
        endpoint = f"{base_sub_url}/subtitles/movie/{imdb_id}.json"
    try:
        res = SESSION.get(endpoint, timeout=5).json()
        subs = res.get('subtitles', [])
        return [s.get('url') for s in subs if s.get('url')]
    except Exception:
        return []

def fetch_tmdb(endpoint, extra_params=None):
    tmdb_key = get_tmdb_key()
    if not tmdb_key:
        return []
    lang_code = 'th-TH' if is_thai() else 'en-US'
    raw_key = f"tmdb_{endpoint}_{lang_code}_{json.dumps(extra_params or {})}"
    cache_key = hashlib.md5(raw_key.encode()).hexdigest()

    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    params = {'api_key': tmdb_key, 'language': lang_code}
    if extra_params:
        params.update(extra_params)

    try:
        res = SESSION.get(f"https://themoviedb.org{endpoint}", params=params, timeout=4).json()
        results = res.get('results', [])
        if results:
            set_cache(cache_key, results)
        return results
    except Exception:
        return []

def fetch_tmdb_details(tmdb_id, media_type='movie'):
    tmdb_key = get_tmdb_key()
    if not tmdb_key or not tmdb_id:
        return None
    lang_code = 'th-TH' if is_thai() else 'en-US'
    cache_key = f"detail_{media_type}_{tmdb_id}_{lang_code}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    try:
        params = {'api_key': tmdb_key, 'language': lang_code, 'append_to_response': 'credits,external_ids'}
        res = SESSION.get(f"https://themoviedb.org{media_type}/{tmdb_id}", params=params, timeout=4).json()
        if res.get('id'):
            res['media_type'] = media_type
            set_cache(cache_key, res, ttl=86400)
            return res
    except Exception:
        pass
    return None

def show_tmdb_extra_info(tmdb_id, media_type):
    detail = fetch_tmdb_details(tmdb_id, media_type)
    if not detail:
        xbmcgui.Dialog().ok("TMDb Info", txt("ไม่พบข้อมูลเพิ่มเติม", "Details not available."))
        return

    title = detail.get('title') or detail.get('name') or ''
    orig_title = detail.get('original_title') or detail.get('original_name') or ''
    genres = ", ".join([g['name'] for g in detail.get('genres', [])]) or "-"
    ep_run = detail.get('episode_run_time') or []
    runtime = detail.get('runtime') or next(iter(ep_run), '-')
    cast_raw = detail.get('credits', {}).get('cast', [])
    cast_list = [c['name'] for c in itertools.islice(cast_raw, 6)]
    cast_str = ", ".join(cast_list) if cast_list else "-"
    overview = detail.get('overview') or txt("ไม่มีเรื่องย่อภาษาไทย", "No overview.")

    info_text = (
        f"[B]{title}[/B] ({orig_title})\n"
        f"[COLOR cyan]หมวดหมู่:[/COLOR] {genres}\n"
        f"[COLOR cyan]ความยาว:[/COLOR] {runtime} นาที\n"
        f"[COLOR gold]นักแสดงนำ:[/COLOR] {cast_str}\n\n"
        f"{overview}"
    )
    xbmcgui.Dialog().textviewer(txt("ข้อมูลภาพยนตร์ / ซีรีส์ (TMDb)", "TMDb Details & Cast"), info_text)

def get_trakt_watchlist():
    token = ADDON.getSetting('trakt_token')
    if not token or not TRAKT_CLIENT_ID:
        return []
    headers = {'Authorization': f'Bearer {token}', 'trakt-api-version': '2', 'trakt-api-key': TRAKT_CLIENT_ID}
    try:
        res = SESSION.get("https://trakt.tv", headers=headers, timeout=4.5).json()
        items = []
        for entry in itertools.islice(res, 20):
            m_type = 'movie' if entry.get('type') == 'movie' else 'tv'
            obj = entry.get('movie') or entry.get('show') or {}
            detail = fetch_tmdb_details(obj.get('ids', {}).get('tmdb'), m_type)
            if detail:
                items.append(detail)
        return items
    except Exception:
        return []

def get_simkl_watchlist():
    token = ADDON.getSetting('simkl_token')
    if not token or not SIMKL_CLIENT_ID:
        return []
    headers = {'Authorization': f'Bearer {token}', 'simkl-api-key': SIMKL_CLIENT_ID}
    try:
        res = SESSION.get("https://simkl.com", headers=headers, timeout=4.5).json()
        items = []
        combined = res.get('movies', []) + res.get('shows', [])
        for entry in itertools.islice(combined, 20):
            m_type = 'movie' if 'movie' in entry else 'tv'
            obj = entry.get('movie') or entry.get('show') or {}
            detail = fetch_tmdb_details(obj.get('ids', {}).get('tmdb'), m_type)
            if detail:
                items.append(detail)
        return items
    except Exception:
        return []

def get_mdblist_items():
    mdb_key = ADDON.getSetting('mdblist_apikey').strip()
    if not mdb_key:
        return []
    try:
        lists = SESSION.get(f"https://mdblist.com{mdb_key}", timeout=4).json()
        first_list = next(iter(lists), None) if isinstance(lists, list) else None
        if not first_list:
            return []
        first_list_id = first_list.get('id')
        res = SESSION.get(f"https://mdblist.com{first_list_id}/items/?apikey={mdb_key}", timeout=4.5).json()
        items = []
        for entry in itertools.islice(res, 20):
            m_type = 'movie' if entry.get('mediatype') == 'movie' else 'tv'
            detail = fetch_tmdb_details(entry.get('id'), m_type)
            if detail:
                items.append(detail)
        return items
    except Exception:
        return []
def auth_alldebrid():
    try:
        res = SESSION.get(f"https://alldebrid.com{AGENT_NAME}", timeout=5).json()
        if res.get('status') != 'success':
            return
        pin, check_id, user_url = res['data']['pin'], res['data']['check'], res['data']['user_url']
        pDialog = xbmcgui.DialogProgress()
        pDialog.create("AllDebrid Auth", f"1. เปิดเว็บ: [B]{user_url}[/B]\n2. กรอก PIN: [COLOR cyan][B]{pin}[/B][/COLOR]")
        for i in range(60):
            if pDialog.iscanceled():
                break
            pDialog.update(int((i / 60) * 100))
            time.sleep(3)
            chk = SESSION.get(f"https://alldebrid.com{AGENT_NAME}&check={check_id}&pin={pin}", timeout=4).json()
            if chk.get('status') == 'success' and chk['data'].get('activated'):
                apikey = chk['data']['apikey']
                u_info = SESSION.get(f"https://alldebrid.com{AGENT_NAME}&apikey={apikey}", timeout=4).json()
                ADDON.setSetting('ad_apikey', apikey)
                ADDON.setSetting('ad_username', u_info['data']['user']['username'])
                pDialog.close()
                xbmcgui.Dialog().ok("RyuStream", txt("เชื่อมต่อ AllDebrid สำเร็จ!", "AllDebrid Connected!"))
                return
        pDialog.close()
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def auth_trakt():
    if not TRAKT_CLIENT_ID:
        xbmcgui.Dialog().ok("Trakt", "กรุณาใส่ TRAKT_CLIENT_ID ในโค้ด default.py ก่อนใช้งาน")
        return
    try:
        res = SESSION.post("https://trakt.tv", json={'client_id': TRAKT_CLIENT_ID}, timeout=5).json()
        device_code, user_code, url = res['device_code'], res['user_code'], res['verification_url']
        pDialog = xbmcgui.DialogProgress()
        pDialog.create("Trakt Auth", f"1. เปิดเว็บ: [B]{url}[/B]\n2. กรอกรหัส: [COLOR lime][B]{user_code}[/B][/COLOR]")
        for i in range(60):
            if pDialog.iscanceled():
                break
            pDialog.update(int((i / 60) * 100))
            time.sleep(5)
            chk = SESSION.post("https://trakt.tv", json={'code': device_code, 'client_id': TRAKT_CLIENT_ID, 'client_secret': TRAKT_CLIENT_SECRET}, timeout=4)
            if chk.status_code == 200:
                ADDON.setSetting('trakt_token', chk.json()['access_token'])
                pDialog.close()
                xbmcgui.Dialog().ok("Trakt", txt("เชื่อมต่อ Trakt สำเร็จ!", "Trakt Connected!"))
                return
        pDialog.close()
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def auth_simkl():
    if not SIMKL_CLIENT_ID:
        xbmcgui.Dialog().ok("Simkl", "กรุณาใส่ SIMKL_CLIENT_ID ในโค้ด default.py ก่อนใช้งาน")
        return
    try:
        res = SESSION.get(f"https://simkl.com{SIMKL_CLIENT_ID}", timeout=5).json()
        user_code, url = res['user_code'], res['verification_url']
        pDialog = xbmcgui.DialogProgress()
        pDialog.create("Simkl Auth", f"1. เปิดเว็บ: [B]{url}[/B]\n2. กรอกรหัส: [COLOR cyan][B]{user_code}[/B][/COLOR]")
        for i in range(60):
            if pDialog.iscanceled():
                break
            pDialog.update(int((i / 60) * 100))
            time.sleep(5)
            chk = SESSION.get(f"https://simkl.com{user_code}?client_id={SIMKL_CLIENT_ID}", timeout=4).json()
            if chk.get('result') == 'OK':
                ADDON.setSetting('simkl_token', chk['access_token'])
                pDialog.close()
                xbmcgui.Dialog().ok("Simkl", txt("เชื่อมต่อ Simkl สำเร็จ!", "Simkl Connected!"))
                return
        pDialog.close()
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def list_all_clouds(filter_keyword=None, imdb_id='', media_type='movie'):
    dir_items = []
    ad_key = ADDON.getSetting('ad_apikey').strip() if ADDON.getSettingBool('enable_ad') else ''
    tb_key = ADDON.getSetting('tb_apikey').strip() if ADDON.getSettingBool('enable_tb') else ''
    rd_key = ADDON.getSetting('rd_apikey').strip() if ADDON.getSettingBool('enable_rd') else ''
    kw = next(iter(filter_keyword.lower().split(':')), '').strip() if filter_keyword else None

    if ad_key:
        try:
            res = SESSION.get(f"https://alldebrid.com{AGENT_NAME}&apikey={ad_key}", timeout=4).json()
            for item in res.get('data', {}).get('links', []):
                fname = item.get('filename', '')
                if kw and kw not in fname.lower():
                    continue
                li = xbmcgui.ListItem(label=f"[COLOR cyan][AllDebrid][/COLOR] {fname}", offscreen=True)
                li.setProperty('IsPlayable', 'true')
                dir_items.append((build_url({'action': 'play', 'provider': 'ad', 'url': item.get('link'), 'imdb_id': imdb_id, 'm_type': media_type}), li, False))
        except Exception:
            pass

    if tb_key:
        try:
            res = SESSION.get("https://torbox.app", headers={'Authorization': f'Bearer {tb_key}'}, timeout=4).json()
            for item in res.get('data', []):
                for f in item.get('files', []):
                    fname = f.get('short_name', '')
                    if kw and kw not in fname.lower():
                        continue
                    li = xbmcgui.ListItem(label=f"[COLOR lime][TorBox][/COLOR] {fname}", offscreen=True)
                    li.setProperty('IsPlayable', 'true')
                    dir_items.append((build_url({'action': 'play', 'provider': 'tb', 'tid': item.get('id'), 'fid': f.get('id'), 'imdb_id': imdb_id, 'm_type': media_type}), li, False))
        except Exception:
            pass

    if rd_key:
        try:
            res = SESSION.get("https://real-debrid.com", headers={'Authorization': f'Bearer {rd_key}'}, timeout=4).json()
            for item in itertools.islice(res, 30):
                fname = item.get('filename', '')
                if kw and kw not in fname.lower():
                    continue
                li = xbmcgui.ListItem(label=f"[COLOR gold][Real-Debrid][/COLOR] {fname}", offscreen=True)
                li.setProperty('IsPlayable', 'true')
                dir_items.append((build_url({'action': 'play', 'provider': 'rd', 'url': item.get('download'), 'imdb_id': imdb_id, 'm_type': media_type}), li, False))
        except Exception:
            pass

    if not dir_items:
        msg = txt(
            f"ไม่พบไฟล์ในคลาวด์ส่วนตัวของคุณ\nติดต่อสอบถามข้อมูล: {TH_SHOP_CONTACT}",
            "No matching files found in your connected Debrid Clouds."
        )
        xbmcgui.Dialog().ok("RyuStream Cloud", msg)
        return

    xbmcplugin.addDirectoryItems(HANDLE, dir_items, len(dir_items))
    xbmcplugin.endOfDirectory(HANDLE)

def play_video(params):
    provider = params.get('provider')
    stream_url = ''
    try:
        if provider == 'ad':
            ad_key = ADDON.getSetting('ad_apikey')
            res = SESSION.get(f"https://alldebrid.com{AGENT_NAME}&apikey={ad_key}&link={urllib.parse.quote(params.get('url', ''))}", timeout=4.5).json()
            stream_url = res['data']['link']
        elif provider == 'tb':
            tb_key = ADDON.getSetting('tb_apikey')
            res = SESSION.get(f"https://torbox.app{tb_key}&torrent_id={params.get('tid')}&file_id={params.get('fid')}", timeout=4.5).json()
            stream_url = res['data']
        elif provider == 'rd':
            stream_url = params.get('url', '')

        if stream_url:
            play_item = xbmcgui.ListItem(path=stream_url)
            imdb_id = params.get('imdb_id', '')
            m_type = params.get('m_type', 'movie')
            sub_urls = fetch_submaker_subs(imdb_id, m_type)
            if sub_urls:
                play_item.setSubtitles(sub_urls)
            xbmcplugin.setResolvedUrl(HANDLE, True, listitem=play_item)
    except Exception as e:
        xbmcgui.Dialog().notification("Playback Error", str(e), xbmcgui.NOTIFICATION_ERROR)
class RyuHomeWindow(xbmcgui.WindowXML):
    def onInit(self):
        self.update_status_bar()
        self.load_items(fetch_tmdb('trending/all/week'), txt("TRENDING NOW / รายการยอดนิยมสัปดาห์นี้", "TRENDING NOW"))

    def update_status_bar(self):
        ad_user = ADDON.getSetting('ad_username')
        badges = []
        if ADDON.getSetting('ad_apikey') and ADDON.getSettingBool('enable_ad'):
            badges.append(f"[COLOR lime]AD:{ad_user}[/COLOR]")
        if ADDON.getSetting('tb_apikey') and ADDON.getSettingBool('enable_tb'):
            badges.append("[COLOR lime]TB[/COLOR]")
        if ADDON.getSetting('rd_apikey') and ADDON.getSettingBool('enable_rd'):
            badges.append("[COLOR lime]RD[/COLOR]")
        if ADDON.getSetting('trakt_token'):
            badges.append("[COLOR cyan]Trakt[/COLOR]")
        if ADDON.getSetting('simkl_token'):
            badges.append("[COLOR cyan]Simkl[/COLOR]")
        if ADDON.getSetting('mdblist_apikey'):
            badges.append("[COLOR gold]MDB[/COLOR]")
        if ADDON.getSetting('submaker_url'):
            badges.append("[COLOR cyan]SubMaker[/COLOR]")

        status_txt = " | ".join(badges) if badges else txt(f"ยังไม่เชื่อมต่อบัญชี | {TH_SHOP_CONTACT}", "No Accounts Connected")
        self.getControl(102).setLabel(status_txt)

    def load_items(self, results, heading):
        self.getControl(103).setLabel(f"[B]{heading}[/B]")
        panel = self.getControl(5000)
        panel.reset()
        no_plot = txt('ไม่มีเรื่องย่อภาษาไทย', 'No overview available.')
        list_items = []

        for item in results:
            m_type = item.get('media_type') or ('movie' if item.get('title') else 'tv')
            if m_type not in ('movie', 'tv'):
                continue

            title = item.get('title') or item.get('name') or 'Unknown'
            orig_title = item.get('original_title') or item.get('original_name') or title
            release_date = item.get('release_date') or item.get('first_air_date') or ''
            year = release_date[:4] if release_date else ''
            overview = item.get('overview') or no_plot
            rating = f"{float(item.get('vote_average') or 0):.1f}"

            poster = f"https://tmdb.org{item['poster_path']}" if item.get('poster_path') else ''
            fanart = f"https://tmdb.org{item['backdrop_path']}" if item.get('backdrop_path') else ''

            li = xbmcgui.ListItem(label=title, offscreen=True)
            li.setArt({'poster': poster, 'thumb': poster, 'fanart': fanart})
            li.setProperty('orig_title', orig_title)
            li.setProperty('tmdb_id', str(item.get('id', '')))
            li.setProperty('media_type', m_type)

            info_tag = li.getVideoInfoTag()
            info_tag.setTitle(title)
            info_tag.setPlot(overview)
            info_tag.setRating(float(rating))
            if year.isdigit():
                info_tag.setYear(int(year))
            list_items.append(li)

        if list_items:
            panel.addItems(list_items)
            self.setFocusId(5000)

    def onClick(self, controlId):
        if controlId == 5000:
            item = self.getControl(5000).getSelectedItem()
            if item:
                orig_title = item.getProperty('orig_title')
                tmdb_id = item.getProperty('tmdb_id')
                m_type = item.getProperty('media_type')
                choice = xbmcgui.Dialog().select(item.getLabel(), [
                    txt("ค้นหาและเล่นไฟล์ใน Debrid Cloud", "Play from Debrid Cloud"),
                    txt("ดูรายละเอียดเพิ่มเติม & รายชื่อนักแสดง (TMDb)", "Details & Cast (TMDb)")
                ])
                if choice == 0:
                    detail = fetch_tmdb_details(tmdb_id, m_type) or {}
                    imdb_id = detail.get('external_ids', {}).get('imdb_id', '')
                    self.close()
                    xbmc.executebuiltin(f"Container.Update({build_url({'action': 'my_cloud', 'filter': orig_title, 'imdb_id': imdb_id, 'm_type': m_type})})")
                elif choice == 1:
                    show_tmdb_extra_info(tmdb_id, m_type)
        elif controlId == 901:
            kb = xbmc.Keyboard('', txt('ค้นหาชื่อภาพยนตร์ / ซีรีส์ / อนิเมะ (ไทย หรือ อังกฤษ)', 'Search Movies, TV & Anime'))
            kb.doModal()
            if kb.isConfirmed() and kb.getText().strip():
                q = kb.getText().strip()
                self.load_items(fetch_tmdb('search/multi', {'query': q}), f"ผลการค้นหา: {q}")
        elif controlId == 904:
            choices = [
                "TMDb: Trending (ยอดนิยมสัปดาห์นี้)",
                "TMDb: Popular Movies (ภาพยนตร์ยอดนิยม)",
                "TMDb: Now Playing (ภาพยนตร์เข้าใหม่)",
                "TMDb: Popular Series (ซีรีส์ยอดนิยม)",
                "TMDb: Anime (อนิเมะยอดนิยม)",
                "Trakt: My Watchlist",
                "Simkl: My Watchlist",
                "MDBList: My Lists"
            ]
            idx = xbmcgui.Dialog().select(txt("เลือกหมวดหมู่รายการ", "Select Catalog"), choices)
            if idx >= 0:
                selected_title = next(itertools.islice(choices, idx, idx + 1), '')
                if idx == 0:
                    self.load_items(fetch_tmdb('trending/all/week'), selected_title)
                elif idx == 1:
                    self.load_items(fetch_tmdb('movie/popular'), selected_title)
                elif idx == 2:
                    self.load_items(fetch_tmdb('movie/now_playing'), selected_title)
                elif idx == 3:
                    self.load_items(fetch_tmdb('tv/popular'), selected_title)
                elif idx == 4:
                    self.load_items(fetch_tmdb('discover/tv', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'}), selected_title)
                elif idx == 5:
                    self.load_items(get_trakt_watchlist(), selected_title)
                elif idx == 6:
                    self.load_items(get_simkl_watchlist(), selected_title)
                elif idx == 7:
                    self.load_items(get_mdblist_items(), selected_title)
        elif controlId == 902:
            self.close()
            xbmc.executebuiltin(f"Container.Update({build_url({'action': 'my_cloud'})})")
        elif controlId == 903:
            opts = [
                txt(f"เช็กวันหมดอายุ AllDebrid ({TH_SHOP_CONTACT})", "Check AllDebrid / Support"),
                "เชื่อมต่อ AllDebrid (PIN)",
                "เชื่อมต่อ Trakt (PIN)",
                "เชื่อมต่อ Simkl (PIN)",
                "เปิดหน้าตั้งค่าทั้งหมด (สวิตช์เปิด-ปิด / TorBox / RD / SubMaker / TMDb)"
            ]
            idx = xbmcgui.Dialog().select("RyuStream Accounts", opts)
            if idx == 0:
                xbmcgui.Dialog().ok("RyuStream Support", txt(f"ติดต่อสอบถาม/ต่ออายุ AllDebrid ได้ที่:\n[COLOR cyan][B]{TH_SHOP_CONTACT}[/B][/COLOR]", f"Support Developer:\n{GLOBAL_DONATE_URL}"))
            elif idx == 1:
                auth_alldebrid()
                self.update_status_bar()
            elif idx == 2:
                auth_trakt()
                self.update_status_bar()
            elif idx == 3:
                auth_simkl()
                self.update_status_bar()
            elif idx == 4:
                self.close()
                ADDON.openSettings()

def router(paramstring):
    params = dict(urllib.parse.parse_qsl(paramstring))
    action = params.get('action')

    if action is None:
        if not get_tmdb_key():
            ADDON.openSettings()
            return
        ui = RyuHomeWindow('ryu_home.xml', ADDON_PATH, 'Default', '1080i')
        ui.doModal()
        del ui
    elif action == 'my_cloud':
        list_all_clouds(params.get('filter'), params.get('imdb_id', ''), params.get('m_type', 'movie'))
    elif action == 'play':
        play_video(params)
    elif action == 'auth_alldebrid':
        auth_alldebrid()
    elif action == 'auth_trakt':
        auth_trakt()
    elif action == 'auth_simkl':
        auth_simkl()

if __name__ == '__main__':
    router(PARAM_STR)
