import io
import os
import re
import unicodedata
from datetime import datetime, timedelta

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from PIL import Image, ImageDraw, ImageFont, ImageFilter
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception as _e:
    print('  [heic] pillow-heif not available: %s' % _e)

import json
import requests

# ============================================================
# CONFIG
# ============================================================
CREDENTIALS_FILE = 'credentials.json'
META_CONFIG_FILE = 'meta_config.json'   # point this at the 6-a-side page later

FIXTURES_SHEET_ID = '1j6ZN3N8aXnB9vKFdWeXhY-fyo8aH1JlmhWZWHwzgu-E'
FRIENDLY_TAB = 'Friendly Fixtures'
LEAGUECUP_TAB = 'League & Cup Fixtures'
INDEX_TAB = 'Index'

FX_DATE, FX_TIME, FX_HOME, FX_AWAY, FX_LOC, FX_TYPE, FX_ROUND, FX_STATUS = range(8)
IDX_TEAM, IDX_CAL, IDX_LEAGUE = 0, 1, 2

OUR_TEAMS = ['6A', '6B', '6C', '6D', 'VETs']

LOGO_FOLDER_ID = '19NNyf1trl1LoA7Tth7PFMbRAv65oXeeR'      # league logos
ASSETS_FOLDER_ID = '1aQ1ay_nCSQptlPyigVvzwbkReIdKioZV'    # background
FONT_FOLDER_ID = '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'      # font
POST_UPLOAD_FOLDER_ID = '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'

BACKGROUND_NAME = '6-a-side'
FONT_NAME = 'Etna'

CANVAS_W = 1536
CANVAS_H = 1920

WHITE = (255, 255, 255)
GREEN = (75, 186, 105)

OUTPUT_DIR = 'output'
_FONT_LOCAL = os.path.join(OUTPUT_DIR, '_etna.ttf')

# --- Layout ---
# Vertical band the match blocks are distributed within (fractions of height).
BAND_TOP = int(CANVAS_H * 0.40)
BAND_BOTTOM = int(CANVAS_H * 0.90)

# Horizontal anchors
LINE1_X = int(CANVAS_W * 0.185)          # left start of the small info line
MATCHUP_LEFT_X = int(CANVAS_W * 0.185)   # GP23 team always starts here
X_MARK_CX = int(CANVAS_W * 0.44)         # the "x" is always centred here
OPP_START_X = int(CANVAS_W * 0.47)       # opponent always starts here
RIGHT_LIMIT = int(CANVAS_W * 0.95)       # names/line must not exceed this

# League logo (right end of the info line)
LEAGUE_LOGO_MAX_H = int(CANVAS_H * 0.030)
LEAGUE_LOGO_RIGHT = int(CANVAS_W * 0.95)

# Sizes (fixed)
INFO_SIZE = int(CANVAS_H * 0.024)        # "11.6 | 19:00   Location"
MATCH_SIZE = int(CANVAS_H * 0.045)       # "GP23 6A x FC ..."
X_SIZE = int(CANVAS_H * 0.030)           # the small "x"
LINE_THICK = max(2, int(CANVAS_H * 0.002))

# ============================================================
# AUTH
# ============================================================
def get_creds():
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
    return ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)

def load_meta_config():
    with open(META_CONFIG_FILE) as f:
        return json.load(f)

def get_gspread_client():
    return gspread.authorize(get_creds())

def get_drive_service():
    return build('drive', 'v3', credentials=get_creds())

# ============================================================
# NORMALIZE / MATCH
# ============================================================
def _norm(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())

# ============================================================
# DRIVE HELPERS
# ============================================================
def list_folder(drive, folder_id):
    out = []
    page_token = None
    while True:
        resp = drive.files().list(
            q="'%s' in parents and trashed = false" % folder_id,
            fields='nextPageToken, files(id,name,mimeType)',
            pageToken=page_token).execute()
        out.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out

def download_bytes(drive, file_id):
    return drive.files().get_media(fileId=file_id).execute()

def upload_public_image(drive, image_path, folder_id=None):
    with open(image_path, 'rb') as fh:
        r = requests.post(
            'https://catbox.moe/user/api.php',
            data={'reqtype': 'fileupload'},
            files={'fileToUpload': (os.path.basename(image_path), fh, 'image/png')})
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith('http'):
        raise RuntimeError('catbox upload failed: %s' % url)
    return url, None

def find_by_basename(files, name):
    target = _norm(name)
    for f in files:
        base = os.path.splitext(f['name'])[0]
        if _norm(base) == target:
            return f
    return None

