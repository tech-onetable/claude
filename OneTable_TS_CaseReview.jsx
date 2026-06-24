import { useState, useEffect, useCallback, useRef } from "react";
import React from "react";

const C = {
  wine: "#8D1B3D", wineLight: "#FCEBEB", wineDark: "#5C0F26",
  cream: "#FFF8E8", olive: "#75815C", oliveLight: "#EAF3DE",
  khaki: "#D2BCA1", khakiLight: "#F5EFE6", purple: "#9BA2FF",
  amber: "#FAC775", amberDark: "#633806", green: "#0F6E56", greenLight: "#E1F5EE",
  text: "#1A1A1A", textSec: "#666666", textTer: "#999999",
  border: "#E8E2D9", bg: "#FAFAF8", bgCard: "#FFFFFF",
};

// ── storage ───────────────────────────────────────────────────────────────────
const STORAGE_KEY = "ts-weekly-runs";
const loadRuns = async () => {
  try { const r = await window.storage.get(STORAGE_KEY, true); return r ? JSON.parse(r.value) : []; }
  catch { return []; }
};
const saveRuns = async (runs) => {
  try { await window.storage.set(STORAGE_KEY, JSON.stringify(runs), true); } catch {}
};

// ── parse ─────────────────────────────────────────────────────────────────────
const parseJson = (text) => {
  let clean = text.replace(/```ts_ui_data|```json|```/g, "").trim();
  const start = clean.indexOf("{");
  const end = clean.lastIndexOf("}");
  if (start === -1 || end === -1) return null;
  try { return JSON.parse(clean.slice(start, end + 1)); }
  catch {
    try {
      let depth = 0, i = start;
      for (; i < clean.length; i++) {
        if (clean[i] === "{") depth++;
        if (clean[i] === "}") { depth--; if (depth === 0) break; }
      }
      return JSON.parse(clean.slice(start, i + 1));
    } catch { return null; }
  }
};

// ── small components ──────────────────────────────────────────────────────────
const Chevron = ({ open }) => (
  <span style={{ fontSize: 16, color: C.textSec, display: "inline-block", transition: "transform 0.2s", transform: open ? "rotate(180deg)" : "none" }}>▾</span>
);

const TierBadge = ({ tier, cluster }) => {
  const map = {
    suspension: { bg: C.wineLight, color: C.wine, label: "Suspension" },
    nourishment_pause: { bg: "#FAEEDA", color: C.amberDark, label: "Nourishment Pause" },
    warning: { bg: C.oliveLight, color: "#27500A", label: "Warning" },
    dismiss: { bg: "#F0F0EE", color: C.textSec, label: "Dismissed · No misuse" },
    program_team: { bg: C.greenLight, color: C.green, label: "Referred to program team" },
  };
  const t = map[tier] || map.warning;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", padding: "3px 10px", borderRadius: 6, fontSize: 12, fontWeight: 500, background: t.bg, color: t.color }}>
      {cluster ? "⚠ Cluster · " : ""}{t.label}
    </span>
  );
};

const FlagBadge = ({ type }) => (
  <span style={{ display: "inline-flex", padding: "3px 10px", borderRadius: 6, fontSize: 12, fontWeight: 500,
    background: type === "program_policy" ? C.greenLight : C.amber,
    color: type === "program_policy" ? C.green : C.amberDark }}>
    {type === "program_policy" ? "Program Policy · Not Fraud" : "Confirm Rule Applied"}
  </span>
);

const SigPill = ({ name, triggered }) => (
  <span style={{ padding: "4px 10px", borderRadius: 6, fontSize: 12, background: triggered ? C.wineLight : "#F0F0EE", color: triggered ? C.wine : C.textTer }}>{name}</span>
);

const ProfileRow = ({ label, val }) => (
  <div style={{ display: "flex", justifyContent: "space-between", padding: "3px 0", borderBottom: `0.5px solid ${C.border}`, fontSize: 13 }}>
    <span style={{ color: C.textSec }}>{label}</span>
    <span style={{ fontWeight: 500, color: C.text, textAlign: "right" }}>{val}</span>
  </div>
);

const Collapsible = ({ label, children }) => {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ border: `0.5px solid ${C.border}`, borderRadius: 8, marginTop: 12, overflow: "hidden" }}>
      <div onClick={() => setOpen(!open)} style={{ padding: "10px 12px", display: "flex", justifyContent: "space-between", cursor: "pointer", fontSize: 13, fontWeight: 500, color: C.text }}>
        <span>{label}</span><Chevron open={open} />
      </div>
      {open && <div style={{ padding: "10px 12px", borderTop: `0.5px solid ${C.border}` }}>{children}</div>}
    </div>
  );
};

const ActionChecklist = ({ tier, hosts, runDate, type, notes }) => {
  if (tier === 'dismiss') return (
    <div style={{ marginTop: 10, padding: "10px 12px", borderRadius: 8, background: "#F0F0EE" }}>
      <div style={{ fontWeight: 500, fontSize: 13, marginBottom: 6, color: C.textSec }}>Dismissed -- no misuse</div>
      {hosts.map(host => (
        <div key={host} style={{ fontSize: 12, color: C.textSec, marginBottom: 2 }}>
          <div style={{ fontWeight: 500 }}>{host}</div>
          <div style={{ paddingLeft: 12 }}>○ Create Salesforce case with Dismissed status (pending write access)</div>
          {notes && <div style={{ paddingLeft: 12 }}>○ Add note: "{notes.slice(0,80)}{notes.length > 80 ? "..." : ""}" (pending write access)</div>}
        </div>
      ))}
    </div>
  );

  if (tier === 'program_team') return (
    <div style={{ marginTop: 10, padding: "10px 12px", borderRadius: 8, background: C.greenLight }}>
      <div style={{ fontWeight: 500, fontSize: 13, marginBottom: 6, color: C.green }}>Referred to program team</div>
      {hosts.map(host => (
        <div key={host} style={{ fontSize: 12, marginBottom: 4 }}>
          <div style={{ fontWeight: 500, color: C.text }}>{host}</div>
          <div style={{ paddingLeft: 12, color: "#854F0B" }}>○ Create Salesforce case -- Program Team Referral (pending write access)</div>
          <div style={{ paddingLeft: 12, color: C.green }}>✓ Create Gmail draft to program team (active)</div>
          {notes && <div style={{ paddingLeft: 12, color: "#854F0B" }}>○ Add note: "{notes.slice(0,80)}{notes.length > 80 ? "..." : ""}" (pending write access)</div>}
        </div>
      ))}
    </div>
  );

  const base = [
    { label: "Create Salesforce case linked to Contact", ready: false },
    { label: `Set Flag__c = true on Contact`, ready: false },
    { label: `Set Flag_Reason__c = "See case from ${runDate}"`, ready: false },
    { label: "Create Gmail draft with consequence email", ready: true },
  ];
  if (notes) {
    base.push({ label: `Add internal note to Salesforce case: "${notes.slice(0, 80)}${notes.length > 80 ? "..." : ""}"`, ready: false });
  }
  if (type === "ts") {
    if (["nourishment_pause","suspension","deactivation"].includes(tier)) {
      base.push({ label: "Set Do_Not_Nourish__c = true on Contact", ready: false });
      base.push({ label: "Move all future dinners to Not Nourishing", ready: false });
    }
    if (["suspension","deactivation"].includes(tier)) {
      base.push({ label: "Set Suspended_Flag__c = true on Contact", ready: false });
      base.push({ label: 'Set Grant Application status = "Suspended"', ready: false });
    }
    if (tier === "deactivation") {
      base.push({ label: "Alert Amalia", ready: true });
      base.push({ label: "Create Zendesk ticket for IP/account ban", ready: false });
    }
    base.push({ label: "Manual: check DNN in backend", ready: false });
    if (["suspension","deactivation"].includes(tier)) base.push({ label: "Manual: check Banned in backend", ready: false });
  } else {
    base[2] = { label: type === "program_policy"
      ? `Set Flag_Reason__c = "See case from ${runDate} -- Program Policy, routed to program team"`
      : `Set Flag_Reason__c = "See case from ${runDate} -- same address, confirm one-Nourishment-per-household rule"`, ready: false };
    if (type === "program_policy") base.push({ label: "Move ineligible dinner(s) to Not Nourishing", ready: false });
  }
  return (
    <div style={{ marginTop: 10, padding: "10px 12px", borderRadius: 8, background: type === "ts" ? C.wineLight : C.greenLight }}>
      <div style={{ fontWeight: 500, fontSize: 13, marginBottom: 6, color: type === "ts" ? C.wine : C.green }}>
        {type === "ts" ? "Actions queued:" : type === "program_policy" ? "Routed to program team:" : "Flagged for review:"}
      </div>
      {hosts.map(host => (
        <div key={host} style={{ marginBottom: 8 }}>
          <div style={{ fontWeight: 500, fontSize: 12, color: C.text, marginBottom: 2 }}>{host}</div>
          {base.map((a, i) => (
            <div key={i} style={{ fontSize: 12, color: a.ready ? C.green : "#854F0B", paddingLeft: 12, marginBottom: 2 }}>
              {a.ready ? "✓" : "○"} {a.label}{a.ready ? " (active)" : " (pending write access)"}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
};

// ── case card ─────────────────────────────────────────────────────────────────
const CaseCard = ({ c, runDate, onAction, onNote, onDiscuss }) => {
  const [open, setOpen] = useState(false);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideTier, setOverrideTier] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [actioned, setActioned] = useState(c.actioned || false);
  const [actionedTier, setActionedTier] = useState(c.actionedTier || c.tier);
  const [showActions, setShowActions] = useState(false);
  const [clusterWarning, setClusterWarning] = useState(false);
  const tierMap = { suspension: { bg: C.wineLight, color: C.wine }, nourishment_pause: { bg: "#FAEEDA", color: C.amberDark }, warning: { bg: C.oliveLight, color: "#27500A" } };
  const t = tierMap[c.tier] || tierMap.warning;
  const hosts = c.is_cluster && c.cluster_hosts?.length ? c.cluster_hosts.map(h => h.name) : [c.name];

  const doApprove = (tier) => {
    if (c.is_cluster && !clusterWarning && !['dismiss','program_team'].includes(tier)) { setClusterWarning(true); return; }
    setActioned(true); setActionedTier(tier); setShowActions(true);
    setOverrideOpen(false); setClusterWarning(false);
    onAction(c.id, tier, overrideReason || null);
  };

  return (
    <div style={{ background: C.bgCard, border: `0.5px solid ${C.border}`, borderRadius: 12, marginBottom: 12, overflow: "hidden" }}>
      <div onClick={() => setOpen(!open)} style={{ padding: "14px 16px", display: "flex", alignItems: "center", gap: 12, cursor: "pointer", userSelect: "none", background: open ? C.khakiLight : "transparent" }}>
        <div style={{ width: 40, height: 40, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 500, fontSize: 15, flexShrink: 0, background: t.bg, color: t.color }}>{c.score}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 3 }}>
            <a href={c.sf_url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} style={{ fontWeight: 500, fontSize: 15, color: C.text, textDecoration: "underline", textUnderlineOffset: 3 }}>{c.name} ↗</a>
            <TierBadge tier={actionedTier} cluster={c.is_cluster} />
            {actioned && <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 4, background: C.oliveLight, color: C.green, fontWeight: 500 }}>✓ Actioned</span>}
          </div>
          <div style={{ fontSize: 12, color: C.textSec, display: "flex", gap: 6, flexWrap: "wrap" }}>
            <span>Score {c.score}</span><span style={{ color: C.border }}>·</span>
            <span>{c.nourishment_received}</span><span style={{ color: C.border }}>·</span>
            <span>{c.future_dinners} future dinner{c.future_dinners !== "1" ? "s" : ""}</span>
            {c.suspended && <><span style={{ color: C.border }}>·</span><span style={{ color: C.wine, fontWeight: 500 }}>active suspension</span></>}
            {c.dnn && !c.suspended && <><span style={{ color: C.border }}>·</span><span style={{ color: C.amberDark, fontWeight: 500 }}>DNN</span></>}
          </div>
          <div style={{ fontSize: 13, color: C.textSec, marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.collapsed_summary}</div>
        </div>
        <Chevron open={open} />
      </div>

      {open && (
        <div style={{ padding: "0 16px 16px", borderTop: `0.5px solid ${C.border}` }}>
          {c.is_cluster && c.cluster_note && (
            <div style={{ background: "#FAEEDA", borderLeft: `3px solid #BA7517`, padding: "8px 12px", fontSize: 13, color: C.amberDark, borderRadius: "0 6px 6px 0", margin: "12px 0 4px" }}>
              🔗 {c.cluster_note}
            </div>
          )}

          {c.is_cluster && c.cluster_hosts?.length > 0 && (
            <>
              <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Hosts</div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead><tr>{["Host","Score","Nourishment","Address","Key signals"].map(h => <th key={h} style={{ textAlign: "left", fontWeight: 500, color: C.textSec, padding: "6px 8px", borderBottom: `0.5px solid ${C.border}`, fontSize: 12 }}>{h}</th>)}</tr></thead>
                <tbody>{c.cluster_hosts.map((h, i) => (
                  <tr key={i}>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, whiteSpace: "nowrap" }}><a href={h.sf_url} target="_blank" rel="noreferrer" style={{ color: C.purple, textDecoration: "none" }}>{h.name}</a></td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, whiteSpace: "nowrap" }}>{h.score}</td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, whiteSpace: "nowrap" }}>{h.nourishment_received}</td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, fontSize: 12, color: C.textSec }}>{h.address || "—"}</td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, fontSize: 12, color: C.textSec }}>{h.key_signals}</td>
                  </tr>
                ))}</tbody>
              </table>
            </>
          )}

          <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Summary</div>
          <ul style={{ margin: "10px 0 0", paddingLeft: 16 }}>
            {(c.bullets || []).map((b, i) => <li key={i} style={{ fontSize: 13, color: C.text, marginBottom: 5, lineHeight: 1.5 }} dangerouslySetInnerHTML={{ __html: b.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>") }} />)}
          </ul>

          <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Signals</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
            {(c.signals || []).map((s, i) => <SigPill key={i} name={s.name} triggered={s.triggered} />)}
          </div>

          {c.score_breakdown && (
            <Collapsible label="Score breakdown">
              <div style={{ fontFamily: "monospace", fontSize: 13, color: C.text, lineHeight: 1.8 }}>
                {(c.signals || []).filter(s => s.triggered && s.score_contribution > 0).map((s, i) => (
                  <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "2px 0", borderBottom: `0.5px solid ${C.border}` }}>
                    <span style={{ color: C.textSec }}>{s.name} <span style={{ color: C.textTer, fontSize: 12 }}>({s.observed})</span></span>
                    <span style={{ color: C.wine, fontWeight: 500 }}>+{s.score_contribution}</span>
                  </div>
                ))}
                {(c.signals || []).filter(s => !s.triggered).map((s, i) => (
                  <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "2px 0", borderBottom: `0.5px solid ${C.border}`, opacity: 0.5 }}>
                    <span style={{ color: C.textTer }}>{s.name} <span style={{ fontSize: 11 }}>— {s.threshold_met === false ? `${s.observed || "below threshold"}` : "not paired"}</span></span>
                    <span style={{ color: C.textTer }}>+0</span>
                  </div>
                ))}
                <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 0 2px", fontWeight: 600, fontSize: 14 }}>
                  <span>Total</span>
                  <span style={{ color: C.wine }}>{c.score}</span>
                </div>
              </div>
            </Collapsible>
          )}

          {c.host_context && (
            <Collapsible label="Host Context Profile">
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px" }}>
                <div>
                  {[["Tenure", c.host_context.tenure], ["Dinners hosted", c.host_context.dinners_hosted], ["Nourishment received", c.host_context.nourishment_received], ["Future dinners", c.host_context.future_dinners], ["DNN", c.host_context.dnn]].map(([l, v]) => <ProfileRow key={l} label={l} val={v} />)}
                </div>
                <div>
                  {[["Benchmark call", c.host_context.benchmark_call], ["Prior T&S cases", c.host_context.prior_ts_cases], ["Suspended", c.host_context.suspended], ["Graduated host", c.host_context.graduated_host], ["New host", c.host_context.new_host]].map(([l, v]) => <ProfileRow key={l} label={l} val={v} />)}
                </div>
              </div>
            </Collapsible>
          )}

          {clusterWarning && (
            <div style={{ marginTop: 12, padding: "10px 12px", background: "#FAEEDA", borderRadius: 8, fontSize: 13, color: C.amberDark }}>
              ⚠ This is a cluster case. Consult Amalia before proceeding.
              <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                <button onClick={() => { setClusterWarning(false); doApprove(c.tier); }} style={{ padding: "5px 12px", borderRadius: 6, border: "none", background: C.wine, color: "#fff", fontSize: 12, cursor: "pointer" }}>Confirmed, proceed</button>
                <button onClick={() => setClusterWarning(false)} style={{ padding: "5px 12px", borderRadius: 6, border: `0.5px solid ${C.border}`, background: "transparent", fontSize: 12, cursor: "pointer" }}>Cancel</button>
              </div>
            </div>
          )}

          {showActions && <ActionChecklist tier={actionedTier} hosts={hosts} runDate={runDate} type="ts" notes={c.notes} />}

          <div style={{ marginTop: 14 }}>
            <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Internal notes</div>
            <textarea
              value={c.notes || ""}
              onChange={e => onNote(c.id, e.target.value)}
              placeholder="Add notes for this case -- context, agreements, decisions. Saved with this run and added to Salesforce case on approval."
              style={{ width: "100%", padding: "8px 10px", borderRadius: 6, border: `0.5px solid ${C.border}`, fontSize: 13, minHeight: 72, resize: "vertical", boxSizing: "border-box", fontFamily: "inherit", color: C.text, background: C.bgCard }}
            />
          </div>

          {overrideOpen && (
            <div style={{ marginTop: 12, padding: 12, background: C.khakiLight, borderRadius: 8, border: `0.5px solid ${C.border}` }}>
              <div style={{ fontSize: 13, color: C.textSec, marginBottom: 6 }}>Select action</div>
              <select value={overrideTier} onChange={e => setOverrideTier(e.target.value)} style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: `0.5px solid ${C.border}`, fontSize: 13, marginBottom: 8 }}>
                <option value="">-- select --</option>
                <optgroup label="Override tier">
                  <option value="warning">Warning (1-8)</option>
                  <option value="nourishment_pause">Nourishment Pause (9-17)</option>
                  <option value="suspension">Suspension (18-39)</option>
                  <option value="deactivation">Deactivation (40+)</option>
                </optgroup>
                <optgroup label="Close without consequence">
                  <option value="dismiss">Dismiss -- no misuse</option>
                  <option value="program_team">Refer to program team</option>
                </optgroup>
              </select>
              <div style={{ fontSize: 13, color: C.textSec, marginBottom: 6 }}>Reason for override</div>
              <textarea value={overrideReason} onChange={e => setOverrideReason(e.target.value)} placeholder="Explain why you are overriding..." style={{ width: "100%", padding: "8px 10px", borderRadius: 6, border: `0.5px solid ${C.border}`, fontSize: 13, minHeight: 60, resize: "vertical", boxSizing: "border-box", marginBottom: 8 }} />
              <button onClick={() => { if (!overrideTier || !overrideReason) return; doApprove(overrideTier); }} style={{ padding: "6px 14px", borderRadius: 6, border: "none", background: C.amberDark, color: "#fff", fontSize: 13, cursor: "pointer", fontWeight: 500 }}>Confirm override</button>
            </div>
          )}

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 14, paddingTop: 12, borderTop: `0.5px solid ${C.border}`, flexWrap: "wrap", gap: 8 }}>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button disabled={actioned} onClick={() => doApprove(c.tier)} style={{ padding: "6px 16px", borderRadius: 6, border: "none", background: actioned ? C.border : C.wine, color: actioned ? C.textSec : "#fff", fontSize: 13, fontWeight: 500, cursor: actioned ? "default" : "pointer" }}>
                {actioned ? "✓ Approved" : "Approve recommendation"}
              </button>
              {!actioned && <button onClick={() => setOverrideOpen(!overrideOpen)} style={{ padding: "6px 14px", borderRadius: 6, border: `0.5px solid ${C.border}`, background: "transparent", fontSize: 13, cursor: "pointer" }}>Override tier</button>}
            </div>
            <button onClick={() => onDiscuss(c)} style={{ padding: "6px 14px", borderRadius: 6, border: `0.5px solid ${C.border}`, background: "transparent", fontSize: 13, cursor: "pointer", color: C.textSec }}>Discuss ↗</button>
          </div>
        </div>
      )}
    </div>
  );
};

