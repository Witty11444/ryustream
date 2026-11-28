import sys
import os
import json
import re
import time
import sqlite3
import hashlib
import base64
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
PROFILE_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
MEDIA_PATH = os.path.join(ADDON_PATH, 'resources', 'media')
if not xbmcvfs.exists(PROFILE_PATH):
    xbmcvfs.mkdirs(PROFILE_PATH)

DB_PATH = os.path.join(PROFILE_PATH, 'ryustream_cache.db')
DB_LOCK = threading.Lock()
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
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT, expires INTEGER)')
        conn.commit()
        conn.close()

def get_cache(key):
    init_db()
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        c = conn.cursor()
        c.execute('SELECT data, expires FROM cache WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()
    if row and row[1] > int(time.time()):
        return json.loads(row[0])
    return None

def set_cache(key, data, ttl=86400):
    init_db()
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        c = conn.cursor()
        c.execute('REPLACE INTO cache (key, data, expires) VALUES (?, ?, ?)', (key, json.dumps(data), int(time.time()) + ttl))
        conn.commit()
        conn.close()

def get_submaker_subtitles(manifest_url, media_type, imdb_id, season=1, episode=1, release_name='', size_bytes=None, video_hash=''):
    """Download subtitle files from a configured Stremio subtitle manifest."""
    manifest_url = (manifest_url or '').strip()
    if not manifest_url or not imdb_id:
        return []
    parsed = urllib.parse.urlsplit(manifest_url)
    if parsed.scheme == 'stremio':
        parsed = parsed._replace(scheme='https')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        xbmc.log('[RyuStream] SubMaker URL must be an http(s) install/manifest URL', xbmc.LOGWARNING)
        return []
    base_path = parsed.path.rsplit('/manifest.json', 1)[0].rstrip('/')
    endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, '', '')).rstrip('/')
    content_type = 'series' if media_type == 'tv' else 'movie'
    resource_id = f'{imdb_id}:{int(season)}:{int(episode)}' if content_type == 'series' else imdb_id
    url = f'{endpoint}/subtitles/{content_type}/{urllib.parse.quote(resource_id, safe=":")}.json'
    params = {}
    if release_name:
        params['filename'] = release_name[:240]
    if size_bytes:
        params['videoSize'] = int(size_bytes)
    if video_hash:
        params['videoHash'] = str(video_hash).lower()
    try:
        response = SESSION.get(url, params=params, timeout=12)
        response.raise_for_status()
        subtitles = response.json().get('subtitles', [])
    except Exception as exc:
        xbmc.log(f'[RyuStream] SubMaker lookup failed: {exc}', xbmc.LOGWARNING)
        return []

    subtitle_paths = []
    temp_dir = xbmcvfs.translatePath('special://temp')
    for index, subtitle in enumerate(subtitles[:6]):
        subtitle_url = str(subtitle.get('url') or '')
        sub_parts = urllib.parse.urlsplit(subtitle_url)
        if sub_parts.scheme not in ('http', 'https') or not sub_parts.netloc:
            continue
        fmt = str(subtitle.get('format') or 'srt').lower()
        ext = fmt if fmt in ('srt', 'ass', 'ssa', 'vtt', 'sub') else 'srt'
        target = os.path.join(temp_dir, f'ryustream_submaker_{index}.{ext}')
        try:
            sub_response = SESSION.get(subtitle_url, timeout=20)
            sub_response.raise_for_status()
            content = sub_response.content
            if not content or len(content) > 8 * 1024 * 1024:
                continue
            file_handle = xbmcvfs.File(target, 'wb')
            file_handle.write(content)
            file_handle.close()
            subtitle_paths.append(target)
        except Exception as exc:
            xbmc.log(f'[RyuStream] SubMaker subtitle download failed: {exc}', xbmc.LOGWARNING)
    return subtitle_paths

def clear_db_cache(optimize=False):
    init_db()
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        c = conn.cursor()
        c.execute('DELETE FROM cache')
        conn.commit()
        if optimize:
            c.execute('VACUUM')
        conn.close()

def set_video_info(li, title, year=0, rating=0.0, plot=''):
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(title)
        if year:
            tag.setYear(int(year))
        if rating:
            tag.setRating(float(rating))
        if plot:
            tag.setPlot(plot)
        return
    except Exception:
        pass
    info = {'title': title}
    if year:
        info['year'] = int(year)
    if rating:
        info['rating'] = float(rating)
    if plot:
        info['plot'] = plot
    li.setInfo('video', info)

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

def fetch_tmdb_detail(media_type, tmdb_id):
    tmdb_key = get_tmdb_key()
    if not tmdb_key or not tmdb_id:
        return None
    endpoint = f"{media_type}/{tmdb_id}"
    raw_key = f"tmdb_detail_v300_{endpoint}"
    cache_key = hashlib.md5(raw_key.encode()).hexdigest()
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    try:
        url = "https://" + "api." + "themoviedb.org/3/" + endpoint
        res = SESSION.get(url, params={'api_key': tmdb_key, 'language': 'en-US'}, timeout=10)
        if res.ok:
            data = res.json()
            if isinstance(data, dict) and data.get('id'):
                set_cache(cache_key, data)
                return data
    except Exception:
        pass
    return None

def get_imdb_id(tmdb_id, media_type='movie'):
    tmdb_key = get_tmdb_key()
    url = f"https://" + f"api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids"
    try:
        res = SESSION.get(url, params={'api_key': tmdb_key}, timeout=8).json()
        return res.get('imdb_id', '')
    except Exception:
        return ''
def resolve_debrid_magnet(magnet_uri):
    ad_key = ADDON.getSetting('ad_apikey').strip() if ADDON.getSetting('enable_ad') != 'false' else ''
    rd_key = ADDON.getSetting('rd_apikey').strip() if ADDON.getSetting('enable_rd') != 'false' else ''
    pm_key = ADDON.getSetting('pm_apikey').strip() if ADDON.getSetting('enable_pm') != 'false' else ''
    tb_key = ADDON.getSetting('tb_apikey').strip() if ADDON.getSetting('enable_tb') != 'false' else ''

    # 2. Real-Debrid Resolver (แก้ไข Path เต็ม + เลือกไฟล์วิดีโอใหญ่สุดอัตโนมัติ)
    if rd_key:
        try:
            headers = {'Authorization': f'Bearer {rd_key}'}
            rd_base = 'https://api.real-debrid.com/rest/1.0'
            add_response = SESSION.post(f'{rd_base}/torrents/addMagnet', data={'magnet': magnet_uri}, headers=headers, timeout=20)
            add_response.raise_for_status()
            add_data = add_response.json()
            tid = add_data.get('id')
            if not tid:
                raise RuntimeError(add_data.get('error', 'Real-Debrid returned no torrent id'))
            info_response = SESSION.get(f'{rd_base}/torrents/info/{tid}', headers=headers, timeout=20)
            info_response.raise_for_status()
            info = info_response.json()
            video_exts = ('.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.webm')
            video_files = [f for f in info.get('files', []) if str(f.get('path', '')).lower().endswith(video_exts)]
            if not video_files:
                raise RuntimeError('No supported video file found in Real-Debrid torrent')
            best_file = max(video_files, key=lambda f: int(f.get('bytes', 0) or 0))
            selection = SESSION.post(f'{rd_base}/torrents/selectFiles/{tid}', data={'files': str(best_file['id'])}, headers=headers, timeout=20)
            selection.raise_for_status()
            for _ in range(15):
                xbmc.sleep(1000)
                info_response = SESSION.get(f'{rd_base}/torrents/info/{tid}', headers=headers, timeout=20)
                info_response.raise_for_status()
                info = info_response.json()
                links = info.get('links') or []
                selected_files = [f for f in info.get('files', []) if int(f.get('selected', 0) or 0) == 1]
                if links and selected_files:
                    unlock_response = SESSION.post(f'{rd_base}/unrestrict/link', data={'link': links[0]}, headers=headers, timeout=20)
                    unlock_response.raise_for_status()
                    unlocked = unlock_response.json()
                    if unlocked.get('download'):
                        match = re.search(r'btih:([^&]+)', str(magnet_uri), re.I)
                        if match:
                            rd_hash = normalize_btih(match.group(1))
                            set_cache('rd_confirmed_cached_' + rd_hash, True, ttl=60 * 86400)
                            xbmc.log(f'[RyuStream] RD cached hash remembered after successful playback: {rd_hash[:12]}', xbmc.LOGINFO)
                        return unlocked['download']
                    raise RuntimeError(unlocked.get('error', 'Real-Debrid failed to unlock selected file'))
                if info.get('status') in ('error', 'virus', 'magnet_error', 'dead'):
                    raise RuntimeError(f"Real-Debrid torrent status: {info.get('status')}")
            raise RuntimeError(f"Torrent is not cached/ready (status: {info.get('status', 'unknown')})")
        except Exception as e:
            xbmc.log(f'[RyuStream] Real-Debrid resolve failed: {e}', xbmc.LOGWARNING)
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code == 401:
                xbmcgui.Dialog().notification('Real-Debrid', 'Authorization expired or invalid. Reauthorize in Debrid Cloud.', xbmcgui.NOTIFICATION_ERROR, 6500)
            else:
                xbmcgui.Dialog().notification('Real-Debrid', str(e)[:90] + ' - trying next provider', xbmcgui.NOTIFICATION_WARNING, 4500)

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
        ("MediaFusion", "https://mediafusion.elfhosted.com/stream/" + f"{strem_type}/{strem_id}.json")
    )

    for provider, url in scrapers:
        try:
            res = SESSION.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8).json()
            for s in res.get('streams', list()):
                title_raw = s.get('title') or s.get('name') or 'Unknown Stream'
                info_hash = s.get('infoHash', '')
                if not info_hash:
                    continue

                clean_line = title_raw.replace('\n', ' | ')[:95]
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(clean_line[:40])}"
                sources.append({
                    'label': f"[INTERNAL / {provider}] {clean_line}",
                    'magnet': magnet,
                    'source_type': 'Internal',
                    'scraper': provider,
                    'name': clean_line,
                    'title_raw': title_raw,
                    'size_bytes': s.get('size') or (s.get('behaviorHints') or {}).get('videoSize')
                })
            xbmc.log(f'[RyuStream] internal scraper {provider}: {len(res.get("streams", []))} results', xbmc.LOGINFO)
        except Exception as e:
            xbmc.log(f'[RyuStream] internal scraper {provider} failed: {e}', xbmc.LOGWARNING)
            continue

    return sources