def download_image_from(drive, files, name):
    f = find_by_basename(files, name)
    if not f:
        return None
    data = download_bytes(drive, f['id'])
    return Image.open(io.BytesIO(data)).convert('RGBA')

def ensure_font(drive, font_files):
    if os.path.exists(_FONT_LOCAL):
        return _FONT_LOCAL
    f = find_by_basename(font_files, FONT_NAME)
    if not f:
        print('  Font "%s" not found; default font.' % FONT_NAME)
        return None
    data = download_bytes(drive, f['id'])
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(_FONT_LOCAL, 'wb') as fh:
        fh.write(data)
    return _FONT_LOCAL

# ============================================================
# GENERIC HELPERS
# ============================================================
def load_font(font_path, size):
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()

def text_w(draw, s, font):
    b = draw.textbbox((0, 0), s, font=font)
    return b[2] - b[0]

def fit_font_to_width(font_path, text, max_w, start_size, min_size=20):
    size = start_size
    tmp = ImageDraw.Draw(Image.new('RGBA', (10, 10)))
    while size > min_size:
        f = load_font(font_path, size)
        if text_w(tmp, text, f) <= max_w:
            return f
        size -= 2
    return load_font(font_path, min_size)

def remove_edge_background(img, tol=40):
    img = img.convert('RGBA')
    w, h = img.size
    px = img.load()
    corners = [px[0, 0], px[w-1, 0], px[0, h-1], px[w-1, h-1]]
    br = sum(c[0] for c in corners) // 4
    bg = sum(c[1] for c in corners) // 4
    bb = sum(c[2] for c in corners) // 4
    def close(c):
        return abs(c[0]-br) <= tol and abs(c[1]-bg) <= tol and abs(c[2]-bb) <= tol
    from collections import deque
    visited = bytearray(w * h)
    dq = deque()
    for x in range(w):
        for y in (0, h-1): dq.append((x, y))
    for y in range(h):
        for x in (0, w-1): dq.append((x, y))
    while dq:
        x, y = dq.popleft()
        idx = y * w + x
        if visited[idx]: continue
        visited[idx] = 1
        c = px[x, y]
        if c[3] == 0 or close(c):
            px[x, y] = (c[0], c[1], c[2], 0)
            for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
                nx, ny = x+dx, y+dy
                if 0 <= nx < w and 0 <= ny < h and not visited[ny*w+nx]:
                    dq.append((nx, ny))
    cb = img.getbbox()
    return img.crop(cb) if cb else img

# ============================================================
# LABELS
# ============================================================
def galaksia_label(team):
    t = team.strip().upper()
    if t == 'VETS' or t == 'VET':
        return 'GP23 VETs'
    return 'GP23 ' + t   # 6A -> GP23 6A

def clean_team_name(name):
    s = (name or '').strip()
    s = re.sub(r'\s*,?\s*(z\.s\.|a\.s\.)\s*$', '', s, flags=re.I)
    return s.strip()

def date_line_short(match_date):
    # "11.6" (D.M, no leading zeros, no year)
    return '%d.%d' % (match_date.day, match_date.month)

