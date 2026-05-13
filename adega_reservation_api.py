"""
Adega Gaucha – OpenTable Reservation API
Python/FastAPI drop-in replacement for the n8n workflow.
This version mirrors the working n8n workflow behavior without connecting to n8n.

Install: pip install fastapi "uvicorn[standard]" httpx
Run:     uvicorn adega_reservation_api:app --host 0.0.0.0 --port 8000 --workers 4
"""
import asyncio
import os
import re
import time
import uuid
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
from typing import Any
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

WIDGET_API_KEY = required_env('WIDGET_API_KEY')
OT_CLIENT_ID = required_env('OT_CLIENT_ID')
OT_CLIENT_SECRET = required_env('OT_CLIENT_SECRET')
OT_OAUTH_URL = os.environ.get('OT_OAUTH_URL', 'https://oauth.opentable.com/api/v2/oauth/token')
OT_API_BASE = os.environ.get('OT_API_BASE', 'https://platform.opentable.com/v2')
VALID_RIDS = {1192183, 1372396, 1372399}
VALID_ACTIONS = {'check-availability', 'availability-metadata', 'lock-slot', 'create-booking', 'restaurant-info', 'check-experiences', 'lock-experience', 'release-slot'}
HOURS = {1192183: {0: '11:30-22:30', 1: '11:30-22:30', 2: '11:30-22:30', 3: '11:30-22:30', 4: '11:30-22:30', 5: '11:30-22:30', 6: '11:30-22:30'}, 1372396: {0: '12:00-22:30', 1: '12:00-22:30', 2: '12:00-22:30', 3: '12:00-22:30', 4: '12:00-22:30', 5: '12:00-22:30', 6: '12:00-22:30'}, 1372399: {0: '12:00-22:30', 1: '17:00-22:30', 2: '17:00-22:30', 3: '17:00-22:30', 4: '17:00-22:30', 5: '12:00-22:30', 6: '12:00-22:30'}}
RID_TO_NAME = {1192183: 'Adega Gaucha - Orlando', 1372396: 'Adega Gaucha - Kissimmee', 1372399: 'Adega Gaucha - Deerfield Beach'}
DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
DAYS_SHORT = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT']
_token_lock = asyncio.Lock()
_cached_token = None
_token_expiry = 0.0