// ── case chat ─────────────────────────────────────────────────────────────────
const CHAT_SYSTEM = `You are the OneTable Trust and Safety case advisor. You help staff review individual host cases flagged during the weekly T&S review. You know the full signal definitions, scoring rules, consequence framework, and policy.

Key rules:
- Warning: score 1-8 | Nourishment Pause: 9-17 | Suspension: 18-39 | Deactivation: 40+
- First instance rule: even at 40+, first consequence is Suspension not Deactivation
- Tier is determined by score only -- never override based on narrative
- Every signal requires pairing except: Shared device FP host+guest, Hard bounces, Reports from users, Deliberate fraud, Deliberate identity change
- Sequential PIDs: gap ≤ 2, denominator = ALL guests

You have the full case data in context. Answer questions directly and concisely. You can help draft outreach emails, explain signal math, suggest what to look for in Salesforce, or discuss edge cases. Always stay grounded in the data provided -- never invent facts about the host.`;

const CaseChat = ({ c, runDate, onClose }) => {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottomRef = React.useRef(null);

  const caseContext = `You are advising on this case:

Host: ${c.name}
Tier: ${c.tier} | Score: ${c.score}
Nourishment received: ${c.nourishment_received}
Suspended: ${c.suspended} | DNN: ${c.dnn}
Run date: ${runDate}

Signals triggered:
${(c.signals || []).filter(s => s.triggered).map(s => `- ${s.name}: ${s.observed || ''} (+${s.score_contribution})`).join('\n')}

Score breakdown: ${c.score_breakdown || c.score}

Host context:
${Object.entries(c.host_context || {}).map(([k, v]) => `- ${k}: ${v}`).join('\n')}

Summary bullets:
${(c.bullets || []).map(b => `- ${b.replace(/\*\*/g, '')}`).join('\n')}`;

  React.useEffect(() => {
    // Initial message
    setMessages([{
      role: 'assistant',
      content: `I'm looking at the **${c.name}** case (score ${c.score} → ${c.tier.replace('_', ' ')}). What would you like to know?`
    }]);
  }, [c.id]);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const send = async () => {
    if (!input.trim() || loading) return;
    const userMsg = input.trim();
    setInput("");
    setMessages(prev => [...prev, { role: 'user', content: userMsg }]);
    setLoading(true);

    try {
      const history = messages.map(m => ({ role: m.role, content: m.content }));
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "claude-sonnet-4-6",
          max_tokens: 1000,
          system: CHAT_SYSTEM + "\n\n" + caseContext,
          messages: [...history, { role: 'user', content: userMsg }],
        }),
      });
      const data = await res.json();
      const text = (data.content || []).filter(b => b.type === 'text').map(b => b.text).join('');
      setMessages(prev => [...prev, { role: 'assistant', content: text }]);
    } catch (e) {
      setMessages(prev => [...prev, { role: 'assistant', content: 'Error connecting to API. Try again.' }]);
    }
    setLoading(false);
  };

  return (
    <div style={{ position: "fixed", bottom: 0, right: 0, width: 400, height: "60vh", background: C.bgCard, border: `1px solid ${C.border}`, borderRadius: "12px 12px 0 0", boxShadow: "0 -4px 24px rgba(0,0,0,0.12)", display: "flex", flexDirection: "column", zIndex: 1000 }}>
      {/* header */}
      <div style={{ padding: "12px 16px", borderBottom: `0.5px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center", background: C.wine, borderRadius: "12px 12px 0 0" }}>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14, color: "#fff" }}>Discuss: {c.name}</div>
          <div style={{ fontSize: 12, color: "rgba(255,255,255,0.7)" }}>Score {c.score} · {c.tier.replace('_',' ')}</div>
        </div>
        <button onClick={onClose} style={{ background: "transparent", border: "none", color: "#fff", fontSize: 18, cursor: "pointer", lineHeight: 1 }}>×</button>
      </div>

      {/* messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "12px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
        {messages.map((m, i) => (
          <div key={i} style={{ display: "flex", justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
            <div style={{
              maxWidth: "85%", padding: "8px 12px", borderRadius: m.role === 'user' ? "12px 12px 2px 12px" : "12px 12px 12px 2px",
              background: m.role === 'user' ? C.wine : C.khakiLight,
              color: m.role === 'user' ? "#fff" : C.text,
              fontSize: 13, lineHeight: 1.5,
            }} dangerouslySetInnerHTML={{ __html: m.content.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br/>') }} />
          </div>
        ))}
        {loading && (
          <div style={{ display: "flex", justifyContent: "flex-start" }}>
            <div style={{ padding: "8px 12px", borderRadius: "12px 12px 12px 2px", background: C.khakiLight, fontSize: 13, color: C.textSec }}>
              Thinking...
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* quick prompts */}
      <div style={{ padding: "6px 16px", display: "flex", gap: 6, flexWrap: "wrap", borderTop: `0.5px solid ${C.border}` }}>
        {["Why this score?", "Draft outreach email", "What to check in SF?", "Explain the signals"].map(q => (
          <button key={q} onClick={() => { setInput(q); }} style={{ fontSize: 11, padding: "3px 8px", borderRadius: 10, border: `0.5px solid ${C.border}`, background: "transparent", cursor: "pointer", color: C.textSec }}>{q}</button>
        ))}
      </div>

      {/* input */}
      <div style={{ padding: "8px 12px", borderTop: `0.5px solid ${C.border}`, display: "flex", gap: 8 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
          placeholder="Ask anything about this case..."
          style={{ flex: 1, padding: "8px 10px", borderRadius: 8, border: `0.5px solid ${C.border}`, fontSize: 13, outline: "none" }}
        />
        <button onClick={send} disabled={loading || !input.trim()} style={{ padding: "8px 14px", borderRadius: 8, border: "none", background: input.trim() ? C.wine : C.border, color: "#fff", fontSize: 13, cursor: input.trim() ? "pointer" : "default" }}>↑</button>
      </div>
    </div>
  );
};
const FlagCard = ({ f, runDate, onAction }) => {
  const [open, setOpen] = useState(false);
  const [actioned, setActioned] = useState(f.actioned || false);
  const [showActions, setShowActions] = useState(false);
  const hosts = f.hosts?.map(h => h.name) || [];

  const doAction = () => { setActioned(true); setShowActions(true); onAction(f.id, f.type); };

  return (
    <div style={{ background: C.bgCard, border: `0.5px solid ${C.border}`, borderRadius: 12, marginBottom: 12, overflow: "hidden" }}>
      <div onClick={() => setOpen(!open)} style={{ padding: "14px 16px", display: "flex", alignItems: "center", gap: 12, cursor: "pointer", userSelect: "none" }}>
        <div style={{ width: 40, height: 40, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18, flexShrink: 0, background: f.type === "program_policy" ? C.greenLight : "#FAEEDA", color: f.type === "program_policy" ? C.green : C.amberDark }}>
          {f.type === "program_policy" ? "⊕" : "⌂"}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 3 }}>
            <span style={{ fontWeight: 500, fontSize: 15, color: C.text }}>{f.title}</span>
            <FlagBadge type={f.type} />
            {actioned && <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 4, background: C.oliveLight, color: C.green, fontWeight: 500 }}>✓ Actioned</span>}
          </div>
          <div style={{ fontSize: 13, color: C.textSec }}>{hosts.join(" · ")}</div>
        </div>
        <Chevron open={open} />
      </div>
      {open && (
        <div style={{ padding: "0 16px 16px", borderTop: `0.5px solid ${C.border}` }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Summary</div>
          <ul style={{ margin: "10px 0 0", paddingLeft: 16 }}>
            {(f.summary_bullets || []).map((b, i) => <li key={i} style={{ fontSize: 13, color: C.text, marginBottom: 5, lineHeight: 1.5 }} dangerouslySetInnerHTML={{ __html: b.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>") }} />)}
          </ul>

          {f.similarity_pct !== undefined && (
            <>
              <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Dinner similarity</div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ flex: 1, height: 4, background: C.border, borderRadius: 2, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${f.similarity_pct}%`, background: C.green, borderRadius: 2 }} />
                </div>
                <span style={{ fontSize: 12, color: C.textSec, minWidth: 32, textAlign: "right" }}>{f.similarity_pct}%</span>
              </div>
            </>
          )}

          {f.flags?.length > 0 && (
            <>
              <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Flags</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {f.flags.map((fl, i) => (
                  <span key={i} style={{ padding: "4px 10px", borderRadius: 6, fontSize: 12,
                    background: fl.type === "yes" ? C.greenLight : fl.type === "no" ? C.wineLight : "#F0F0EE",
                    color: fl.type === "yes" ? C.green : fl.type === "no" ? C.wine : C.textSec }}>{fl.label}</span>
                ))}
              </div>
            </>
          )}

          {f.hosts?.length > 0 && (
            <>
              <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "14px 0 6px" }}>Dinners</div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
                <colgroup><col style={{ width: "22%" }} /><col style={{ width: "40%" }} /><col style={{ width: "18%" }} /><col style={{ width: "20%" }} /></colgroup>
                <thead><tr>{["Host","Description","Eligible","Status"].map(h => <th key={h} style={{ textAlign: "left", fontWeight: 500, color: C.textSec, padding: "6px 8px", borderBottom: `0.5px solid ${C.border}`, fontSize: 12 }}>{h}</th>)}</tr></thead>
                <tbody>{f.hosts.map((h, i) => (
                  <tr key={i}>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, verticalAlign: "top" }}>
                      <a href={h.sf_url} target="_blank" rel="noreferrer" style={{ color: C.purple, textDecoration: "none" }}>{h.name}</a>
                      <div style={{ fontSize: 11, color: C.textSec, marginTop: 2 }}>{h.dinner}</div>
                    </td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, fontSize: 12, color: C.textSec, lineHeight: 1.4, verticalAlign: "top" }}>{h.description}</td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, verticalAlign: "top" }}>{h.eligible}</td>
                    <td style={{ padding: "8px", borderBottom: `0.5px solid ${C.border}`, verticalAlign: "top", fontSize: 12 }}>{h.status}</td>
                  </tr>
                ))}</tbody>
              </table>
            </>
          )}

          {showActions && <ActionChecklist tier={null} hosts={hosts} runDate={runDate} type={f.type} />}

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 14, paddingTop: 12, borderTop: `0.5px solid ${C.border}` }}>
            <button disabled={actioned} onClick={doAction} style={{ padding: "6px 16px", borderRadius: 6, border: "none", fontSize: 13, fontWeight: 500, cursor: actioned ? "default" : "pointer",
              background: actioned ? C.border : f.type === "program_policy" ? C.green : C.amber,
              color: actioned ? C.textSec : f.type === "program_policy" ? "#fff" : C.amberDark }}>
              {actioned ? "✓ Done" : f.type === "program_policy" ? "Route to program team" : "Flag for review"}
            </button>
            <button onClick={() => {
              const msg = `I want to discuss the ${f.title} cross-host flag from the ${runDate || "June 19, 2026"} weekly run.`;
              try {
                if (typeof window.sendPrompt === "function") { window.sendPrompt(msg); }
                else if (typeof sendPrompt === "function") { sendPrompt(msg); }
                else { navigator.clipboard?.writeText(msg); alert("Copied to clipboard -- paste into chat"); }
              } catch(e) { navigator.clipboard?.writeText(msg); alert("Copied to clipboard -- paste into chat"); }
            }} style={{ padding: "6px 14px", borderRadius: 6, border: `0.5px solid ${C.border}`, background: "transparent", fontSize: 13, cursor: "pointer", color: C.textSec }}>Discuss ↗</button>
          </div>
        </div>
      )}
    </div>
  );
};