# ============================================================
# IMAGE BUILD
# ============================================================
def build_image(bg_src, font_path, matches, league_logos):
    """matches: list of dicts with keys:
       date_str, time_str, loc, gp_label, opp_name, league_logo (PIL or None)"""
    bg = bg_src.copy().convert('RGBA')
    if bg.size != (CANVAS_W, CANVAS_H):
        bg = bg.resize((CANVAS_W, CANVAS_H))
    draw = ImageDraw.Draw(bg)

    n = len(matches)
    slot_h = (BAND_BOTTOM - BAND_TOP) / n
    info_font = load_font(font_path, INFO_SIZE)
    x_font = load_font(font_path, X_SIZE)

    for i, m in enumerate(matches):
        slot_cy = int(BAND_TOP + slot_h * (i + 0.5))
        line1_y = int(slot_cy - MATCH_SIZE * 0.75)
        line2_y = int(slot_cy + MATCH_SIZE * 0.10)

        # ---------- Line 1: date | time   location ----------
        info_text = '%s | %s    %s' % (m['date_str'], m['time_str'], m['loc'])
        draw.text((LINE1_X, line1_y), info_text, font=info_font, fill=WHITE)
        info_bottom = line1_y + (draw.textbbox((0, 0), info_text, font=info_font)[3])

        # Underline extends from the end of the info text to just left of the
        # league logo (or to RIGHT_LIMIT if friendly / no logo).
        info_right = LINE1_X + text_w(draw, info_text, info_font) + int(CANVAS_W * 0.02)

        logo = m.get('league_logo')
        line_end = RIGHT_LIMIT
        if logo is not None:
            league_norm = _norm(m.get('league', ''))
            if league_norm in ('pkfl', 'psmf'):
                lg = logo  # keep background
            else:
                lg = remove_edge_background(logo)
            cb = lg.getbbox()
            if cb:
                lg = lg.crop(cb)
            scale = LEAGUE_LOGO_MAX_H / lg.height
            lg = lg.resize((max(1, int(lg.width * scale)), LEAGUE_LOGO_MAX_H),
                           Image.LANCZOS)
            logo_x = LEAGUE_LOGO_RIGHT - lg.width
            logo_cy = line1_y + (draw.textbbox((0, 0), info_text, font=info_font)[3]
                                 - draw.textbbox((0, 0), info_text, font=info_font)[1]) // 2
            bg.alpha_composite(lg, (logo_x, int(logo_cy - lg.height / 2)))
            line_end = logo_x - int(CANVAS_W * 0.015)

        underline_y = int(info_bottom - INFO_SIZE * 0.15)
        if line_end > info_right:
            draw.line([(info_right, underline_y), (line_end, underline_y)],
                      fill=WHITE, width=LINE_THICK)

        # ---------- Line 2: GP23 <team>  x  <opponent> ----------
        match_font = load_font(font_path, MATCH_SIZE)

        gp = m['gp_label']
        opp = m['opp_name'].strip()

        # GP label: from MATCHUP_LEFT_X, must not overrun the x mark.
        gp_max_w = (X_MARK_CX - int(CANVAS_W * 0.02)) - MATCHUP_LEFT_X
        gp_font = match_font
        if text_w(draw, gp, match_font) > gp_max_w:
            gp_font = fit_font_to_width(font_path, gp, gp_max_w, MATCH_SIZE)

        # Opponent: from OPP_START_X to RIGHT_LIMIT.
        opp_max_w = RIGHT_LIMIT - OPP_START_X
        opp_font = match_font
        if text_w(draw, opp, match_font) > opp_max_w:
            opp_font = fit_font_to_width(font_path, opp, opp_max_w, MATCH_SIZE)

        # Draw GP label (left-anchored at MATCHUP_LEFT_X)
        draw.text((MATCHUP_LEFT_X, line2_y), gp, font=gp_font, fill=WHITE)

        # Draw the "x" centred at X_MARK_CX, vertically aligned with line2
        xw = text_w(draw, 'x', x_font)
        # nudge x down a touch to sit on the baseline of the big text
        x_y = int(line2_y + MATCH_SIZE * 0.28)
        draw.text((int(X_MARK_CX - xw / 2), x_y), 'x', font=x_font, fill=WHITE)

        # Draw opponent (left-anchored at OPP_START_X)
        draw.text((OPP_START_X, line2_y), opp, font=opp_font, fill=WHITE)

    return bg.convert('RGB')

# ============================================================
# STORY VERSION
# ============================================================
STORY_W = 1080
STORY_H = 1920