def stream_quality_bucket(stream):
    quality = str(stream.get('quality', '')).lower()
    label = str(stream.get('label', stream.get('name', ''))).lower()
    text = quality + ' ' + label
    if '2160' in text or '4k' in text or 'uhd' in text:
        return '4K'
    if '1080' in text:
        return '1080p'
    if '720' in text:
        return '720p'
    return None


def quality_counts(streams):
    counts = {'4K': 0, '1080p': 0, '720p': 0}
    for stream in streams:
        bucket = stream_quality_bucket(stream)
        if bucket:
            counts[bucket] += 1
    return counts
def quality_counts_by_source(streams):
    """Nested counts split by source: {'Internal': {'4K': n, '1080p': n, '720p': n}, 'External': {...}}."""
    counts = {
        'Internal': {'4K': 0, '1080p': 0, '720p': 0},
        'External': {'4K': 0, '1080p': 0, '720p': 0},
    }
    for stream in streams:
        bucket = stream_quality_bucket(stream)
        if not bucket:
            continue
        srcs = [str(s).lower() for s in stream.get('sources', [])]
        if any(s.startswith('internal /') for s in srcs):
            counts['Internal'][bucket] += 1
        elif any(s.startswith('external /') for s in srcs):
            counts['External'][bucket] += 1
    return counts


def real_debrid_cached_hashes():
    """Return torrent hashes already completed in this user's Real-Debrid cloud."""
    if ADDON.getSetting('enable_rd') == 'false':
        return None
    token = ADDON.getSetting('rd_apikey').strip()
    if not token:
        return None
    headers = {'Authorization': f'Bearer {token}'}
    url = 'https://api.real-debrid.com/rest/1.0/torrents'
    cached = set()
    offset = 0
    while True:
        response = SESSION.get(url, params={'limit': 5000, 'offset': offset}, headers=headers, timeout=20)
        # Requests considers HTTP 204 successful, but it has no JSON body.
        # Real-Debrid uses it when this account has no saved torrents.
        if response.status_code == 204:
            break
        if not response.ok:
            body = (response.text or '').strip().replace('\r', ' ').replace('\n', ' ')[:180]
            raise RuntimeError(f'Real-Debrid HTTP {response.status_code}: {body or "empty response"}')
        try:
            torrents = response.json()
        except ValueError:
            content_type = response.headers.get('Content-Type', 'unknown')
            body = (response.text or '').strip().replace('\r', ' ').replace('\n', ' ')[:180]
            raise RuntimeError(f'Real-Debrid returned non-JSON (HTTP {response.status_code}, {content_type}): {body or "empty response"}')
        if not isinstance(torrents, list):
            raise RuntimeError('Unexpected Real-Debrid torrents response')
        for torrent in torrents:
            info_hash = str(torrent.get('hash', '')).strip().lower()
            if info_hash and torrent.get('status') == 'downloaded':
                cached.add(info_hash)
        if len(torrents) < 5000:
            break
        offset += len(torrents)
    return cached


def normalize_btih(info_hash):
    value = str(info_hash or '').strip()
    if len(value) == 32:
        try:
            return base64.b32decode(value.upper() + '=' * ((8 - len(value) % 8) % 8)).hex()
        except Exception:
            pass
    return value.lower()


def mark_real_debrid_cache(streams):
    try:
        cached_hashes = real_debrid_cached_hashes()
        if cached_hashes is None:
            xbmc.log('[RyuStream] RD cache check skipped: Real-Debrid is not authorized/enabled', xbmc.LOGINFO)
            return False
        for stream in streams:
            match = re.search(r'btih:([^&]+)', str(stream.get('magnet', '')), re.I)
            info_hash = normalize_btih(match.group(1)) if match else ''
            remembered = get_cache('rd_confirmed_cached_' + info_hash) if info_hash else None
            stream['rd_cached'] = bool(info_hash and (info_hash in cached_hashes or remembered is True))
        cached_count = sum(1 for stream in streams if stream.get('rd_cached'))
        xbmc.log(f'[RyuStream] RD cache matches={cached_count} of {len(streams)} (cloud hashes={len(cached_hashes)})', xbmc.LOGINFO)
        return True
    except Exception as e:
        for stream in streams:
            stream['rd_cached'] = None
        xbmc.log(f'[RyuStream] RD cache check failed: {e}', xbmc.LOGWARNING)
        return False


def apply_quality_filter(streams, quality_filter):
    if '1080p & 720p' in quality_filter:
        return [s for s in streams if stream_quality_bucket(s) in ('1080p', '720p')]
    if '1080p Full HD' in quality_filter:
        return [s for s in streams if stream_quality_bucket(s) == '1080p']
    if '4K UHD' in quality_filter:
        return [s for s in streams if stream_quality_bucket(s) == '4K']
    if '720p HD' in quality_filter:
        return [s for s in streams if stream_quality_bucket(s) == '720p']
    return streams


def balance_scraper_sources(streams, limit=None):
    """Put RD cached results first, grouped by source, quality and descending size."""
    quality_order = {'4K': 0, '1080p': 1, '720p': 2, None: 3}
    def source_rank(item):
        return 0 if any(str(tag).lower().startswith('internal /') for tag in item.get('sources', [])) else 1
    def size_bytes(item):
        try:
            if item.get('size_bytes') is not None:
                return float(item['size_bytes'])
            raw = item.get('size') or 0
            if isinstance(raw, (int, float)) and raw > 0:
                return float(raw) * (1024 ** 3)
            text = str(raw) if raw else ' '.join(str(item.get(k, '')) for k in ('title_raw', 'name', 'label'))
            match = re.search(r'(\d+(?:[.,]\d+)?)\s*(TB|GB|GIB|MB|MIB)', text, re.I)
            if match:
                amount = float(match.group(1).replace(',', '.'))
                unit = match.group(2).upper()
                return amount * (1024 ** 4 if unit == 'TB' else 1024 ** 2 if unit in ('MB', 'MIB') else 1024 ** 3)
        except (TypeError, ValueError):
            pass
        return 0
    def cache_rank(item):
        status = item.get('rd_cached')
        return 0 if status is True else (1 if status is False else 2)
    return sorted(streams, key=lambda item: (cache_rank(item), source_rank(item), quality_order.get(stream_quality_bucket(item), 3), -size_bytes(item), str(item.get('name', '')).lower()))


def stream_detail_fields(stream):
    name = str(stream.get('name') or stream.get('label') or '')
    metadata = ' '.join(str(stream.get(key, '')) for key in ('name', 'label', 'info', 'description', 'title_raw', 'size', 'size_bytes'))
    size_gb = None
    try:
        if stream.get('size_bytes') is not None:
            size_gb = float(stream['size_bytes']) / (1024 ** 3)
        elif isinstance(stream.get('size'), (int, float)):
            size_gb = float(stream['size'])  # CocoScrapers reports normalized GB values.
    except (TypeError, ValueError):
        pass
    if size_gb is None:
        match = re.search(r'(\d+(?:[.,]\d+)?)\s*(TB|GB|GIB|MB|MIB)', str(stream.get('size', '') or metadata), re.I)
        if match:
            amount = float(match.group(1).replace(',', '.'))
            unit = match.group(2).upper()
            size_gb = amount * (1024 if unit == 'TB' else 1 / 1024 if unit in ('MB', 'MIB') else 1)
    ext_match = re.search(r'\.(mkv|mp4|avi|mov|m2ts|ts|webm)(?:$|[\s?])', name, re.I)
    ext = ext_match.group(1).upper() if ext_match else ''
    container = ext if ext in ('MKV', 'MP4', 'AVI', 'MOV', 'TS', 'M2TS', 'WEBM') else 'N/A'
    def find(patterns):
        return next((value for pattern, value in patterns if re.search(pattern, metadata, re.I)), 'N/A')
    video = find(((r'\bAV1\b', 'AV1'), (r'\b(?:HEVC|H\.265|x265)\b', 'HEVC / H.265'), (r'\b(?:AVC|H\.264|x264)\b', 'AVC / H.264')))
    hdr = []
    for pattern, value in ((r'Dolby.?Vision|\bDV\b', 'Dolby Vision'), (r'HDR10\+', 'HDR10+'), (r'\bHDR10\b', 'HDR10'), (r'\bHDR\b', 'HDR')):
        if re.search(pattern, metadata, re.I) and value not in hdr:
            hdr.append(value)
    audio_parts = []
    if re.search(r'\bAtmos\b', metadata, re.I):
        audio_parts.append('Dolby Atmos')
    audio_codec = find(((r'TrueHD', 'TrueHD'), (r'DTS.?X', 'DTS:X'), (r'DTS.?HD.?MA|DTS-HD Master Audio', 'DTS-HD MA'), (r'\bDTS\b', 'DTS'), (r'DDP|EAC3|E-AC-3|DD\+', 'Dolby Digital Plus'), (r'\bAC3\b|\bDD\b', 'Dolby Digital'), (r'\bAAC\b', 'AAC'), (r'\bFLAC\b', 'FLAC')))
    if audio_codec != 'N/A':
        audio_parts.append(audio_codec)
    audio = ' / '.join(audio_parts) or 'N/A'
    channels = re.search(r'(?<!\d)(\d\.\d(?:\.\d)?)(?!\d)', metadata)
    # Combine audio format + channel layout for display (e.g. "Dolby Atmos 7.1", "TrueHD 5.1").
    if audio != 'N/A' and channels:
        audio_display = f'{audio} {channels.group(1)}'
    elif audio != 'N/A':
        audio_display = audio
    elif channels:
        audio_display = channels.group(1)
    else:
        audio_display = 'N/A'
    return (f'{size_gb:.2f} GB' if size_gb is not None and size_gb > 0 else 'N/A', container, video, ' / '.join(hdr) or 'N/A', audio_display, channels.group(1) if channels else 'N/A')