// ── insights ──────────────────────────────────────────────────────────────────
const InsightsSection = ({ insights }) => {
  if (!insights) return null;
  return (
    <Collapsible label="Weekly Insights">
      {insights.patterns && <><div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Patterns</div><p style={{ fontSize: 13, color: C.textSec, marginBottom: 12 }}>{insights.patterns}</p></>}
      {insights.emerging_trends && <><div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Emerging Trends</div><p style={{ fontSize: 13, color: C.textSec, marginBottom: 12 }}>{insights.emerging_trends}</p></>}
      {insights.below_threshold?.length > 0 && (
        <>
          <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Below-threshold signals</div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, marginBottom: 12 }}>
            <thead><tr>{["Signal","Count","Threshold","Avg observed"].map(h => <th key={h} style={{ textAlign: "left", padding: "6px 8px", borderBottom: `0.5px solid ${C.border}`, fontWeight: 500, color: C.textSec, fontSize: 12 }}>{h}</th>)}</tr></thead>
            <tbody>{insights.below_threshold.map((b, i) => <tr key={i}><td style={{ padding: "6px 8px" }}>{b.signal}</td><td style={{ padding: "6px 8px" }}>{b.count}</td><td style={{ padding: "6px 8px" }}>{b.threshold}</td><td style={{ padding: "6px 8px" }}>{b.avg_observed}</td></tr>)}</tbody>
          </table>
        </>
      )}
      {insights.open_questions?.length > 0 && (
        <>
          <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Open Questions</div>
          <ul style={{ paddingLeft: 16, margin: 0 }}>{insights.open_questions.map((q, i) => <li key={i} style={{ fontSize: 13, color: C.textSec, marginBottom: 4 }}>{q}</li>)}</ul>
        </>
      )}
    </Collapsible>
  );
};