async def get_ot_token(client):
    global _cached_token, _token_expiry
    async with _token_lock:
        if _cached_token and time.monotonic() < _token_expiry - 60:
            return _cached_token
        import base64
        basic = 'Basic ' + base64.b64encode(f'{OT_CLIENT_ID}:{OT_CLIENT_SECRET}'.encode()).decode()
        resp = await client.post(OT_OAUTH_URL, headers={'Authorization': basic, 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, data={'grant_type': 'client_credentials'}, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        _cached_token = data['access_token']
        _token_expiry = time.monotonic() + float(data.get('expires_in', 3600))
        return _cached_token
app = FastAPI(title='Adega Gaucha Reservation API')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['POST', 'OPTIONS'], allow_headers=['Content-Type', 'x-api-key'], max_age=86400)

def cors_json(data, status=200):
    return JSONResponse(content=data, status_code=status, headers={'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json'})

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def _safe_json(resp):
    """Parse an OpenTable response without letting non-JSON error bodies crash FastAPI."""
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _extract_ot_error(raw, fallback):
    """Mirror n8n's best-effort extraction of OpenTable error messages."""
    errors = raw.get('errors') if isinstance(raw, dict) else None
    if isinstance(errors, list) and errors:
        parts = []
        for err in errors:
            if isinstance(err, dict):
                msg = err.get('message') or err.get('detail') or err.get('code')
                if msg:
                    parts.append(str(msg))
            elif err:
                parts.append(str(err))
        if parts:
            return '; '.join(parts)
    error = raw.get('error') if isinstance(raw, dict) else None
    if isinstance(error, dict):
        msg = error.get('message') or error.get('detail') or error.get('code')
        if msg:
            return str(msg)
    if isinstance(error, str) and error:
        return error
    message = raw.get('message') if isinstance(raw, dict) else None
    if message:
        return str(message)
    return fallback

def detect_phone(digits):
    """Return (country_code, local_number). Never throws – always falls back to US."""
    d = digits
    if not d or len(d) < 4:
        return ('US', d or '0000000000')
    for prefix, cc, min_len in [('971', 'AE', 11), ('966', 'SA', 11), ('234', 'NG', 12), ('254', 'KE', 12), ('213', 'DZ', 11), ('880', 'BD', 12), ('593', 'EC', 12), ('506', 'CR', 11), ('507', 'PA', 10), ('502', 'GT', 11), ('504', 'HN', 11), ('503', 'SV', 11), ('505', 'NI', 11), ('351', 'PT', 12)]:
        if d.startswith(prefix) and len(d) >= min_len:
            return (cc, d[3:])
    for prefix, cc, min_len in [('92', 'PK', 10), ('91', 'IN', 12), ('44', 'GB', 11), ('61', 'AU', 11), ('55', 'BR', 12), ('52', 'MX', 12), ('49', 'DE', 11), ('33', 'FR', 11), ('34', 'ES', 11), ('39', 'IT', 11), ('86', 'CN', 12), ('81', 'JP', 11), ('82', 'KR', 11), ('27', 'ZA', 11), ('20', 'EG', 11), ('90', 'TR', 12), ('31', 'NL', 11), ('47', 'NO', 10), ('46', 'SE', 11), ('45', 'DK', 10), ('57', 'CO', 12), ('58', 'VE', 12), ('54', 'AR', 12), ('56', 'CL', 11), ('51', 'PE', 11), ('53', 'CU', 10)]:
        if d.startswith(prefix) and len(d) >= min_len:
            return (cc, d[2:])
    if d.startswith('7') and len(d) == 11:
        return ('RU', d[1:])
    if d.startswith('1') and len(d) == 11:
        return ('US', d[1:])
    if len(d) == 10:
        return ('US', d)
    return ('US', d)

def _nth_weekday(year, js_month, js_dow, n):
    """
    Return YYYY-MM-DD of the nth occurrence of js_dow in the given month.
    js_month is 0-indexed (JS convention: 0=Jan, 4=May, 5=Jun, 10=Nov).
    js_dow is JS day-of-week (0=Sun, 4=Thu, 6=Sat).
    """
    py_month = js_month + 1
    py_wd = (js_dow - 1) % 7
    d = dt_date(year, py_month, 1)
    count = 0
    while True:
        if d.weekday() == py_wd:
            count += 1
            if count == n:
                return d.isoformat()
        d += timedelta(days=1)

def _get_holiday_date(name, selected_date):
    if not selected_date:
        return None
    year = int(selected_date[:4])
    n = (name or '').lower()
    if re.search('mother.?s day', n, re.I):
        return _nth_weekday(year, 4, 0, 2)
    if re.search('father.?s day', n, re.I):
        return _nth_weekday(year, 5, 0, 3)
    if re.search('valentine', n, re.I):
        return f'{year}-02-14'
    if re.search('new year.?s eve', n, re.I):
        return f'{year}-12-31'
    if re.search('thanksgiving', n, re.I):
        return _nth_weekday(year, 10, 4, 4)
    return None
_DAY_PATTERNS = [(re.compile('\\bsunday\\b', re.I), 0), (re.compile('\\bmonday\\b', re.I), 1), (re.compile('\\btuesday\\b', re.I), 2), (re.compile('\\bwednesday\\b', re.I), 3), (re.compile('\\bthursday\\b', re.I), 4), (re.compile('\\bfriday\\b', re.I), 5), (re.compile('\\bsaturday\\b', re.I), 6)]

def _day_token_to_num(token):
    t = (token or '').lower().strip()
    if t.startswith('sun'):
        return 0
    if t.startswith('mon'):
        return 1
    if t.startswith('tue'):
        return 2
    if t.startswith('wed'):
        return 3
    if t.startswith('thu'):
        return 4
    if t.startswith('fri'):
        return 5
    if t.startswith('sat'):
        return 6
    return None

def _day_range_match_from_name(name, js_dow):
    # Generic day-range parser for OpenTable experience names/descriptions.
    # Example from OT: "Monday-Thursday" means Monday through Thursday,
    # not only Monday and Thursday.
    if js_dow is None or not name:
        return None

    n = str(name).lower()
    n = n.replace('\u2010', '-').replace('\u2011', '-').replace('\u2012', '-')
    n = n.replace('\u2013', '-').replace('\u2014', '-').replace('\u2015', '-')

    day = r'(sun(?:day)?|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday|rday)?|thur(?:sday)?|fri(?:day)?|sat(?:urday)?)'
    pattern = re.compile(r'\b' + day + r'\s*(?:-|to|through|thru)\s*' + day + r'\b', re.I)

    for m in pattern.finditer(n):
        start = _day_token_to_num(m.group(1))
        end = _day_token_to_num(m.group(2))
        if start is None or end is None:
            continue

        days = []
        d = start
        while True:
            days.append(d)
            if d == end:
                break
            d = (d + 1) % 7
            if len(days) > 7:
                break

        if js_dow in days:
            return True
        return False

    return None


def _day_match(e, js_dow, selected_date):
    if js_dow is None:
        return True
    name = e.get('name') or ''
    if re.search('paella', name, re.I):
        return js_dow == 6
    candidates = []
    for src in [e.get('availability'), e.get('schedule'), e.get('dining_schedule'), e.get('service_schedule'), e.get('booking_availability'), e.get('availability_schedule'), e]:
        if not isinstance(src, dict):
            continue
        for key in ('days_of_week', 'days', 'weekdays', 'available_days_of_week'):
            v = src.get(key)
            if isinstance(v, list) and len(v) > 0:
                candidates.append(v)
    if candidates:
        short = DAYS_SHORT[js_dow]
        return any((d == js_dow if isinstance(d, int) else str(d).upper()[:3] == short for d in candidates[0]))
    range_match = _day_range_match_from_name(name, js_dow)
    if range_match is not None:
        return range_match

    matched_dows = [dow for pat, dow in _DAY_PATTERNS if pat.search(name)]
    if matched_dows:
        return js_dow in matched_dows
    for src in [e.get('availability'), e.get('date_range'), e.get('schedule'), e]:
        if not isinstance(src, dict):
            continue
        if any((src.get(k) for k in ('start_date', 'startDate', 'start', 'end_date', 'endDate', 'end'))):
            return True
    if _get_holiday_date(name, selected_date) is not None:
        return True
    return 1 <= js_dow <= 4

def _is_friday_couple_experience(e):
    text = " ".join([
        str(e.get("name") or ""),
        str(e.get("description") or ""),
    ]).lower()

    return (
        "friday" in text
        and (
            "couple" in text
            or "date night" in text
            or "date-night" in text
            or "date_night" in text
        )
    )

def _time_match(e, time_min):
    name = e.get('name') or ''
    if re.search('paella', name, re.I):
        return time_min is not None and 780 <= time_min <= 900
    if time_min is None:
        return True
    start_str = end_str = None
    for src in [e.get('schedule'), e.get('availability'), e.get('dining_schedule'), e]:
        if not isinstance(src, dict):
            continue
        if not start_str:
            start_str = src.get('start_time') or src.get('startTime') or src.get('open_time')
        if not end_str:
            end_str = src.get('end_time') or src.get('endTime') or src.get('close_time')
    if not start_str and (not end_str):
        if _is_friday_couple_experience(e):
            return time_min is not None and time_min >= 16 * 60
        return True

    def to_min(s):
        if not s:
            return None
        parts = str(s).split(':')
        return int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else None
    start_min = to_min(start_str)
    end_min = to_min(end_str)
    if start_min is not None and time_min < start_min:
        return False
    if end_min is not None and time_min > end_min:
        return False
    return True

def _date_range_match(e, selected_date):
    if not selected_date:
        return True
    start = end = None
    for src in [e.get('availability'), e.get('date_range'), e.get('schedule'), e]:
        if not isinstance(src, dict):
            continue
        if not start:
            start = src.get('start_date') or src.get('startDate') or src.get('start')
        if not end:
            end = src.get('end_date') or src.get('endDate') or src.get('end')
    if start or end:
        if start and selected_date < str(start)[:10]:
            return False
        if end and selected_date > str(end)[:10]:
            return False
        return True
    hd = _get_holiday_date(e.get('name') or '', selected_date)
    if hd is not None:
        return selected_date == hd
    return True

def _map_exp(e):
    pi = e.get('price_info') or {}
    price_label = ''
    prices = pi.get('prices') or []
    if prices:
        div = pi.get('multiplier') or 100
        if len(prices) == 1:
            amt = prices[0]['minUnitAmount'] / div
            price_label = f'${amt:.0f} / party' if pi.get('priceType') == 'PER_PARTY' else f'${amt:.0f} / person'
        else:
            amounts = [p['minUnitAmount'] / div for p in prices]
            lo, hi = (min(amounts), max(amounts))
            price_label = f'${lo:.0f}-${hi:.0f} / person'
        if pi.get('prePaymentRequired'):
            price_label += ' (pre-pay)'
    return {'experience_id': e.get('experience_id'), 'version': e.get('version'), 'name': e.get('name') or '', 'description': e.get('description') or '', 'price_label': price_label, 'pre_payment_required': bool(pi.get('prePaymentRequired')), 'has_addons': bool((e.get('add_ons_summary') or {}).get('available') == 'OPTIONAL')}

def handle_restaurant_info():
    return {'success': True, 'restaurants': [{'rid': 1192183, 'name': 'Adega Gaucha - Orlando', 'address': '8598 International Dr, Orlando, FL 32819', 'phone': '(407) 203-1088', 'party_min': 1, 'party_max': 20}, {'rid': 1372396, 'name': 'Adega Gaucha - Kissimmee', 'address': '3045 Entry Point Blvd, Kissimmee, FL 34747', 'phone': '(407) 507-3188', 'party_min': 1, 'party_max': 10}, {'rid': 1372399, 'name': 'Adega Gaucha - Deerfield Beach', 'address': '1401 S Federal Hwy, Deerfield Beach, FL 33441', 'phone': '(754) 223-4288', 'party_min': 1, 'party_max': 10}], 'generated_at': now_iso()}

async def handle_check_availability(body, rid, token, client):
    date_str = (body.get('date') or '').strip()
    party_size = int(body.get('partySize') or body.get('party_size') or 2)
    chosen_time = body.get('time') or ''
    if not date_str or not re.match('^\\d{4}-\\d{2}-\\d{2}$', date_str):
        return {'success': False, 'error': 'Please select a date.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    try:
        chosen_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return {'success': False, 'error': 'Invalid date format.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    today = datetime.now().date()
    max_date = today + timedelta(days=90)
    if chosen_date < today:
        return {'success': False, 'error': 'Please select a future date.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    if chosen_date > max_date:
        return {'success': False, 'error': 'Reservations can be made up to 90 days in advance.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    if party_size > 20:
        return {'success': False, 'error': 'Maximum party size is 20 guests. Please call us for larger groups.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    rid_hours = HOURS.get(rid)
    if not rid_hours:
        return {'success': False, 'error': 'Restaurant not found. Please refresh and try again.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    js_dow = (chosen_date.weekday() + 1) % 7
    today_hours_str = rid_hours.get(js_dow)
    if not today_hours_str or today_hours_str == 'closed':
        return {'success': False, 'error': f'This restaurant is closed on {DAYS[js_dow]}. Please choose a different date.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    open_str, close_str = today_hours_str.split('-')
    open_h, open_m = map(int, open_str.split(':'))
    close_h, close_m = map(int, close_str.split(':'))
    if chosen_date == today:
        now_local = datetime.now()
        close_dt = datetime.strptime(f'{date_str}T{close_str}:00', '%Y-%m-%dT%H:%M:%S')
        if now_local + timedelta(hours=2) >= close_dt:
            return {'success': False, 'error': 'No more available times today. Please book for a future date.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    open_mins = open_h * 60 + open_m
    close_mins = close_h * 60 + close_m
    window_min = min(close_mins - open_mins, 720)
    start_dt = f'{date_str}T{open_str}:00'
    url = f'{OT_API_BASE}/availability/{rid}?start_date_time={start_dt}&forward_minutes={window_min}&backward_minutes=0&party_size={party_size}&include_credit_card_results=false&include_experiences=true'
    try:
        resp = await client.get(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=15.0)
    except httpx.RequestError as exc:
        return {'success': True, 'total_slots': 0, 'available_slots': [], 'no_availability_reasons': [], 'friendly_no_avail_msg': str(exc), 'open_time': open_str, 'generated_at': now_iso()}
    if resp.status_code == 403:
        err_msg = 'Credentials not authorised for this restaurant.'
    elif resp.status_code == 401:
        err_msg = 'Authentication failed. Please refresh and try again.'
    elif resp.status_code == 404:
        err_msg = 'Restaurant not found.'
    elif resp.status_code != 200:
        err_msg = f'API error {resp.status_code}.'
    else:
        err_msg = None
    if err_msg:
        return {'success': True, 'total_slots': 0, 'available_slots': [], 'no_availability_reasons': [], 'friendly_no_avail_msg': err_msg, 'open_time': open_str, 'generated_at': now_iso()}
    raw = _safe_json(resp)
    REASON_MAP = {'NoTimesExist': None, 'NoAvailability': None, 'NotFarEnoughInAdvance': 'Please try a date further in the future.', 'TooFarInAdvance': 'Too far ahead. Please choose a closer date.', 'FullyBooked': 'Fully booked on this date. Please try a different date.', 'RestaurantClosed': 'The restaurant is closed on this date.', 'OutsideHours': 'Outside operating hours. Try a different time.', 'PartyTooLarge': 'Party size too large. Please call us directly.', 'PartyTooSmall': 'Party size too small.', 'NoShiftsAvailable': 'No dining shifts on this date. Try a different date.', 'PrivateEvent': 'Closed for a private event on this date.', 'TemporarilyClosed': 'Temporarily closed. Please try another date.', 'NoOnlineReservations': 'Online reservations unavailable. Please call us.', 'ShiftFull': 'Fully booked at this time. Try a different time.', 'DuplicateReservation': 'You already have a reservation at this time.', 'TableUnavailable': 'No tables available. Please try another time.'}
    reasons = [REASON_MAP[r] for r in raw.get('no_availability_reasons') or [] if r in REASON_MAP and REASON_MAP[r] is not None]
    ta_map = {ta['time']: ta for ta in raw.get('times_available') or [] if 'time' in ta}
    seen_times = set()
    all_slots = []
    for t in raw.get('times') or []:
        full_dt = t
        if t and (not re.match('^\\d{4}-\\d{2}-\\d{2}T', t)) and date_str:
            full_dt = date_str + 'T' + t[:5]
        elif t and len(t) > 16:
            full_dt = t[:16]
        if not full_dt or full_dt in seen_times:
            continue
        seen_times.add(full_dt)
        time_part = (full_dt.split('T')[1] if 'T' in full_dt else '00:00')[:5]
        h, m = map(int, time_part.split(':'))
        ampm = 'PM' if h >= 12 else 'AM'
        h12 = h - 12 if h > 12 else 12 if h == 0 else h
        ta = ta_map.get(t, {})
        types = ta.get('availability_types') or []
        exp_raw = ta.get('experiences')
        exp_ids = [e.get('experience_id') or e.get('id') for e in exp_raw if e.get('experience_id') or e.get('id')] if exp_raw is not None else None
        all_slots.append({'date_time': full_dt, 'display_time': f'{h12}:{m:02d} {ampm}', 'hour': h, 'requires_credit_card': any(((at.get('cancellation_policy') or {}).get('type') for at in types)), 'valid_experience_ids': exp_ids})
    all_slots.sort(key=lambda s: s['date_time'])
    slots = all_slots
    if chosen_time and all_slots:
        try:
            ph, pm2 = map(int, str(chosen_time).split(':')[:2])
            pref = ph * 60 + pm2
            windowed = [s for s in all_slots if abs(s['hour'] * 60 - pref) <= 180]
            slots = windowed if len(windowed) >= 2 else all_slots
        except (ValueError, AttributeError):
            pass
    friendly_msg = reasons[0] if not slots and reasons else None
    return {'success': True, 'rid': raw.get('rid') or rid, 'party_size': raw.get('party_size') or party_size, 'total_slots': len(slots), 'available_slots': slots, 'no_availability_reasons': reasons, 'friendly_no_avail_msg': friendly_msg, 'open_time': open_str, 'close_time': close_str, 'generated_at': now_iso()}

async def handle_availability_metadata(rid, token, client):
    try:
        resp = await client.get(f'{OT_API_BASE}/availability-metadata/{rid}', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=10.0)
    except httpx.RequestError as exc:
        return {'success': False, 'error': str(exc), 'code': 'API_ERROR', 'generated_at': now_iso()}
    if resp.status_code != 200:
        return {'success': False, 'error': f'API error {resp.status_code}', 'code': 'API_ERROR', 'generated_at': now_iso()}
    raw = _safe_json(resp)
    data = raw.get('data') or raw
    return {'success': True, 'environments': data.get('environments') or [], 'dining_areas': [{'id': a.get('id'), 'name': a.get('name'), 'description': a.get('description') or '', 'environment': a.get('environment')} for a in data.get('dining_areas') or []], 'attributes': data.get('attributes') or [], 'generated_at': now_iso()}

async def handle_lock_slot(body, rid, token, client):
    """Handles both lock-slot and lock-experience (same OT endpoint)."""
    party_size = int(body.get('partySize') or body.get('party_size') or 2)
    date_time = (body.get('dateTime') or body.get('date_time') or '').strip()
    search_date = (body.get('searchDate') or body.get('search_date') or '').strip()
    attribute = body.get('reservation_attribute') or body.get('reservationAttribute') or 'default'
    if date_time and search_date and (not re.match('^\\d{4}-\\d{2}-\\d{2}T', date_time)):
        time_part = date_time.split('T')[-1][:5]
        date_time = search_date + 'T' + time_part
    if date_time and len(date_time) > 16:
        date_time = date_time[:16]
    if not date_time or not re.match('^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}', date_time):
        return {'success': False, 'error': f"date_time required in ISO format (e.g. 2026-04-15T19:00). Received: {body.get('dateTime') or body.get('date_time')!r}", 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    lock_body = {'party_size': party_size, 'date_time': date_time, 'reservation_attribute': attribute}
    if body.get('dining_area_id'):
        lock_body['dining_area_id'] = int(body['dining_area_id'])
    if body.get('environment'):
        lock_body['environment'] = body['environment']
    if body.get('experience'):
        lock_body['experience'] = body['experience']
    try:
        resp = await client.post(f'{OT_API_BASE}/booking/{rid}/slot_locks', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json', 'Content-Type': 'application/json'}, json=lock_body, timeout=15.0)
    except httpx.RequestError as exc:
        return {'success': False, 'error': str(exc), 'code': 'API_ERROR', 'generated_at': now_iso()}
    raw = _safe_json(resp)
    if resp.status_code not in (200, 201):
        msg = _extract_ot_error(raw, f'API error {resp.status_code}')
        return {'success': False, 'error': msg, 'code': 'API_ERROR', 'generated_at': now_iso()}
    if raw.get('errors'):
        msg = _extract_ot_error(raw, 'Slot lock failed')
        return {'success': False, 'error': msg, 'code': 'SLOT_LOCK_FAILED', 'generated_at': now_iso()}
    return {'success': True, 'expires_at': raw.get('expires_at'), 'reservation_token': raw.get('reservation_token'), 'rid': rid, 'generated_at': now_iso()}

def _clean_booking_error(msg):
    m = msg.lower()
    if 'overlapping' in m or 'duplicate' in m:
        return 'You already have a reservation at this time. Please choose a different time.'
    if 'slot' in m and ('taken' in m or 'unavailable' in m or 'lock' in m):
        return 'This time slot is no longer available. Please go back and select a different time.'
    if 'token' in m or 'expired' in m:
        return 'Your time slot expired. Please go back and select a new time.'
    if 'closed' in m:
        return 'This restaurant is not accepting reservations at this time.'
    if 'notfarenough' in m or 'advance' in m:
        return 'This date requires more advance notice. Please try a later date.'
    if 'toofar' in m:
        return 'This date is too far in advance. Please choose a closer date.'
    if 'fullybooked' in m or 'fully booked' in m:
        return 'No tables available at this time. Please try a different time.'
    if 'partysize' in m or 'party_size' in m or 'party size' in m:
        return 'Party size not available. Please try fewer guests.'
    if 'phone' in m:
        return 'Invalid phone number. Please check your number and try again.'
    if 'email' in m:
        return 'Invalid email address. Please check your email and try again.'
    return 'Unable to complete reservation. Please try again or call us directly.'

async def handle_create_booking(body, rid, token, client):
    reservation_token = body.get('reservationToken') or body.get('reservation_token') or ''
    first_name = (body.get('firstName') or body.get('first_name') or '').strip()
    last_name = (body.get('lastName') or body.get('last_name') or '').strip()
    email_raw = (body.get('email') or body.get('email_address') or '').strip().lower()
    phone_raw = body.get('phone') or ''
    special_req = body.get('specialRequest') or body.get('special_request') or ''
    party_size = int(body.get('partySize') or body.get('party_size') or 2)
    raw_phone_str = phone_raw.get('number', '') if isinstance(phone_raw, dict) else str(phone_raw)
    digits = re.sub('[\\s\\-\\.\\(\\)]', '', raw_phone_str).lstrip('+')
    country_code, local_number = detect_phone(digits)
    override_cc = str(body.get('countryCode') or '').upper().strip()
    if len(override_cc) == 2:
        country_code = override_cc
    if not reservation_token:
        return {'success': False, 'error': 'Your time slot expired. Please go back and select a time again.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    if not first_name or not last_name:
        return {'success': False, 'error': 'Please enter your first and last name.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    if not email_raw or not re.match('^[^@]+@[^@]+\\.[^@]+$', email_raw):
        return {'success': False, 'error': 'Please enter a valid email address.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    if not digits or len(digits) < 4:
        return {'success': False, 'error': 'Please enter a phone number.', 'code': 'INVALID_REQUEST', 'generated_at': now_iso()}
    res_body = {'restaurant_id': rid, 'reservation_token': reservation_token, 'first_name': first_name, 'last_name': last_name, 'email_address': email_raw, 'phone': {'number': local_number, 'country_code': country_code, 'phone_type': 'Mobile'}, 'reservation_attribute': 'default', 'restaurant_email_marketing_opt_in': False}
    if special_req:
        res_body['special_request'] = special_req
    if body.get('experienceId'):
        res_body['experiences'] = [{'experience_id': body['experienceId'], 'version': body.get('experienceVersion', 1)}]
    try:
        resp = await client.post(f'{OT_API_BASE}/booking/{rid}/reservations', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Request-Id': str(uuid.uuid4())}, json=res_body, timeout=20.0)
    except httpx.RequestError as exc:
        return {'success': False, 'error': _clean_booking_error(str(exc)), 'generated_at': now_iso()}
    raw = _safe_json(resp)
    if resp.status_code not in (200, 201):
        msg = _extract_ot_error(raw, f'API error {resp.status_code}')
        return {'success': False, 'error': _clean_booking_error(msg), 'generated_at': now_iso()}
    if raw.get('errors'):
        msg = _extract_ot_error(raw, 'Reservation failed')
        return {'success': False, 'error': _clean_booking_error(msg), 'generated_at': now_iso()}
    raw_dt_str = raw.get('date_time') or ''
    display_date = raw_dt_str
    try:
        dt = datetime.fromisoformat(raw_dt_str.replace('Z', '+00:00'))
        h12 = dt.hour % 12 or 12
        ampm = 'AM' if dt.hour < 12 else 'PM'
        display_date = dt.strftime('%A, %B ') + str(dt.day) + ', ' + str(dt.year) + ' at ' + f'{h12}:{dt.minute:02d} {ampm}'
    except (ValueError, AttributeError):
        pass
    return {'success': True, 'confirmation_number': raw.get('confirmation_number'), 'confirmationNumber': raw.get('confirmation_number'), 'date_time': raw.get('date_time'), 'display_date': display_date, 'party_size': raw.get('party_size'), 'manage_reservation_url': raw.get('manage_reservation_url') or '', 'cancel_cutoff': raw.get('cancel_cutoff_date_utc'), 'guest_name': f'{first_name} {last_name}', 'guest_email': email_raw, 'guest_phone': raw_phone_str, 'restaurant_name': RID_TO_NAME.get(rid, 'Adega Gaucha'), 'party_size_confirmed': raw.get('party_size'), 'special_request': special_req, 'rid': rid, 'generated_at': now_iso()}

_KISSIMMEE_RULES = [
    ("gourmet table takeover",         {6},        13*60, 15*60),
    ("saturday tomahawk",              {6},        15*60, 23*60),
    ("picanha & gourmet table dinner", {1,2,3,4},  16*60, 23*60),
]

def _kissimmee_time_gate(exps, js_dow, time_min):
    if js_dow is None or time_min is None:
        return exps
    result = []
    for exp in exps:
        name_lower = (exp.get("name") or "").lower()
        restricted = False
        allowed = False
        for fragment, dows, t_start, t_end in _KISSIMMEE_RULES:
            if fragment in name_lower:
                restricted = True
                if js_dow in dows and t_start <= time_min < t_end:
                    allowed = True
                break
        if not restricted or allowed:
            result.append(exp)
    return result

_DEERFIELD_RULES = [
    ("saturday tomahawk",              {6},        15*60, 23*60),
    ("picanha & gourmet table dinner", {1,2,3,4},  16*60, 23*60),
]

def _deerfield_time_gate(exps, js_dow, time_min):
    if js_dow is None or time_min is None:
        return exps
    result = []
    for exp in exps:
        name_lower = (exp.get("name") or "").lower()
        restricted = False
        allowed = False
        for fragment, dows, t_start, t_end in _DEERFIELD_RULES:
            if fragment in name_lower:
                restricted = True
                if js_dow in dows and t_start <= time_min < t_end:
                    allowed = True
                break
        if not restricted or allowed:
            result.append(exp)
    return result

async def handle_check_experiences(body, rid, token, client):
    valid_ids = body.get('validExperienceIds')
    selected_dt = body.get('date')
    selected_tm = body.get('time')
    try:
        resp = await client.get(f'{OT_API_BASE}/experiences/{rid}/active', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=15.0)
    except httpx.RequestError as exc:
        return {'success': True, 'experiences': [], 'error': str(exc), 'generated_at': now_iso()}
    if resp.status_code != 200:
        return {'success': True, 'experiences': [], 'error': f'API error {resp.status_code}', 'generated_at': now_iso()}
    raw = _safe_json(resp)
    items = raw.get('data') if isinstance(raw.get('data'), list) else []
    bookable = [e for e in items if e.get('bookable') is not False]
    js_dow = None
    if selected_dt:
        try:
            d = datetime.strptime(selected_dt, '%Y-%m-%d').date()
            js_dow = (d.weekday() + 1) % 7
        except ValueError:
            pass
    time_min = None
    if selected_tm:
        try:
            parts = str(selected_tm).split(':')
            time_min = int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            pass
    filtered = []
    for e in bookable:
        if not _day_match(e, js_dow, selected_dt):
            continue
        if not _date_range_match(e, selected_dt):
            continue
        if not _time_match(e, time_min):
            continue
        filtered.append(_map_exp(e))
    if isinstance(valid_ids, list):
        if len(valid_ids) == 0:
            final_exp = []
        else:
            valid_set = set(valid_ids)
            final_exp = [
                _map_exp(e)
                for e in bookable
                if (e.get('experience_id') or e.get('id')) in valid_set
                and _day_match(e, js_dow, selected_dt)
                and _date_range_match(e, selected_dt)
                and _time_match(e, time_min)
            ]
    else:
        final_exp = filtered
    if rid in (1372396, 1192183):
        final_exp = _kissimmee_time_gate(final_exp, js_dow, time_min)
    elif rid == 1372399:
        final_exp = _deerfield_time_gate(final_exp, js_dow, time_min)
    return {'success': True, 'experiences': final_exp, 'total': len(final_exp), 'rid': rid, 'generated_at': now_iso()}

async def handle_release_slot(body, rid, token, client):
    reservation_token = body.get('reservationToken') or body.get('reservation_token') or ''
    if reservation_token:
        try:
            await client.delete(f'{OT_API_BASE}/booking/{rid}/slot_locks/{reservation_token}', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=8.0)
        except httpx.RequestError:
            pass
    return {'success': True, 'generated_at': now_iso()}

@app.post('/webhook/adega-book')
async def adega_book(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    headers = dict(request.headers)
    query = dict(request.query_params)
    api_key = headers.get('x-api-key') or headers.get('X-Api-Key') or ''
    action = (body.get('action') or query.get('action') or '').lower().strip()
    rid = int(body.get('rid') or 0)
    if api_key != WIDGET_API_KEY:
        return cors_json({'success': False, 'error': 'Invalid or missing API key.', 'code': 'VALIDATION_ERROR', 'generated_at': now_iso()}, 400)
    if action not in VALID_ACTIONS:
        return cors_json({'success': False, 'error': f'Invalid action: {action}', 'code': 'VALIDATION_ERROR', 'generated_at': now_iso()}, 400)
    if action != 'restaurant-info' and rid not in VALID_RIDS:
        return cors_json({'success': False, 'error': f'Invalid restaurant ID: {rid}', 'code': 'VALIDATION_ERROR', 'generated_at': now_iso()}, 400)
    if action == 'restaurant-info':
        return cors_json(handle_restaurant_info())
    async with httpx.AsyncClient() as client:
        try:
            token = await get_ot_token(client)
        except Exception as exc:
            return cors_json({'success': False, 'error': f'OAuth token request failed: {exc}', 'code': 'AUTH_ERROR', 'generated_at': now_iso()}, 401)
        if action == 'check-availability':
            result = await handle_check_availability(body, rid, token, client)
        elif action == 'availability-metadata':
            result = await handle_availability_metadata(rid, token, client)
        elif action in ('lock-slot', 'lock-experience'):
            result = await handle_lock_slot(body, rid, token, client)
        elif action == 'create-booking':
            result = await handle_create_booking(body, rid, token, client)
        elif action == 'check-experiences':
            result = await handle_check_experiences(body, rid, token, client)
        elif action == 'release-slot':
            result = await handle_release_slot(body, rid, token, client)
        else:
            result = {'success': False, 'error': 'Unknown action', 'code': 'UNKNOWN_ACTION', 'generated_at': now_iso()}
    status = 400 if result.get('code') in {'INVALID_REQUEST', 'UNKNOWN_ACTION'} else 200
    return cors_json(result, status)
if __name__ == '__main__':
    import uvicorn
    uvicorn.run('adega_reservation_api:app', host='0.0.0.0', port=8000, workers=4)
