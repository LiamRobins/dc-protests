import requests
import json
import re
import time
from datetime import datetime, timedelta, timezone

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────────────────

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

MOBILIZE_INCLUDED_TYPES = {
    'RALLY', 'MARCH', 'COMMUNITY_EVENT', 'SOLIDARITY_EVENT',
    'TOWN_HALL', 'AUTONOMOUS_ACTION', 'BARNSTORM', 'PARTY',
    'PETITION_CIRCULATE', 'OTHER_DISTRIBUTED',
}

EVENTBRITE_SLUGS = ['rally', 'march', 'protest', 'community-festival']

NOW     = datetime.now()
WINDOW  = NOW + timedelta(days=7)
NOW_UTC = datetime.now(timezone.utc)
WIN_UTC = NOW_UTC + timedelta(days=7)

# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_html(text):
    if not text:
        return ''
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>',       '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>',  '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>',      '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>',    '',   text)
    text = re.sub(r'\n{3,}',     '\n\n', text)
    return text.strip()


WEEKDAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

def parse_eb_date(raw):
    """
    Parse Eventbrite date strings, e.g.:
      "Today at 2:00 PM"
      "Tomorrow at 11:45 AM"
      "Sunday at 11:00 AM"
      "Sat, May 29, 7:00 PM"
      "Sat, May 29, 7:00 PM + 7 more"
    Returns a datetime or None.
    """
    if not raw:
        return None
    s = re.sub(r'\s*\+\s*\d+\s*more.*', '', raw, flags=re.IGNORECASE).strip()
    base = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

    def parse_time(t):
        return datetime.strptime(t.upper().replace(' ', ''), '%I:%M%p')

    # "Today at H:MM AM/PM"
    m = re.match(r'Today at (\d{1,2}:\d{2}\s*[AP]M)', s, re.IGNORECASE)
    if m:
        try:
            t = parse_time(m.group(1))
            return base.replace(hour=t.hour, minute=t.minute)
        except ValueError:
            pass

    # "Tomorrow at H:MM AM/PM"
    m = re.match(r'Tomorrow at (\d{1,2}:\d{2}\s*[AP]M)', s, re.IGNORECASE)
    if m:
        try:
            t = parse_time(m.group(1))
            return (base + timedelta(days=1)).replace(hour=t.hour, minute=t.minute)
        except ValueError:
            pass

    # "Weekday at H:MM AM/PM"
    day_pat = '|'.join(WEEKDAY_NAMES)
    m = re.match(rf'({day_pat}) at (\d{{1,2}}:\d{{2}}\s*[AP]M)', s, re.IGNORECASE)
    if m:
        try:
            target_wd  = WEEKDAY_NAMES.index(m.group(1).capitalize())
            current_wd = base.weekday()
            days_ahead = (target_wd - current_wd) % 7 or 7
            candidate  = (base + timedelta(days=days_ahead)).replace(
                hour=parse_time(m.group(2)).hour,
                minute=parse_time(m.group(2)).minute,
            )
            if candidate < NOW:
                candidate += timedelta(days=7)
            return candidate
        except (ValueError, IndexError):
            pass

    # "Mon, May 29, 7:00 PM" or "May 29 at 7:00 PM"
    m = re.search(r'(\w+ \d{1,2})[,\s]+(?:at\s+)?(\d{1,2}:\d{2}\s*[AP]M)', s, re.IGNORECASE)
    if m:
        try:
            dt = datetime.strptime(
                f"{m.group(1)} {NOW.year} {m.group(2).upper().replace(' ','')}",
                '%B %d %Y %I:%M%p'
            )
            if dt < NOW:
                dt = dt.replace(year=NOW.year + 1)
            return dt
        except ValueError:
            pass

    return None


def clean_eb_url(url):
    """Strip Eventbrite tracking parameters from a URL."""
    return re.sub(r'\?.*', '', url)


def normalize(title):
    """Lowercase + strip punctuation, for deduplication."""
    return re.sub(r'[^a-z0-9]', '', title.lower())


# ── Source 1: Mobilize.us ─────────────────────────────────────────────────────