// ── slack section ─────────────────────────────────────────────────────────────
const SlackSection = ({ slack, runDate, allActioned }) => {
  const [sent, setSent] = useState(false);
  const [draft, setDraft] = useState("");
  useEffect(() => {
    if (!slack || !allActioned) return;
    const lines = [
      `*Trust and Safety · Week of ${runDate}*`,
      `${slack.totals?.suspension || 0} suspensions · ${slack.totals?.nourishment_pause || 0} Nourishment pauses · ${slack.totals?.warning || 0} warnings · ${slack.totals?.clusters || 0} clusters`,
      "",
      slack.actioned?.length ? `*Actioned this week:*\n${slack.actioned.map(a => `• <${a.sf_case_url}|${a.name}> · ${a.tier}${a.note ? ` · ${a.note}` : ""}`).join("\n")}` : "",
      slack.trends ? `\n*Trends:* ${slack.trends}` : "",
      slack.urgent ? `\n⚠ *Urgent:* ${slack.urgent}` : "",
    ].filter(Boolean).join("\n");
    setDraft(lines);
  }, [slack, runDate, allActioned]);

  if (!allActioned) return (
    <div style={{ padding: 16, background: C.khakiLight, borderRadius: 8, textAlign: "center", fontSize: 13, color: C.textSec, marginTop: 16 }}>
      🔒 Slack summary unlocks after all cases are actioned
    </div>
  );
  return (
    <div style={{ marginTop: 16, padding: 16, background: C.bgCard, border: `0.5px solid ${C.border}`, borderRadius: 12 }}>
      <div style={{ fontWeight: 500, fontSize: 14, color: C.text, marginBottom: 10 }}>Slack summary — #trust-and-safety</div>
      <textarea value={draft} onChange={e => setDraft(e.target.value)} style={{ width: "100%", minHeight: 140, padding: "10px 12px", borderRadius: 8, border: `0.5px solid ${C.border}`, fontSize: 13, fontFamily: "monospace", resize: "vertical", boxSizing: "border-box", marginBottom: 10 }} />
      <button disabled={sent} onClick={() => setSent(true)} style={{ padding: "6px 16px", borderRadius: 6, border: "none", background: sent ? C.border : C.purple, color: sent ? C.textSec : "#fff", fontSize: 13, fontWeight: 500, cursor: sent ? "default" : "pointer" }}>
        {sent ? "✓ Sent to Slack" : "Send to #trust-and-safety"}
      </button>
    </div>
  );
};

