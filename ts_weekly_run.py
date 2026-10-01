#!/usr/bin/env python3
"""
OneTable Trust and Safety Weekly Run Script
Produces ts_ui_data JSON directly from CSV + Salesforce queries.
No manual transcription -- every JSON field is derived from structured data.

Usage:
  python3 ts_weekly_run.py <combined_csv_path> [--no-sf]
  
  Combined CSV contains both Contact and Lead guest rows in one file.
  Contact rows are identified by populated Contact ID field.
  Lead rows (plus-ones) are identified by populated Lead ID field and no Contact ID.
  Lead rows never have Platform Profile IDs and are excluded from sequential PID calculations.
  
  Legacy two-file mode still supported:
  python3 ts_weekly_run.py <contact_csv_path> <lead_csv_path> [--no-sf]

Output:
  Prints a single ```ts_ui_data ... ``` block to stdout.
  Also writes ts_ui_data_<date>.json to /home/claude/
"""

import csv, collections, json, sys, re
from datetime import datetime, date

# ── config ────────────────────────────────────────────────────────────────────
REVIEW_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)  # Set dynamically at runtime
SUSPICIOUS_DOMAINS = {
    'atomicmail.io','mailshield.org','tutamail.com','otheremail.org',
    'bumpmail.io','simplelogin.com','membermail.net','freemail.is','ourisp.net',
    'altaddress.org','dropons.com','jourrapide.com','armyspy.com','teleworm.us','dayrep.com',
    # Added 2026-10-01 from Alfie Elliott case
    'hudzer.com','flakeian.com','cwsgear.com','mail2usa.com',
}
HIGH_VOLUME_THRESHOLD = 20   # FP on 20+ dinners = shared infrastructure, not scored
SIGNAL_WEIGHTS = {
    'sig1': 7,   # Shared device FP host+guest (standalone)
    'sig2': 7,   # Cross-dinner device FP match (needs pairing)
    'sig3': 5,   # Same device FP across guests (needs pairing)
    'sig4': 5,   # Sequential RSVP timing (needs pairing) -- not in CSV, skip
    'sig5': 3,   # VPN use (needs pairing) -- not reliably in CSV, skip
    'sig6': 0,   # Geographic mismatch (watch flag only)
    'sig7': 2,   # Same IP across guests (needs pairing, 80%+)
    'sig8': 0,   # Clearly fake guest identities -- anomaly note only, does not score
    'sig9': 0,   # Suspicious guest email domains -- anomaly note only, does not score (only meaningful with bounce)
    'sig10': 4,  # Suspicious phone number patterns (needs pairing, 50%+)
    'sig11': 4,  # Host/guest email similarity (needs pairing)
    'sig12_low': 4,  # Guest email bounces 50-74% (any type, corroborating)
    'sig12': 8,  # Guest email bounces 75%+ (any type, high-confidence, Warning DNN eligible)
    'sig14_low': 3,  # Sequential guest PIDs 55-99% of profiled (corroborating, can score standalone)
    'sig14': 6,  # Sequential guest PIDs 100% of profiled (Warning-eligible standalone)
    'sig15': 3,  # Recycled bounced guest lists (needs pairing -- same exact email across 2+ dinners AND bounced)
    'sig16': 1,  # Privacy domain no bounce (needs pairing)
    'sig17': 3,  # AI not pass (needs guest integrity signal)
    'sig18': 3,  # Description degradation (needs pairing)
    'sig19': 2,  # Privacy type mismatch (needs pairing)
    'sig20': 2,  # Multiple future dinners (needs pairing)
    'sig21': 6,  # Reports from users (standalone)
    'sig22': 10, # Deliberate fraud (standalone, staff judgment)
    'sig23': 8,  # Deliberate identity change (standalone, staff judgment)
}
STANDALONE = {'sig1', 'sig12', 'sig22', 'sig23', 'sig14', 'sig14_low'}
GUEST_INTEGRITY_SIGNALS = {'sig12', 'sig12_low', 'sig14', 'sig14_low', 'sig10'}
SF_BASE = "https://onetable.lightning.force.com/lightning/r/Contact/{}/view"
SF_CAMPAIGN_BASE = "https://onetable.lightning.force.com/lightning/r/Campaign/{}/view"
TS_RECORD_TYPE_ID = '012PO000001F53dYAC'  # Trust and Safety record type for Salesforce Cases
FP_BASE = "https://api.onetable.org/cp/device_activity/details?fingerprint={}"


def sf_15_to_18(id15):
    """Convert 15-char Salesforce ID to 18-char. Used only as fallback."""
    if not id15 or len(id15) != 15:
        return id15
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    suffix = ""
    for i in range(3):
        chunk = id15[i*5:(i+1)*5]
        val = sum(1 << j for j, c in enumerate(chunk) if c.isupper())
        suffix += chars[val]
    return id15 + suffix


