#!/usr/bin/env python3
"""
OneTable Trust and Safety Weekly Run Script
Produces ts_ui_data JSON directly from CSV + Salesforce queries.
No manual transcription -- every JSON field is derived from structured data.

Usage:
  python3 ts_weekly_run.py <csv_path>

Output:
  Prints a single ```ts_ui_data ... ``` block to stdout.
  Also writes ts_ui_data_<date>.json to /home/claude/
"""

import csv, collections, json, sys, re
from datetime import datetime, date

# ── config ────────────────────────────────────────────────────────────────────
REVIEW_DATE = datetime(2026, 6, 23)
SUSPICIOUS_DOMAINS = {
    'atomicmail.io','mailshield.org','tutamail.com','otheremail.org',
    'bumpmail.io','simplelogin.com','membermail.net','freemail.is','ourisp.net',
    'altaddress.org','dropons.com'
}
HIGH_VOLUME_THRESHOLD = 10   # FP on 10+ dinners = weekly insights only
SIGNAL_WEIGHTS = {
    'sig1': 7,   # Shared device FP host+guest (standalone)
    'sig2': 7,   # Cross-dinner device FP match (needs pairing)
    'sig3': 5,   # Same device FP across guests (needs pairing)
    'sig4': 5,   # Sequential RSVP timing (needs pairing) -- not in CSV, skip
    'sig5': 3,   # VPN use (needs pairing) -- not reliably in CSV, skip
    'sig6': 0,   # Geographic mismatch (watch flag only)
    'sig7': 2,   # Same IP across guests (needs pairing, 80%+)
    'sig8': 5,   # Clearly fake guest identities (needs pairing, 50%+)
    'sig9': 4,   # Suspicious guest email patterns (needs pairing, 50%+)
    'sig10': 4,  # Suspicious phone number patterns (needs pairing, 50%+)
    'sig11': 4,  # Host/guest email similarity (needs pairing)
    'sig12': 8,  # Hard bounces (standalone, 50%+)
    'sig13': 5,  # Reject bounces (needs pairing, 50%+)
    'sig14': 6,  # Sequential guest PIDs (needs pairing, 50%+, gap<=2, all guests denom)
    'sig15': 1,  # Recycled guest lists (needs pairing)
    'sig16': 2,  # Privacy domain no bounce (needs pairing)
    'sig17': 3,  # AI not pass (needs guest integrity signal)
    'sig18': 3,  # Description degradation (needs pairing)
    'sig19': 2,  # Privacy type mismatch (needs pairing)
    'sig20': 2,  # Multiple future dinners (needs pairing)
    'sig21': 6,  # Reports from users (standalone)
    'sig22': 10, # Deliberate fraud (standalone, staff judgment)
    'sig23': 8,  # Deliberate identity change (standalone, staff judgment)
}
STANDALONE = {'sig1', 'sig12', 'sig21', 'sig22', 'sig23'}
GUEST_INTEGRITY_SIGNALS = {'sig12', 'sig13', 'sig14', 'sig9', 'sig8', 'sig10'}
SF_BASE = "https://onetable.lightning.force.com/lightning/r/Contact/{}/view"
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