// ── load run panel ─────────────────────────────────────────────────────────────
const LoadPanel = ({ onLoad }) => {
  const [jsonText, setJsonText] = useState("");
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);

  const tryLoad = (text) => {
    const data = parseJson(text);
    if (!data) { setError("Could not parse JSON. Make sure you copied the full ts_ui_data block from the agent output."); return; }
    setError("");
    onLoad(data);
  };

  const handleDrop = (e) => {
    e.preventDefault(); setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => { setJsonText(ev.target.result); tryLoad(ev.target.result); };
    reader.readAsText(file);
  };

  return (
    <div style={{ maxWidth: 600, margin: "48px auto", padding: "0 16px" }}>
      <div style={{ textAlign: "center", marginBottom: 32 }}>
        <div style={{ fontSize: 32, marginBottom: 12 }}>📋</div>
        <div style={{ fontWeight: 600, fontSize: 18, marginBottom: 8 }}>Load Weekly Run</div>
        <div style={{ fontSize: 14, color: C.textSec }}>Paste the <code style={{ fontSize: 13, background: C.khakiLight, padding: "1px 5px", borderRadius: 4 }}>ts_ui_data</code> JSON block from the agent chat, or drop a JSON file</div>
      </div>

      <div onDragOver={e => { e.preventDefault(); setDragOver(true); }} onDragLeave={() => setDragOver(false)} onDrop={handleDrop}
        style={{ border: `2px dashed ${dragOver ? C.wine : C.border}`, borderRadius: 12, padding: 16, background: dragOver ? C.wineLight : C.bgCard, marginBottom: 12, transition: "all 0.15s" }}>
        <textarea value={jsonText} onChange={e => setJsonText(e.target.value)} placeholder='Paste JSON here or drag and drop a .json file...'
          style={{ width: "100%", minHeight: 180, padding: "10px 12px", border: "none", background: "transparent", fontSize: 13, fontFamily: "monospace", resize: "vertical", boxSizing: "border-box", outline: "none" }} />
      </div>

      {error && <div style={{ padding: "8px 12px", background: C.wineLight, borderRadius: 6, color: C.wine, fontSize: 13, marginBottom: 12 }}>{error}</div>}

      <button onClick={() => tryLoad(jsonText)} style={{ width: "100%", padding: "10px 0", borderRadius: 8, border: "none", background: C.wine, color: "#fff", fontSize: 14, fontWeight: 500, cursor: "pointer" }}>
        Load run →
      </button>
    </div>
  );
};