def parse_csv(contact_path, lead_path=None):
    """Parse a single combined CSV (or Contact + Lead CSVs) into campaigns dict.
    
    Combined CSV: one report containing both Contact and Lead guest rows.
    Contact guest rows have Contact ID populated; Lead guest rows have Lead ID populated.
    Lead rows never have Platform Profile IDs -- they are always plus-ones.
    
    Legacy two-file mode still supported if lead_path is provided.
    """
    KEEP = {
        "Campaign ID","Start Date","Campaign Status","Address","Member Status",
        "Platform Profile ID","First Name","Last Name","Host?","Campaign Member Email",
        "Mandrill Bounce Reason","Mandrill Bounce Time/Date","RSVP Device Fingerprint ID",
        "RSVP IP","Contact ID","Lead ID","Do Not Nourish","Suspended Flag","Problem Flag",
        "Problem Flag Reason","AI Not Pass Summary","Further Review Reason",
        "Unresolved Further Review Reason","Dinner Privacy","Host Application Date OLD",
        "Host Application Date","Campaign Name","FYI Flag Reason",
        "Dinner Created Device ID","Total Eligible Nourishment","Grant Application",
        "Campaign Description","Total Nourishment Received","Requested Nourishment","Notes",
        "Email","Lead: Created Date","Mandrill Bounce Time + Date",
        # New device/IP fields (populated from 2026-08-21 onward; legacy RSVP fields empty from that date)
        "Device ID","IP Address Reservation"
    }
    ONETABLE_DOMAIN = 'onetable.org'

    def read_and_classify(path):
        """Read CSV rows and classify each as host, contact guest, or lead guest."""
        rows = []
        with open(path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                r = {k: row.get(k, '').strip() for k in reader.fieldnames if k in KEEP}
                # Normalise email field -- Lead rows may use 'Email' instead of 'Campaign Member Email'
                if not r.get('Campaign Member Email') and r.get('Email'):
                    r['Campaign Member Email'] = r.pop('Email', '')
                # Device/IP field migration: Device_ID__c and IP_Address_Reservation__c replaced
                # RSVP_Device_Fingerprint_ID__c and RSVP_IP__c from 2026-08-21. Coalesce so every
                # downstream signal reads one field. Legacy field wins when both are populated.
                if not r.get('RSVP Device Fingerprint ID') and r.get('Device ID'):
                    r['RSVP Device Fingerprint ID'] = r['Device ID']
                if not r.get('RSVP IP') and r.get('IP Address Reservation'):
                    r['RSVP IP'] = r['IP Address Reservation']
                # Classify as Lead if Lead ID present and no Contact ID
                contact_id = r.get('Contact ID', '').strip()
                lead_id = r.get('Lead ID', '').strip()
                r['is_lead'] = bool(lead_id and not contact_id)
                # Lead guests never have Profile IDs
                if r['is_lead']:
                    r['Platform Profile ID'] = ''
                rows.append(r)
        return rows

    all_rows = read_and_classify(contact_path)

    # Legacy two-file mode
    if lead_path:
        lead_rows = read_and_classify(lead_path)
        for r in lead_rows:
            r['is_lead'] = True
            r['Platform Profile ID'] = ''
        all_rows += lead_rows

    campaigns = collections.defaultdict(lambda: {
        'host': None, 'guests': [], 'name': '', 'address': '', 'description': '',
        '_skip': False, '_existing_case': False, '_suspended_status': ''
    })

    for row in all_rows:
        cid = row.get('Campaign ID', '')
        ms = row.get('Member Status', '')

        if ms == 'Host':
            # Staff exclusion
            host_email = row.get('Campaign Member Email', '').lower().strip()
            if host_email.endswith('@' + ONETABLE_DOMAIN):
                campaigns[cid]['_skip'] = True
                continue

            # Suspended host filter
            suspended = str(row.get('Suspended Flag', '')).strip() in ('1', '1.0', 'True', 'true')
            dnn = str(row.get('Do Not Nourish', '')).strip() in ('1', '1.0', 'True', 'true')
            status = row.get('Campaign Status', '').strip().lower()
            if suspended and status in ('not nourishing', 'aborted'):
                campaigns[cid]['_skip'] = True
                continue

            campaigns[cid]['host'] = row
            campaigns[cid]['name'] = row.get('Campaign Name', '')
            campaigns[cid]['address'] = row.get('Address', '')
            campaigns[cid]['description'] = row.get('Campaign Description', '')

            # Suspended with active dinner → existing cases, high priority
            if suspended and status not in ('not nourishing', 'aborted'):
                campaigns[cid]['_existing_case'] = True
                campaigns[cid]['_suspended_status'] = row.get('Campaign Status', '')
                campaigns[cid]['_existing_case_type'] = 'suspension'

            # DNN (not suspended) with active dinner → existing cases, softer flag
            elif dnn and not suspended and status not in ('not nourishing', 'aborted'):
                campaigns[cid]['_existing_case'] = True
                campaigns[cid]['_suspended_status'] = row.get('Campaign Status', '')
                campaigns[cid]['_existing_case_type'] = 'dnn'

        elif ms in ('Attended', 'Applied', 'Pending', 'Guest of Guest'):
            campaigns[cid]['guests'].append(row)

        if not campaigns[cid]['description'] and row.get('Campaign Description', '').strip():
            campaigns[cid]['description'] = row.get('Campaign Description', '')

    filtered = {k: v for k, v in campaigns.items() if not v.get('_skip')}
    return dict(filtered), len(all_rows)


def build_fp_maps(campaigns):
    """Build cross-dinner FP map using full IDs."""
    all_fps = collections.defaultdict(set)
    for cid, camp in campaigns.items():
        if camp['host']:
            fp = camp['host'].get('RSVP Device Fingerprint ID', '')
            if fp:
                all_fps[fp].add(cid)
        for g in camp['guests']:
            fp = g.get('RSVP Device Fingerprint ID', '')
            if fp:
                all_fps[fp].add(cid)
    high_volume = {fp for fp, cids in all_fps.items() if len(cids) >= HIGH_VOLUME_THRESHOLD}
    cross_dinner = {fp: list(cids) for fp, cids in all_fps.items()
                    if 3 <= len(cids) < HIGH_VOLUME_THRESHOLD}
    all_cross = {fp: list(cids) for fp, cids in all_fps.items() if len(cids) >= 2}
    return high_volume, cross_dinner, all_cross, all_fps


def score_campaign(cid, camp, cross_dinner, high_volume):
    """
    Score a single campaign. Returns dict of raw signals keyed by signal ID.
    Pairing is applied AFTER all raw signals are computed to avoid circular deps.
    """
    host = camp['host']
    guests = camp['guests']
    n = len(guests)
    raw = {}  # sig_key -> {weight, desc, observed, threshold, threshold_met, score_contribution}

    def add_sig(key, desc, observed, threshold, threshold_met, contribution=None):
        w = SIGNAL_WEIGHTS[key]
        raw[key] = {
            'name': SIG_NAMES[key],
            'weight': w,
            'desc': desc,
            'observed': observed,
            'threshold': threshold,
            'threshold_met': threshold_met,
            'score_contribution': contribution if contribution is not None else (w if threshold_met else 0),
            'triggered': threshold_met,
        }

    # ── Signal 1: Shared FP host+guest (standalone, 25%+, min 3 guests) ──────
    host_fp = host.get('RSVP Device Fingerprint ID', '') if host else ''
    guest_fps = [g.get('RSVP Device Fingerprint ID', '') for g in guests
                 if g.get('RSVP Device Fingerprint ID', '')]
    if host_fp and n >= 3:  # min 3 guests required for device FP signals
        shared = [fp for fp in guest_fps if fp == host_fp]
        pct = len(shared) / n
        met = pct >= 0.25
        add_sig('sig1',
                f"Shared device fingerprint host and guest: {len(shared)}/{n} ({round(100*pct)}%) [{host_fp}]",
                f"{round(100*pct)}% ({len(shared)}/{n} guests)", "25%+ (min 3 guests)", met)

    # ── Signal 2: Cross-dinner FP match (needs pairing, any, skip high-volume) ──
    # Only score if 2+ guests in this dinner share the cross-dinner FP
    dinner_fps = set(guest_fps)
    if host_fp:
        dinner_fps.add(host_fp)
    cross_hits = []
    for fp, nd in [(fp, len(cross_dinner[fp])) for fp in dinner_fps
                   if fp in cross_dinner and fp not in high_volume]:
        # Count how many guests in THIS dinner share this FP
        guests_with_fp = guest_fps.count(fp) if hasattr(guest_fps, 'count') else sum(1 for g in guest_fps if g == fp)
        # Also count host if host has it
        if host_fp == fp:
            # host+guest case -- sig1 handles this; for sig2 count guests only
            pass
        if guests_with_fp >= 3 or (guests_with_fp >= 2 and host_fp == fp):
            cross_hits.append((fp, nd, guests_with_fp))
    if cross_hits and n >= 3:  # min 3 guests required
        total_weight = SIGNAL_WEIGHTS['sig2'] * len(cross_hits)
        descs = [f"{fp} across {nd} dinners ({gc} guests share it here)" for fp, nd, gc in cross_hits]
        add_sig('sig2',
                "Cross-dinner device fingerprint match: " + "; ".join(descs),
                f"{len(cross_hits)} FP(s) across multiple dinners",
                "Any (needs pairing, min 3 guests, ≥2 guests sharing)", True,
                contribution=total_weight)

    # ── Signal 3: Same FP across guests (needs pairing, 25%+, min 3 guests, min 2 sharing) ──
    if n >= 3 and guest_fps:  # min 3 guests required
        fp_counts = collections.Counter(guest_fps)
        top_fp, top_count = fp_counts.most_common(1)[0]
        if top_fp not in high_volume:
            pct = top_count / n
            # Require at least 2 guests sharing the same FP -- 1 guest is not meaningful
            met = pct >= 0.40 and top_count >= 3
            add_sig('sig3',
                    f"Same device fingerprint across guests: {top_count}/{n} ({round(100*pct)}%) [{top_fp}]",
                    f"{round(100*pct)}% ({top_count}/{n} guests)", "40%+ and ≥3 guests sharing (min 3 guests total)", met)

    # ── Signal 12: Guest email bounces (any type -- hard or reject combined) ──
    # 50-74% = weight 4 corroborating; 75%+ = weight 8 high-confidence Warning DNN eligible
    # Bounces on throwaway domains always count regardless of Mandrill classification
    def is_bounce(g):
        reason = g.get('Mandrill Bounce Reason', '').lower()
        if not reason or reason == 'nan': return False
        email = g.get('Campaign Member Email', '').lower()
        domain = email.split('@')[1] if '@' in email else ''
        if 'bounce' in reason or 'reject' in reason or 'invalid' in reason: return True
        if domain in SUSPICIOUS_DOMAINS and reason.strip(): return True
        return False

    bounced = [g for g in guests if is_bounce(g)]
    if n > 0:
        pct = len(bounced) / n
        if pct >= 0.75:
            add_sig('sig12',
                    f"Guest email bounces (75%+): {len(bounced)}/{n} ({round(100*pct)}%) -- any bounce type",
                    f"{round(100*pct)}% ({len(bounced)}/{n} guests)", "75%+", True)
        elif pct >= 0.50:
            add_sig('sig12_low',
                    f"Guest email bounces (50-74%): {len(bounced)}/{n} ({round(100*pct)}%) -- any bounce type",
                    f"{round(100*pct)}% ({len(bounced)}/{n} guests)", "50-74% (needs pairing)", True)

    # ── Signal 14: Sequential PIDs (profiled guests only as denominator) ────────
    # 55-99% of profiled = weight 3 corroborating (sig14_low, can score standalone)
    # 100% of profiled = weight 6 Warning-eligible standalone (sig14)
    pids = []
    for g in guests:
        try:
            pids.append((int(float(g.get('Platform Profile ID', ''))), g))
        except (ValueError, TypeError):
            pass
    profiled_n = len(pids)  # denominator = profiled guests ONLY (plus-ones excluded)
    if profiled_n >= 2:
        ps = sorted(pids, key=lambda x: x[0])
        pid_vals = [p[0] for p in ps]
        sequential_pairs = sum(1 for i in range(len(pid_vals)-1) if pid_vals[i+1] - pid_vals[i] <= 2)
        total_pairs = len(pid_vals) - 1
        seq_pct = sequential_pairs / total_pairs if total_pairs > 0 else 0
        if seq_pct >= 1.0:
            add_sig('sig14',
                    f"Sequential guest Profile IDs (100% of profiled): {profiled_n}/{profiled_n} profiled guests fully sequential (gap≤2). Plus-ones excluded from calculation.",
                    f"100% ({profiled_n}/{profiled_n} profiled guests)", "100% of profiled guests", True)
        elif seq_pct >= 0.55:
            add_sig('sig14_low',
                    f"Sequential guest Profile IDs (55-99% of profiled): {round(100*seq_pct)}% of {profiled_n} profiled guests sequential (gap≤2). Plus-ones excluded from calculation.",
                    f"{round(100*seq_pct)}% ({profiled_n} profiled guests)", "55-99% of profiled guests", True)

    # ── Signal 9: Suspicious email domains -- anomaly note only, does not score ──
    # Without a confirmed bounce, domain alone is not sufficient evidence.
    # Surfaced as context when bounces are also present.
    sus = [g for g in guests if
           any(g.get('Campaign Member Email', '').lower().endswith('@' + d)
               for d in SUSPICIOUS_DOMAINS)]
    if n > 0 and len(sus) > 0:
        pct = len(sus) / n
        add_sig('sig9',
                f"Suspicious guest email domains (anomaly note only -- does not score without confirmed bounce): {len(sus)}/{n} ({round(100*pct)}%)",
                f"{round(100*pct)}% ({len(sus)}/{n} guests)", "Anomaly note only", False)

    # ── Signal 8: Clearly fake guest identities -- anomaly note only, does not score ──
    # Only meaningful when combined with bounces. Surfaced as context note.
    FAKE_NAMES = {
        'taylor swift', 'bing bong', 'mickey mouse', 'john doe', 'jane doe',
        'test user', 'test guest', 'asdf', 'qwerty', 'xxx', 'aaa', 'bbb',
        'fake user', 'fake guest', 'no name', 'anonymous', 'guest guest',
    }
    def is_fake_name(g):
        first = g.get('First Name', '').strip().lower()
        last = g.get('Last Name', '').strip().lower()
        full = f"{first} {last}".strip()
        if full in FAKE_NAMES: return True
        if not first or not last: return True
        if first == last: return True
        if all(c == first[0] for c in first) or all(c == last[0] for c in last): return True
        return False
    fake_guests = [g for g in guests if is_fake_name(g)]
    # Note: single-letter last names are NOT treated as fake
    if n > 0 and len(fake_guests) >= 2:
        pct = len(fake_guests) / n
        examples = ', '.join(
            f"{g.get('First Name','')} {g.get('Last Name','')}".strip()
            for g in fake_guests[:3]
        )
        add_sig('sig8',
                f"Possible fake guest identities (anomaly note only -- does not score): {len(fake_guests)}/{n} ({round(100*pct)}%) -- e.g. {examples}",
                f"{round(100*pct)}% ({len(fake_guests)}/{n} guests)", "Anomaly note only", False)

    # ── Signal 16: Privacy email domain + no hard bounce (needs pairing) ──────
    # Guests using known privacy domains (not already in SUSPICIOUS_DOMAINS) with no bounce
    PRIVACY_DOMAINS = {'protonmail.com', 'proton.me', 'pm.me', 'tutanota.com',
                       'tuta.io', 'guerrillamail.com', 'sharklasers.com',
                       'guerrillamailblock.com', 'grr.la', 'guerrillamail.info'}
    privacy_guests = [g for g in guests if
                      any(g.get('Campaign Member Email','').lower().endswith('@' + d)
                          for d in PRIVACY_DOMAINS)
                      and not g.get('Mandrill Bounce Reason','').strip()]
    if n > 0 and len(privacy_guests) > 0:
        pct = len(privacy_guests) / n
        met = pct >= 0.5 and len(privacy_guests) >= 2
        add_sig('sig16',
                f"Privacy email domains with no bounce: {len(privacy_guests)}/{n} ({round(100*pct)}%)",
                f"{round(100*pct)}% ({len(privacy_guests)}/{n} guests)", "50%+ and ≥2 guests", met)

    # ── Signal 20: Multiple future dinners posted immediately (standalone) ─────
    # Count from the CSV: how many dinners does this host have with a future start date
    host_contact_id = host.get('Contact ID', '') if host else ''
    if host_contact_id:
        review_date_str = REVIEW_DATE.strftime('%m/%d/%Y')
        future_count = host.get('_future_dinner_count', 0)
        if future_count >= 3:
            add_sig('sig20',
                    f"Multiple future dinners posted: {future_count} upcoming dinners",
                    f"{future_count} future dinners", "3+ future dinners posted", True)

    # ── Signal 21: Reports from other users / user reports (standalone) ─────────
    # Problem flag alone does NOT score -- surfaces as context only
    # T&S-specific Further Review Reason = standalone score
    # User reports (future: platform reporting function) = standalone score
    problem_flag = host.get('Problem Flag', '').strip() in ('1', '1.0') if host else False
    further_review = host.get('Further Review Reason', '').strip() if host else ''
    ts_flag_phrases = ['trust & safety', 'misuse', 'fraud', 'suspicious', 'report']
    further_is_ts = any(p in further_review.lower() for p in ts_flag_phrases)
    if further_is_ts:
        # T&S-specific review reason = scores as standalone
        add_sig('sig21',
                f"T&S review flag: {further_review[:100]}",
                "T&S-specific further review reason present", "Any", True)
    elif problem_flag:
        # Problem flag alone = does not score, surfaces as anomaly note only
        pass  # anomaly note added in build_bullets

    # ── Signal 7: Same IP across guests (needs pairing, 80%+, min 3 guests) ──
    guest_ips = [g.get('RSVP IP', '').strip() for g in guests if g.get('RSVP IP', '').strip()]
    if n >= 3 and guest_ips:
        ip_counts = collections.Counter(guest_ips)
        top_ip, top_ip_count = ip_counts.most_common(1)[0]
        ip_pct = top_ip_count / n
        # Only score if 3+ guests share the IP (not just 1-2)
        met = ip_pct >= 0.80 and top_ip_count >= 3
        add_sig('sig7',
                f"Same IP across guests: {top_ip_count}/{n} ({round(100*ip_pct)}%) share IP {top_ip}",
                f"{round(100*ip_pct)}% ({top_ip_count}/{n} guests)", "80%+ and ≥3 guests sharing (min 3 guests total)", met)


    # ── Signal 11: Host/guest email similarity (needs pairing, 50%+) ──────────
    # Only meaningful when host uses a non-common domain shared with guests
    COMMON_DOMAINS = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
                      'icloud.com', 'aol.com', 'me.com', 'live.com', 'msn.com',
                      'mac.com', 'ymail.com', 'googlemail.com'}
    host_email = host.get('Campaign Member Email', '').lower().strip() if host else ''
    if host_email and '@' in host_email:
        host_domain = host_email.split('@')[1]
        host_local = host_email.split('@')[0]
        # Only flag if host uses a non-common domain, or if local parts are suspiciously similar
        similar_guests = []
        for g in guests:
            g_email = g.get('Campaign Member Email', '').lower().strip()
            if not g_email or '@' not in g_email: continue
            g_domain = g_email.split('@')[1]
            g_local = g_email.split('@')[0]
            # Same non-common domain
            if g_domain == host_domain and host_domain not in COMMON_DOMAINS:
                similar_guests.append(g_email)
            # Or very similar local part (e.g. host123@gmail, guest124@gmail)
            elif len(host_local) > 5 and len(g_local) > 5:
                shared = sum(1 for a, b in zip(host_local, g_local) if a == b)
                if shared / max(len(host_local), len(g_local)) >= 0.85:
                    similar_guests.append(g_email)
        if n > 0 and len(similar_guests) >= 2:
            pct = len(similar_guests) / n
            met = pct >= 0.5
            add_sig('sig11',
                    f"Host/guest email similarity: {len(similar_guests)}/{n} ({round(100*pct)}%) guests share domain or pattern with host ({host_email})",
                    f"{round(100*pct)}% ({len(similar_guests)}/{n} guests)", "50%+ and ≥2 guests (non-common domain match)", met)

    # ── Signal 10: Suspicious phone number patterns (needs pairing, 50%+) ─────
    # Sequential or patterned phone numbers suggest bulk account creation
    phone_numbers = [g.get('Phone', '').strip() for g in guests if g.get('Phone', '').strip()]
    if len(phone_numbers) >= 3:
        # Extract last 4 digits and check for sequential patterns
        def last4(p):
            digits = ''.join(c for c in p if c.isdigit())
            return digits[-4:] if len(digits) >= 4 else None
        last4s = [last4(p) for p in phone_numbers if last4(p)]
        if len(last4s) >= 3:
            # Check for sequential last 4 digits (e.g. 5551, 5552, 5553)
            sorted_nums = sorted(set(int(x) for x in last4s if x.isdigit()))
            sequential = 0
            for i in range(1, len(sorted_nums)):
                if sorted_nums[i] - sorted_nums[i-1] <= 2:
                    sequential += 1
            pct_seq = (sequential + 1) / len(last4s) if last4s else 0
            # Also check same prefix (first 6 digits match across guests)
            prefixes = [''.join(c for c in p if c.isdigit())[:6] for p in phone_numbers]
            prefix_counts = collections.Counter(p for p in prefixes if len(p) == 6)
            top_prefix_count = prefix_counts.most_common(1)[0][1] if prefix_counts else 0
            prefix_pct = top_prefix_count / n if n > 0 else 0
            met = (pct_seq >= 0.5 or prefix_pct >= 0.5) and n >= 3
            if met or pct_seq >= 0.3 or prefix_pct >= 0.3:
                add_sig('sig10',
                        f"Suspicious phone patterns: {round(100*max(pct_seq, prefix_pct))}% sequential or shared-prefix phones",
                        f"{round(100*max(pct_seq, prefix_pct))}% pattern match", "50%+ sequential or shared prefix (min 3 guests)", met)

    # ── Signal 5: Known VPN/proxy IP (needs pairing) ──────────────────────────
    # Check guest IPs against known VPN/datacenter ranges
    # Common VPN/proxy subnets -- not exhaustive but catches obvious cases
    KNOWN_VPN_PREFIXES = {
        '104.16.', '104.17.', '104.18.', '104.19.',  # Cloudflare
        '162.158.', '172.64.', '172.65.', '172.66.', '172.67.',  # Cloudflare
        '10.', '192.168.', '172.16.',  # Private/internal (spoofed)
        '155.117.',  # Known VPN range from prior investigations
        '45.14.', '45.15.',  # Common VPN datacenter ranges
        '185.220.',  # Tor exit nodes
        '194.165.',  # Common proxy range
    }
    vpn_guests = []
    for g in guests:
        ip = g.get('RSVP IP', '').strip()
        if ip and any(ip.startswith(pfx) for pfx in KNOWN_VPN_PREFIXES):
            vpn_guests.append(ip)
    if n > 0 and len(vpn_guests) > 0:
        pct = len(vpn_guests) / n
        met = pct >= 0.3 and len(vpn_guests) >= 2
        add_sig('sig5',
                f"Known VPN/proxy IPs detected: {len(vpn_guests)}/{n} ({round(100*pct)}%) guests",
                f"{round(100*pct)}% ({len(vpn_guests)}/{n} guests)", "30%+ and ≥2 guests from known VPN ranges", met)

    # ── Signal 17: AI Not Pass (needs any guest integrity signal) ───────────
    ai_flag = host.get('AI Not Pass Summary', '') if host else ''
    if ai_flag:
        has_gi = any(raw.get(k, {}).get('threshold_met') for k in GUEST_INTEGRITY_SIGNALS)
        add_sig('sig17',
                f"AI-generated or templated description: {ai_flag[:80]}",
                "AI Not Pass flag present", "Needs guest integrity signal", has_gi)

    # ── Apply pairing rules ──────────────────────────────────────────────────
    # Step 1: which signals met their threshold?
    threshold_met = {k for k, v in raw.items() if v['threshold_met']}
    guest_integrity_met = threshold_met & GUEST_INTEGRITY_SIGNALS
    device_met = threshold_met & {'sig1', 'sig2', 'sig3'}

    scored = {}
    for key, sig in raw.items():
        if not sig['threshold_met']:
            scored[key] = {**sig, 'score_contribution': 0}
            continue
        if key in STANDALONE:
            scored[key] = sig
        elif key == 'sig11':
            # Email similarity needs guest integrity or device signal
            if guest_integrity_met or device_met:
                scored[key] = sig
            else:
                scored[key] = {**sig, 'score_contribution': 0}
        elif key in ('sig17', 'sig16'):
            # AI description and privacy domain need at least one guest integrity signal
            if guest_integrity_met:
                scored[key] = sig
            else:
                scored[key] = {**sig, 'score_contribution': 0}
        else:
            # All other non-standalone signals: need at least one OTHER threshold_met signal
            companions = threshold_met - {key}
            if companions:
                scored[key] = sig
            else:
                scored[key] = {**sig, 'score_contribution': 0}

    return scored