def fetch_mobilize():
    print('\n[1/2] Mobilize.us...')
    params = {
        'zipcode':         '20001',
        'max_dist':        15,
        'timeslot_start':  f'gte_{int(NOW_UTC.timestamp())}',
        'per_page':        200,
        'visibility':      'PUBLIC',
        'is_virtual':      'false',
    }
    raw, page_url, first = [], 'https://api.mobilize.us/v1/events', True
    while page_url:
        try:
            r = requests.get(page_url, params=params if first else {}, headers=HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            raw.extend(data.get('data', []))
            page_url, first = data.get('next'), False
        except requests.exceptions.ConnectionError:
            print('  ERROR: No internet connection.')
            return []
        except Exception as e:
            print(f'  ERROR: {e}')
            break

    events, seen = [], set()
    for ev in raw:
        if ev.get('event_type') not in MOBILIZE_INCLUDED_TYPES:
            continue
        loc    = ev.get('location') or {}
        city   = loc.get('locality') or loc.get('city')  or ''
        state  = loc.get('region')   or loc.get('state') or ''
        if 'DC' not in state.upper() and 'Washington' not in city:
            continue
        sponsor = ev.get('sponsor') or {}
        for slot in (ev.get('timeslots') or []):
            ts = slot.get('start_date')
            if not ts:
                continue
            start_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
            if start_utc > WIN_UTC:
                continue
            eid = f"mob_{ev['id']}_{slot['id']}"
            if eid in seen:
                continue
            seen.add(eid)
            local_start = start_utc.astimezone()
            end_ts      = slot.get('end_date')
            local_end   = datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone() if end_ts else None
            venue   = loc.get('venue') or ''
            address = ', '.join(filter(None, loc.get('address_lines') or []))
            full_loc = ', '.join(filter(None, [venue, address, city, state]))
            events.append({
                'id':                eid,
                'title':             ev.get('title') or 'Untitled',
                'description':       strip_html(ev.get('description') or ev.get('summary') or ''),
                'start':             local_start.isoformat(),
                'end':               local_end.isoformat() if local_end else None,
                'location':          full_loc or 'Washington, DC',
                'organizer':         sponsor.get('name') or '',
                'organizer_website': sponsor.get('website') or '',
                'event_url':         ev.get('browser_url') or '',
                'event_type':        ev.get('event_type') or 'EVENT',
                'source':            'Mobilize.us',
            })

    print(f'  {len(events)} events found.')
    return events


# ── Source 2: Eventbrite ──────────────────────────────────────────────────────

def parse_eb_listing(html):
    """Pull basic event data from an Eventbrite listing page."""
    soup   = BeautifulSoup(html, 'html.parser')
    events = []
    for li in soup.find_all('li'):
        h3 = li.find('h3')
        if not h3:
            continue
        url = None
        for a in li.find_all('a', href=True):
            if '/e/' in a['href']:
                href = a['href']
                url  = href if href.startswith('http') else f'https://www.eventbrite.com{href}'
                break
        if not url:
            continue
        title = h3.get_text(strip=True)
        if len(title) < 5:
            continue
        date_raw = loc_raw = ''
        for text in li.stripped_strings:
            t = text.strip()
            if t == title or len(t) < 4:
                continue
            if not date_raw and re.search(
                r'\b(AM|PM|Today|Tomorrow|' + '|'.join(WEEKDAY_NAMES) + r')\b', t, re.IGNORECASE
            ):
                date_raw = t
            elif not loc_raw and ('·' in t or re.search(r'\b(Washington|DC|Arlington|Alexandria)\b', t)):
                loc_raw = t
        events.append({'title': title, 'url': url, 'date_raw': date_raw, 'loc_raw': loc_raw})
    return events


def fetch_eventbrite():
    if not BS4_AVAILABLE:
        print('\n[2/2] Skipping Eventbrite — beautifulsoup4 not installed.')
        print('  Fix: pip install beautifulsoup4')
        return []

    print('\n[2/2] Eventbrite...')
    raw, seen_urls = [], set()

    for slug in EVENTBRITE_SLUGS:
        url = f'https://www.eventbrite.com/d/dc--washington/{slug}/'
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                found = [e for e in parse_eb_listing(r.text) if e['url'] not in seen_urls]
                for e in found:
                    seen_urls.add(e['url'])
                raw.extend(found)
                print(f'  [{slug}]: {len(found)} items')
            else:
                print(f'  [{slug}]: HTTP {r.status_code}')
        except Exception as e:
            print(f'  [{slug}]: {e}')
        time.sleep(0.75)

    events, seen_ids = [], set()
    for ev in raw:
        loc = ev.get('loc_raw', '')
        if not re.search(r'\b(Washington|DC)\b', loc):
            continue
        dt = parse_eb_date(ev.get('date_raw', ''))
        if not dt or dt < NOW or dt > WINDOW:
            continue
        clean = clean_eb_url(ev['url'])
        if clean in seen_ids:
            continue
        seen_ids.add(clean)
        parts    = [p.strip() for p in loc.split('·')]
        city     = parts[0] if parts else loc
        venue    = parts[1] if len(parts) > 1 else ''
        full_loc = ', '.join(filter(None, [venue, city]))
        events.append({
            'id':                f'eb_{abs(hash(clean))}',
            'title':             ev['title'],
            'description':       '',
            'start':             dt.isoformat(),
            'end':               None,
            'location':          full_loc or 'Washington, DC',
            'organizer':         '',
            'organizer_website': '',
            'event_url':         clean,
            'event_type':        'EVENT',
            'source':            'Eventbrite',
        })

    print(f'  {len(events)} DC events within 7 days.')
    return events


# ── Merge & deduplicate ───────────────────────────────────────────────────────

def merge(sources):
    combined = [ev for src in sources for ev in src]
    seen, out = {}, []
    for ev in combined:
        key = (normalize(ev['title']), ev['start'][:10])
        if key not in seen:
            seen[key] = True
            out.append(ev)
    out.sort(key=lambda x: x['start'])
    return out


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('=' * 50)
    print('DC Protests & Gatherings — Event Fetcher')
    print('=' * 50)

    mob = fetch_mobilize()
    eb  = fetch_eventbrite()
    all_events = merge([mob, eb])

    print(f'\nTotal unique events: {len(all_events)}')
    print(f'  Mobilize.us : {len(mob)}')
    print(f'  Eventbrite  : {len(eb)}')

    with open('events.js', 'w', encoding='utf-8') as f:
        f.write('const EVENTS_DATA = ')
        json.dump(all_events, f, indent=2, ensure_ascii=False)
        f.write(';\n')
        f.write(f'const LAST_UPDATED = "{datetime.now().strftime("%B %d, %Y at %I:%M %p")}";\n')
    print('\nSaved to events.js.')
    if all_events:
        print('Open index.html in your browser to view.')
    else:
        print('No events found — the site will show an empty state.')

    print('=' * 50)