// ── main app ──────────────────────────────────────────────────────────────────
export default function App() {
  const [view, setView] = useState("load"); // load | review | history
  const [runs, setRuns] = useState([]);
  const [currentRun, setCurrentRun] = useState(null);
  const [filter, setFilter] = useState("all");
  const [activeChat, setActiveChat] = useState(null); // case object for chat panel
  const [actionState, setActionState] = useState({});

  useEffect(() => { loadRuns().then(r => { setRuns(r); }); }, []);

  const handleLoad = async (data) => {
    const run = { ...data, id: Date.now(), loadedAt: new Date().toISOString() };
    const newRuns = [run, ...runs].slice(0, 24);
    setRuns(newRuns);
    setCurrentRun(run);
    setActionState({});
    setFilter("all");
    setView("review");
    await saveRuns(newRuns);
  };

  const handleAction = (id, tier, reason) => {
    setActionState(prev => ({ ...prev, [id]: { ...prev[id], tier, reason } }));
  };

  const handleNote = useCallback((id, note) => {
    setActionState(prev => ({ ...prev, [id]: { ...prev[id], note } }));
  }, []);

  const cases = currentRun?.cases || [];
  const flags = currentRun?.cross_host_flags || [];
  const allActioned = cases.length > 0 && cases.every(c => actionState[c.id]) && flags.every(f => actionState[f.id]);
  const runDate = currentRun?.run?.run_date || currentRun?.run?.week_of || "";

  const filteredCases = [...cases].filter(c => {
    if (filter === "all") return true;
    if (filter === "cluster") return c.is_cluster;
    return c.tier === filter;
  }).sort((a, b) => {
    if (a.is_cluster && !b.is_cluster) return -1;
    if (!a.is_cluster && b.is_cluster) return 1;
    return b.score - a.score;
  });

  return (
    <div style={{ fontFamily: "system-ui, -apple-system, sans-serif", background: C.bg, minHeight: "100vh", color: C.text }}>
      {/* header */}
      <div style={{ background: C.wine, padding: "12px 24px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: "#fff", fontWeight: 600, fontSize: 16 }}>OneTable</span>
          <span style={{ color: "rgba(255,255,255,0.6)", fontSize: 14 }}>Trust & Safety</span>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {[["load","Load Run"],["review","Current Run"],["history","History"]].map(([v, l]) => (
            <button key={v} onClick={() => setView(v)} style={{ padding: "5px 12px", borderRadius: 6, border: "none", fontSize: 13, cursor: "pointer",
              background: view === v ? "rgba(255,255,255,0.2)" : "transparent", color: "#fff", fontWeight: view === v ? 600 : 400 }}>{l}</button>
          ))}
        </div>
      </div>

      {/* load view */}
      {view === "load" && <LoadPanel onLoad={handleLoad} />}

      {/* review view */}
      {view === "review" && currentRun && (
        <div style={{ maxWidth: 900, margin: "0 auto", padding: "20px 16px" }}>
          {/* summary bar */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginBottom: 20 }}>
            {[
              { label: "Suspension", n: currentRun.run?.summary?.suspension || 0, color: C.wine },
              { label: "Nourishment pause", n: currentRun.run?.summary?.nourishment_pause || 0, color: C.amberDark },
              { label: "Warning", n: currentRun.run?.summary?.warning || 0, color: C.olive },
              { label: "Dinners reviewed", n: currentRun.run?.dinners_reviewed || 0, color: C.textSec },
            ].map(({ label, n, color }) => (
              <div key={label} style={{ background: C.bgCard, border: `0.5px solid ${C.border}`, borderRadius: 8, padding: "12px 14px" }}>
                <div style={{ fontSize: 22, fontWeight: 500, color }}>{n}</div>
                <div style={{ fontSize: 12, color: C.textSec, marginTop: 2 }}>{label}</div>
              </div>
            ))}
          </div>

          {/* run info + load new */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div style={{ fontSize: 12, color: C.textSec }}>Week of {currentRun.run?.week_of} · Run date: {runDate} · Agent v5.1</div>
            <button onClick={() => setView("load")} style={{ padding: "5px 12px", borderRadius: 6, border: `0.5px solid ${C.border}`, background: "transparent", fontSize: 12, cursor: "pointer", color: C.textSec }}>+ Load new run</button>
          </div>

          {/* filter bar */}
          <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
            {[["all","All"],["cluster","⚠ Cluster"],["suspension","Suspension"],["nourishment_pause","Nourishment pause"],["warning","Warning"]].map(([f, l]) => (
              <button key={f} onClick={() => setFilter(f)} style={{ fontSize: 13, padding: "5px 12px", borderRadius: 6, border: `0.5px solid ${filter === f ? C.wine : C.border}`, background: filter === f ? C.wineLight : "transparent", color: filter === f ? C.wine : C.textSec, cursor: "pointer" }}>{l}</button>
            ))}
          </div>

          {/* cases */}
          {filteredCases.length === 0
            ? <div style={{ fontSize: 14, color: C.textSec, marginBottom: 16 }}>No cases match this filter.</div>
            : filteredCases.map(c => <CaseCard key={c.id} c={{ ...c, actioned: !!actionState[c.id], actionedTier: actionState[c.id]?.tier, notes: actionState[c.id]?.note || "" }} runDate={runDate} onAction={handleAction} onNote={handleNote} onDiscuss={setActiveChat} />)
          }

          {/* cross-host flags */}
          {flags.length > 0 && (
            <>
              <div style={{ fontSize: 11, fontWeight: 500, color: C.textTer, textTransform: "uppercase", letterSpacing: "0.05em", margin: "24px 0 10px" }}>Cross-host flags — program team</div>
              {flags.map(f => <FlagCard key={f.id} f={{ ...f, actioned: !!actionState[f.id] }} runDate={runDate} onAction={(id, type) => handleAction(id, type, null)} />)}
            </>
          )}

          <InsightsSection insights={currentRun.insights} />
          <SlackSection slack={currentRun.slack_summary} runDate={runDate} allActioned={allActioned} />
        </div>
      )}

      {view === "review" && !currentRun && (
        <div style={{ textAlign: "center", padding: "64px 24px", color: C.textSec }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>📭</div>
          <div style={{ fontSize: 16, marginBottom: 8 }}>No run loaded</div>
          <button onClick={() => setView("load")} style={{ padding: "8px 18px", borderRadius: 8, border: "none", background: C.wine, color: "#fff", fontSize: 13, cursor: "pointer" }}>Load a run</button>
        </div>
      )}

      {/* history view */}
      {view === "history" && (
        <div style={{ maxWidth: 900, margin: "0 auto", padding: "20px 16px" }}>
          <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 16 }}>Run History</div>
          {runs.length === 0
            ? <div style={{ fontSize: 14, color: C.textSec }}>No runs saved yet.</div>
            : runs.map((r) => (
              <div key={r.id} onClick={() => { setCurrentRun(r); setActionState({}); setFilter("all"); setView("review"); }}
                style={{ padding: "12px 16px", background: C.bgCard, border: `0.5px solid ${C.border}`, borderRadius: 8, marginBottom: 8, cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Week of {r.run?.week_of || "unknown"}</div>
                  <div style={{ fontSize: 12, color: C.textSec, marginTop: 2 }}>
                    {r.run?.summary?.suspension || 0} suspension · {r.run?.summary?.nourishment_pause || 0} pause · {r.run?.summary?.warning || 0} warning · {r.run?.dinners_reviewed || 0} dinners reviewed
                  </div>
                </div>
                <span style={{ fontSize: 12, color: C.textSec }}>View →</span>
              </div>
            ))
          }
        </div>
      )}

      {/* case chat panel */}
      {activeChat && (
        <CaseChat
          c={activeChat}
          runDate={runDate}
          onClose={() => setActiveChat(null)}
        />
      )}
    </div>
  );
}