def compute_total_score(scored_signals):
    return sum(v['score_contribution'] for v in scored_signals.values())


def tier_from_score(score):
    if score >= 18:
        return 'suspension'
    elif score >= 9:
        return 'warning_dnn'
    elif score >= 1:
        return 'warning'
    return None


def build_collapsed_summary(host_name, score, scored_signals, nourishment, address):
    """Build collapsed summary purely from structured data -- no free text."""
    triggered = [v for v in scored_signals.values() if v['triggered'] and v['score_contribution'] > 0]
    top_sigs = ' · '.join(v['name'].replace(' on guest emails', '').replace(' across guests', '')
                           for v in sorted(triggered, key=lambda x: -x['score_contribution'])[:2])
    addr_city = address.split(',')[1].strip() if address and ',' in address else ''
    parts = [f"Score {score}", top_sigs]
    if nourishment and nourishment != '$0 (Contact)':
        parts.append(nourishment)
    if addr_city:
        parts.append(addr_city)
    return ' · '.join(p for p in parts if p)


def build_bullets(host_data, scored_signals, score, tier, sf_contact):
    """Build 3-5 bullets from structured data. No copy-paste between hosts."""
    bullets = []
    triggered = sorted(
        [v for v in scored_signals.values() if v['triggered'] and v['score_contribution'] > 0],
        key=lambda x: -x['score_contribution']
    )

    # Confidence bullet
    has_bounce = any(v for v in triggered if 'bounce' in v['name'].lower())
    has_cross = any(v for v in triggered if 'cross-dinner' in v['name'].lower())
    has_pid = any(v for v in triggered if 'Profile IDs' in v['name'])
    if has_bounce:
        conf = "High"
        reason = "Hard bounces are high-confidence. Guest emails confirmed non-existent."
    elif has_cross and has_pid:
        conf = "High"
        reason = "Cross-dinner device fingerprint paired with sequential PIDs."
    elif has_cross:
        conf = "Medium"
        reason = "Cross-dinner device fingerprint(s) with within-dinner concentration. No bounce or PID signals."
    else:
        conf = "Medium"
        reason = "Multiple corroborating signals."
    bullets.append(f"**Confidence: {conf}.** {reason}")

    # Top signal bullets (max 2)
    for v in triggered[:2]:
        bullets.append(f"**{v['name']}:** {v['observed']}.")

    # Financial bullet
    nourishment = host_data.get('_sf_nourishment', 'pending Contact verification')
    bullets.append(f"**Nourishment received:** {nourishment}.")

    # Suspension-specific / anomaly bullet
    suspended = host_data.get('_sf_suspended', False)
    dnn = host_data.get('_sf_dnn', False)
    prior_cases = host_data.get('_sf_prior_cases')
    if suspended:
        bullets.append("**⚠ Active suspension** -- hosted while suspended. Immediate action required.")
    elif dnn:
        bullets.append("**Active DNN** -- hosted while Do Not Nourish is set. Hold Nourishment on this dinner.")
    elif prior_cases and int(prior_cases or 0) > 0:
        bullets.append(f"**Prior T&S cases: {prior_cases}.** Review before actioning -- escalation rule may apply.")

    return bullets[:5]


def build_score_breakdown(scored_signals, total_score):
    """Build human-readable score breakdown string."""
    parts = []
    for v in sorted(scored_signals.values(), key=lambda x: -x['score_contribution']):
        if v['score_contribution'] > 0:
            parts.append(f"{v['name']} (+{v['score_contribution']})")
    if not parts:
        return f"Score: {total_score}"
    return ' + '.join(parts) + f' = {total_score}'


SIG_NAMES = {
    'sig1': 'Shared device fingerprint host and guest',
    'sig2': 'Cross-dinner device fingerprint match',
    'sig3': 'Same device fingerprint across guests',
    'sig4': 'Sequential RSVP timing -- tight',
    'sig5': 'VPN use across multiple guests/hosts',
    'sig6': 'Geographic mismatch',
    'sig7': 'Same IP across guests',
    'sig8': 'Clearly fake guest identities',
    'sig9': 'Suspicious guest email patterns',
    'sig10': 'Suspicious phone number patterns',
    'sig11': 'Host/guest email similarity',
    'sig12': 'Guest email bounces (75%+)',
    'sig12_low': 'Guest email bounces (50-74%)',
    'sig14': 'Sequential guest Profile IDs (100%)',
    'sig14_low': 'Sequential guest Profile IDs (55-99%)',
    'sig15': 'Recycled bounced guest list',
    'sig16': 'Privacy domain, no bounce',
    'sig17': 'AI-generated or templated description',
    'sig18': 'Description quality degradation over time',
    'sig19': 'Privacy type mismatch',
    'sig20': 'Multiple future dinners posted immediately',
    'sig21': 'Reports from other users',
    'sig22': 'Deliberate activity to defraud the program',
    'sig23': 'Deliberate identity change',
}


def build_signals_array(scored_signals):
    """Build signals array for JSON -- includes all triggered + key non-triggered."""
    out = []
    # Triggered first
    for key, v in sorted(scored_signals.items(),
                         key=lambda x: -x[1]['score_contribution']):
        if v['triggered']:
            out.append({
                'name': v['name'],
                'triggered': True,
                'weight': v['weight'],
                'observed': v['observed'],
                'threshold': v['threshold'],
                'threshold_met': v['threshold_met'],
                'score_contribution': v['score_contribution'],
            })
    # Non-triggered (threshold not met or not paired)
    for key, v in sorted(scored_signals.items(), key=lambda x: x[0]):
        if not v['triggered']:
            out.append({
                'name': v['name'],
                'triggered': False,
                'weight': v['weight'],
                'observed': v.get('observed', None),
                'threshold': v['threshold'],
                'threshold_met': False,
                'score_contribution': 0,
            })
    return out


