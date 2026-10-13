#!/usr/bin/env python3
"""
Sync history.json's changelog from monday.com activity logs.

Runs in GitHub Actions (see .github/workflows/deploy.yml) with the API token in
the MONDAY_TOKEN env var. It reads the activity log of:
  - the Product Roadmap board  (FFS date, confidence, status columns, name,
                                group moves, items added / deleted)
  - the GTM Launches board     (FCS Date changes only)
and appends any new changes to history.json["changelog"] with the real time
and author of each change. Entries are de-duplicated by id, so re-running
over an overlapping time window is safe.

Usage:  MONDAY_TOKEN=... python3 scripts/update_history.py [path/to/history.json]
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROADMAP_BOARD = 18364310005
GTM_BOARD     = 18434045053
GTM_FCS_COL   = 'date_mm7v9p4f'
API_URL       = 'https://api.monday.com/v2'
API_VERSION   = '2024-01'

# Roadmap columns tracked in the changelog (id -> fallback title)
ROADMAP_COLS = {
    'date_mm06txhj':    'FFS',
    'numeric_mkz3rvnr': 'Confidence',
    'status':           'Type',
    'color_mkxf9nhp':   'Action',
    'boolean_mm06c6vy': 'Pre-Tool Review',
    'boolean_mm06jadd': 'Post-Tool Review',
}
GROUP_NAMES = {
    'group_title':    'Concept Development',
    'group_mkxfjfcm': 'Product Upgrades',
    'topics':         'Active Developments',
    'group_mkxncbsv': 'Completed Developments',
    'group_mkxnn81v': 'Ideas',
}
DEFAULT_START = '2026-05-01T00:00:00Z'   # first sync reaches back this far
OVERLAP       = timedelta(days=2)        # re-scan window; dedupe makes this safe


# ── helpers ──────────────────────────────────────────────────────────────────
def norm_name(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def ts_from_monday(created_at):
    """monday activity created_at is in 100-nanosecond units since the epoch."""
    secs = int(created_at) / 1e7
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def as_dict(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def cell_value(v, ctype):
    """Convert an activity-log column value into the string stored in history."""
    v = as_dict(v)
    if not v:
        return 'false' if ctype in ('boolean', 'checkbox') else None
    if ctype == 'date':
        return v.get('date') or None
    if ctype in ('numeric', 'numbers'):
        x = v.get('value')
        if x is None or x == '':
            return None
        x = float(x)
        return str(int(x)) if x.is_integer() else str(x)
    if ctype in ('color', 'status'):
        return (v.get('label') or {}).get('text') or None
    if ctype in ('boolean', 'checkbox'):
        return 'true' if v.get('checked') in (True, 'true') else 'false'
    if ctype == 'name':
        return v.get('name')
    return v.get('text') if isinstance(v, dict) else str(v)


def gtm_aliases(index_path):
    """Read GTM_NAME_ALIASES from index.html so there is one source of truth."""
    try:
        html = open(index_path, encoding='utf-8').read()
        block = re.search(r'GTM_NAME_ALIASES\s*=\s*\{(.*?)\};', html, re.S).group(1)
        pairs = re.findall(r"'([^']*)'\s*:\s*'([^']*)'", block)
        return {gtm: roadmap for roadmap, gtm in pairs}      # GTM name -> roadmap name
    except Exception:
        return {}


# ── log → changelog entries ──────────────────────────────────────────────────
def entry(log, board, pulse_id, field, field_title, frm, to, **kw):
    e = {
        'id':         f"{board}:{log['created_at']}:{log['event']}:{pulse_id}:{field}",
        'timestamp':  ts_from_monday(log['created_at']),
        'field':      field,
        'fieldTitle': field_title,
        'from':       frm,
        'to':         to,
        'changedBy':  kw.pop('users', {}).get(str(log.get('user_id'))),
    }
    e.update(kw)
    return e


def roadmap_entries(logs, users):
    out = []
    for log in logs:
        ev, d = log['event'], as_dict(log.get('data')) or {}
        pid = str(d.get('pulse_id') or (d.get('pulse') or {}).get('id') or '')
        gid = d.get('group_id')
        common = dict(itemId=pid, groupId=gid, groupTitle=GROUP_NAMES.get(gid, gid), users=users)

        if ev == 'update_column_value' and d.get('column_id') in ROADMAP_COLS:
            ctype = d.get('column_type')
            frm, to = cell_value(d.get('previous_value'), ctype), cell_value(d.get('value'), ctype)
            if frm != to:
                out.append(entry(log, ROADMAP_BOARD, pid, d['column_id'],
                                 d.get('column_title') or ROADMAP_COLS[d['column_id']],
                                 frm, to, itemName=d.get('pulse_name'), **common))
        elif ev == 'update_name':
            frm, to = (as_dict(d.get('previous_value')) or {}).get('name'), (as_dict(d.get('value')) or {}).get('name')
            if frm != to:
                out.append(entry(log, ROADMAP_BOARD, pid, 'name', 'Name', frm, to, itemName=to, **common))
        elif ev == 'move_pulse_from_group':
            src, dst = d.get('source_group') or {}, d.get('dest_group') or {}
            common.update(groupId=dst.get('id'), groupTitle=GROUP_NAMES.get(dst.get('id'), dst.get('title')))
            out.append(entry(log, ROADMAP_BOARD, pid, '_group', 'Moved to group',
                             GROUP_NAMES.get(src.get('id'), src.get('title')), common['groupTitle'],
                             itemName=(d.get('pulse') or {}).get('name'), **common))
        elif ev == 'create_pulse':
            out.append(entry(log, ROADMAP_BOARD, pid, '_added', 'Added to board', None,
                             common['groupTitle'] or d.get('group_name'),
                             itemName=d.get('pulse_name'), **common))
        elif ev in ('delete_pulse', 'archive_pulse'):
            out.append(entry(log, ROADMAP_BOARD, pid, '_deleted',
                             'Archived' if ev == 'archive_pulse' else 'Deleted', None,
                             'Archived' if ev == 'archive_pulse' else 'Deleted',
                             itemName=d.get('pulse_name'), **common))
    return out


def gtm_entries(logs, users, roadmap_items, aliases):
    """FCS Date changes on GTM Launches, attributed to the matching roadmap item."""
    by_name = {norm_name(i['name']): i for i in roadmap_items}
    out = []
    for log in logs:
        ev, d = log['event'], as_dict(log.get('data')) or {}
        frm = to = None
        if ev == 'update_column_value' and d.get('column_id') == GTM_FCS_COL:
            frm, to = cell_value(d.get('previous_value'), 'date'), cell_value(d.get('value'), 'date')
        elif ev == 'create_pulse':
            cols = as_dict(d.get('column_values_json')) or {}
            to = cell_value(cols.get(GTM_FCS_COL), 'date')
        else:
            continue
        if frm == to:
            continue
        gtm_name = d.get('pulse_name')
        match = by_name.get(norm_name(aliases.get(gtm_name, gtm_name)))
        pid = str(d.get('pulse_id'))
        out.append(entry(log, GTM_BOARD, pid, 'fcs_date', 'FCS Date', frm, to,
                         itemId=match['id'] if match else f'gtm-{pid}',
                         itemName=match['name'] if match else gtm_name,
                         groupId='gtm', groupTitle='GTM Launches', users=users))
    return out


# ── monday API ───────────────────────────────────────────────────────────────
def gql(token, query):
    req = urllib.request.Request(API_URL, data=json.dumps({'query': query}).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': token, 'API-Version': API_VERSION})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())
    if res.get('errors'):
        raise RuntimeError('; '.join(e.get('message', '') for e in res['errors']))
    return res['data']


def fetch_logs(token, board, since_iso, now_iso):
    logs, page = [], 1
    while True:
        q = f'''{{ boards(ids: [{board}]) {{ activity_logs(from: "{since_iso}", to: "{now_iso}",
                 limit: 500, page: {page}) {{ id event user_id data created_at }} }} }}'''
        batch = gql(token, q)['boards'][0]['activity_logs'] or []
        logs += batch
        if len(batch) < 500:
            return logs
        page += 1


def fetch_roadmap_items(token):
    q = f'{{ boards(ids: [{ROADMAP_BOARD}]) {{ items_page(limit: 500) {{ items {{ id name }} }} }} }}'
    return gql(token, q)['boards'][0]['items_page']['items']


def fetch_users(token, ids):
    if not ids:
        return {}
    try:
        q = '{ users(ids: [%s]) { id name } }' % ', '.join(ids)
        return {str(u['id']): u['name'] for u in gql(token, q)['users']}
    except Exception as e:
        print(f'warning: could not resolve user names ({e})')
        return {}


# ── main ─────────────────────────────────────────────────────────────────────
def merge(history, new_entries):
    log = history.setdefault('changelog', [])
    seen = {e.get('id') for e in log}
    added = [e for e in new_entries if e['id'] not in seen]
    log.extend(added)
    log.sort(key=lambda e: e['timestamp'])
    return added


def since_for(history):
    stamps = [e['timestamp'] for e in history.get('changelog', []) if e.get('id')]
    if not stamps:
        return DEFAULT_START
    last = datetime.fromisoformat(max(stamps).replace('Z', '+00:00'))
    return (last - OVERLAP).strftime('%Y-%m-%dT%H:%M:%SZ')


def update_meta(history):
    meta = history.setdefault('_meta', {})
    meta['description'] = ('Change history for the Product Roadmap board (FFS, confidence, status, '
                           'group moves) and FCS Date changes from the GTM Launches board. Synced '
                           'from monday.com activity logs by scripts/update_history.py.')
    fields = meta.setdefault('tracked_fields', [])
    if not any(f.get('id') == 'fcs_date' for f in fields):
        fields.append({'id': 'fcs_date', 'title': 'FCS Date', 'type': 'date',
                       'source': f'GTM Launches ({GTM_BOARD}) column {GTM_FCS_COL}'})


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'history.json'
    token = os.environ.get('MONDAY_TOKEN')
    if not token:
        sys.exit('MONDAY_TOKEN is not set')
    history = json.load(open(path, encoding='utf-8'))
    since = since_for(history)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'Syncing activity from {since} to {now}')

    rm_logs  = fetch_logs(token, ROADMAP_BOARD, since, now)
    gtm_logs = fetch_logs(token, GTM_BOARD, since, now)
    users    = fetch_users(token, sorted({str(l['user_id']) for l in rm_logs + gtm_logs if l.get('user_id')}))
    aliases  = gtm_aliases(os.path.join(os.path.dirname(os.path.abspath(path)), 'index.html'))

    new = roadmap_entries(rm_logs, users) + gtm_entries(gtm_logs, users, fetch_roadmap_items(token), aliases)
    added = merge(history, new)
    update_meta(history)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print(f'{len(rm_logs)} roadmap + {len(gtm_logs)} GTM log events; {len(added)} new changelog entries')


if __name__ == '__main__':
    main()