def search_external_streams(title, year, imdb_id, media_type='movie', season=1, episode=1):
    addon_roots = []
    appdata_path = os.environ.get('APPDATA', '')
    if appdata_path:
        addon_roots.append(os.path.join(appdata_path, 'Kodi', 'addons'))
    addon_roots.append(os.path.dirname(os.path.abspath(ADDON_PATH)))
    coco_lib = next((os.path.join(root, 'script.module.cocoscrapers', 'lib')
                     for root in addon_roots
                     if os.path.isdir(os.path.join(root, 'script.module.cocoscrapers', 'lib'))), '')
    if not coco_lib:
        raise RuntimeError('CocoScrapers module is not installed')
    if coco_lib not in sys.path:
        sys.path.insert(0, coco_lib)
    from cocoscrapers import sources as coco_sources

    data = {'title': title, 'year': str(year or '0'), 'imdb': imdb_id, 'aliases': [], 'timeout': 7}
    if media_type == 'tv':
        data.update({'tvshowtitle': title, 'season': str(season), 'episode': str(episode)})

    streams, seen, failures = [], set(), []
    providers = coco_sources(['torrents'])
    for provider, scraper in providers:
        try:
            # CocoScrapers returns scraper classes; POV instantiates each source before calling it.
            provider_results = scraper().sources(data, {})
            xbmc.log(f'[RyuStream] external scraper {provider}: {len(provider_results or [])} results', xbmc.LOGINFO)
            for result in provider_results or []:
                url = result.get('url', '')
                if not url.startswith('magnet:?'):
                    continue
                info_hash = result.get('hash') or url.lower()
                if info_hash in seen:
                    continue
                seen.add(info_hash)
                name = result.get('name') or url
                quality = result.get('quality') or ''
                site_provider = result.get('provider') or provider
                label = f"[EXTERNAL / {provider} / {site_provider}] {name}"
                if quality:
                    label += f" | {quality}"
                streams.append({
                    'label': label[:220], 'magnet': url, 'quality': quality, 'name': name,
                    'source_type': 'External', 'scraper': str(provider), 'site_provider': str(site_provider),
                    'size': result.get('size'), 'info': result.get('info') or result.get('quality')
                })
        except Exception as e:
            failures.append((provider, str(e)))
            xbmc.log(f'[RyuStream] external scraper {provider} failed: {e}', xbmc.LOGWARNING)
    if not providers:
        raise RuntimeError('CocoScrapers has no enabled torrent providers')
    if not streams and failures and len(failures) == len(providers):
        raise RuntimeError('All CocoScrapers providers failed: ' + failures[0][1])
    return streams


class ScrapeBackdropDialog(xbmcgui.WindowDialog):
    def __init__(self, title, backdrop):
        super().__init__()
        white_tex = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', 'media', 'white.png')
        self.status = xbmcgui.ControlLabel(150, 390, 1620, 70, '', font='font16', textColor='FFFFFFFF', alignment=2)
        self.details = xbmcgui.ControlLabel(150, 480, 1620, 90, '', font='font14', textColor='FF10E6BC', alignment=2)
        self.addControl(xbmcgui.ControlImage(0, 0, 1920, 1080, backdrop or white_tex, aspectRatio=0))
        self.addControl(xbmcgui.ControlImage(0, 0, 1920, 1080, white_tex, colorDiffuse='AA07111F'))
        self.addControl(xbmcgui.ControlLabel(160, 300, 1600, 60, f'[B]{title}[/B]', font='font16', textColor='FFFFFFFF', alignment=2))
        self.addControl(self.status)
        self.addControl(self.details)

    def update_status(self, status, details=''):
        self.status.setLabel(status)
        self.details.setLabel(details)