def build_case_json(cid, camp, scored_signals, score, tier, sf_data=None):
    """
    Build a single case JSON object.
    sf_data: dict with Salesforce Contact fields (or None if not queried).
    All fields derived from structured data -- no free text copying between cases.
    """
    host = camp['host']
    host_name = (host.get('First Name', '') + ' ' + host.get('Last Name', '')).strip() if host else 'Unknown'
    host_id_15 = host.get('Contact ID', '') if host else ''

    # Use SF-returned 18-char ID if available, else convert
    if sf_data and sf_data.get('Id'):
        host_id_18 = sf_data['Id']
    else:
        host_id_18 = sf_15_to_18(host_id_15)

    sf_url = SF_BASE.format(host_id_18)

    # Nourishment -- read from CSV first, fall back to SF Contact if available
    nourishment_lifetime = None
    nourishment_eligible = None
    nourishment_requested = None

    # Try CSV first (always available)
    if host:
        try:
            csv_received = host.get('Total Nourishment Received', '')
            if csv_received and str(csv_received).strip() not in ('', 'nan'):
                nourishment_lifetime = float(str(csv_received).replace('$','').replace(',',''))
        except (ValueError, TypeError):
            pass
        try:
            csv_eligible = host.get('Total Eligible Nourishment', '')
            if csv_eligible and str(csv_eligible).strip() not in ('', 'nan'):
                nourishment_eligible = float(str(csv_eligible).replace('$','').replace(',',''))
        except (ValueError, TypeError):
            pass
        try:
            csv_requested = host.get('Requested Nourishment', '')
            if csv_requested and str(csv_requested).strip() not in ('', 'nan'):
                nourishment_requested = float(str(csv_requested).replace('$','').replace(',',''))
        except (ValueError, TypeError):
            pass

    # SF Contact overrides CSV for lifetime figure (more reliable)
    if sf_data and sf_data.get('Total_Nourishment_Received__c') is not None:
        nourishment_lifetime = sf_data['Total_Nourishment_Received__c']

    # Format display strings
    nourishment = f"${nourishment_lifetime:,.0f}" if nourishment_lifetime is not None else 'not available'
    nourishment_eligible_str = f"${nourishment_eligible:,.0f}" if nourishment_eligible is not None else 'not available'
    nourishment_requested_str = f"${nourishment_requested:,.0f}" if nourishment_requested is not None else 'not available'

    suspended = bool(sf_data.get('Suspended_Flag__c')) if sf_data else False
    dnn = bool(sf_data.get('Do_Not_Nourish__c')) if sf_data else False
    flag = bool(sf_data.get('Flag__c')) if sf_data else False
    flag_reason = sf_data.get('Flag_Reason__c', '') if sf_data else ''
    prior_cases = sf_data.get('Number_of_T_S_Cases__c') if sf_data else None
    dinners_hosted = sf_data.get('Number_of_times_hosted__c') if sf_data else None
    benchmark = bool(sf_data.get('Had_Benchmark_Checkin__c')) if sf_data else None
    app_date = sf_data.get('Host_Application_Date__c') if sf_data else host.get('Host Application Date', '')

    # Future dinners from Pass 2
    future_list = sf_data.get('_future_dinners', []) if sf_data else []
    future_count = len(future_list)
    future_dinners_str = str(future_count) if future_count > 0 else '0'

    # New host check
    new_host = False
    if app_date:
        try:
            app_dt = datetime.strptime(str(app_date)[:10], '%Y-%m-%d')
            new_host = (REVIEW_DATE - app_dt).days <= 90
        except:
            pass

    # Tenure -- use Host Application Date only
    tenure_str = 'unknown'
    if app_date:
        try:
            app_dt = datetime.strptime(str(app_date)[:10], '%Y-%m-%d')
            delta = REVIEW_DATE - app_dt
            tenure_str = f"{delta.days // 30} months" if delta.days >= 30 else f"{delta.days} days"
        except:
            pass

    # Merge sf_data into host_data for bullet building
    host_data = {
        '_sf_nourishment': nourishment,
        '_sf_suspended': suspended,
        '_sf_dnn': dnn,
        '_sf_prior_cases': prior_cases,
    }

    bullets = build_bullets(host_data, scored_signals, score, tier, sf_url)
    collapsed = build_collapsed_summary(host_name, score, scored_signals, nourishment, camp.get('address',''))
    breakdown = build_score_breakdown(scored_signals, score)

    # Anomaly flags as additional bullets
    anomaly_notes = []
    if flag and flag_reason:
        anomaly_notes.append(f"Existing flag: {flag_reason}")
    if dinners_hosted and app_date:
        try:
            _app_dt = datetime.strptime(str(app_date)[:10], '%Y-%m-%d')
            _days_since_app = (REVIEW_DATE - _app_dt).days
            if _days_since_app < 30 and int(dinners_hosted or 0) > 10:
                anomaly_notes.append(f"Host approved {_days_since_app} days ago but has {int(dinners_hosted)} dinners -- very new host with high dinner count.")
        except:
            pass

    # sig22 pattern surfacing -- flag if multiple high-weight signals co-occur with suspicious domain
    triggered_sigs = {k for k, v in scored_signals.items() if v.get('triggered') and v.get('score_contribution', 0) > 0}
    has_device = bool(triggered_sigs & {'sig1', 'sig2', 'sig3'})
    has_guest_integrity = bool(triggered_sigs & {'sig12', 'sig14', 'sig9', 'sig8'})
    has_suspicious_host = host and any(
        host.get('Campaign Member Email', '').lower().endswith('@' + d)
        for d in SUSPICIOUS_DOMAINS
    ) if host else False
    if has_device and has_guest_integrity and has_suspicious_host:
        anomaly_notes.append("Pattern consistent with sig22 (deliberate activity to defraud): device signals + guest integrity signals + suspicious host email domain co-occurring. Staff judgment required.")
    for note in anomaly_notes:
        bullets.append(f"**Anomaly:** {note}")

    return {
        'id': f"case-{cid}",
        'name': host_name,
        'dinner_name': camp.get('name', ''),
        'dinner_description': (camp.get('description', '') or '')[:400],
        'campaign_id': cid,
        'email': sf_data.get('Email', host.get('Campaign Member Email', '')) if sf_data else host.get('Campaign Member Email', ''),
        'contact_id': host.get('Contact ID', '') if host else '',
        'tier': tier,
        'is_cluster': False,
        'score': score,
        'sf_url': sf_url,
        'nourishment_received': nourishment,
        'nourishment_eligible_this_dinner': nourishment_eligible_str,
        'nourishment_requested': nourishment_requested_str,
        'future_dinners': future_dinners_str,
        'suspended': suspended,
        'dnn': dnn,
        'collapsed_summary': collapsed,
        'bullets': bullets[:5],
        'signals': build_signals_array(scored_signals),
        'score_breakdown': breakdown,
        'host_context': None if tier == 'warning' else {
            'tenure': tenure_str,
            'dinners_hosted': str(int(dinners_hosted)) if dinners_hosted is not None else 'pending',
            'nourishment_received': nourishment,
            'future_dinners': future_dinners_str,
            'dnn': 'Yes · active' if dnn else 'No',
            'benchmark_call': ('Yes' if benchmark else 'No') if benchmark is not None else 'pending',
            'prior_ts_cases': str(int(prior_cases)) if prior_cases is not None else 'pending',
            'suspended': 'Yes · active' if suspended else 'No',
            'graduated_host': 'pending',
            'new_host': f"Yes · approved {app_date}" if new_host and app_date else ('No' if not new_host else 'pending'),
            'unique_guests_12mo': 'pending',
        },
        'cluster_note': None,
        'cluster_hosts': [],
        'open_ts_cases': sf_data.get('_open_ts_cases', []) if sf_data else [],
    }


def detect_clusters(scored_cases, campaigns, cross_dinner_fps):
    """
    Identify clusters: hosts sharing cross-dinner FP + at least one other signal.
    Returns list of cluster groups (each group is a list of cids).
    """
    # Build FP -> [cid] map for scored cases only
    case_fps = {}
    for cid in scored_cases:
        camp = campaigns[cid]
        fps = set()
        host = camp['host']
        if host:
            fp = host.get('RSVP Device Fingerprint ID', '')
            if fp: fps.add(fp)
        for g in camp['guests']:
            fp = g.get('RSVP Device Fingerprint ID', '')
            if fp: fps.add(fp)
        case_fps[cid] = fps

    # Find groups sharing a cross-dinner FP
    shared_fp_groups = collections.defaultdict(set)
    for cid, fps in case_fps.items():
        for fp in fps:
            if fp in cross_dinner_fps:
                shared_fp_groups[fp].add(cid)

    # Cluster = 2+ scored cases sharing a FP
    clusters = []
    seen = set()
    for fp, group in sorted(shared_fp_groups.items(), key=lambda x: -len(x[1])):
        if len(group) < 2:
            continue
        new_members = group - seen
        if len(new_members) < 2:
            continue

        # Cross-dinner PID sequentiality check
        # Collect all profiled guest PIDs across all cluster member dinners
        all_pids = []
        for cid in new_members:
            for g in campaigns[cid]['guests']:
                try:
                    pid = int(g.get('Platform Profile ID', ''))
                    all_pids.append(pid)
                except (ValueError, TypeError):
                    pass
        cross_dinner_pid_sequential = False
        pid_range_note = None
        if len(all_pids) >= 3:
            all_pids_sorted = sorted(all_pids)
            pid_range = all_pids_sorted[-1] - all_pids_sorted[0]
            # Sequential across dinners if range is small relative to count
            if pid_range <= len(all_pids) * 3:
                cross_dinner_pid_sequential = True
                pid_range_note = f"Cross-dinner PIDs sequential: {all_pids_sorted[0]}–{all_pids_sorted[-1]} (range {pid_range} across {len(all_pids)} guests)"

        clusters.append({
            'fp': fp,
            'members': sorted(new_members),
            'cross_dinner_pid_sequential': cross_dinner_pid_sequential,
            'pid_range_note': pid_range_note,
        })
        seen.update(new_members)

    return clusters