def make_story_version(feed_img_path):
    feed = Image.open(feed_img_path).convert('RGB')
    bg = feed.copy()
    scale = max(STORY_W / bg.width, STORY_H / bg.height)
    bg = bg.resize((int(bg.width * scale), int(bg.height * scale)), Image.LANCZOS)
    left = (bg.width - STORY_W) // 2
    top = (bg.height - STORY_H) // 2
    bg = bg.crop((left, top, left + STORY_W, top + STORY_H))
    bg = bg.filter(ImageFilter.GaussianBlur(40))
    fg = feed.copy()
    fscale = min(STORY_W / fg.width, STORY_H / fg.height) * 0.92
    fg = fg.resize((int(fg.width * fscale), int(fg.height * fscale)), Image.LANCZOS)
    bg.paste(fg, ((STORY_W - fg.width) // 2, (STORY_H - fg.height) // 2))
    out_path = feed_img_path.replace('.png', '_story.png')
    bg.save(out_path, 'PNG', quality=95)
    return out_path

# ============================================================
# CAPTION
# ============================================================
def build_caption(matches):
    lines = []
    for m in matches:
        lines.append('%s | %s — %s x %s (%s)'
                     % (m['date_str'], m['time_str'],
                        m['gp_label'], m['opp_name'], m['loc']))
    body = '\n'.join(lines)
    return (
        "⚫️⚪️🟢 NEXT MATCHES – 6-a-side!\n\n"
        + body +
        "\n\nCome support the boys! 💪\n\n"
        "#GalaksiaPraha23 #GP23 #NextMatches #Prague #Praha #PragueFootball "
        "#Fotbal #BlackWhiteGreen #GreenArmy #6aside #COYG #FootballFamily"
    )

# ============================================================
# META
# ============================================================
GRAPH = 'https://graph.facebook.com/v20.0'

def _fb_page_photo(page_id, token, image_url, caption, published=True):
    r = requests.post('%s/%s/photos' % (GRAPH, page_id),
                      data={'url': image_url, 'caption': caption,
                            'published': 'true' if published else 'false',
                            'access_token': token})
    r.raise_for_status()
    return r.json()

def _fb_story(page_id, token, photo_id):
    r = requests.post('%s/%s/photo_stories' % (GRAPH, page_id),
                      data={'photo_id': photo_id, 'access_token': token})
    r.raise_for_status()
    return r.json()

def _ig_publish(ig_id, token, image_url, caption=None, is_story=False):
    data = {'image_url': image_url, 'access_token': token}
    if is_story:
        data['media_type'] = 'STORIES'
    elif caption:
        data['caption'] = caption
    c = requests.post('%s/%s/media' % (GRAPH, ig_id), data=data)
    c.raise_for_status()
    creation_id = c.json()['id']
    p = requests.post('%s/%s/media_publish' % (GRAPH, ig_id),
                      data={'creation_id': creation_id, 'access_token': token})
    p.raise_for_status()
    return p.json()

def post_to_meta(caption, image_url=None, story_url=None):
    cfg = load_meta_config()
    page_id = cfg['page_id']; ig_id = cfg['ig_user_id']; token = cfg['page_access_token']
    if not image_url:
        print('    [meta] ERROR: no public image_url; cannot post.')
        return
    try:
        _fb_page_photo(page_id, token, image_url, caption, published=True)
        print('    [meta] FB feed OK')
    except Exception as e:
        print('    [meta] FB feed FAILED: %s' % e)
    try:
        photo = _fb_page_photo(page_id, token, story_url or image_url, '', published=False)
        _fb_story(page_id, token, photo['id'])
        print('    [meta] FB story OK')
    except Exception as e:
        print('    [meta] FB story FAILED: %s' % e)
    try:
        _ig_publish(ig_id, token, image_url, caption=caption, is_story=False)
        print('    [meta] IG feed OK')
    except Exception as e:
        print('    [meta] IG feed FAILED: %s' % e)
    try:
        _ig_publish(ig_id, token, story_url or image_url, is_story=True)
        print('    [meta] IG story OK')
    except Exception as e:
        print('    [meta] IG story FAILED: %s' % e)

# ============================================================
# ERROR EMAIL (stub)
# ============================================================
def send_error_email(errors):
    if not errors:
        return
    print('  [error-email] (stub) %d error(s):' % len(errors))
    for e in errors:
        print('    - %s' % e)

# ============================================================
# HELPERS
# ============================================================
def parse_date(value):
    s = (value or '').strip()
    if not s:
        return None
    for fmt in ('%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def parse_time(value):
    s = (value or '').strip()
    if not s:
        return None
    s2 = s.replace('h', ':')
    m = re.match(r'^(\d{1,2}):(\d{2})', s2)
    if m:
        return '%d:%02d' % (int(m.group(1)), int(m.group(2)))
    m = re.match(r'^(\d{1,2})$', s2)
    if m:
        return '%d:00' % int(m.group(1))
    return None

def time_sort_key(t):
    m = re.match(r'^(\d{1,2}):(\d{2})', t or '')
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 9999

def build_team_league_map(client):
    ws = client.open_by_key(FIXTURES_SHEET_ID).worksheet(INDEX_TAB)
    data = ws.get_all_values()
    mapping = {}
    for row in data[1:]:
        if len(row) <= max(IDX_TEAM, IDX_LEAGUE):
            continue
        team = (row[IDX_TEAM] or '').strip()
        league = (row[IDX_LEAGUE] or '').strip()
        if team:
            mapping[team.lower()] = league
    return mapping

def which_gp_team(home, away):
    ours = [t.upper() for t in OUR_TEAMS]
    h = home.strip().upper(); a = away.strip().upper()
    if h in ours and a in ours:
        return home.strip(), away.strip(), True
    if h in ours:
        return home.strip(), away.strip(), False
    if a in ours:
        return away.strip(), home.strip(), False
    return None, None, False

# ============================================================
# MAIN
# ============================================================
def run_6aside_next_matches():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    errors = []

    print('Auth...')
    client = get_gspread_client()
    drive = get_drive_service()

    team_league = build_team_league_map(client)

    logo_files = list_folder(drive, LOGO_FOLDER_ID)
    asset_files = list_folder(drive, ASSETS_FOLDER_ID)
    font_files = list_folder(drive, FONT_FOLDER_ID)

    background = download_image_from(drive, asset_files, BACKGROUND_NAME)
    if background is None:
        msg = 'FATAL: background "%s" not found.' % BACKGROUND_NAME
        print(msg); errors.append(msg); send_error_email(errors); return
    font_path = ensure_font(drive, font_files)

    today = datetime.now().date()
    end = today + timedelta(days=6)
    print('Window: %s .. %s' % (today, end))

    league_logo_cache = {}

    def get_league_logo(league):
        if not league:
            return None
        lk = _norm(league)
        if lk not in league_logo_cache:
            f = find_by_basename(logo_files, league)
            if f:
                try:
                    d = download_bytes(drive, f['id'])
                    league_logo_cache[lk] = Image.open(io.BytesIO(d)).convert('RGBA')
                except Exception:
                    league_logo_cache[lk] = None
            else:
                league_logo_cache[lk] = None
                errors.append('No league logo for "%s".' % league)
        return league_logo_cache[lk]

    # ---- Collect matches in the window ----
    matches = []
    ss = client.open_by_key(FIXTURES_SHEET_ID)
    for tab in (FRIENDLY_TAB, LEAGUECUP_TAB):
        ws = ss.worksheet(tab)
        data = ws.get_all_values()
        for i, row in enumerate(data[1:], start=2):
            if len(row) <= FX_STATUS:
                continue
            m_date = parse_date(row[FX_DATE])
            if not m_date:
                continue
            if (row[FX_STATUS] or '').strip() != 'Completed':
                continue
            if m_date < today or m_date > end:
                continue

            home = (row[FX_HOME] or '').strip()
            away = (row[FX_AWAY] or '').strip()
            gp_team, opp_raw, is_derby = which_gp_team(home, away)
            if gp_team is None:
                continue

            t_str = parse_time(row[FX_TIME])
            if not t_str:
                errors.append('%s row %d: missing kick-off time.' % (tab, i))
                continue

            match_type = (row[FX_TYPE] or '').strip().lower()
            loc = (row[FX_LOC] or '').strip()

            # Opponent name: derbies use full sheet name; else clean z.s./a.s.
            if is_derby:
                opp_name = galaksia_label(opp_raw)
            else:
                opp_name = clean_team_name(opp_raw)

            # League logo (none for friendlies)
            if match_type == 'friendly':
                lg = None
                league = ''
            else:
                league = team_league.get(gp_team.lower(), '')
                lg = get_league_logo(league)

            matches.append({
                'date': m_date,
                'time_key': time_sort_key(t_str),
                'date_str': date_line_short(m_date),
                'time_str': t_str,
                'loc': loc,
                'gp_label': galaksia_label(gp_team),
                'opp_name': opp_name,
                'league_logo': lg,
                'league': league,
            })

    if not matches:
        print('No 6-a-side matches in the window. Nothing to post.')
        send_error_email(errors)
        return

    # Sort by date, then time
    matches.sort(key=lambda m: (m['date'], m['time_key']))

    # Cap at 5 (max per your spec)
    if len(matches) > 5:
        errors.append('More than 5 matches (%d) - only first 5 shown.' % len(matches))
        matches = matches[:5]

    print('Building image with %d match(es).' % len(matches))
    try:
        img = build_image(background, font_path, matches, league_logo_cache)
    except Exception as e:
        errors.append('Image build failed: %s' % e)
        send_error_email(errors)
        return

    date_str = today.isoformat()
    out_path = os.path.join(OUTPUT_DIR, 'nextmatches_6aside_%s.png' % date_str)
    img.save(out_path, 'PNG', quality=95)
    print('  saved %s' % out_path)

    caption = build_caption(matches)
    print('  caption:\n%s' % caption)
    make_story_version(out_path)   # build story locally to review
    print('  [meta] posting DISABLED for testing - not posted.')

    print('Done.')
    send_error_email(errors)


if __name__ == '__main__':
    run_6aside_next_matches()