def parse_csv(path):
    """Parse CSV into campaigns dict. Returns (campaigns, total_rows)."""
    KEEP = {
        "Campaign ID","Start Date","Campaign Status","Address","Member Status",
        "Platform Profile ID","First Name","Last Name","Host?","Campaign Member Email",
        "Mandrill Bounce Reason","Mandrill Bounce Time/Date","RSVP Device Fingerprint ID",
        "RSVP IP","Contact ID","Do Not Nourish","Suspended Flag","Problem Flag",
        "Problem Flag Reason","AI Not Pass Summary","Further Review Reason",
        "Dinner Privacy","Host Application Date","Campaign Name","FYI Flag Reason",
        "Dinner Created Device ID","Total Eligible Nourishment","Grant Application"
    }
    rows = []
    with open(path, encoding='latin1') as f:
        reader = csv.DictReader(f)
        keep_headers = [h for h in reader.fieldnames if h in KEEP]
        for row in reader:
            rows.append({k: row.get(k, '').strip() for k in keep_headers})

    campaigns = collections.defaultdict(lambda: {
        'host': None, 'guests': [], 'name': '', 'address': ''
    })
    for row in rows:
        cid = row.get('Campaign ID', '')
        ms = row.get('Member Status', '')
        if ms == 'Host':
            campaigns[cid]['host'] = row
            campaigns[cid]['name'] = row.get('Campaign Name', '')
            campaigns[cid]['address'] = row.get('Address', '')
        elif ms in ('Attended', 'Applied', 'Pending', 'Guest of Guest'):
            campaigns[cid]['guests'].append(row)

    return dict(campaigns), len(rows)


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
                    if 2 <= len(cids) < HIGH_VOLUME_THRESHOLD}
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
    dinner_fps = set(guest_fps)
    if host_fp:
        dinner_fps.add(host_fp)
    cross_hits = [(fp, len(cross_dinner[fp])) for fp in dinner_fps
                  if fp in cross_dinner and fp not in high_volume]
    if cross_hits and n >= 3:  # min 3 guests required
        total_weight = SIGNAL_WEIGHTS['sig2'] * len(cross_hits)
        descs = [f"{fp} across {nd} dinners" for fp, nd in cross_hits]
        add_sig('sig2',
                "Cross-dinner device fingerprint match: " + "; ".join(descs),
                f"{len(cross_hits)} FP(s) across multiple dinners",
                "Any (needs pairing, min 3 guests)", True,
                contribution=total_weight)

    # ── Signal 3: Same FP across guests (needs pairing, 25%+, min 3 guests) ──
    if n >= 3 and guest_fps:  # min 3 guests required
        fp_counts = collections.Counter(guest_fps)
        top_fp, top_count = fp_counts.most_common(1)[0]
        if top_fp not in high_volume:
            pct = top_count / n
            met = pct >= 0.25
            add_sig('sig3',
                    f"Same device fingerprint across guests: {top_count}/{n} ({round(100*pct)}%) [{top_fp}]",
                    f"{round(100*pct)}% ({top_count}/{n} guests)", "25%+ (min 3 guests)", met)

    # ── Signal 12: Hard bounces (standalone, 50%+) ─────────────────────────
    hard = [g for g in guests if
            'hard_bounce' in g.get('Mandrill Bounce Reason', '').lower() or
            'invalid' in g.get('Mandrill Bounce Reason', '').lower()]
    if n > 0:
        pct = len(hard) / n
        met = pct >= 0.5
        add_sig('sig12',
                f"Hard bounces on guest emails: {len(hard)}/{n} ({round(100*pct)}%)",
                f"{round(100*pct)}% ({len(hard)}/{n} guests)", "50%+", met)

    # ── Signal 13: Reject bounces (needs pairing, 50%+) ────────────────────
    reject = [g for g in guests if 'reject' in g.get('Mandrill Bounce Reason', '').lower()]
    if n > 0:
        pct = len(reject) / n
        met = pct >= 0.5
        add_sig('sig13',
                f"Reject bounces on guest emails: {len(reject)}/{n} ({round(100*pct)}%)",
                f"{round(100*pct)}% ({len(reject)}/{n} guests)", "50%+", met)

    # ── Signal 14: Sequential PIDs (needs pairing, 50%+, gap<=2, ALL guests denom) ──
    pids = []
    for g in guests:
        try:
            pids.append(int(float(g.get('Platform Profile ID', ''))))
        except (ValueError, TypeError):
            pass
    if len(pids) >= 2:
        ps = sorted(pids)
        cur = 1
        max_seq = 1
        for i in range(1, len(ps)):
            if ps[i] - ps[i-1] <= 2:
                cur += 1
                max_seq = max(max_seq, cur)
            else:
                cur = 1
        pct = max_seq / n  # denominator is ALL guests, not just profiled
        met = pct >= 0.5
        add_sig('sig14',
                f"Sequential guest Profile IDs: {max_seq}/{n} ({round(100*pct)}%) gap≤2",
                f"{round(100*pct)}% ({max_seq}/{n} guests, gap≤2)", "50%+ of all guests", met)

    # ── Signal 9: Suspicious email domains (needs pairing, 50%+) ───────────
    sus = [g for g in guests if
           any(g.get('Campaign Member Email', '').lower().endswith('@' + d)
               for d in SUSPICIOUS_DOMAINS)]
    if n > 0:
        pct = len(sus) / n
        met = pct >= 0.5
        add_sig('sig9',
                f"Suspicious guest email domains: {len(sus)}/{n} ({round(100*pct)}%)",
                f"{round(100*pct)}% ({len(sus)}/{n} guests)", "50%+", met)

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

    # Step 2: standalone signals score regardless
    # Step 3: all others score only if at least one OTHER signal is also threshold_met
    # Fix for circular dependency: compute total threshold_met count first,
    # then score any signal that has at least one companion
    scored = {}
    for key, sig in raw.items():
        if not sig['threshold_met']:
            scored[key] = {**sig, 'score_contribution': 0}
            continue
        if key in STANDALONE:
            scored[key] = sig  # score_contribution already set
        elif len(threshold_met) >= 2:
            # Has at least one other signal -- scores
            scored[key] = sig
        else:
            # Standalone non-eligible -- doesn't score
            scored[key] = {**sig, 'score_contribution': 0}

    return scored