def run(csv_path, lead_path=None, wednesday_mode=False):
    print(f"[T&S] Parsing {csv_path}...", file=sys.stderr)
    if lead_path:
        print(f"[T&S] Also merging Lead report: {lead_path}...", file=sys.stderr)
    campaigns, total_rows = parse_csv(csv_path, lead_path)
    print(f"[T&S] {total_rows} rows, {len(campaigns)} campaigns", file=sys.stderr)

    if wednesday_mode:
        # Wednesday mode: only score campaigns that are Ready to Nourish
        READY_STATUSES = {'ready to nourish', 'ready to nourish - pending review'}
        before = len(campaigns)
        campaigns = {
            cid: camp for cid, camp in campaigns.items()
            if str(camp.get('host', {}).get('Campaign Status', '') or '').strip().lower() in READY_STATUSES
        }
        print(f"[T&S] Wednesday filter: {len(campaigns)} Ready to Nourish campaigns (of {before} total)", file=sys.stderr)

    # ── FIELD POPULATION HEALTH CHECK ────────────────────────────────────────
    # Alert immediately if key scoring fields are unexpectedly empty
    # These fields are critical -- if they drop to near-zero something is broken in the data pipeline
    all_guest_rows = []
    for camp in campaigns.values():
        all_guest_rows.extend(camp.get('guests', []))

    total_guests = len(all_guest_rows)
    if total_guests > 0:
        CRITICAL_FIELDS = {
            'RSVP Device Fingerprint ID': 'Device fingerprint signals (Signals 1-3) will not score',
            'RSVP IP': 'IP-based signals (Signal 6) will not score',
            'Mandrill Bounce Reason': 'Guest email bounce signal (Signal 12) will not score',
            'Platform Profile ID': 'Sequential PID signal (Signal 13-14) will not score',
        }
        alerts = []
        for field, impact in CRITICAL_FIELDS.items():
            populated = sum(1 for g in all_guest_rows if g.get(field, '').strip() not in ('', 'nan', 'None'))
            pct = populated / total_guests
            if pct < 0.05:  # Less than 5% populated = critical alert
                alerts.append(f"⚠ CRITICAL: '{field}' is {pct:.0%} populated ({populated}/{total_guests} guests). {impact}.")
            elif pct < 0.20:  # Less than 20% = warning
                alerts.append(f"⚠ WARNING: '{field}' is only {pct:.0%} populated ({populated}/{total_guests} guests). Signals may be underscoring.")

        if alerts:
            print("[T&S] DATA PIPELINE ALERTS:", file=sys.stderr)
            for alert in alerts:
                print(f"[T&S] {alert}", file=sys.stderr)
        else:
            print(f"[T&S] Field health check: all key fields populated", file=sys.stderr)

    # Store alerts for inclusion in JSON output
    field_alerts = alerts if total_guests > 0 else []

    high_volume, cross_dinner, all_cross, all_fps = build_fp_maps(campaigns)
    print(f"[T&S] High-volume FPs (20+, not scored): {len(high_volume)}", file=sys.stderr)
    print(f"[T&S] Cross-dinner FPs (2-9): {len(cross_dinner)}", file=sys.stderr)

    # ── Pass 1A: Cross-host checks ────────────────────────────────────────────
    # These checks compare hosts against each other -- invisible within a single dinner.

    # 1. Cross-host shared exact IP
    # Flag any IP appearing on 2+ dinners from different hosts this week.
    # Uses dinner creation IP (Campaign) and guest RSVP IP (CampaignMember).
    cross_host_ips = collections.defaultdict(list)  # ip -> [cid]
    for cid, camp in campaigns.items():
        host = camp['host']
        if not host:
            continue
        # Dinner creation IP
        dinner_ip = (host.get('Dinner Created IP', '') or '').strip()
        if dinner_ip and dinner_ip not in ('nan', 'None', ''):
            cross_host_ips[dinner_ip].append(cid)
        # Guest RSVP IPs
        for g in camp['guests']:
            guest_ip = (g.get('RSVP IP', '') or '').strip()
            if guest_ip and guest_ip not in ('nan', 'None', ''):
                if cid not in cross_host_ips[guest_ip]:
                    cross_host_ips[guest_ip].append(cid)
    # Keep only IPs appearing on 2+ different campaigns
    cross_host_ips = {ip: list(set(cids)) for ip, cids in cross_host_ips.items()
                      if len(set(cids)) >= 2}
    # Filter out high-volume IPs (likely NAT/shared infrastructure -- threshold 10+ campaigns)
    cross_host_ips = {ip: cids for ip, cids in cross_host_ips.items()
                      if len(cids) < 10}
    print(f"[T&S] Cross-host shared IPs: {len(cross_host_ips)}", file=sys.stderr)

    # Cross-host PID batch detection is handled in Pass 2 by the agent on confirmed clusters.
    # Single-week population-level PID batches have too high a false-positive rate to be
    # useful in Pass 1. The agent checks PIDs across cluster members after cluster confirmation.
    cross_host_pid_batches = []
    print(f"[T&S] Cross-host PID check: deferred to Pass 2 (agent-executed on confirmed clusters)", file=sys.stderr)
    all_scored = {}  # cid -> {score, scored_signals, host_data}
    for cid, camp in campaigns.items():
        if not camp['host']:
            continue
        scored_signals = score_campaign(cid, camp, cross_dinner, high_volume)
        score = compute_total_score(scored_signals)
        tier = tier_from_score(score)

        # Also auto-include suspended/DNN
        host = camp['host']
        suspended = host.get('Suspended Flag', '') in ('1', '1.0')
        dnn = host.get('Do Not Nourish', '') in ('1', '1.0')

        if tier or suspended or dnn:
            all_scored[cid] = {
                'score': score,
                'tier': tier or 'watch',
                'scored_signals': scored_signals,
                'suspended': suspended,
                'dnn': dnn,
            }

    sus_cases = {c: d for c, d in all_scored.items() if d['score'] >= 18}
    pause_cases = {c: d for c, d in all_scored.items() if 9 <= d['score'] < 18}
    warn_cases = {c: d for c, d in all_scored.items() if 1 <= d['score'] < 9}

    print(f"[T&S] Suspension: {len(sus_cases)} | Warning DNN: {len(pause_cases)} | Warning: {len(warn_cases)}", file=sys.stderr)

    # ── Pass 2: Salesforce queries ───────────────────────────────────────────
    # This section is called by the agent after Pass 1.
    # The script outputs the scored data; the agent runs SF queries and merges.
    # For now, output the scored data with sf_data=None (pending SF query).
    # The agent will call run_pass2(all_scored, campaigns) after querying SF.

    # Detect clusters -- from device fingerprints, cross-host IPs, and PID batches
    clusters = detect_clusters(all_scored, campaigns, cross_dinner)
    print(f"[T&S] Clusters identified: {len(clusters)}", file=sys.stderr)

    # Build set of all member pairs already covered by device FP clusters
    fp_cluster_pairs = set()
    for cl in clusters:
        mems = cl['members']
        for i in range(len(mems)):
            for j in range(i+1, len(mems)):
                fp_cluster_pairs.add(frozenset([mems[i], mems[j]]))

    # Also flag hosts sharing a cross-host IP as a softer cluster signal
    # IP-only clusters require corroboration: at least one member must have a non-IP
    # scored signal (bounce, device FP, sequential PIDs), or both must be Warning DNN+.
    # IP alone is not sufficient to form a cluster case.
    NON_IP_SIGNALS = {'Guest email bounces (75%+)', 'Guest email bounces (50-74%)',
                      'Same device fingerprint across guests', 'Cross-dinner device fingerprint match',
                      'Sequential guest Profile IDs (100%)', 'Sequential guest Profile IDs (55-99%)'}

    for ip, cids in cross_host_ips.items():
        scored_members = [cid for cid in cids if cid in all_scored]
        if len(scored_members) < 2:
            continue
        # Check if every pair is already covered by a device FP cluster
        all_pairs_covered = all(
            frozenset([scored_members[i], scored_members[j]]) in fp_cluster_pairs
            for i in range(len(scored_members))
            for j in range(i+1, len(scored_members))
        )
        if all_pairs_covered:
            continue
        # Require at least one member to have a non-IP corroborating signal
        has_corroboration = False
        for cid in scored_members:
            d = all_scored.get(cid, {})
            member_signals = {v['name'] for v in d.get('scored_signals', {}).values()
                              if v.get('score_contribution', 0) > 0}
            if member_signals & NON_IP_SIGNALS:
                has_corroboration = True
                break
        if not has_corroboration:
            continue
        clusters.append({
            'fp': None,
            'members': scored_members,
            'cross_dinner_pid_sequential': False,
            'pid_range_note': None,
            'shared_ip_note': f"Shared IP {ip} across {len(cids)} dinners this week",
        })

    # Merge overlapping IP-only clusters into single clusters using union-find
    # Also: if an IP cluster overlaps with an FP cluster, add new members to the FP cluster
    # rather than creating a separate entry.
    ip_clusters_raw = [cl for cl in clusters if not cl.get('fp')]
    fp_clusters = [cl for cl in clusters if cl.get('fp')]

    # For each IP cluster, check if its members overlap with an FP cluster
    for ip_cl in ip_clusters_raw:
        ip_members = set(ip_cl['members'])
        for fp_cl in fp_clusters:
            fp_members = set(fp_cl['members'])
            overlap = ip_members & fp_members
            if overlap:
                # Add any new members from IP cluster to the FP cluster
                new_members = ip_members - fp_members
                if new_members:
                    fp_cl['members'] = sorted(fp_members | new_members)
                    existing_note = fp_cl.get('shared_ip_note', '')
                    fp_cl['shared_ip_note'] = (existing_note + '; ' if existing_note else '') + \
                        f"Extended via {ip_cl.get('shared_ip_note','shared IP')} -- added: {', '.join(sorted(new_members))}"
                ip_cl['_merged_into_fp'] = True
                break

    # Keep only IP clusters that weren't merged into an FP cluster
    ip_clusters_remaining = [cl for cl in ip_clusters_raw if not cl.get('_merged_into_fp')]

    if ip_clusters_remaining:
        # Union-find to merge overlapping IP clusters
        parent = {}
        def find(x):
            parent.setdefault(x, x)
            if parent[x] != x: parent[x] = find(parent[x])
            return parent[x]
        def union(x, y):
            parent[find(x)] = find(y)

        ip_notes = {}
        for cl in ip_clusters_remaining:
            mems = cl['members']
            note = cl.get('shared_ip_note', '')
            for m in mems: find(m)
            for i in range(len(mems)): union(mems[0], mems[i])
            root = find(mems[0])
            ip_notes.setdefault(root, []).append(note)

        merged = {}
        for cl in ip_clusters_remaining:
            root = find(cl['members'][0])
            if root not in merged:
                merged[root] = {'members': set(), 'notes': ip_notes.get(root, [])}
            merged[root]['members'].update(cl['members'])

        ip_clusters_final = [
            {
                'fp': None,
                'members': sorted(v['members']),
                'cross_dinner_pid_sequential': False,
                'pid_range_note': None,
                'shared_ip_note': '; '.join(dict.fromkeys(v['notes'])),
            }
            for v in merged.values()
            if len(v['members']) >= 2
        ]
    else:
        ip_clusters_final = []

    clusters = fp_clusters + ip_clusters_final
    print(f"[T&S] Clusters after merge: {len(clusters)}", file=sys.stderr)
    cluster_cids = set()
    for cl in clusters:
        cluster_cids.update(cl['members'])

    # Apply cluster rule: any host in a cluster gets Warning DNN minimum
    # Suspension still requires score 18+ -- cluster membership alone is not sufficient
    # for the more serious consequence tier and email language
    for cid in cluster_cids:
        if cid in all_scored and all_scored[cid]['tier'] == 'warning':
            all_scored[cid]['tier'] = 'warning_dnn'
            all_scored[cid]['_cluster_override'] = True

    # Remove existing cases (suspended + active status) from scored output
    existing_case_cids = {cid for cid, camp in campaigns.items() if camp.get('_existing_case')}
    all_scored = {cid: d for cid, d in all_scored.items() if cid not in existing_case_cids}

    # Same-address cross-host flags
    addresses = collections.defaultdict(list)
    for cid, camp in campaigns.items():
        addr = normalize_address(camp.get('address', ''))
        host = camp['host']
        if addr and host:
            eligible_raw = host.get('Total Eligible Nourishment', '')
            try:
                eligible_str = f"${float(eligible_raw):,.0f}" if eligible_raw else '—'
            except:
                eligible_str = eligible_raw or '—'
            addresses[addr].append({
                'cid': cid,
                'contact_id': host.get('Contact ID', ''),
                'name': (host.get('First Name', '') + ' ' + host.get('Last Name', '')).strip(),
                'address': camp.get('address', ''),
                'dinner_name': camp.get('name', ''),
                'description': camp.get('description', '') or '',
                'eligible': eligible_str,
            })
    same_address = {addr: hosts for addr, hosts in addresses.items() if len(hosts) >= 2}

    # Save intermediate for agent to use
    output = {
        'campaigns': {cid: {
            'score': d['score'],
            'tier': d['tier'],
            'suspended': d['suspended'],
            'dnn': d['dnn'],
            'host_id_15': campaigns[cid]['host'].get('Contact ID', '') if campaigns[cid]['host'] else '',
            'host_name': (campaigns[cid]['host'].get('First Name','') + ' ' + campaigns[cid]['host'].get('Last Name','')).strip() if campaigns[cid]['host'] else '',
            'dinner_name': campaigns[cid].get('name',''),
            'address': campaigns[cid].get('address',''),
            'eligible': campaigns[cid]['host'].get('Total Eligible Nourishment','') if campaigns[cid]['host'] else '',
            'guest_count': len(campaigns[cid]['guests']),
            'signals': {k: {
                'name': v['name'],
                'triggered': v['triggered'],
                'weight': v['weight'],
                'observed': v.get('observed',''),
                'threshold': v['threshold'],
                'threshold_met': v['threshold_met'],
                'score_contribution': v['score_contribution'],
            } for k, v in d['scored_signals'].items()},
            'score_breakdown': build_score_breakdown(d['scored_signals'], d['score']),
        } for cid, d in all_scored.items()},
        'clusters': clusters,
        'same_address': {addr: hosts for addr, hosts in same_address.items()},
        'cross_host_ips': cross_host_ips,
        'cross_host_pid_batches': cross_host_pid_batches,
        'high_volume_fps': {fp: len(all_fps[fp]) for fp in high_volume},
        'total_dinners': len(campaigns),
        'field_alerts': field_alerts,
        'summary': {
            'suspension': len(sus_cases),
            'warning_dnn': len(pause_cases),
            'warning': len(warn_cases),
            'clusters': len(clusters),
        }
    }

    fname = f'/home/claude/ts_pass1_{REVIEW_DATE.strftime("%Y%m%d")}.json'
    with open(fname, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"[T&S] Pass 1 results saved to {fname}", file=sys.stderr)
    return output