class StreamPickerDialog(xbmcgui.WindowDialog):
    def __init__(self, title, backdrop, counts, streams, total_count):
        super().__init__()
        self.title = title
        self.backdrop = backdrop
        self.streams = streams
        self.selected_index = -1
        self.closed = False
        white_tex = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', 'media', 'white.png')

        self.addControl(xbmcgui.ControlImage(0, 0, 1920, 1080, backdrop or white_tex))
        self.addControl(xbmcgui.ControlImage(0, 0, 1920, 1080, white_tex, colorDiffuse='C907111E'))
        self.addControl(xbmcgui.ControlImage(0, 0, 1920, 150, white_tex, colorDiffuse='E607111E'))
        self.addControl(xbmcgui.ControlLabel(90, 30, 1500, 58, f'[B]{title}[/B]', font='font16', textColor='FFFFFFFF'))
        self.addControl(xbmcgui.ControlLabel(90, 96, 1500, 34, 'AVAILABLE STREAM QUALITY', font='font12', textColor='FF10E6BC'))

        self.addControl(xbmcgui.ControlImage(70, 175, 340, 710, white_tex, colorDiffuse='A807111E'))
        self.addControl(xbmcgui.ControlLabel(95, 210, 290, 44, f"4K UHD     {counts['4K']}", font='font14', textColor='FF10E6BC'))
        self.addControl(xbmcgui.ControlLabel(95, 275, 290, 44, f"1080p HD   {counts['1080p']}", font='font14', textColor='FFFFFFFF'))
        self.addControl(xbmcgui.ControlLabel(95, 340, 290, 44, f"720p HD    {counts['720p']}", font='font14', textColor='FFFFFFFF'))
        self.addControl(xbmcgui.ControlLabel(95, 420, 290, 110, f'{total_count} streams found', font='font13', textColor='FFCBD5E1'))

        self.page_start = 0
        self.page_size = 9
        self.row_top = 175
        self.row_height = 82
        self.row_buttons = []
        self.row_labels = []
        self.addControl(xbmcgui.ControlImage(430, 160, 1440, 770, white_tex, colorDiffuse='9907111E'))
        self.focus_background = xbmcgui.ControlImage(445, self.row_top, 1410, 72, white_tex, colorDiffuse='6610B981')
        self.addControl(self.focus_background)
        for row in range(self.page_size):
            y = self.row_top + row * self.row_height
            button = xbmcgui.ControlButton(
                445, y, 1410, 72, '', '', '', 0, 0, 0, 'font13',
                '0x00FFFFFF', '0x00FFFFFF', 0, '0x00000000', '0x00FFFFFF'
            )
            self.row_buttons.append(button)
            self.addControl(button)
            label = xbmcgui.ControlLabel(465, y + 4, 1360, 66, '', font='font13', textColor='FFE2E8F0')
            self.row_labels.append(label)
            self.addControl(label)
        self.page_label = xbmcgui.ControlLabel(430, 940, 1440, 42, '', font='font12', textColor='FF10E6BC', alignment=2)
        self.addControl(self.page_label)
        self.addControl(xbmcgui.ControlLabel(90, 1010, 1760, 34, 'UP / DOWN to browse   |   ENTER to select   |   BACK to cancel', font='font12', textColor='FFCBD5E1'))

    def onInit(self):
        # The native Kodi select dialog owns stream navigation and selection.
        # Keep the fanart backdrop while hiding the custom controls behind it.
        self.focus_background.setVisible(False)
        self.page_label.setVisible(False)
        for label, button in zip(self.row_labels, self.row_buttons):
            label.setVisible(False)
            button.setVisible(False)

    def choose(self):
        self.show()
        try:
            labels = [str(stream.get('label') or stream.get('name') or 'Stream') for stream in self.streams]
            shown_counts = quality_counts(self.streams)
            heading = (
                f"Select Stream ({len(labels)} shown) | 4K {shown_counts['4K']}"
                f" | 1080p {shown_counts['1080p']} | 720p {shown_counts['720p']}"
            )
            xbmc.log(
                f"[RyuStream] opening native stream chooser items={len(labels)} "
                f"first={labels[0][:120] if labels else 'none'}",
                xbmc.LOGINFO
            )
            return xbmcgui.Dialog().select(heading, labels, useDetails=True)
        finally:
            self.close()

    def _render_page(self, page, focus_row=0):
        page_count = max(1, (len(self.streams) + self.page_size - 1) // self.page_size)
        self.page_start = max(0, min(page, page_count - 1)) * self.page_size
        visible_rows = min(self.page_size, max(0, len(self.streams) - self.page_start))
        for row, (label, button) in enumerate(zip(self.row_labels, self.row_buttons)):
            visible = row < visible_rows
            label.setVisible(visible)
            button.setVisible(visible)
            if visible:
                label.setLabel(str(self.streams[self.page_start + row].get('label', 'Stream'))[:150])
                button.controlUp(self.row_buttons[row - 1] if row > 0 else button)
                button.controlDown(self.row_buttons[row + 1] if row + 1 < visible_rows else button)
        self.page_label.setLabel(f'Page {self.page_start // self.page_size + 1} / {page_count}    •    Page Up / Page Down to change page')
        if visible_rows:
            focus_row = max(0, min(focus_row, visible_rows - 1))
            self.focus_background.setPosition(445, self.row_top + focus_row * self.row_height)
            self.setFocus(self.row_buttons[focus_row])

    def _choose_focused_stream(self):
        if self.closed:
            return
        try:
            row = self.row_buttons.index(self.getFocus())
        except (ValueError, RuntimeError):
            return
        index = self.page_start + row
        if index < len(self.streams):
            self.selected_index = index
            self.closed = True
            self.close()

    def onFocus(self, control_id):
        try:
            row = self.row_buttons.index(self.getFocus())
            self.focus_background.setPosition(445, self.row_top + row * self.row_height)
        except (ValueError, RuntimeError):
            pass

    def onClick(self, control_id):
        self._choose_focused_stream()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (10, 13, 92) or action.getButtonCode() in (27, 61467, 61448):
            self.selected_index = -1
            self.closed = True
            self.close()
        elif action_id == 7:
            self._choose_focused_stream()
        elif action_id in (3, 4):
            try:
                row = self.row_buttons.index(self.getFocus())
            except (ValueError, RuntimeError):
                return
            visible_rows = min(self.page_size, len(self.streams) - self.page_start)
            if action_id == 4 and row == visible_rows - 1 and self.page_start + visible_rows < len(self.streams):
                self._render_page(self.page_start // self.page_size + 1, 0)
            elif action_id == 3 and row == 0 and self.page_start > 0:
                self._render_page(self.page_start // self.page_size - 1, self.page_size - 1)
        elif action_id == 5 and self.page_start > 0:
            self._render_page(self.page_start // self.page_size - 1, self.page_size - 1)
        elif action_id == 6 and self.page_start + self.page_size < len(self.streams):
            self._render_page(self.page_start // self.page_size + 1, 0)

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
        self.addControl(xbmcgui.ControlImage(528, 228, 224, 224, qr_img_url))
        self.addControl(xbmcgui.ControlLabel(360, 515, 560, 30, f"URL: [COLOR cyan]{display_url}[/COLOR]", font='font12', textColor='FFFFFFFF', alignment=2))
        self.addControl(xbmcgui.ControlLabel(360, 545, 560, 35, f"CODE: [COLOR lime][B]{pin_code}[/B][/COLOR]", font='font14', textColor='FFFFFFFF', alignment=2))
        self.addControl(xbmcgui.ControlLabel(360, 595, 560, 30, "Press ESC or Back to Close", font='font12', textColor='FF94A3B8', alignment=2))

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (10, 13, 92, 110, 122) or action.getButtonCode() in (27, 61467, 61448):
            self.cancelled = True
            self.close()

def authorize_service(srv_id, name, setting_id, on_success=None):
    monitor = xbmc.Monitor()
    try:
        if srv_id not in ('support', 'coffee') and setting_id:
            current_credential = ADDON.getSetting(setting_id).strip()
            if current_credential:
                confirmed = xbmcgui.Dialog().yesno(
                    f"Reauthorize {name}?",
                    f"{name} is already connected. Reauthorize and replace the saved credential?"
                )
                if not confirmed:
                    return

        if srv_id in ('support', 'coffee'):
            coffee_url = "https://" + "buymeacoffee.com" + "/ryustream"
            qr_dlg = QRAuthDialog('Support RyuStream Developer', coffee_url, 'buymeacoffee.com', 'THANK YOU!')
            qr_dlg.show()
            while not qr_dlg.cancelled and not monitor.abortRequested():
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
                error = data.get('error') or res.get('error') or 'Authorization service returned no PIN'
                raise RuntimeError(f"AllDebrid: {error}")
            qr_dlg = QRAuthDialog('Authorize AllDebrid', user_url, base_url, pin)
            qr_dlg.show()
            for _ in range(60):
                if qr_dlg.cancelled or monitor.abortRequested():
                    break
                xbmc.sleep(2000)
                chk_url = "https://" + "api." + "alldebrid.com" + f"/v4/pin/check?agent=RyuStream&check={check}&pin={pin}"
                chk = SESSION.get(chk_url, headers={'User-Agent': 'RyuStream/3.0.0'}, timeout=8).json()
                if chk.get('data', dict()).get('activated'):
                    ADDON.setSetting('ad_apikey', chk.get('data', dict()).get('apikey', ''))
                    qr_dlg.close()
                    xbmcgui.Dialog().notification('AllDebrid', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                    if on_success:
                        on_success()
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
                error = res.get('error') or res.get('error_description') or 'Authorization service returned no device code'
                raise RuntimeError(f"Real-Debrid: {error}")
            qr_dlg = QRAuthDialog('Authorize Real-Debrid', direct_url, ver_url, user_code)
            qr_dlg.show()
            for _ in range(60):
                if qr_dlg.cancelled or monitor.abortRequested():
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
                        if on_success:
                            on_success()
                        return
            qr_dlg.close()

        elif srv_id == 'premiumize':
            url = "https://" + "www.premiumize.me/token"
            res = SESSION.post(url, data={'client_id': PM_CLIENT_ID, 'response_type': 'device_code'}, timeout=10).json()
            dev_code = res.get('device_code', '')
            user_code = res.get('user_code', '')
            ver_url = res.get('verification_uri') or ("https://" + "www.premiumize.me/device")
            if not dev_code:
                error = res.get('error') or 'Authorization service returned no device code'
                raise RuntimeError(f"Premiumize: {error}")
            qr_dlg = QRAuthDialog('Authorize Premiumize', f"{ver_url}?user_code={user_code}", ver_url, user_code)
            qr_dlg.show()
            for _ in range(60):
                if qr_dlg.cancelled or monitor.abortRequested():
                    break
                xbmc.sleep(2500)
                tok_res = SESSION.post(url, data={'client_id': PM_CLIENT_ID, 'code': dev_code, 'grant_type': 'device_code'}, timeout=8).json()
                if tok_res.get('access_token'):
                    ADDON.setSetting('pm_apikey', tok_res['access_token'])
                    qr_dlg.close()
                    xbmcgui.Dialog().notification('Premiumize', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                    if on_success:
                        on_success()
                    return
            qr_dlg.close()

        elif srv_id == 'trakt':
            t_headers = {'Content-Type': 'application/json', 'trakt-api-version': '2', 'trakt-api-key': TRAKT_CLIENT_ID}
            url = "https://" + "api.trakt.tv/oauth/device/code"
            r = SESSION.post(url, json={'client_id': TRAKT_CLIENT_ID}, headers=t_headers, timeout=10)
            if not r.ok:
                raise RuntimeError(f"Trakt: HTTP {r.status_code}")
            res = r.json()
            dev_code = res.get('device_code', '')
            user_code = res.get('user_code', '')
            ver_url = res.get('verification_url') or ("https://" + "trakt.tv/activate")
            if not dev_code:
                raise RuntimeError("Trakt: Authorization service returned no device code")
            qr_dlg = QRAuthDialog('Authorize Trakt.tv', f"{ver_url}?code={user_code}", ver_url, user_code)
            qr_dlg.show()
            for _ in range(60):
                if qr_dlg.cancelled or monitor.abortRequested():
                    break
                xbmc.sleep(3000)
                tok_url = "https://" + "api.trakt.tv/oauth/device/token"
                tok_res = SESSION.post(tok_url, json={'code': dev_code, 'client_id': TRAKT_CLIENT_ID}, headers=t_headers, timeout=8)
                if tok_res.status_code == 200:
                    ADDON.setSetting('trakt_token', tok_res.json().get('access_token', ''))
                    qr_dlg.close()
                    xbmcgui.Dialog().notification('Trakt.tv', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                    if on_success:
                        on_success()
                    return
            qr_dlg.close()

        elif srv_id == 'simkl':
            url = "https://" + "api." + "simkl.com" + f"/oauth/pin?client_id={SIMKL_CLIENT_ID}"
            res = SESSION.get(url, timeout=10).json()
            user_code = res.get('user_code', '')
            ver_url = res.get('verification_url') or ("https://" + "simkl.com" + "/pin")
            if not user_code:
                raise RuntimeError("Simkl: Authorization service returned no PIN")
            qr_dlg = QRAuthDialog('Authorize Simkl', f"{ver_url}/{user_code}", ver_url, user_code)
            qr_dlg.show()
            for _ in range(60):
                if qr_dlg.cancelled or monitor.abortRequested():
                    break
                xbmc.sleep(3000)
                chk_url = "https://" + "api." + "simkl.com" + f"/oauth/pin/{user_code}?client_id={SIMKL_CLIENT_ID}"
                chk = SESSION.get(chk_url, timeout=8).json()
                if chk.get('result') == 'OK' and chk.get('access_token'):
                    ADDON.setSetting('simkl_token', chk['access_token'])
                    qr_dlg.close()
                    xbmcgui.Dialog().notification('Simkl', 'Connected via QR Code!', xbmcgui.NOTIFICATION_INFO, 4000)
                    if on_success:
                        on_success()
                    return
            qr_dlg.close()

        elif srv_id in ('torbox', 'mdblist'):
            web_url = "https://" + ("torbox.app/settings" if srv_id == 'torbox' else "mdblist.com")
            qr_dlg = QRAuthDialog(f"{name} API Key", web_url, web_url, "SCAN QR -> COPY API KEY")
            qr_dlg.show()
            while not qr_dlg.cancelled and not monitor.abortRequested():
                xbmc.sleep(500)
            qr_dlg.close()
            if monitor.abortRequested():
                return
            current = ADDON.getSetting(setting_id)
            kb = xbmc.Keyboard(current, f"Paste {name} API Key")
            kb.doModal()
            if kb.isConfirmed():
                ADDON.setSetting(setting_id, kb.getText().strip())
                xbmcgui.Dialog().notification(name, 'Saved Successfully!', xbmcgui.NOTIFICATION_INFO, 3000)
                if on_success:
                    on_success()

    except Exception as e:
        xbmcgui.Dialog().notification(f'{name} Error', str(e)[:60], xbmcgui.NOTIFICATION_ERROR, 5000)

class RyuHomeWindow(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loaded = False
        self.row1_mode = 'catalog'
        self.quality_mode = ADDON.getSetting('quality_filter') or 'All (4K / 1080p / 720p)'

    def onInit(self):
        xbmc.executebuiltin('Dialog.Close(busydialog)')
        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        try:
            self.getControl(102).setLabel(f"v{ADDON.getAddonInfo('version')}")
        except Exception:
            pass
        self.update_quality_button()
        if not self.loaded:
            self.loaded = True
            self.load_default_rows()

    def update_quality_button(self):
        pass

    def focus_panel_or_home(self, panel_id=5001):
        try:
            panel = self.getControl(panel_id)
            if panel.size() > 0:
                self.setFocusId(panel_id)
                return
        except Exception:
            pass
        self.setFocusId(901)

    def set_cloud_mode(self, enabled):
        if enabled:
            self.setProperty('cloud_mode', 'true')
        else:
            self.clearProperty('cloud_mode')
        self.getControl(8011).setVisible(not enabled)
        self.getControl(8010).setVisible(enabled)
        self.getControl(8012).setVisible(not enabled)
        self.set_provider_logos_visible(enabled)

    def set_provider_logos_visible(self, visible):
        for control_id in range(7001, 7017):
            try:
                self.getControl(control_id).setVisible(visible)
            except Exception:
                pass

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
            li.setProperty('year', year)
            li.setProperty('tmdb_id', str(item.get('id', '')))
            li.setProperty('media_type', m_type)
            set_video_info(li, m_title, int(year) if year.isdigit() else 0, float(rating), plot)
            list_items.append(li)

        if list_items:
            panel.addItems(list_items)

    def load_default_rows(self):
        self.set_cloud_mode(False)
        self.populate_row(5001, 103, 'trending/all/week', 'TRENDING NOW')
        self.populate_row(5002, 104, 'movie/popular', 'POPULAR MOVIES')
        self.populate_row(5003, 105, 'discover/tv', 'POPULAR ANIME & SERIES', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'})
        self.focus_panel_or_home(5001)

    def load_authorize_cards(self):
        self.set_cloud_mode(True)
        media_dir = MEDIA_PATH

        def make_card(srv_id, name, setting_id, img_name, is_key=False):
            val = ADDON.getSetting(setting_id).strip()
            status = "CONNECTED" if val else ("Enter API Key" if is_key else "Scan QR Code")
            icon_p = os.path.join(media_dir, img_name)
            li = xbmcgui.ListItem(label=name, label2=status)
            li.setProperty('srv_id', srv_id)
            li.setProperty('srv_icon', icon_p)
            li.setProperty('srv_name', name)
            li.setProperty('setting_id', setting_id)
            li.setProperty('plot', f"{name} — Status: {status}")
            li.setArt({'poster': icon_p, 'thumb': icon_p, 'icon': icon_p})
            return li

        # แถวที่ 1 (5001): Debrid Cloud Providers (4 การ์ดหลัก)
        row1_cards = [
            make_card('alldebrid', 'AllDebrid', 'ad_apikey', 'alldebrid.png'),
            make_card('realdebrid', 'Real-Debrid', 'rd_apikey', 'realdebrid.png'),
            make_card('torbox', 'TorBox', 'tb_apikey', 'torbox.png'),
            make_card('premiumize', 'Premiumize', 'pm_apikey', 'premiumize.png')
        ]

        # แถวที่ 2 (5002): Watchlist Sync & Buy Me a Coffee (4 การ์ดเห็นพร้อมกันทันที)
        row2_cards = [
            make_card('trakt', 'Trakt', 'trakt_token', 'trakt.png'),
            make_card('simkl', 'Simkl', 'simkl_token', 'simkl.png'),
            make_card('mdblist', 'MDBList', 'mdblist_apikey', 'mdblist.png', is_key=True),
            make_card('coffee', 'Support RyuStream', 'coffee_supporter', 'support.png')
        ]

        self.getControl(603).setLabel("Debrid Cloud Providers  •  Scan QR Code to Authorize")
        p1 = self.getControl(6001)
        p1.reset()
        p1.addItems(row1_cards)

        self.getControl(604).setLabel("Watchlist Sync (Trakt • Simkl • MDBList)  &  Support RyuStream")
        p2 = self.getControl(6002)
        p2.reset()
        p2.addItems(row2_cards)

        # ซ่อนแถวที่ 3 (5003) ชั่วคราวเพื่อให้โฟกัสอยู่แค่ 2 แถวนี้สะอาดตา
        # Keep focus on the Debrid Cloud tab after rendering the provider rows.


    def handle_auth_click(self, srv_id, name, setting_id):
        authorize_service(srv_id, name, setting_id, on_success=self.load_authorize_cards)

    def play_media_item(self, li):
        m_title = li.getLabel()
        tmdb_id = li.getProperty('tmdb_id')
        m_type = li.getProperty('media_type') or 'movie'

        # ตรวจสอบว่าเชื่อมต่อ Debrid อย่างน้อย 1 บริการหรือยัง
        has_debrid = any(
            ADDON.getSetting(k).strip() and ADDON.getSetting(en) != 'false'
            for k, en in (('ad_apikey', 'enable_ad'), ('rd_apikey', 'enable_rd'), ('pm_apikey', 'enable_pm'), ('tb_apikey', 'enable_tb'))
        )
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

        backdrop = li.getArt('fanart') or li.getArt('poster')
        scraper_mode = getattr(self, 'scraper_mode', ADDON.getSetting('scraper_mode') or 'Internal')
        internal_enabled = 'Internal' in scraper_mode or 'Hybrid' in scraper_mode
        external_enabled = 'External' in scraper_mode or 'Hybrid' in scraper_mode
        mode_label = 'Internal + External' if internal_enabled and external_enabled else ('External' if external_enabled else 'Internal')
        scrape_dialog = ScrapeBackdropDialog(m_title, backdrop)
        scrape_dialog.update_status(f'Searching with {mode_label} scraper...', 'Gathering available 4K / 1080p / 720p streams')
        scrape_dialog.show()
        xbmc.sleep(250)

        result = {'streams': [], 'errors': []}

        def scrape():
            try:
                imdb_id = get_imdb_id(tmdb_id, m_type)
                if not imdb_id:
                    raise RuntimeError('Could not find IMDb ID for this title')
                found = []
                if internal_enabled:
                    try:
                        internal_results = search_internal_streams(imdb_id, m_type, season, episode, 'All (4K / 1080p / 720p)')
                        found.extend(internal_results)
                        xbmc.log(f'[RyuStream] internal scraper total={len(internal_results)}', xbmc.LOGINFO)
                    except Exception as e:
                        xbmc.log(f'[RyuStream] internal scraper failed: {e}', xbmc.LOGWARNING)
                        result['errors'].append(f'Internal: {e}')
                if external_enabled:
                    try:
                        external_results = search_external_streams(m_title, li.getProperty('year'), imdb_id, m_type, season, episode)
                        found.extend(external_results)
                        xbmc.log(f'[RyuStream] external scraper total={len(external_results)}', xbmc.LOGINFO)
                    except Exception as e:
                        xbmc.log(f'[RyuStream] external scraper failed: {e}', xbmc.LOGWARNING)
                        result['errors'].append(f'External: {e}')
                unique, seen = [], {}
                for stream in found:
                    magnet = stream.get('magnet', '')
                    if not magnet:
                        continue
                    info_hash = re.search(r'btih:([^&]+)', magnet, re.IGNORECASE)
                    source_type = str(stream.get('source_type', 'Scraper')).lower()
                    dedupe_key = (source_type, info_hash.group(1).lower() if info_hash else magnet.lower())
                    source_name = f"{stream.get('source_type', 'Scraper')} / {stream.get('scraper', 'Unknown')}"
                    if dedupe_key in seen:
                        existing = unique[seen[dedupe_key]]
                        if source_name not in existing['sources']:
                            existing['sources'].append(source_name)
                            existing['label'] = f"[{', '.join(existing['sources'])}] {existing.get('name') or 'Stream'}"[:220]
                    else:
                        stream['sources'] = [source_name]
                        stream['label'] = f"[{source_name}] {stream.get('name') or stream.get('label') or 'Stream'}"[:220]
                        seen[dedupe_key] = len(unique)
                        unique.append(stream)
                result['streams'] = unique
            except Exception as e:
                result['errors'].append(f'Scrape: {e}')

        worker = threading.Thread(target=scrape, name='RyuStreamScraper', daemon=True)
        worker.start()
        while worker.is_alive():
            xbmc.sleep(100)
        all_streams = result['streams']
        cache_checked = mark_real_debrid_cache(all_streams)
        counts = quality_counts(all_streams)
        count_label = f"Available: 4K {counts['4K']}   |   1080p {counts['1080p']}   |   720p {counts['720p']}"
        scrape_dialog.update_status('Scrape complete', count_label)
        xbmc.sleep(1800)
        scrape_dialog.close()
        source_counts = {
            'Internal': sum(1 for item in all_streams if any(source.startswith('Internal /') for source in item.get('sources', []))),
            'External': sum(1 for item in all_streams if any(source.startswith('External /') for source in item.get('sources', [])))
        }
        total_found = len(all_streams)
        filtered_streams = apply_quality_filter(all_streams, self.quality_mode)
        streams = balance_scraper_sources(filtered_streams)
        xbmc.log(f'[RyuStream] picker source mix internal={sum(any(str(src).lower().startswith("internal /") for src in item.get("sources", [])) for item in streams)} external={sum(any(str(src).lower().startswith("external /") for src in item.get("sources", [])) for item in streams)} shown={len(streams)}', xbmc.LOGINFO)

        if not streams:
            message = '; '.join(result['errors']) or 'Try changing Quality Filter to "All"'
            xbmcgui.Dialog().notification('No Streams Found', message[:100], xbmcgui.NOTIFICATION_WARNING, 5000)
            return

        external_status = 'unavailable' if external_enabled and source_counts['External'] == 0 and any(error.startswith('External:') for error in result['errors']) else str(source_counts['External'])
        picker = FullscreenStreamPickerDialog(m_title, backdrop, counts, streams, source_counts, external_status, total_found, all_streams=all_streams, cache_checked=cache_checked)
        sel = picker.choose()
        del picker
        if sel < 0:
            return

        pDlg = xbmcgui.DialogProgress()
        pDlg.create('RyuStream Debrid Engine', 'Resolving high-speed Debrid stream link...')
        pDlg.update(50)
        stream_url = resolve_debrid_magnet(streams[sel]['magnet'])
        pDlg.close()

        if not stream_url:
            xbmcgui.Dialog().notification('Debrid Error', 'Could not resolve stream link (Check Debrid account)', xbmcgui.NOTIFICATION_ERROR, 5000)
            return

        play_li = xbmcgui.ListItem(label=m_title, path=stream_url)
        set_video_info(play_li, m_title)
        manifest_url = ADDON.getSetting('submaker_url').strip()
        if manifest_url:
            try:
                subtitle_imdb_id = get_imdb_id(tmdb_id, m_type)
                selected_stream = streams[sel]
                stream_size = selected_stream.get('size_bytes')
                if stream_size is None and isinstance(selected_stream.get('size'), (int, float)):
                    stream_size = int(float(selected_stream['size']) * (1024 ** 3))
                hash_match = re.search(r'btih:([^&]+)', str(selected_stream.get('magnet') or ''), re.I)
                subtitle_paths = get_submaker_subtitles(
                    manifest_url, m_type, subtitle_imdb_id, season, episode,
                    selected_stream.get('name') or selected_stream.get('label') or m_title,
                    stream_size, hash_match.group(1) if hash_match else ''
                )
                if subtitle_paths:
                    play_li.setSubtitles(subtitle_paths)
                    xbmc.log(f'[RyuStream] attached {len(subtitle_paths)} SubMaker subtitle(s)', xbmc.LOGINFO)
            except Exception as exc:
                xbmc.log(f'[RyuStream] Could not attach SubMaker subtitles: {exc}', xbmc.LOGWARNING)
        # Close the add-on home window before handing playback to Kodi so it
        # cannot remain in front of the video until the user presses Escape.
        self.close()
        xbmc.sleep(150)
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

    def open_search_dialog(self):
        self.set_cloud_mode(False)
        kb = xbmc.Keyboard('', 'Search Movies, TV Shows & Anime')
        kb.doModal()
        if not kb.isConfirmed():
            return
        query = kb.getText().strip()
        if not query:
            return
        self.populate_row(5001, 103, 'search/multi', f'Search Results: {query}', {'query': query})
        self.focus_panel_or_home(5001)

    def open_discover_menu(self):
        self.set_cloud_mode(False)
        try:
            # หน้าเมนูหลักเรียงลงมาเหมือน Homelander / Fen
            main_menu = [
                'Movies',
                'TV Shows',
                'Anime'
            ]
            m_idx = xbmcgui.Dialog().select('Discover', main_menu)
            if m_idx < 0:
                return

            cat_name = main_menu[m_idx]
            sub_menu = [
                'Trending Today',
                'Most Popular',
                'Top Rated',
                'New Releases',
                'Genres',
                'Years'
            ]
            s_idx = xbmcgui.Dialog().select(f'Discover • {cat_name}', sub_menu)
            if s_idx < 0:
                return

            sub_name = sub_menu[s_idx]
            is_movie = (m_idx == 0)
            is_anime = (m_idx == 2)
            m_type = 'movie' if is_movie else 'tv'

            # 1. Trending Today
            if s_idx == 0:
                if is_anime:
                    self.populate_row(5001, 103, 'discover/tv', 'Anime • Trending Today', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'})
                else:
                    self.populate_row(5001, 103, f'trending/{m_type}/day', f'{cat_name} • Trending Today')
                self.focus_panel_or_home(5001)
                return

            # 2. Most Popular / Top Rated / New Releases
            if s_idx in (1, 2, 3):
                sort_map = {
                    1: ('Most Popular', 'popularity.desc'),
                    2: ('Top Rated', 'vote_average.desc'),
                    3: ('New Releases', 'primary_release_date.desc' if is_movie else 'first_air_date.desc')
                }
                lbl, sort_val = sort_map[s_idx]
                params = {'sort_by': sort_val, 'vote_count.gte': 40}
                if is_anime:
                    params['with_genres'] = '16'
                    params['with_original_language'] = 'ja'
                self.populate_row(5001, 103, f'discover/{m_type}', f'{cat_name} • {lbl}', params)
                self.focus_panel_or_home(5001)
                return

            # 3. Genres (เรียงรายการแนวหนังลงมาให้เลือก)
            if s_idx == 4:
                genres = [
                    ('Action & Adventure', '28' if is_movie else '10759'),
                    ('Animation', '16'),
                    ('Comedy', '35'),
                    ('Crime', '80'),
                    ('Documentary', '99'),
                    ('Drama', '18'),
                    ('Family', '10751'),
                    ('Horror & Mystery', '27' if is_movie else '9648'),
                    ('Romance', '10749'),
                    ('Sci-Fi & Fantasy', '878' if is_movie else '10765'),
                    ('Thriller', '53' if is_movie else '10768')
                ]
                g_idx = xbmcgui.Dialog().select(f'{cat_name} • Genres', [g[0] for g in genres])
                if g_idx < 0:
                    return
                g_label, g_id = genres[g_idx]
                params = {'with_genres': g_id, 'sort_by': 'popularity.desc', 'vote_count.gte': 30}
                if is_anime:
                    params['with_genres'] = f'16,{g_id}' if g_id != '16' else '16'
                    params['with_original_language'] = 'ja'
                self.populate_row(5001, 103, f'discover/{m_type}', f'{cat_name} • {g_label}', params)
                self.focus_panel_or_home(5001)
                return

            # 4. Years (เรียงรายการปีลงมาให้เลือก)
            if s_idx == 5:
                years = ['2026', '2025', '2024', '2023', '2022', '2021', '2020', '2019', '2018', '2015']
                y_idx = xbmcgui.Dialog().select(f'{cat_name} • Years', years)
                if y_idx < 0:
                    return
                yr = years[y_idx]
                yr_key = 'primary_release_year' if is_movie else 'first_air_date_year'
                params = {yr_key: yr, 'sort_by': 'popularity.desc', 'vote_count.gte': 20}
                if is_anime:
                    params['with_genres'] = '16'
                    params['with_original_language'] = 'ja'
                self.populate_row(5001, 103, f'discover/{m_type}', f'{cat_name} • Year {yr}', params)
                self.focus_panel_or_home(5001)
                return

        except Exception as e:
            xbmcgui.Dialog().notification('Discover Error', str(e)[:60], xbmcgui.NOTIFICATION_ERROR, 4000)


    def open_cloud_lists_menu(self):
        self.set_cloud_mode(False)
        if not hasattr(self, '_lists_cache'):
            self._lists_cache = dict()

        trakt_tok = ADDON.getSetting('trakt_token').strip()
        simkl_tok = ADDON.getSetting('simkl_token').strip()
        mdb_key = ADDON.getSetting('mdblist_apikey').strip()

        choices, actions = list(), list()
        if trakt_tok:
            choices.extend(['[Trakt] Watchlist (Movies)', '[Trakt] Watchlist (TV Shows)', '[Trakt] My Custom Lists'])
            actions.extend([('trakt', 'w_movie'), ('trakt', 'w_tv'), ('trakt', 'custom')])
        if simkl_tok:
            choices.extend(['[Simkl] Plan to Watch (Anime)', '[Simkl] Plan to Watch (TV Shows)', '[Simkl] Plan to Watch (Movies)', '[Simkl] Currently Watching'])
            actions.extend([('simkl', 'anime'), ('simkl', 'shows'), ('simkl', 'movies'), ('simkl', 'watching')])
        if mdb_key:
            choices.append('[MDBList] Watchlist & Custom Lists')
            actions.append(('mdblist', 'all'))

        if not choices:
            xbmcgui.Dialog().notification('My Lists', 'Connect Trakt, Simkl, or MDBList in Debrid Cloud first!', xbmcgui.NOTIFICATION_WARNING, 4000)
            return

        choices.append('[Refresh & Clear Cache]')
        actions.append(('refresh', 'clear'))

        idx = xbmcgui.Dialog().select('My Cloud Lists (Trakt • Simkl • MDBList)', choices)
        if idx < 0:
            return
        prov, mode = actions[idx]
        if prov == 'refresh':
            self._lists_cache.clear()
            xbmcgui.Dialog().notification('My Lists', 'Cache cleared!', xbmcgui.NOTIFICATION_INFO, 2000)
            return

        cache_key = f"{prov}_{mode}"
        if cache_key in self._lists_cache and mode not in ('custom', 'all'):
            items, lbl = self._lists_cache[cache_key]
            self.getControl(103).setLabel(lbl)
            p = self.getControl(5001)
            p.reset()
            p.addItems(items)
            self.focus_panel_or_home(5001)
            return

        s = chr(47)
        raw_entries = list()
        header_lbl = choices[idx]
        pDlg = xbmcgui.DialogProgress()
        pDlg.create('RyuStream Cloud Lists', f'Loading {header_lbl}...')
        pDlg.update(35)

        try:
            if prov == 'trakt':
                t_base = "https:" + s + s + "api." + "trakt.tv"
                cid = ADDON.getSetting('trakt_client_id').strip() or TRAKT_CLIENT_ID
                hdr = {'Content-Type': 'application/json', 'trakt-api-version': '2', 'trakt-api-key': cid, 'Authorization': f'Bearer {trakt_tok}'}
                if mode == 'w_movie':
                    for x in SESSION.get(f"{t_base}/sync/watchlist/movies", headers=hdr, timeout=8).json():
                        m = x.get('movie', dict())
                        if m.get('ids', {}).get('tmdb'):
                            raw_entries.append((m['ids']['tmdb'], 'movie', m.get('title', '')))
                elif mode == 'w_tv':
                    for x in SESSION.get(f"{t_base}/sync/watchlist/shows", headers=hdr, timeout=8).json():
                        sh = x.get('show', dict())
                        if sh.get('ids', {}).get('tmdb'):
                            raw_entries.append((sh['ids']['tmdb'], 'tv', sh.get('title', '')))
                elif mode == 'custom':
                    pDlg.close()
                    ulists = SESSION.get(f"{t_base}/users/me/lists", headers=hdr, timeout=8).json()
                    if not isinstance(ulists, list) or not ulists:
                        xbmcgui.Dialog().notification('Trakt', 'No custom lists found', xbmcgui.NOTIFICATION_INFO, 3000)
                        return
                    li_idx = xbmcgui.Dialog().select('Select Trakt List', [u.get('name', 'List') for u in ulists])
                    if li_idx < 0:
                        return
                    slug = ulists[li_idx].get('ids', {}).get('slug')
                    header_lbl = f"Trakt: {ulists[li_idx].get('name')}"
                    pDlg.create('RyuStream Cloud Lists', f'Loading {header_lbl}...')
                    for x in SESSION.get(f"{t_base}/users/me/lists/{slug}/items", headers=hdr, timeout=8).json():
                        if 'movie' in x and x['movie'].get('ids', {}).get('tmdb'):
                            raw_entries.append((x['movie']['ids']['tmdb'], 'movie', x['movie'].get('title', '')))
                        elif 'show' in x and x['show'].get('ids', {}).get('tmdb'):
                            raw_entries.append((x['show']['ids']['tmdb'], 'tv', x['show'].get('title', '')))

            elif prov == 'simkl':
                s_base = "https:" + s + s + "api." + "simkl.com"
                scid = ADDON.getSetting('simkl_client_id').strip() or SIMKL_CLIENT_ID
                shdr = {'Authorization': f'Bearer {simkl_tok}', 'simkl-api-key': scid}
                ep_map = {
                    'anime': ('/sync/all-items/anime/plantowatch', 'anime', 'tv'),
                    'shows': ('/sync/all-items/shows/plantowatch', 'shows', 'tv'),
                    'movies': ('/sync/all-items/movies/plantowatch', 'movies', 'movie'),
                    'watching': ('/sync/all-items/watching', 'shows', 'tv')
                }
                ep, kname, mt = ep_map[mode]
                data = SESSION.get(f"{s_base}{ep}", headers=shdr, timeout=8).json()
                for x in (data.get(kname, list()) if isinstance(data, dict) else list()):
                    obj = x.get('show') or x.get('movie') or dict()
                    if obj.get('ids', {}).get('tmdb'):
                        raw_entries.append((obj['ids']['tmdb'], mt, obj.get('title', '')))

            elif prov == 'mdblist':
                pDlg.close()
                m_base = "https:" + s + s + "mdblist.com" + s + "api"
                ulists = SESSION.get(f"{m_base}/lists/user", params={'apikey': mdb_key}, timeout=8).json()
                if not isinstance(ulists, list) or not ulists:
                    xbmcgui.Dialog().notification('MDBList', 'No lists found on MDBList', xbmcgui.NOTIFICATION_INFO, 3000)
                    return
                li_idx = xbmcgui.Dialog().select('Select MDBList Watchlist / List', [f"{u.get('name')} ({u.get('items', 0)})" for u in ulists])
                if li_idx < 0:
                    return
                lid = ulists[li_idx].get('id')
                header_lbl = f"MDBList: {ulists[li_idx].get('name')}"
                pDlg.create('RyuStream Cloud Lists', f'Loading {header_lbl}...')
                ritems = SESSION.get(f"{m_base}/lists/{lid}/items", params={'apikey': mdb_key}, timeout=8).json()
                comb = (ritems.get('movies', list()) if isinstance(ritems, dict) else list()) + (ritems.get('shows', list()) if isinstance(ritems, dict) else list())
                for it in comb:
                    tid = it.get('id') or it.get('tmdb_id')
                    mt = 'tv' if it.get('mediatype') == 'show' else 'movie'
                    if tid:
                        raw_entries.append((tid, mt, it.get('title', '')))

            pDlg.update(70, 'Loading Posters in Parallel...')
            built_cards = list()

            def build_card(entry):
                tid, mt, ftitle = entry
                info = fetch_tmdb_detail(mt, tid)
                if not info:
                    return None
                title = info.get('title') or info.get('name') or ftitle
                year = str((info.get('release_date') or info.get('first_air_date') or '')[:4])
                rating = str(round(float(info.get('vote_average', 0) or 0), 1))
                plot = info.get('overview') or 'No overview available.'
                poster = f"https:{s}{s}image.tmdb.org{s}t{s}p{s}w500{info['poster_path']}" if info.get('poster_path') else ''
                fanart = f"https:{s}{s}image.tmdb.org{s}t{s}p{s}w1280{info['backdrop_path']}" if info.get('backdrop_path') else ''
                li = xbmcgui.ListItem(label=title)
                li.setProperty('tmdb_id', str(tid))
                li.setProperty('media_type', mt)
                li.setProperty('year', year)
                li.setProperty('subtitle', f"TMDb {rating}   |   {year}")
                li.setProperty('plot', plot)
                li.setArt({'poster': poster, 'thumb': poster, 'icon': poster, 'fanart': fanart})
                set_video_info(li, title, int(year) if year.isdigit() else 0, float(rating), plot)
                return li

            with ThreadPoolExecutor(max_workers=6) as ex:
                for c in ex.map(build_card, raw_entries[:30]):
                    if c:
                        built_cards.append(c)

            pDlg.close()
            if built_cards:
                self._lists_cache[cache_key] = (built_cards, header_lbl)
                self.getControl(103).setLabel(header_lbl)
                p = self.getControl(5001)
                p.reset()
                p.addItems(built_cards)
                self.focus_panel_or_home(5001)
            else:
                xbmcgui.Dialog().notification('My Lists', 'This list is empty', xbmcgui.NOTIFICATION_INFO, 3000)
        except Exception as e:
            pDlg.close()
            xbmcgui.Dialog().notification('Lists Error', str(e)[:55], xbmcgui.NOTIFICATION_ERROR, 3500)

    def select_scraper_mode(self):
        modes = ['Internal (Torrentio + MediaFusion)', 'External (CocoScrapers / POV)', 'Hybrid (Internal + External)']
        idx = xbmcgui.Dialog().select('Select Scraper Engine', modes)
        if idx >= 0:
            self.scraper_mode = modes[idx]
            ADDON.setSetting('scraper_mode', self.scraper_mode)
            xbmcgui.Dialog().notification('Scraper Engine', self.scraper_mode, xbmcgui.NOTIFICATION_INFO, 2500)

    def open_more_menu(self):
        self.set_cloud_mode(False)
        q_cur = (getattr(self, 'quality_mode', '') or ADDON.getSetting('quality_filter') or 'All').split(' ')[0]
        s_cur = (getattr(self, 'scraper_mode', '') or ADDON.getSetting('scraper_mode') or 'Internal').split(' ')[0]
        tiles = [
            ('Discover', 'Movies • Series • Anime', 'tile_discover_v4.png', 'discover'),
            ('My Lists', 'Trakt • Simkl • MDBList', 'tile_lists_v4.png', 'lists'),
            ('Anime Hub', 'Trending Japanese Anime', 'tile_anime_v4.png', 'anime'),
            (f'Scraper: {s_cur}', 'Switch Internal / External', 'tile_scraper_v4.png', 'scraper'),
            (f'Quality: {q_cur}', 'Filter 4K / 1080p / 720p', 'tile_quality_v4.png', 'quality')
        ]
        built = list()
        for title, sub, icon_fn, act in tiles:
            icon_path = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', 'media', icon_fn)
            li = xbmcgui.ListItem(label=title, label2=sub)
            li.setProperty('more_action', act)
            li.setProperty('plot', f"{title} — {sub}")
            li.setArt({'poster': icon_path, 'thumb': icon_path, 'icon': icon_path})
            built.append(li)

        self.getControl(103).setLabel("More  •  Tools, Lists & Preferences")
        panel = self.getControl(5001)
        panel.reset()
        panel.addItems(built)
        self.focus_panel_or_home(5001)

    def handle_more_tile(self, act):
        if act == 'discover':
            self.open_discover_menu()
        elif act == 'lists':
            self.open_cloud_lists_menu()
        elif act == 'anime':
            self.populate_row(5001, 103, 'discover/tv', 'Anime Hub • Trending Japanese Anime', {'with_genres': '16', 'with_original_language': 'ja', 'sort_by': 'popularity.desc'})
            self.focus_panel_or_home(5001)
        elif act == 'scraper':
            self.select_scraper_mode()
            self.open_more_menu()
        elif act == 'quality':
            self.select_quality_filter()
            self.open_more_menu()

    def onClick(self, controlId):
        try:
            if controlId == 901:
                self.load_default_rows()
                self.focus_panel_or_home(5001)
            elif controlId == 904:
                self.open_search_dialog()
            elif controlId == 902:
                self.load_authorize_cards()
                self.setFocusId(902)
            elif controlId == 910:
                self.open_more_menu()
            elif controlId == 911:
                ADDON.openSettings()
                self.setFocusId(911)
            elif controlId == 909:
                self.close()
            elif 7001 <= controlId <= 7016:
                providers = [
                    ('alldebrid', 'AllDebrid', 'ad_apikey'),
                    ('realdebrid', 'Real-Debrid', 'rd_apikey'),
                    ('torbox', 'TorBox', 'tb_apikey'),
                    ('premiumize', 'Premiumize', 'pm_apikey'),
                    ('trakt', 'Trakt', 'trakt_token'),
                    ('simkl', 'Simkl', 'simkl_token'),
                    ('mdblist', 'MDBList', 'mdblist_apikey'),
                    ('coffee', 'Support RyuStream', 'coffee_supporter'),
                ]
                provider_index = controlId - 7001 if controlId <= 7008 else controlId - 7009
                provider = providers[provider_index]
                xbmc.log(f"[RyuStream] provider control={controlId} id={provider[0]}", xbmc.LOGINFO)
                xbmcgui.Dialog().notification('RyuStream', f"{provider[1]} selected", xbmcgui.NOTIFICATION_INFO, 1500)
                self.handle_auth_click(*provider)
            elif controlId in (5001, 5002, 5003, 6001, 6002):
                panel = self.getControl(controlId)
                li = panel.getSelectedItem()
                if not li:
                    return
                more_act = li.getProperty('more_action')
                srv_id = li.getProperty('srv_id')
                if more_act:
                    self.handle_more_tile(more_act)
                elif srv_id:
                    self.handle_auth_click(srv_id, li.getProperty('srv_name') or li.getLabel(), li.getProperty('setting_id'))
                else:
                    self.play_media_item(li)
        except Exception as e:
            xbmc.log(f'[RyuStream] menu click failed control={controlId}: {e}', xbmc.LOGERROR)
            xbmcgui.Dialog().notification('Menu Error', str(e)[:60], xbmcgui.NOTIFICATION_ERROR, 4000)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (10, 13, 92, 110, 122) or action.getButtonCode() in (27, 61467, 61448):
            self.close()


class FullscreenStreamPickerDialog(xbmcgui.WindowDialog):
    def __init__(self, title, backdrop, counts, streams, source_counts, external_status, total_found, all_streams=None, cache_checked=False):
        super().__init__()
        self.streams = streams
        self.source_counts = source_counts
        self.external_status = external_status
        self.total_found = total_found
        self.cache_checked = cache_checked
        self.selected_index = -1
        self.screen_w = xbmcgui.getScreenWidth()
        self.screen_h = xbmcgui.getScreenHeight()
        self.sx = self.screen_w / 1920.0
        self.sy = self.screen_h / 1080.0
        self.list_x = int(24 * self.sx)
        self.list_y = int(165 * self.sy)
        self.list_w = self.screen_w - int(48 * self.sx)
        self.list_h = int(790 * self.sy)
        self.item_h = int(108 * self.sy)
        self.item_space = int(8 * self.sy)
        self.row_pitch = self.item_h + self.item_space
        visible_rows = max(2, int(self.list_h // max(1, self.row_pitch)))
        self.visible_rows = visible_rows
        self.viewport_start = 0
        self.page_step = max(1, self.visible_rows - 1)
        self.last_page_action = 0.0
        white_tex = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', 'media', 'white.png')
        # Keep focus off the native ControlList; it otherwise applies its own page movement
        # in addition to the explicit one-item navigation handled below.
        self.navigation_trap = xbmcgui.ControlButton(
            0, 0, 2, 2, '', '', '', 0, 0, 0, 'font13',
            '0x00000000', '0x00000000', 0, '0x00000000', '0x00000000'
        )
        self.addControl(self.navigation_trap)
        self.addControl(xbmcgui.ControlImage(0, 0, self.screen_w, self.screen_h, backdrop or white_tex))
        self.addControl(xbmcgui.ControlImage(0, 0, self.screen_w, self.screen_h, white_tex, colorDiffuse='B807111E'))
        self.addControl(xbmcgui.ControlImage(0, 0, self.screen_w, int(145 * self.sy), white_tex, colorDiffuse='E607111E'))
        summary_w = min(int(1400 * self.sx), int(self.screen_w * 0.74))
        summary_x = self.screen_w - int(24 * self.sx) - summary_w
        self.addControl(xbmcgui.ControlLabel(
            int(18 * self.sx), int(25 * self.sy), summary_x - int(38 * self.sx), int(58 * self.sy),
            f'[B]{title}[/B]', font='font16', textColor='FFFFFFFF'
        ))
        self.focus_band = xbmcgui.ControlImage(self.list_x, self.list_y, self.list_w, self.item_h, white_tex, colorDiffuse='5510E6BC')
        self.addControl(self.focus_band)
        self.list_control = xbmcgui.ControlList(
            self.list_x, self.list_y, self.list_w, self.list_h,
            'font14', '0xFFE2E8F0', '', '', '0xFF10E6BC',
            0, 0, int(8 * self.sx), 0, self.item_h, self.item_space, 4
        )
        self.addControl(self.list_control)
        self.focus_top = xbmcgui.ControlImage(self.list_x, self.list_y, self.list_w, max(3, int(4 * self.sy)), white_tex, colorDiffuse='FF10E6BC')
        self.focus_bottom = xbmcgui.ControlImage(self.list_x, self.list_y + self.item_h - max(3, int(4 * self.sy)), self.list_w, max(3, int(4 * self.sy)), white_tex, colorDiffuse='FF10E6BC')
        self.addControl(self.focus_top)
        self.addControl(self.focus_bottom)
        self.list_items = []
        for stream in streams:
            source_tags = {'internal': [], 'external': [], 'other': []}
            for source in stream.get('sources', []):
                source_text = str(source)
                lowered_source = source_text.lower()
                if lowered_source.startswith('internal /'):
                    source_tags['internal'].append(f'[COLOR FF10E6BC][B]{source_text.upper()}[/B][/COLOR]')
                elif lowered_source.startswith('external /'):
                    source_tags['external'].append(f'[COLOR FFFC9A45][B]{source_text.upper()}[/B][/COLOR]')
                else:
                    source_tags['other'].append(f'[COLOR FFCBD5E1][B]{source_text.upper()}[/B][/COLOR]')
            stream_name = str(stream.get('name') or stream.get('label') or 'Stream').replace('\n', ' | ')
            stream_name = ''.join(char for char in stream_name if ord(char) < 0x10000 and not 0xE000 <= ord(char) <= 0xF8FF and char.isprintable())
            stream_name = re.sub(r'\s+', ' ', stream_name).strip()[:180]
            quality = stream_quality_bucket(stream) or 'SD'
            quality_color = {'4K': 'FFBE8CFF', '1080p': 'FFFFC857', '720p': 'FF65D98A'}.get(quality, 'FFCBD5E1')
            quality_tag = f'[COLOR {quality_color}][B]{quality.upper()}[/B][/COLOR]'
            internal_text = ' / '.join(source_tags['internal'])
            external_text = ' / '.join(source_tags['external'])
            other_text = ' / '.join(source_tags['other'])
            source_text = internal_text or external_text or other_text or '[COLOR FFCBD5E1]SCRAPER / UNKNOWN[/COLOR]'
            cache_tag = '  |  [COLOR FF65D98A][B]RD CACHED[/B][/COLOR]' if stream.get('rd_cached') is True else ''
            size, container, video, hdr, audio, channels = stream_detail_fields(stream)
            line1 = f'{quality_tag}  |  {source_text}{cache_tag}'
            line2 = f'SIZE: {size}  |  FILE: {container}  |  VIDEO: {video}  |  HDR: {hdr}'
            line3 = f'AUDIO: {audio}  |  CHANNELS: {channels}  |  {stream_name}'
            label = f'{line1}[CR]{line2}[CR]{line3}'
            item = xbmcgui.ListItem(label=label[:500])
            self.list_items.append(item)
        self.list_control.addItems(self.list_items)
        footer_y = int(985 * self.sy)
        self.addControl(xbmcgui.ControlLabel(self.list_x, footer_y, self.screen_w - int(560 * self.sx), int(38 * self.sy), 'UP / DOWN browse   |   PAGE UP / DOWN next result   |   ENTER select   |   BACK cancel', font='font12', textColor='FFCBD5E1', alignment=2))
        self.page_label = xbmcgui.ControlLabel(self.screen_w - int(470 * self.sx), footer_y, int(400 * self.sx), int(38 * self.sy), '', font='font12', textColor='FF10E6BC', alignment=1)
        self.addControl(self.page_label)
        self.summary_panel = xbmcgui.ControlImage(summary_x, int(10 * self.sy), summary_w, int(145 * self.sy), white_tex, colorDiffuse='E607111E')
        self.addControl(self.summary_panel)
        
        src_q = quality_counts_by_source(all_streams if all_streams is not None else streams)
        ext_summary = (
            "[COLOR FFFC9A45]EXTERNAL unavailable[/COLOR]"
            if external_status == 'unavailable'
            else f"[COLOR FFFC9A45]EXTERNAL 4K:{src_q['External']['4K']} 1080p:{src_q['External']['1080p']} 720p:{src_q['External']['720p']}[/COLOR]"
        )
        self.source_summary = xbmcgui.ControlLabel(
            summary_x + int(14 * self.sx), int(16 * self.sy), summary_w - int(28 * self.sx), int(78 * self.sy),
            f"[COLOR FF10E6BC]INTERNAL  4K:{src_q['Internal']['4K']}  1080p:{src_q['Internal']['1080p']}  720p:{src_q['Internal']['720p']}[/COLOR][CR]{ext_summary}",
            font='font13', textColor='FFFFFFFF', alignment=0
        )
        self.total_summary = xbmcgui.ControlLabel(
            summary_x + int(14 * self.sx), int(98 * self.sy), summary_w - int(28 * self.sx), int(42 * self.sy),
            f"[B]TOTAL {total_found}[/B]   |   4K {counts['4K']}   |   1080p {counts['1080p']}   |   720p {counts['720p']}   |   {('RD CACHED ' + str(sum(1 for item in (all_streams if all_streams is not None else streams) if item.get('rd_cached') is True)) if cache_checked else 'RD CACHE UNKNOWN')}",
            font='font13', textColor='FFFFFFFF', alignment=0
        )
        self.addControl(self.source_summary)
        self.addControl(self.total_summary)

    def onInit(self):
        if self.list_items:
            self.list_control.selectItem(0)
        self.setFocus(self.navigation_trap)
        self._update_focus_band()
        self._update_page_label()
        xbmc.log(f'[RyuStream] fullscreen ControlList ready items={len(self.list_items)} focus={self.list_control.getSelectedPosition()} screen={self.screen_w}x{self.screen_h} internal={self.source_counts["Internal"]} external={self.external_status} total={self.total_found}', xbmc.LOGINFO)

    def onClick(self, control_id):
        self._select_current()

    def _select_current(self):
        index = self.list_control.getSelectedPosition()
        if 0 <= index < len(self.streams):
            xbmc.log(f'[RyuStream] stream selected index={index}', xbmc.LOGINFO)
            self.selected_index = index
            self.close()

    def _update_page_label(self):
        index = max(0, self.list_control.getSelectedPosition())
        self.page_label.setLabel(f'Item {min(len(self.streams), index + 1)} / {len(self.streams)}')

    def _move_selection(self, delta):
        if not self.streams:
            return
        current = self.list_control.getSelectedPosition()
        if current < 0:
            current = 0
        target = max(0, min(len(self.streams) - 1, current + delta))
        if target < self.viewport_start:
            self.viewport_start = target
        elif target >= self.viewport_start + self.visible_rows:
            self.viewport_start = target - self.visible_rows + 1
        self.list_control.selectItem(target)
        self.setFocus(self.navigation_trap)
        self._update_focus_band()
        self._update_page_label()
        xbmc.log(f'[RyuStream] stream navigation delta={delta} selected={target}', xbmc.LOGDEBUG)

    def _update_focus_band(self):
        index = max(0, self.list_control.getSelectedPosition())
        row = max(0, min(self.visible_rows - 1, index - self.viewport_start))
        row_y = self.list_y + row * self.row_pitch
        self.focus_band.setPosition(self.list_x, row_y)
        self.focus_top.setPosition(self.list_x, row_y)
        self.focus_bottom.setPosition(self.list_x, row_y + self.item_h - max(3, int(4 * self.sy)))

    def onAction(self, action):
        action_id = action.getId()
        button_code = action.getButtonCode()
        xbmc.log(f'[RyuStream] picker action id={action_id} button={button_code}', xbmc.LOGDEBUG)
        if action_id == 7:
            self._select_current()
        elif action_id == 3:
            self._move_selection(-1)
        elif action_id == 4:
            self._move_selection(1)
        elif action_id == 5 or button_code == 33:
            now = time.monotonic()
            if now - self.last_page_action >= 0.12:
                self.last_page_action = now
                self._move_selection(-self.page_step)
        elif action_id == 6 or button_code == 34:
            now = time.monotonic()
            if now - self.last_page_action >= 0.12:
                self.last_page_action = now
                self._move_selection(self.page_step)
        elif action_id in (10, 13, 92, 110, 122) or action.getButtonCode() in (27, 61467, 61448):
            self.selected_index = -1
            self.close()

    def choose(self):
        self.doModal()
        return self.selected_index


def router():
    params = {}
    if len(sys.argv) > 2 and sys.argv[2]:
        params = dict(urllib.parse.parse_qsl(sys.argv[2].lstrip('?')))
    action = params.get('action')
    try:
        handle = int(sys.argv[1])
        xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)
    except Exception:
        pass

    if action == 'auth_alldebrid':
        authorize_service('alldebrid', 'AllDebrid', 'ad_apikey')
        return
    if action == 'auth_trakt':
        authorize_service('trakt', 'Trakt', 'trakt_token')
        return
    if action == 'auth_simkl':
        authorize_service('simkl', 'Simkl', 'simkl_token')
        return
    if action == 'clear_cache':
        ctype = params.get('type', 'all')
        if ctype in ('tmdb', 'all'):
            clear_db_cache(optimize=(ctype == 'all'))
        xbmcgui.Dialog().notification('RyuStream Cache', f'Cleared ({ctype})', xbmcgui.NOTIFICATION_INFO, 3000)
        return

    win = RyuHomeWindow('ryu_home.xml', ADDON_PATH, 'Default', '1080i')
    win.doModal()
    del win


if __name__ == '__main__':
    router()