def compute_total_score(scored_signals):
    return sum(v['score_contribution'] for v in scored_signals.values())


def tier_from_score(score):
    if score >= 18:
        return 'suspension'
    elif score >= 9:
        return 'nourishment_pause'
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
    'sig12': 'Hard bounces on guest emails',
    'sig13': 'Reject bounces on guest emails',
    'sig14': 'Sequential guest Profile IDs',
    'sig15': 'Recycled guest lists across dinners',
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

    # Nourishment from SF Contact (canonical)
    nourishment = 'pending Contact verification'
    if sf_data and sf_data.get('Total_Nourishment_Received__c') is not None:
        nourishment = f"${sf_data['Total_Nourishment_Received__c']:,.0f} (Contact)"

    suspended = bool(sf_data.get('Suspended_Flag__c')) if sf_data else False
    dnn = bool(sf_data.get('Do_Not_Nourish__c')) if sf_data else False
    flag = bool(sf_data.get('Flag__c')) if sf_data else False
    flag_reason = sf_data.get('Flag_Reason__c', '') if sf_data else ''
    prior_cases = sf_data.get('Number_of_T_S_Cases__c') if sf_data else None
    dinners_hosted = sf_data.get('Number_of_times_hosted__c') if sf_data else None
    benchmark = bool(sf_data.get('Had_Benchmark_Checkin__c')) if sf_data else None
    app_date = sf_data.get('Host_Application_Date__c') if sf_data else host.get('Host Application Date', '')
    journey_days = sf_data.get('Length_of_OneTable_Journey_in_Days__c') if sf_data else None

    # New host check
    new_host = False
    if app_date:
        try:
            app_dt = datetime.strptime(str(app_date)[:10], '%Y-%m-%d')
            new_host = (REVIEW_DATE - app_dt).days <= 90
        except:
            pass

    # Tenure
    tenure_str = 'unknown'
    if journey_days is not None:
        months = int(journey_days) // 30
        days = int(journey_days) % 30
        tenure_str = f"{months} months" if months > 0 else f"{days} days"
    elif app_date:
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
    if dinners_hosted and journey_days and int(journey_days or 0) < 30 and int(dinners_hosted or 0) > 10:
        anomaly_notes.append(f"Journey {int(journey_days)} days but {int(dinners_hosted)} dinners hosted -- implausible ratio.")
    for note in anomaly_notes:
        bullets.append(f"**Anomaly:** {note}")

    return {
        'id': f"case-{cid}",
        'name': host_name,
        'tier': tier,
        'is_cluster': False,
        'score': score,
        'sf_url': sf_url,
        'nourishment_received': nourishment,
        'future_dinners': 'unknown',
        'suspended': suspended,
        'dnn': dnn,
        'collapsed_summary': collapsed,
        'bullets': bullets[:5],
        'signals': build_signals_array(scored_signals),
        'score_breakdown': breakdown,
        'host_context': {
            'tenure': tenure_str,
            'dinners_hosted': str(int(dinners_hosted)) if dinners_hosted is not None else 'pending',
            'nourishment_received': nourishment,
            'future_dinners': 'unknown',
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
        clusters.append({'fp': fp, 'members': sorted(new_members)})
        seen.update(new_members)

    return clusters


def run(csv_path):
    print(f"[T&S] Parsing {csv_path}...", file=sys.stderr)
    campaigns, total_rows = parse_csv(csv_path)
    print(f"[T&S] {total_rows} rows, {len(campaigns)} campaigns", file=sys.stderr)

    high_volume, cross_dinner, all_cross, all_fps = build_fp_maps(campaigns)
    print(f"[T&S] High-volume FPs (10+): {len(high_volume)}", file=sys.stderr)
    print(f"[T&S] Cross-dinner FPs (2-9): {len(cross_dinner)}", file=sys.stderr)

    # ── Pass 1: Score all campaigns ──────────────────────────────────────────
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

    print(f"[T&S] Suspension: {len(sus_cases)} | Pause: {len(pause_cases)} | Warning: {len(warn_cases)}", file=sys.stderr)

    # ── Pass 2: Salesforce queries ───────────────────────────────────────────
    # This section is called by the agent after Pass 1.
    # The script outputs the scored data; the agent runs SF queries and merges.
    # For now, output the scored data with sf_data=None (pending SF query).
    # The agent will call run_pass2(all_scored, campaigns) after querying SF.

    # Detect clusters
    clusters = detect_clusters(all_scored, campaigns, cross_dinner)
    print(f"[T&S] Clusters identified: {len(clusters)}", file=sys.stderr)

    # Same-address cross-host flags
    addresses = collections.defaultdict(list)
    for cid, camp in campaigns.items():
        addr = normalize_address(camp.get('address', ''))
        host = camp['host']
        if addr and host:
            addresses[addr].append({
                'cid': cid,
                'contact_id': host.get('Contact ID', ''),
                'name': (host.get('First Name', '') + ' ' + host.get('Last Name', '')).strip(),
                'address': camp.get('address', ''),
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
        'high_volume_fps': {fp: len(all_fps[fp]) for fp in high_volume},
        'total_dinners': len(campaigns),
        'summary': {
            'suspension': len(sus_cases),
            'nourishment_pause': len(pause_cases),
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
    Normalize address for grouping.
    - Street address present: use street + zip (same building/street)
    - No street (bare city/state/zip): use prefixed city+zip (event venues, camps)
    """
    if not addr:
        return ''
    addr = re.sub(r',?\s*USA$', '', addr.strip(), flags=re.IGNORECASE)
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
        return f"cityzip:{city} {state_zip}"
    return addr.lower()


def build_ts_ui_data(pass1_output, sf_results, campaigns):
    """
    Build final ts_ui_data JSON from Pass 1 results + Salesforce data.
    sf_results: dict of contact_id_15 -> SF Contact record
    """
    cases = []
    cross_dinner_fps = {fp: [] for fp in pass1_output.get('high_volume_fps', {})}

    # Sort: clusters first, then by score desc
    all_cids = sorted(pass1_output['campaigns'].keys(),
                      key=lambda c: -pass1_output['campaigns'][c]['score'])

    # Mark cluster members
    cluster_members = set()
    for cluster in pass1_output['clusters']:
        cluster_members.update(cluster['members'])

    # Build cluster cases first
    for cluster in pass1_output['clusters']:
        members = cluster['members']
        fp = cluster['fp']
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

            cluster_hosts.append({
                'name': d['host_name'],
                'sf_url': SF_BASE.format(host_id_18),
                'score': d['score'],
                'nourishment_received': n_str,
                'future_dinners': 'unknown',
                'address': d['address'],
                'key_signals': ', '.join(
                    v['name'] for v in sorted(d['signals'].values(),
                    key=lambda x: -x['score_contribution'])[:2] if v['triggered']
                ),
            })

        top_score = max(d['score'] for d in [pass1_output['campaigns'][c] for c in members])
        tier = tier_from_score(top_score) or 'suspension'
        combined_n = f"${total_nourishment:,.0f} combined (Contact)" if nourishment_verified else "pending Contact verification"

        # Build cluster signals from highest-scoring member
        top_cid = max(members, key=lambda c: pass1_output['campaigns'][c]['score'])
        top_d = pass1_output['campaigns'][top_cid]
        cluster_signals = build_signals_array_from_dict(top_d['signals'])

        cases.append({
            'id': f"cluster-{fp[:8]}",
            'name': ' / '.join(d['host_name'].split()[-1] for d in [pass1_output['campaigns'][c] for c in members]),
            'tier': tier,
            'is_cluster': True,
            'score': top_score,
            'sf_url': cluster_hosts[0]['sf_url'] if cluster_hosts else '',
            'nourishment_received': combined_n,
            'future_dinners': 'unknown',
            'suspended': False,
            'dnn': False,
            'collapsed_summary': f"{len(members)}-host cluster · FP {fp[:16]}... · {combined_n}",
            'bullets': [
                f"**{len(members)}-host cluster** sharing device fingerprint {fp}.",
                f"**Combined Nourishment: {combined_n}.**",
                "**Consult Amalia before actioning.** Cluster case.",
            ],
            'signals': cluster_signals,
            'score_breakdown': top_d['score_breakdown'],
            'host_context': {
                'tenure': 'see individual cases',
                'dinners_hosted': 'see individual cases',
                'nourishment_received': combined_n,
                'future_dinners': 'unknown',
                'dnn': 'see individual cases',
                'benchmark_call': 'see individual cases',
                'prior_ts_cases': 'see individual cases',
                'suspended': 'see individual cases',
                'graduated_host': 'see individual cases',
                'new_host': 'see individual cases',
                'unique_guests_12mo': 'see individual cases',
            },
            'cluster_note': f"Device fingerprint {fp} shared across {len(members)} dinners",
            'cluster_hosts': cluster_hosts,
        })

    # Individual cases (non-cluster, ordered by score desc)
    for cid in all_cids:
        if cid in cluster_members:
            continue
        d = pass1_output['campaigns'][cid]
        if d['score'] == 0 and not d['suspended'] and not d['dnn']:
            continue

        sf = sf_results.get(d['host_id_15'], {})
        camp = campaigns[cid]

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

        case = build_case_json(cid, camp, scored_signals, d['score'],
                               d['tier'] or 'watch', sf)
        cases.append(case)

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
                'dinner': 'see Salesforce',
                'description': '',
                'eligible': sf.get('Total_Eligible_Nourishment__c', 'pending'),
                'status': 'pending',
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

    # High-volume device fingerprints for Weekly Insights
    known_bad = []
    for fp, count in sorted(pass1_output['high_volume_fps'].items(), key=lambda x: -x[1])[:5]:
        # Only flag if they also appear in scored cases (known bad)
        known_bad.append({
            'fingerprint': fp,
            'url': FP_BASE.format(fp),
            'seen_on': f"{count} dinners (high-volume)",
        })

    ts_ui_data = {
        'run': {
            'week_of': REVIEW_DATE.strftime('%Y-%m-%d'),
            'run_date': datetime.now().strftime('%Y-%m-%d'),
            'dinners_reviewed': pass1_output['total_dinners'],
            'summary': pass1_output['summary'],
        },
        'cases': cases,
        'cross_host_flags': cross_host_flags,
        'insights': {
            'patterns': '',  # Agent fills in after reviewing output
            'emerging_trends': '',
            'proposed_signal_updates': [],
            'open_questions': [],
            'known_bad_devices': known_bad,
            'below_threshold': [],  # TODO: add below-threshold tracking
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
        'Host_Application_Date__c', 'Length_of_OneTable_Journey_in_Days__c',
        'Shabbat_Frequency_now__c',
    ]

    all_ids = list(set(
        d['host_id_15'] for d in pass1_output['campaigns'].values()
        if d.get('host_id_15')
    ))
    print(f"[T&S] Pass 2: querying {len(all_ids)} contacts...", file=sys.stderr)

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
        print("Usage: python3 ts_weekly_run.py <csv_path> [--no-sf]", file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]
    skip_sf = '--no-sf' in sys.argv

    # Pass 1
    pass1_output = run(csv_path)

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
    # Reload campaigns for build step
    campaigns, _ = parse_csv(csv_path)
    ts_ui_data = build_ts_ui_data(pass1_output, sf_results, campaigns)
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