def normalize_address(addr):
    """
    Normalize address for same-address grouping.
    - Street address (starts with number): match on street + zip
    - Venue/city-only entry (no street number): match on city + zip
      These catch retreat centers, camps, and shared venues.
    """
    if not addr:
        return ''
    addr = re.sub(r',?\s*USA$', '', addr.strip(), flags=re.IGNORECASE)
    addr = re.sub(r',?\s*États-Unis$', '', addr, flags=re.IGNORECASE)
    parts = [p.strip() for p in addr.split(',')]
    has_street = bool(re.match(r'^\d+', parts[0])) if parts else False
    if has_street and len(parts) >= 2:
        street = parts[0].lower()
        zip_match = re.search(r'\d{5}', parts[-1])
        zip_code = zip_match.group() if zip_match else parts[-1].strip().lower()
        return f"{street} {zip_code}"
    elif len(parts) >= 2:
        city = parts[-2].strip().lower()
        state_zip = parts[-1].strip().lower()
        # Skip bare city matches for large metros -- too coarse to be meaningful
        LARGE_CITIES = {'new york', 'brooklyn', 'manhattan', 'los angeles', 'chicago',
                        'houston', 'philadelphia', 'phoenix', 'san antonio', 'san diego',
                        'dallas', 'san jose', 'austin', 'jacksonville', 'san francisco',
                        'columbus', 'charlotte', 'indianapolis', 'seattle', 'denver',
                        'boston', 'miami', 'atlanta', 'washington', 'nashville',
                        'portland', 'las vegas', 'memphis', 'baltimore', 'milwaukee'}
        if city in LARGE_CITIES:
            return ''
        return f"cityzip:{city} {state_zip}"
    return addr.lower()


def build_ts_ui_data(pass1_output, sf_results, campaigns, wednesday_mode=False):
    """
    Build final ts_ui_data JSON from Pass 1 results + Salesforce data.
    sf_results: dict of contact_id_15 -> SF Contact record
    """
    # Build shared IP partner lookup: cid -> list of (ip, partner_cids)
    # Used to note IP connections on individual case cards without creating IP cluster cases
    shared_ip_partners = {}
    for ip, cids in pass1_output.get('cross_host_ips', {}).items():
        scored_cids = [c for c in cids if c in pass1_output.get('campaigns', {})]
        if len(scored_cids) >= 2:
            for cid in scored_cids:
                partners = [c for c in scored_cids if c != cid]
                if partners:
                    shared_ip_partners.setdefault(cid, []).append((ip, partners))
    # Build existing cases FIRST so we can exclude them from scored cases
    existing_cases = []
    for cid, camp in campaigns.items():
        if camp.get('_existing_case') and camp.get('host'):
            host = camp['host']
            contact_id = host.get('Contact ID', '')
            host_id_18 = sf_results.get(contact_id, {}).get('Id', sf_15_to_18(contact_id)) if contact_id else ''
            existing_cases.append({
                'host_name': f"{host.get('First Name','')} {host.get('Last Name','')}".strip(),
                'contact_id': contact_id,
                'sf_url': SF_BASE.format(host_id_18) if host_id_18 else '',
                'campaign_id': cid,
                'campaign_url': SF_CAMPAIGN_BASE.format(cid),
                'dinner_name': camp.get('name', ''),
                'campaign_status': camp.get('_suspended_status', ''),
                'nourishment_received': f"${float(host.get('Total Nourishment Received', 0) or 0):,.0f}",
                'nourishment_eligible': f"${float(host.get('Total Eligible Nourishment', 0) or 0):,.0f}",
                'note': (
                    f"Suspended host with active dinner status: {camp.get('_suspended_status','')}. Review immediately -- Nourishment may have been sent while account was suspended."
                    if camp.get('_existing_case_type') == 'suspension'
                    else f"DNN host posted a new dinner (status: {camp.get('_suspended_status','')}). Check in before approving Nourishment -- may be returning after a gap."
                ),
                'existing_case_type': camp.get('_existing_case_type', 'suspension'),
            })
    existing_case_cids = {c['campaign_id'] for c in existing_cases}

    cases = []
    cross_dinner_fps = {fp: [] for fp in pass1_output.get('high_volume_fps', {})}

    # Sort: clusters first, then by score desc
    all_cids = sorted(pass1_output['campaigns'].keys(),
                      key=lambda c: -pass1_output['campaigns'][c]['score'])

    # Mark cluster members -- device FP clusters only
    # IP-only cluster members still appear as individual cases with a shared IP note
    cluster_members = set()
    for cluster in pass1_output['clusters']:
        if cluster.get('fp'):  # device FP clusters only
            cluster_members.update(cluster['members'])

    # Build cluster cases -- device FP clusters only
    # IP-only clusters surface in cross_host_flags for program team, not as scored cases
    seen_cluster_member_sets = set()
    for cluster in pass1_output['clusters']:
        if not cluster.get('fp'):
            continue  # IP-only clusters go to cross_host_flags, not case cards
        members = [m for m in cluster['members'] if m in pass1_output['campaigns']]
        if len(members) < 2:
            continue
        member_set = frozenset(members)
        fp = cluster['fp']
        # Skip IP-only cluster if an identical or superset member set already appeared via device FP
        if not fp and member_set in seen_cluster_member_sets:
            continue
        seen_cluster_member_sets.add(member_set)
        # IP-based clusters (from cross_host_ips) have no device fingerprint
        ip_note = cluster.get('shared_ip_note') or 'Shared IP across cluster dinners'
        cluster_key = fp[:8] if fp else 'ip-' + '-'.join(sorted(members))[:20]
        cluster_hosts = []
        total_nourishment = 0
        nourishment_verified = True

        for cid in members:
            d = pass1_output['campaigns'][cid]
            sf = sf_results.get(d['host_id_15'], {})
            host_id_18 = sf.get('Id', sf_15_to_18(d['host_id_15']))
            n_str = 'pending Contact verification'
            if sf.get('Total_Nourishment_Received__c') is not None:
                val = sf['Total_Nourishment_Received__c']
                n_str = f"${val:,.0f} (Contact)"
                total_nourishment += val
            else:
                nourishment_verified = False

            # Build host_context for this cluster member
            app_date = sf.get('Host_Application_Date__c', d.get('host_app', ''))
            if app_date:
                try:
                    app_dt = datetime.strptime(str(app_date)[:10], '%Y-%m-%d')
                    delta = REVIEW_DATE - app_dt
                    tenure_str = f"{delta.days // 30} months" if delta.days >= 30 else f"{delta.days} days"
                except:
                    tenure_str = 'unknown'
            else:
                tenure_str = 'unknown'

            dinners_hosted = sf.get('Number_of_times_hosted__c')
            benchmark = sf.get('Had_Benchmark_Checkin__c')
            prior_cases = sf.get('Number_of_T_S_Cases__c')
            suspended_sf = bool(sf.get('Suspended_Flag__c'))
            dnn_sf = bool(sf.get('Do_Not_Nourish__c'))
            new_host_flag = False
            if app_date:
                try:
                    app_dt = datetime.strptime(str(app_date)[:10], '%Y-%m-%d')
                    new_host_flag = (REVIEW_DATE - app_dt).days <= 90
                except:
                    pass

            host_context = {
                'tenure': tenure_str,
                'dinners_hosted': str(int(dinners_hosted)) if dinners_hosted is not None else 'pending',
                'nourishment_received': n_str,
                'future_dinners': str(len(sf.get('_future_dinners', []))),
                'dnn': 'Yes · active' if dnn_sf else 'No',
                'benchmark_call': ('Yes' if benchmark else 'No') if benchmark is not None else 'pending',
                'prior_ts_cases': str(int(prior_cases)) if prior_cases is not None else 'pending',
                'suspended': 'Yes · active' if suspended_sf else 'No',
                'graduated_host': 'pending',
                'new_host': f"Yes · approved {app_date}" if new_host_flag and app_date else ('No' if not new_host_flag else 'pending'),
                'unique_guests_12mo': str(int(sf['Unique_Guests_Last_12_Months__c'])) if sf.get('Unique_Guests_Last_12_Months__c') is not None else 'pending',
                'guest_to_host': 'Yes' if sf.get('Guest_to_Host_formula__c') else ('No' if sf.get('Guest_to_Host_formula__c') is not None else 'pending'),
                'times_attended_as_guest': str(int(sf['Times_Attended_as_Guest__c'])) if sf.get('Times_Attended_as_Guest__c') is not None else 'pending',
            }

            # Device sharing detail for this host's dinner
            camp = campaigns[cid]
            n_guests = len(camp['guests'])
            host_on_device = camp['host'] and camp['host'].get('RSVP Device Fingerprint ID','').strip() == fp
            guests_on_device = sum(1 for g in camp['guests'] if g.get('RSVP Device Fingerprint ID','').strip() == fp)
            guest_pct = round(100 * guests_on_device / n_guests) if n_guests > 0 else 0

            cluster_hosts.append({
                'name': d['host_name'],
                'email': sf.get('Email', ''),
                'sf_url': SF_BASE.format(host_id_18),
                'score': d['score'],
                'nourishment_received': n_str,
                'future_dinners': str(len(sf.get('_future_dinners', []))),
                'address': d['address'],
                'key_signals': ', '.join(
                    v['name'] for v in sorted(d['signals'].values(),
                    key=lambda x: -x['score_contribution'])[:2] if v['triggered']
                ),
                'host_context': host_context,
                # Device sharing detail
                'host_on_device': host_on_device,
                'guests_on_device': guests_on_device,
                'total_guests': n_guests,
                'guest_device_pct': guest_pct,
                # Combined context fields
                'applied_date': str(app_date)[:10] if app_date else '—',
                'tenure': tenure_str,
                'dinners_hosted': str(int(dinners_hosted)) if dinners_hosted is not None else 'pending',
                'prior_ts_cases': str(int(prior_cases)) if prior_cases is not None else 'pending',
                'dnn': 'Yes' if dnn_sf else 'No',
                'suspended': 'Yes' if suspended_sf else 'No',
            })

        top_score = max(d['score'] for d in [pass1_output['campaigns'][c] for c in members])
        tier = tier_from_score(top_score) or 'warning_dnn'
        # Cluster minimum is warning_dnn -- never just warning
        if tier == 'warning':
            tier = 'warning_dnn'
        combined_n = f"${total_nourishment:,.0f} combined (Contact)" if nourishment_verified else "pending Contact verification"

        # Build cluster signals from highest-scoring member
        top_cid = max(members, key=lambda c: pass1_output['campaigns'][c]['score'])
        top_d = pass1_output['campaigns'][top_cid]
        cluster_signals = build_signals_array_from_dict(top_d['signals'])

        # Determine who is sharing the device (hosts, guests, or both)
        fp_link = FP_BASE.format(fp) if fp else ''
        host_fps_in_cluster = []
        guest_fps_in_cluster = []
        for cid in members:
            camp = campaigns[cid]
            if camp['host'] and camp['host'].get('RSVP Device Fingerprint ID','').strip() == fp:
                host_fps_in_cluster.append(pass1_output['campaigns'][cid]['host_name'])
            for g in camp['guests']:
                if g.get('RSVP Device Fingerprint ID','').strip() == fp:
                    guest_fps_in_cluster.append(pass1_output['campaigns'][cid]['host_name'])
                    break
        has_host_fp = len(host_fps_in_cluster) > 0
        has_guest_fp = len(guest_fps_in_cluster) > 0
        if has_host_fp and has_guest_fp:
            sharing_who = "hosts and guests"
        elif has_host_fp:
            sharing_who = "hosts"
        else:
            sharing_who = "guests"

        # Scores per host for display
        scores_str = ' · '.join(f"{h['name'].split()[-1]} {h['score']}" for h in cluster_hosts)

        # Build device sharing detail bullets
        host_on_device_names = [h['name'].split()[0] for h in cluster_hosts if h['host_on_device']]
        host_device_str = (
            f"**Host accounts on device:** {', '.join(host_on_device_names)}" if host_on_device_names
            else "**Host accounts:** none of the host accounts RSVPed from this device"
        )
        guest_breakdown = ' · '.join(
            f"{h['name'].split()[0]}: {h['guests_on_device']}/{h['total_guests']} guests ({h['guest_device_pct']}%)"
            for h in cluster_hosts
        )

        cases.append({
            'id': f"cluster-{cluster_key}",
            'name': ' / '.join(d['host_name'].split()[-1] for d in [pass1_output['campaigns'][c] for c in members]),
            'email': ', '.join(h.get('email','') for h in cluster_hosts if h.get('email','')),
            'tier': tier,
            'is_cluster': True,
            'score': top_score,
            'sf_url': cluster_hosts[0]['sf_url'] if cluster_hosts else '',
            'nourishment_received': combined_n,
            'future_dinners': str(sum(len(sf_results.get(h['sf_url'].split('/')[-2], {}).get('_future_dinners', [])) for h in cluster_hosts)),
            'suspended': False,
            'dnn': False,
            'collapsed_summary': f"{len(members)}-host cluster · top score {top_score} · {combined_n}",
            'bullets': [
                (f"**{len(members)}-host cluster.** Device fingerprint [{fp}]({fp_link}) detected across {len(members)} dinners. {host_device_str}." if fp else f"**{len(members)}-host cluster.** {ip_note}."),
                f"**Guest device breakdown:** {guest_breakdown}.",
                f"**Individual scores:** {scores_str}. Score shown is the highest individual score, not a combined total.",
                f"**Combined Nourishment: {combined_n}.**",
            ],
            'signals': cluster_signals,
            'score_breakdown': top_d['score_breakdown'],
            'host_context': {
                'tenure': 'see individual cases below',
                'dinners_hosted': 'see individual cases below',
                'nourishment_received': combined_n,
                'future_dinners': str(sum(len(h.get('_future_dinners', [])) for h in [sf_results.get(ch['sf_url'].split('/')[-2], {}) for ch in cluster_hosts])),
                'dnn': 'see individual cases below',
                'benchmark_call': 'see individual cases below',
                'prior_ts_cases': 'see individual cases below',
                'suspended': 'see individual cases below',
                'graduated_host': 'see individual cases below',
                'new_host': 'see individual cases below',
                'unique_guests_12mo': 'see individual cases below',
            },
            'cluster_note': (f"{ip_note} · " if not fp else f"Device [{fp}]({fp_link}) · ") + f"{host_device_str.replace('**Host accounts on device:**', 'hosts on device:').replace('**Host accounts:**', '')} · {guest_breakdown}" + (f" · {cluster.get('pid_range_note','')}" if cluster.get('pid_range_note') else ''),
            'cluster_hosts': cluster_hosts,
        })

    # Individual cases (non-cluster, ordered by score desc)
    for cid in all_cids:
        if cid in cluster_members:
            continue
        if cid in existing_case_cids:
            continue
        d = pass1_output['campaigns'][cid]
        if d['score'] == 0 and not d['suspended'] and not d['dnn']:
            continue

        sf = sf_results.get(d['host_id_15'], {})
        camp = campaigns[cid]

        # If host has open T&S cases, surface in existing_cases rather than scored queue
        open_cases = sf.get('_open_ts_cases', [])
        if open_cases:
            host = camp.get('host', {})
            contact_id = host.get('Contact ID', '') if host else ''
            host_id_18 = sf.get('Id', sf_15_to_18(contact_id)) if contact_id else ''
            case_refs = '; '.join(f"Case {c['case_number']} ({c['status']})" for c in open_cases)
            existing_cases.append({
                'host_name': f"{host.get('First Name','')} {host.get('Last Name','')}".strip() if host else '',
                'contact_id': contact_id,
                'sf_url': SF_BASE.format(host_id_18) if host_id_18 else '',
                'campaign_id': cid,
                'campaign_url': SF_CAMPAIGN_BASE.format(cid),
                'dinner_name': camp.get('name', ''),
                'campaign_status': host.get('Campaign Status', '') if host else '',
                'nourishment_received': f"${float(host.get('Total Nourishment Received', 0) or 0):,.0f}" if host else '—',
                'nourishment_eligible': f"${float(host.get('Total Eligible Nourishment', 0) or 0):,.0f}" if host else '—',
                'note': f"Open T&S case(s): {case_refs}. Score this week: {d['score']} ({d['tier']}). Review before taking new action.",
                'existing_case_type': 'open_case',
            })
            existing_case_cids.add(cid)
            continue

        # Reconstruct scored_signals for case building
        scored_signals = {k: {
            'name': v['name'],
            'weight': v['weight'],
            'observed': v.get('observed', ''),
            'threshold': v['threshold'],
            'threshold_met': v['threshold_met'],
            'score_contribution': v['score_contribution'],
            'triggered': v['triggered'],
        } for k, v in d['signals'].items()}

        # Attach future dinner count to host row for sig20
        if camp.get('host') and sf:
            camp['host']['_future_dinner_count'] = len(sf.get('_future_dinners', []))
        case = build_case_json(cid, camp, scored_signals, d['score'],
                               d['tier'] or 'watch', sf)
        # Add shared IP partner note if this host shares an IP with another scored host
        if cid in shared_ip_partners and not case.get('is_cluster'):
            ip_notes = []
            for ip, partner_cids in shared_ip_partners[cid]:
                partner_names = []
                for pcid in partner_cids:
                    pcamp = campaigns.get(pcid, {})
                    phost = pcamp.get('host', {})
                    if phost:
                        pname = f"{phost.get('First Name','')} {phost.get('Last Name','')}".strip()
                        partner_names.append(pname)
                if partner_names:
                    ip_notes.append(f"Shares IP {ip} with {', '.join(partner_names)} -- possible coordination, review together.")
            if ip_notes:
                case['bullets'] = case.get('bullets', []) + [f"**Shared IP:** {n}" for n in ip_notes]
        cases.append(case)

    # Wednesday mode: surface Warning DNN and above only
    if wednesday_mode:
        cases = [c for c in cases if c.get('tier') in ('warning_dnn', 'suspension', 'deactivation')]
        print(f"[T&S] Wednesday mode: {len(cases)} cases at Warning DNN or above", file=sys.stderr)

        # Build list of campaign IDs to move to Not Approved
        not_approved_ids = [c.get('campaign_id', '') for c in cases if c.get('campaign_id')]
        if not_approved_ids:
            print(f"[T&S] Campaigns to move to Not Approved:", file=sys.stderr)
            for cid in not_approved_ids:
                camp = campaigns.get(cid, {})
                name = camp.get('name', cid)
                print(f"[T&S]   {cid} -- {name}", file=sys.stderr)

    # Cross-host flags
    cross_host_flags = []
    for addr, hosts in pass1_output['same_address'].items():
        flag_type = 'program_policy' if len(hosts) == 2 else 'confirm_rule'
        flag_hosts = []
        for h in hosts:
            sf = sf_results.get(h['contact_id'], {})
            host_id_18 = sf.get('Id', sf_15_to_18(h['contact_id']))
            flag_hosts.append({
                'name': h['name'],
                'sf_url': SF_BASE.format(host_id_18),
                'dinner': h.get('dinner_name', ''),
                'dinner_url': SF_CAMPAIGN_BASE.format(h['cid']),
                'description': (h.get('description', '') or '')[:200],
                'eligible': h.get('eligible', '—'),
            })
        cross_host_flags.append({
            'id': f"flag-{addr[:20].replace(' ','-')}",
            'type': 'confirm_rule',
            'title': f"Same address · {addr[:50]}",
            'hosts': flag_hosts,
            'summary_bullets': [
                f"**{len(hosts)} hosts at the same address.** Confirm whether same or separate households.",
                "**One-Nourishment-per-household rule applies** if same household.",
            ],
            'flags': [
                {'label': 'Same address', 'type': 'yes'},
                {'label': 'Unit numbers unconfirmed', 'type': 'neutral'},
            ],
            'similarity_pct': None,
        })

    # Add cross-host IP flags to cross_host_flags
    for ip, cids in pass1_output.get('cross_host_ips', {}).items():
        flag_hosts = []
        for cid in cids:
            camp = campaigns.get(cid, {})
            host = camp.get('host', {})
            if not host:
                continue
            contact_id = host.get('Contact ID', '')
            sf = sf_results.get(contact_id, {})
            host_id_18 = sf.get('Id', sf_15_to_18(contact_id))
            flag_hosts.append({
                'name': (host.get('First Name','') + ' ' + host.get('Last Name','')).strip(),
                'sf_url': SF_BASE.format(host_id_18),
                'dinner': camp.get('name', ''),
                'dinner_url': SF_CAMPAIGN_BASE.format(cid),
                'description': (camp.get('description', '') or '')[:200],
                'eligible': '—',
            })
        if len(flag_hosts) >= 2:
            cross_host_flags.append({
                'id': f"flag-ip-{ip.replace('.', '-')}",
                'type': 'shared_ip',
                'title': f"Shared IP · {ip}",
                'hosts': flag_hosts,
                'summary_bullets': [
                    f"**{len(flag_hosts)} dinners share IP {ip} this week.** May indicate same person hosting under multiple accounts.",
                    "**Review for coordinated fraud** -- check if hosts know each other or share device fingerprints.",
                ],
                'flags': [{'label': 'Shared IP', 'type': 'warning'}],
                'similarity_pct': None,
            })

    # Note: cross-host PID batches are surfaced in insights, not cross_host_flags,
    # because single-week false positive rate is too high for program team action.
    # They become meaningful when the same batch recurs across multiple weeks.

    # High-volume device fingerprints for Weekly Insights
    known_bad = []
    for fp, count in sorted(pass1_output['high_volume_fps'].items(), key=lambda x: -x[1])[:5]:
        known_bad.append({
            'fingerprint': fp,
            'url': FP_BASE.format(fp),
            'seen_on': f"{count} dinners (high-volume)",
        })

    # Auto-generate basic pattern observations from Pass 1 data
    sus_cases = [c for c in cases if c.get('tier') == 'suspension']
    dnn_cases = [c for c in cases if c.get('tier') == 'warning_dnn']
    all_signals = [s for c in cases for s in c.get('signals', []) if s.get('score_contribution', 0) > 0]
    signal_counts = {}
    for s in all_signals:
        signal_counts[s['name']] = signal_counts.get(s['name'], 0) + 1
    top_signals = sorted(signal_counts.items(), key=lambda x: -x[1])[:5]
    bounce_cases = [c for c in cases if any('bounce' in (s.get('name','').lower()) and s.get('score_contribution',0) > 0 for s in c.get('signals',[]))]
    pid_cases = [c for c in cases if any('profile id' in (s.get('name','').lower()) and s.get('score_contribution',0) > 0 for s in c.get('signals',[]))]
    patterns = []
    if sus_cases:
        patterns.append(f"{len(sus_cases)} suspension case{'s' if len(sus_cases)>1 else ''} this week.")
    if dnn_cases:
        patterns.append(f"{len(dnn_cases)} Warning DNN case{'s' if len(dnn_cases)>1 else ''}.")
    if bounce_cases:
        patterns.append(f"Bounce signal present in {len(bounce_cases)}/{len(cases)} scored cases.")
    if pid_cases:
        patterns.append(f"Sequential Profile IDs in {len(pid_cases)} cases.")
    pid_batch_flags = [f for f in cross_host_flags if f.get('type') == 'pid_batch']
    ip_flags = [f for f in cross_host_flags if f.get('type') == 'shared_ip']
    addr_flags = [f for f in cross_host_flags if f.get('type') in ('confirm_rule', 'program_policy')]
    if ip_flags:
        patterns.append(f"{len(ip_flags)} cross-host shared IP group{'s' if len(ip_flags)>1 else ''} flagged for review.")
    if addr_flags:
        patterns.append(f"{len(addr_flags)} same-address group{'s' if len(addr_flags)>1 else ''} routed to program team.")

    ts_ui_data = {
        'run': {
            'week_of': REVIEW_DATE.strftime('%Y-%m-%d'),
            'run_date': datetime.now().strftime('%Y-%m-%d'),
            'dinners_reviewed': pass1_output['total_dinners'],
            'summary': pass1_output['summary'],
            'field_alerts': pass1_output.get('field_alerts', []),
            'wednesday_mode': wednesday_mode,
            'not_approved_pending': [c.get('campaign_id') for c in cases if wednesday_mode and c.get('campaign_id')],
        },
        'existing_cases': existing_cases,
        'cases': cases,
        'cross_host_flags': cross_host_flags,
        'insights': {
            'patterns': ' '.join(patterns),
            'emerging_trends': '',
            'proposed_signal_updates': [],
            'open_questions': [],
            'known_bad_devices': known_bad,
            'below_threshold': [],
            'top_signals_this_week': [{'name': n, 'count': c} for n, c in top_signals],
        },
        'slack_summary': {
            'week_of': REVIEW_DATE.strftime('%Y-%m-%d'),
            'totals': pass1_output['summary'],
            'actioned': [],
            'trends': '',
            'urgent': None,
        },
    }

    return ts_ui_data


def build_signals_array_from_dict(signals_dict):
    """Build signals array from already-serialized dict (for clusters)."""
    out = []
    for k, v in sorted(signals_dict.items(), key=lambda x: -x[1]['score_contribution']):
        if v['triggered']:
            out.append({
                'name': v['name'],
                'triggered': True,
                'weight': v['weight'],
                'observed': v.get('observed', ''),
                'threshold': v['threshold'],
                'threshold_met': v['threshold_met'],
                'score_contribution': v['score_contribution'],
            })
    for k, v in sorted(signals_dict.items(), key=lambda x: x[0]):
        if not v['triggered']:
            out.append({
                'name': v['name'],
                'triggered': False,
                'weight': v['weight'],
                'observed': v.get('observed', None),
                'threshold': v['threshold'],
                'threshold_met': False,
                'score_contribution': 0,
            })
    return out


def run_pass2(pass1_output, sf):
    """
    Query Salesforce for all flagged hosts.
    sf: simple_salesforce.Salesforce instance
    Returns dict of contact_id_15 -> SF Contact record
    """
    CONTACT_FIELDS = [
        'Id', 'FirstName', 'LastName', 'Email',
        'Total_Nourishment_Received__c', 'Do_Not_Nourish__c', 'Suspended_Flag__c',
        'Flag__c', 'Flag_Reason__c', 'Total_Misuse_Score__c', 'Number_of_T_S_Cases__c',
        'Platform_Profile_ID__c', 'Number_of_times_hosted__c',
        'Unique_Guests_Last_12_Months__c', 'Had_Benchmark_Checkin__c',
        'Host_Application_Date__c',
        'Guest_to_Host_formula__c', 'Times_Attended_as_Guest__c',
    ]

    all_ids = list(set(
        d['host_id_15'] for d in pass1_output['campaigns'].values()
        if d.get('host_id_15') and d.get('tier') != 'warning'
    ))
    print(f"[T&S] Pass 2: querying {len(all_ids)} contacts (warning cases skipped)...", file=sys.stderr)

    sf_results = {}
    BATCH = 50  # Salesforce IN clause limit
    fields_str = ', '.join(CONTACT_FIELDS)

    for i in range(0, len(all_ids), BATCH):
        batch = all_ids[i:i+BATCH]
        ids_str = "', '".join(batch)
        soql = f"SELECT {fields_str} FROM Contact WHERE Id IN ('{ids_str}') LIMIT {BATCH}"
        try:
            result = sf.query(soql)
            for record in result['records']:
                # Index by 15-char ID for lookup
                id18 = record['Id']
                id15 = id18[:15]
                sf_results[id15] = record
                sf_results[id18] = record  # also index by 18-char
            print(f"[T&S]   batch {i//BATCH + 1}: {len(result['records'])} records", file=sys.stderr)
        except Exception as e:
            print(f"[T&S]   batch {i//BATCH + 1} error: {e}", file=sys.stderr)

    print(f"[T&S] Pass 2 complete: {len(sf_results)//2} contacts retrieved", file=sys.stderr)

    # ── Future dinners query ─────────────────────────────────────────────────
    # Query upcoming campaigns hosted by flagged contacts
    future_dinners_by_contact = {}
    today_str = REVIEW_DATE.strftime('%Y-%m-%d')
    try:
        # Build set of 18-char IDs for the query
        ids_18 = list(set(r['Id'] for r in sf_results.values() if 'Id' in r))
        for i in range(0, len(ids_18), 50):
            batch = ids_18[i:i+50]
            ids_str = "', '".join(batch)
            future_soql = (
                f"SELECT CampaignId, Campaign.Name, Campaign.StartDate, Campaign.Status, "
                f"Campaign.Do_Not_Nourish__c, ContactId "
                f"FROM CampaignMember "
                f"WHERE Status = 'Host' "
                f"AND ContactId IN ('{ids_str}') "
                f"AND Campaign.StartDate >= {today_str} "
                f"ORDER BY Campaign.StartDate ASC "
                f"LIMIT 200"
            )
            result = sf.query(future_soql)
            for rec in result.get('records', []):
                cid = rec.get('ContactId', '')
                if cid not in future_dinners_by_contact:
                    future_dinners_by_contact[cid] = []
                future_dinners_by_contact[cid].append({
                    'id': rec.get('CampaignId', ''),
                    'name': rec.get('Campaign', {}).get('Name', ''),
                    'date': rec.get('Campaign', {}).get('StartDate', ''),
                    'status': rec.get('Campaign', {}).get('Status', ''),
                    'dnn': rec.get('Campaign', {}).get('Do_Not_Nourish__c', False),
                })
        print(f"[T&S] Future dinners: {len(future_dinners_by_contact)} hosts with upcoming dinners", file=sys.stderr)
    except Exception as e:
        print(f"[T&S] Future dinners query error: {e}", file=sys.stderr)

    # Attach future dinner counts to sf_results for easy lookup
    for id18, record in sf_results.items():
        if len(id18) == 18:
            future = future_dinners_by_contact.get(id18, [])
            record['_future_dinners'] = future

    # ── Open T&S cases query ─────────────────────────────────────────────────
    # Flag any flagged host who already has an open T&S case -- surfaces in existing_cases
    open_cases_by_contact = {}
    try:
        ids_18 = list(set(r['Id'] for r in sf_results.values() if 'Id' in r))
        for i in range(0, len(ids_18), 30):
            batch = ids_18[i:i+30]
            ids_str = "', '".join(batch)
            cases_soql = (
                f"SELECT Id, CaseNumber, Subject, Status, ContactId "
                f"FROM Case "
                f"WHERE ContactId IN ('{ids_str}') "
                f"AND T_S_Type__c = 'Misuse of Platform' "
                f"AND Status != 'Closed' "
                f"LIMIT 100"
            )
            result = sf.query(cases_soql)
            for rec in result.get('records', []):
                cid = rec.get('ContactId', '')
                if cid not in open_cases_by_contact:
                    open_cases_by_contact[cid] = []
                open_cases_by_contact[cid].append({
                    'id': rec.get('Id', ''),
                    'case_number': rec.get('CaseNumber', ''),
                    'subject': rec.get('Subject', ''),
                    'status': rec.get('Status', ''),
                })
        print(f"[T&S] Open T&S cases: found for {len(open_cases_by_contact)} contacts", file=sys.stderr)
    except Exception as e:
        print(f"[T&S] Open T&S cases query error: {e}", file=sys.stderr)

    # Attach open cases to sf_results
    for id18, record in sf_results.items():
        if len(id18) == 18:
            record['_open_ts_cases'] = open_cases_by_contact.get(id18, [])

    return sf_results


def connect_salesforce():
    """
    Connect to Salesforce using environment variables or prompt.
    Expects SF_INSTANCE_URL and SF_ACCESS_TOKEN env vars,
    or SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN, SF_DOMAIN.
    """
    import os
    try:
        from simple_salesforce import Salesforce, SalesforceLogin
    except ImportError:
        print("[T&S] simple-salesforce not installed. Run: pip install simple-salesforce --break-system-packages", file=sys.stderr)
        return None

    instance_url = os.environ.get('SF_INSTANCE_URL')
    access_token = os.environ.get('SF_ACCESS_TOKEN')

    if instance_url and access_token:
        sf = Salesforce(instance_url=instance_url, session_id=access_token)
        print("[T&S] Salesforce connected via access token", file=sys.stderr)
        return sf

    username = os.environ.get('SF_USERNAME')
    password = os.environ.get('SF_PASSWORD')
    token = os.environ.get('SF_SECURITY_TOKEN', '')
    domain = os.environ.get('SF_DOMAIN', 'login')

    if username and password:
        sf = Salesforce(username=username, password=password,
                        security_token=token, domain=domain)
        print(f"[T&S] Salesforce connected as {username}", file=sys.stderr)
        return sf

    print("[T&S] No Salesforce credentials found. Set SF_INSTANCE_URL + SF_ACCESS_TOKEN or SF_USERNAME + SF_PASSWORD + SF_SECURITY_TOKEN.", file=sys.stderr)
    return None


def add_agent_layer(ts_ui_data, pass1_output):
    """
    Placeholder for agent-written content.
    The agent fills in these fields after reviewing the structured output.
    The script leaves them as empty strings / empty lists.
    Agent is ONLY permitted to populate these specific fields -- nothing else.
    """
    # These are the only fields the agent may write:
    AGENT_WRITABLE = {
        'insights.patterns',           # Cross-case patterns observed this week
        'insights.emerging_trends',    # New tactics identified
        'insights.proposed_signal_updates',  # 3+ cases required
        'insights.open_questions',     # Judgment calls for staff
        'cases[*].bullets',            # Context-specific notes (max 2 additional bullets per case)
        'cases[*].cluster_note',       # Cluster banner text
        'slack_summary.trends',        # 2-3 sentence plain language summary
        'slack_summary.urgent',        # Time-sensitive items
    }
    # The script has already populated everything else.
    # Agent may NOT modify: name, sf_url, score, tier, signals, score_breakdown,
    # host_context, nourishment_received, collapsed_summary, suspended, dnn
    return ts_ui_data


if __name__ == '__main__':
    import os

    if len(sys.argv) < 2:
        print("Usage: python3 ts_weekly_run.py <combined_csv_path> [--no-sf] [--wednesday]", file=sys.stderr)
        print("       Combined CSV contains both Contact and Lead guest rows.", file=sys.stderr)
        print("       Legacy two-file mode: python3 ts_weekly_run.py <contact_csv> <lead_csv> [--no-sf] [--wednesday]", file=sys.stderr)
        print("       --wednesday: pre-Nourishment gate run -- only scores Ready to Nourish campaigns, surfaces Warning DNN+ only", file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]
    lead_path = None
    skip_sf = '--no-sf' in sys.argv
    wednesday_mode = '--wednesday' in sys.argv

    if wednesday_mode:
        print("[T&S] Wednesday pre-Nourishment mode -- filtering to Ready to Nourish campaigns, Warning DNN+ only", file=sys.stderr)

    # Legacy: second positional arg (not a flag) is a separate Lead CSV
    if len(sys.argv) >= 3 and not sys.argv[2].startswith('--'):
        lead_path = sys.argv[2]
        print(f"[T&S] Legacy two-file mode: Contact={csv_path}, Lead={lead_path}", file=sys.stderr)
    else:
        print(f"[T&S] Single combined CSV mode: {csv_path}", file=sys.stderr)

    # Pass 1
    pass1_output = run(csv_path, lead_path, wednesday_mode=wednesday_mode)

    # Pass 2
    sf_results = {}
    if not skip_sf:
        sf = connect_salesforce()
        if sf:
            sf_results = run_pass2(pass1_output, sf)
        else:
            print("[T&S] Skipping Pass 2 -- no Salesforce connection", file=sys.stderr)
    else:
        print("[T&S] Skipping Pass 2 (--no-sf flag)", file=sys.stderr)

    # Build final JSON
    campaigns, _ = parse_csv(csv_path, lead_path)
    ts_ui_data = build_ts_ui_data(pass1_output, sf_results, campaigns, wednesday_mode=wednesday_mode)
    ts_ui_data = add_agent_layer(ts_ui_data, pass1_output)

    # Save and print
    fname = f'/home/claude/ts_ui_data_{REVIEW_DATE.strftime("%Y%m%d")}.json'
    with open(fname, 'w') as f:
        json.dump(ts_ui_data, f, indent=2)
    print(f"[T&S] Final JSON saved to {fname}", file=sys.stderr)

    # Print the ts_ui_data block for the agent to output
    print("\n```ts_ui_data")
    print(json.dumps(ts_ui_data, indent=2))
    print("```")
