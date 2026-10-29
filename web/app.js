// Dispute Triage Service web client. Plain JS, no build step; talks only to this API:
// POST /disputes, GET /runs/{id}, POST /runs/{id}/feedback, GET /quarantine, GET /readyz.
"use strict";

// ---------- Contract values ----------
// Enum values mirror contracts/dispute.py and contracts/result.py (and
// contracts/schema/dispute_request.schema.json). tests/test_web.py fails if they drift.

const ACCOUNT_TYPES = [
  ["BSBD", "Basic savings bank deposit (BSBD)"],
  ["SAVINGS", "Savings"],
  ["CURRENT_MSME", "Current: MSME"],
  ["CURRENT_INDIVIDUAL_UPTO_25L", "Current: individual, balance or limit up to ₹25 lakh"],
  ["CURRENT_OTHER", "Current: other"],
  ["CREDIT_CARD_UPTO_5L", "Credit card, limit up to ₹5 lakh"],
  ["CREDIT_CARD_ABOVE_5L", "Credit card, limit above ₹5 lakh"],
  ["PREPAID", "Prepaid payment instrument"],
];

const CHANNELS = [
  ["CARD_PRESENT", "Card present (shop / POS)"],
  ["CARD_NOT_PRESENT", "Card not present (online)"],
  ["UPI", "UPI"],
  ["NET_BANKING", "Net banking"],
  ["MOBILE_BANKING", "Mobile banking"],
  ["ATM", "ATM"],
];

const ROUTES = {
  provisional_credit: "Provisional credit",
  chargeback: "Chargeback",
  decline: "Decline",
  escalate: "Escalate to analyst",
};

const FRAUD_TYPES = {
  card_not_present_fraud: "Card-not-present fraud",
  skimming: "Skimming / card cloning",
  upi_social_engineering: "UPI social engineering",
  merchant_dispute: "Merchant dispute",
  duplicate_charge: "Duplicate charge",
  unknown: "Not determined",
};

const STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  escalated: "Escalated",
  failed: "Failed",
};
const TERMINAL = new Set(["completed", "escalated", "failed"]);

// Corpus documents (contracts.agent.DocId).
const DOC_NAMES = {
  IDBI_POLICY: "IDBI Bank policy",
  RBI_2017_CIRCULAR: "RBI circular, 6 July 2017",
  RBI_IOS_2026: "RBI Integrated Ombudsman Scheme 2026",
};

// customer_claims: each is true, false or null ("not stated").
const CLAIMS = [
  { name: "shared_otp_or_pin", label: "Shared an OTP, PIN or password with anyone" },
  { name: "card_in_possession", label: "Card was in their possession", hint: "Must be “Not stated” for UPI." },
  { name: "received_alert", label: "Received the bank's SMS or email alert" },
];

const FIELD_LABELS = {
  customer_id: "Customer ID",
  account_type: "Account type",
  home_branch_state: "Home branch state",
  transaction_id: "Transaction ID",
  channel: "Channel",
  transaction_amount_inr: "Disputed amount",
  transaction_datetime: "Transaction time",
  bank_alert_datetime: "Bank alert time",
  reported_datetime: "Reported time",
  complaint_text: "Complaint",
  shared_otp_or_pin: "Shared OTP or PIN",
  card_in_possession: "Card in possession",
  received_alert: "Received alert",
};

// Limits from contracts/dispute.py.
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const STATE_PATTERN = /^[A-Za-z][A-Za-z .&-]*$/;
const MAX_AMOUNT_INR = 10_000_000;
const COMPLAINT_MIN = 10;
const COMPLAINT_MAX = 4000;
const FUTURE_SKEW_MS = 5 * 60e3;
// The contract needs an explicit offset; the form is in Indian Standard Time.
const IST_OFFSET = "+05:30";
const IST_OFFSET_MS = 330 * 60e3;
// FeedbackRequest.reason must be non-empty; sent when the analyst leaves it blank.
const FEEDBACK_DEFAULT_REASON = "No reason given.";

const STATES = [
  "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
  "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
  "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan",
  "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
  "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
  "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
];

// ---------- Demo cases ----------
// Taken from eval/scenarios.jsonl where one fits.
const EXAMPLES = [
  {
    // S01, with the report moved to two days after the alert.
    label: "1. ATM card skimmed, reported in 2 days",
    request: {
      customer_id: "CUST-100201",
      account_type: "SAVINGS",
      channel: "ATM",
      transaction_id: "TXN-2026-090801",
      transaction_amount_inr: 20000,
      transaction_datetime: "2026-09-08T22:40:00+05:30",
      bank_alert_datetime: "2026-09-08T22:41:00+05:30",
      reported_datetime: "2026-09-10T10:15:00+05:30",
      home_branch_state: "Karnataka",
      complaint_text: "Rs 20,000 was withdrawn from an ATM in Pune at 10:40 pm on 8th Sept. I was at home in Bengaluru and my debit card was with me the whole time. I have never shared my PIN with anybody. Please block the card and return my money.",
      customer_claims: { shared_otp_or_pin: false, card_in_possession: true, received_alert: true },
    },
  },
  {
    // S14: the section 4.1 case.
    label: "2. Same-day bank SMS, reported 3 weeks later, OTP given to fake bank caller",
    request: {
      customer_id: "CUST-100214",
      account_type: "SAVINGS",
      channel: "UPI",
      transaction_id: "TXN-2026-070101",
      transaction_amount_inr: 45000,
      transaction_datetime: "2026-07-01T11:02:00+05:30",
      bank_alert_datetime: "2026-07-01T11:03:00+05:30",
      reported_datetime: "2026-07-22T16:45:00+05:30",
      home_branch_state: "Maharashtra",
      complaint_text: "The bank's SMS alert for the transaction came the same day, but I did not notice it and reported it three weeks later. Before that, I had shared an OTP with someone who called saying he was from the bank.",
      customer_claims: { shared_otp_or_pin: true, card_in_possession: null, received_alert: true },
    },
  },
  {
    // S12.
    label: "3. Duplicate charge on a credit card",
    request: {
      customer_id: "CUST-100212",
      account_type: "CREDIT_CARD_UPTO_5L",
      channel: "CARD_NOT_PRESENT",
      transaction_id: "TXN-2026-100502",
      transaction_amount_inr: 5640,
      transaction_datetime: "2026-10-05T20:31:00+05:30",
      bank_alert_datetime: "2026-10-05T20:32:00+05:30",
      reported_datetime: "2026-10-06T09:10:00+05:30",
      home_branch_state: "Telangana",
      complaint_text: "Booked one Hyderabad-Delhi ticket for Rs 5,640 on the airline website. Payment page hung, I did not retry, but two debits of 5,640 came within one minute. Only one PNR was issued. Please reverse the second charge.",
      customer_claims: { shared_otp_or_pin: false, card_in_possession: true, received_alert: true },
    },
  },
  {
    // S02 with the report dated before the transaction: the server rejects and quarantines it.
    label: "4. Malformed: reported before the transaction (quarantine demo)",
    request: {
      customer_id: "CUST-100202",
      account_type: "SAVINGS",
      channel: "CARD_NOT_PRESENT",
      transaction_id: "TXN-2026-082401",
      transaction_amount_inr: 8450.75,
      transaction_datetime: "2026-08-24T02:13:00+05:30",
      bank_alert_datetime: "2026-08-24T02:14:00+05:30",
      reported_datetime: "2026-08-22T09:30:00+05:30",
      home_branch_state: "Tamil Nadu",
      complaint_text: "Got SMS at 2 am for a USD payment on some foreign website. I did not do this, no OTP came to me. Card is in my wallet only.",
      customer_claims: { shared_otp_or_pin: false, card_in_possession: true, received_alert: true },
    },
  },
];

// ---------- Timing ----------
const POLL_MS = 1500;
const SLOW_AFTER_MS = 20e3;
const GIVE_UP_AFTER_MS = 180e3; // well past RUN_TIMEOUT_SECONDS (90 s)
const HEALTH_MS = 15e3;
const RECENT_KEY = "dispute-triage.recent-runs";
const RECENT_MAX = 20;

// ---------- Small helpers ----------

const $ = (id) => document.getElementById(id);
const form = $("dispute-form");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const ICONS = {
  check: '<path d="M4.5 10.5l3.5 3.5 7.5-8"/>',
  credit: '<circle cx="10" cy="10" r="8.25"/><path d="M6.5 10.2l2.4 2.4 4.8-5"/>',
  chargeback: '<path d="M4 7h11l-3-3M16 13H5l3 3"/>',
  decline: '<circle cx="10" cy="10" r="8.25"/><path d="M7 7l6 6M13 7l-6 6"/>',
  escalate: '<circle cx="10" cy="6.5" r="3"/><path d="M4 17c.6-3.2 3-5 6-5s5.4 1.8 6 5"/>',
  alert: '<path d="M10 2.8l7.6 13.4H2.4z"/><path d="M10 8v3.8M10 14v.1"/>',
  info: '<circle cx="10" cy="10" r="8.25"/><path d="M10 9v5M10 6.2v.1"/>',
  cross: '<path d="M5.5 5.5l9 9M14.5 5.5l-9 9"/>',
};
const icon = (name) => `<svg class="icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">${ICONS[name]}</svg>`;
const ROUTE_ICONS = { provisional_credit: "credit", chargeback: "chargeback", decline: "decline", escalate: "escalate" };

const fmtDateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const fmtTime = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtINR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });

// Server timestamps are UTC; a value without an offset (SQLite) is read as UTC.
function parseServerTime(s) {
  if (!s) return NaN;
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`);
}
const showTime = (ms) => (Number.isFinite(ms) ? `${fmtDateTime.format(ms)} IST` : "–");

function duration(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}

// datetime-local holds IST wall-clock time ("2026-09-08T22:40"); the API wants an offset.
function inputToIso(value) {
  if (!value) return null;
  return `${value.length === 16 ? `${value}:00` : value}${IST_OFFSET}`;
}
function isoToInput(iso) {
  if (!iso) return "";
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? new Date(ms + IST_OFFSET_MS).toISOString().slice(0, 16) : "";
}
const inputMs = (value) => (value ? Date.parse(inputToIso(value)) : NaN);

// Calendar days between two IST dates, ignoring the time of day.
function calendarDays(fromValue, toValue) {
  const day = (v) => Date.UTC(+v.slice(0, 4), +v.slice(5, 7) - 1, +v.slice(8, 10));
  return Math.round((day(toValue) - day(fromValue)) / 86400e3);
}

function announce(text) {
  const el = $("announcer");
  el.textContent = "";
  setTimeout(() => { el.textContent = text; }, 50);
}

const storage = {
  read() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; }
  },
  write(list) {
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* private mode: keep in memory only */ }
  },
};

// ---------- Form setup ----------

function fillSelect(select, pairs, placeholder) {
  select.innerHTML = `<option value="">${esc(placeholder)}</option>` +
    pairs.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
}

function buildClaims() {
  $("claims").innerHTML = CLAIMS.map(({ name, label, hint }) => {
    const options = [["true", "Yes"], ["false", "No"], ["", "Not stated"]];
    const radios = options.map(([value, text], i) => {
      const id = `${name}-${i}`;
      return `<input type="radio" id="${id}" name="${name}" value="${value}"${value === "" ? " checked" : ""}>` +
        `<label for="${id}">${text}</label>`;
    }).join("");
    const hintHtml = hint ? `<p class="hint" id="${name}-hint">${esc(hint)}</p>` : "";
    return `<div class="tri" role="radiogroup" data-field="${name}" aria-labelledby="${name}-label" aria-describedby="${name}-error${hint ? ` ${name}-hint` : ""}">
      <span class="tri__label" id="${name}-label">${esc(label)}</span>
      <div class="segmented">${radios}</div>
      ${hintHtml}
      <p class="field-error" id="${name}-error"></p>
    </div>`;
  }).join("");
}

function setupForm() {
  fillSelect(form.account_type, ACCOUNT_TYPES, "Select account type…");
  fillSelect(form.channel, CHANNELS, "Select channel…");
  $("states").innerHTML = STATES.map((s) => `<option value="${esc(s)}">`).join("");
  buildClaims();
  const picker = $("example-select");
  picker.innerHTML += EXAMPLES.map((ex, i) => `<option value="${i}">${esc(ex.label)}</option>`).join("");
  picker.addEventListener("change", () => {
    const ex = EXAMPLES[picker.value];
    if (!ex) return;
    fillForm(ex.request);
    picker.value = "";
    $("submit-note").textContent = `Loaded: ${ex.label.replace(/^\d+\.\s*/, "")}`;
    announce(`Example loaded: ${ex.label}`);
  });

  form.addEventListener("input", onFormChange);
  form.addEventListener("change", onFormChange);
  form.addEventListener("submit", (ev) => { ev.preventDefault(); submitDispute(); });
  $("reset-btn").addEventListener("click", resetForm);
  $("form-summary").addEventListener("click", onSummaryClick);
  updateDerived();
}

function onFormChange(ev) {
  const field = ev.target.closest("[data-field]");
  if (field && field.classList.contains("is-invalid")) clearFieldError(field.dataset.field);
  if (ev.target.name === "channel") applyChannelRules();
  updateDerived();
}

function updateDerived() {
  updateCounter();
  updateGapHint();
}

function applyChannelRules() {
  const upi = form.channel.value === "UPI";
  for (const radio of form.querySelectorAll('input[name="card_in_possession"]')) {
    radio.disabled = upi && radio.value !== "";
    if (upi && radio.value === "") radio.checked = true;
  }
}

function updateCounter() {
  const n = form.complaint_text.value.length;
  const counter = $("complaint-counter");
  const left = n > 0 && n < COMPLAINT_MIN ? `At least ${COMPLAINT_MIN} characters` : `${COMPLAINT_MIN}–${COMPLAINT_MAX.toLocaleString("en-IN")} characters`;
  counter.innerHTML = `<span>${left}</span><span>${n.toLocaleString("en-IN")} / ${COMPLAINT_MAX.toLocaleString("en-IN")}</span>`;
  counter.classList.toggle("is-over", n > COMPLAINT_MAX);
}

function updateGapHint() {
  const hint = $("gap-hint");
  const txn = form.transaction_datetime.value;
  const alert = form.bank_alert_datetime.value;
  const reported = form.reported_datetime.value;
  const from = alert || txn;
  if (!from || !reported || inputMs(reported) < inputMs(from)) {
    hint.classList.remove("is-shown");
    hint.textContent = "";
    return;
  }
  const days = calendarDays(from, reported);
  const what = alert ? "the bank alert" : "the transaction";
  const when = days === 0 ? `on the same day as ${what}` : `${days} calendar day${days === 1 ? "" : "s"} after ${what}`;
  hint.innerHTML = `${icon("info")}<span><strong>Reported ${when}.</strong>` +
    `<span class="tag">Indicative</span><br>The service computes the delay in working days at the home branch, which may differ.</span>`;
  hint.classList.add("is-shown");
}

function fillForm(req) {
  clearAllErrors();
  hideSummary();
  for (const name of ["customer_id", "account_type", "home_branch_state", "transaction_id", "channel", "complaint_text"]) {
    form[name].value = req[name] ?? "";
  }
  form.transaction_amount_inr.value = req.transaction_amount_inr ?? "";
  for (const name of ["transaction_datetime", "bank_alert_datetime", "reported_datetime"]) {
    form[name].value = isoToInput(req[name]);
  }
  const claims = req.customer_claims || {};
  for (const { name } of CLAIMS) {
    const value = claims[name] === true ? "true" : claims[name] === false ? "false" : "";
    form.querySelector(`input[name="${name}"][value="${value}"]`).checked = true;
  }
  applyChannelRules();
  updateDerived();
}

function resetForm() {
  form.reset();
  clearAllErrors();
  hideSummary();
  applyChannelRules();
  updateDerived();
  $("submit-note").textContent = "";
  form.customer_id.focus();
}

function readClaim(name) {
  const checked = form.querySelector(`input[name="${name}"]:checked`);
  return !checked || checked.value === "" ? null : checked.value === "true";
}

function buildPayload() {
  const amount = form.transaction_amount_inr.value;
  return {
    customer_id: form.customer_id.value.trim(),
    account_type: form.account_type.value,
    channel: form.channel.value,
    transaction_id: form.transaction_id.value.trim(),
    transaction_amount_inr: amount === "" ? null : Number(amount),
    transaction_datetime: inputToIso(form.transaction_datetime.value),
    bank_alert_datetime: inputToIso(form.bank_alert_datetime.value),
    reported_datetime: inputToIso(form.reported_datetime.value),
    home_branch_state: form.home_branch_state.value.trim(),
    complaint_text: form.complaint_text.value.trim(),
    customer_claims: Object.fromEntries(CLAIMS.map(({ name }) => [name, readClaim(name)])),
  };
}

// Quick checks that mirror the contract. The server stays the source of truth.
function clientChecks(p) {
  const errors = {};
  const idCheck = (name, min, max) => {
    const v = p[name];
    if (!v) errors[name] = "Required.";
    else if (v.length < min || v.length > max) errors[name] = `Must be ${min}–${max} characters.`;
    else if (!ID_PATTERN.test(v)) errors[name] = "Use letters, digits, hyphens or underscores, starting with a letter or digit.";
  };
  idCheck("customer_id", 3, 32);
  idCheck("transaction_id", 4, 64);
  if (!p.account_type) errors.account_type = "Select an account type.";
  if (!p.channel) errors.channel = "Select a channel.";
  if (!p.home_branch_state) errors.home_branch_state = "Required.";
  else if (p.home_branch_state.length < 2 || p.home_branch_state.length > 50 || !STATE_PATTERN.test(p.home_branch_state)) {
    errors.home_branch_state = "Enter a state or union territory name (letters and spaces).";
  }

  const amount = p.transaction_amount_inr;
  if (amount === null) errors.transaction_amount_inr = "Enter the disputed amount.";
  else if (!Number.isFinite(amount) || amount <= 0) errors.transaction_amount_inr = "Must be more than ₹0.";
  else if (amount > MAX_AMOUNT_INR) errors.transaction_amount_inr = "Must not exceed ₹1,00,00,000.";

  const txn = Date.parse(p.transaction_datetime);
  const alert = Date.parse(p.bank_alert_datetime);
  const reported = Date.parse(p.reported_datetime);
  const latest = Date.now() + FUTURE_SKEW_MS;
  if (!p.transaction_datetime) errors.transaction_datetime = "Required.";
  if (!p.reported_datetime) errors.reported_datetime = "Required.";
  for (const [name, ms] of [["transaction_datetime", txn], ["bank_alert_datetime", alert], ["reported_datetime", reported]]) {
    if (ms > latest) errors[name] = "Cannot be in the future.";
  }
  if (reported < txn && !errors.reported_datetime) errors.reported_datetime = "Cannot be before the transaction time.";
  if (alert < txn && !errors.bank_alert_datetime) errors.bank_alert_datetime = "Cannot be before the transaction time.";

  if (p.complaint_text.length < COMPLAINT_MIN) errors.complaint_text = `Describe the complaint in at least ${COMPLAINT_MIN} characters.`;
  else if (p.complaint_text.length > COMPLAINT_MAX) errors.complaint_text = `Must not exceed ${COMPLAINT_MAX.toLocaleString("en-IN")} characters.`;

  if (p.channel === "UPI" && p.customer_claims.card_in_possession !== null) {
    errors.card_in_possession = "Must be “Not stated” for UPI transactions.";
  }
  return errors;
}

// ---------- Field errors and the summary ----------

function setFieldError(name, message) {
  const wrap = form.querySelector(`[data-field="${name}"]`);
  if (!wrap) return false;
  wrap.classList.add("is-invalid");
  const msg = $(`${name}-error`);
  msg.textContent = msg.textContent ? `${msg.textContent} ${message}` : message;
  wrap.querySelectorAll("input, select, textarea").forEach((c) => c.setAttribute("aria-invalid", "true"));
  return true;
}

function clearFieldError(name) {
  const wrap = form.querySelector(`[data-field="${name}"]`);
  if (!wrap) return;
  wrap.classList.remove("is-invalid");
  $(`${name}-error`).textContent = "";
  wrap.querySelectorAll("[aria-invalid]").forEach((c) => c.removeAttribute("aria-invalid"));
}

function clearAllErrors() {
  form.querySelectorAll("[data-field].is-invalid").forEach((w) => clearFieldError(w.dataset.field));
}

function hideSummary() {
  const box = $("form-summary");
  box.hidden = true;
  box.className = "summary";
  box.innerHTML = "";
}

function showSummary(tone, html) {
  const box = $("form-summary");
  box.className = `summary tone-${tone}`;
  box.innerHTML = html;
  box.hidden = false;
  box.focus();
}

function fieldLink(name) {
  const label = FIELD_LABELS[name] || name;
  return `<a href="#${esc(name)}" data-focus="${esc(name)}">${esc(label)}</a>`;
}

function focusField(name) {
  const target = form.querySelector(`[data-field="${name}"] input:not([type=radio]), [data-field="${name}"] select, [data-field="${name}"] textarea`) ||
    form.querySelector(`[data-field="${name}"] input:checked`);
  target?.focus();
  target?.scrollIntoView({ block: "center", behavior: "smooth" });
}

function onSummaryClick(ev) {
  const link = ev.target.closest("[data-focus]");
  if (link) {
    ev.preventDefault();
    focusField(link.dataset.focus);
    return;
  }
  const action = ev.target.closest("[data-action]")?.dataset.action;
  if (action === "send-anyway") submitDispute({ skipChecks: true });
  if (action === "show-rejected") selectTab("tab-rejected");
}

// Which form field a server error belongs to. Field errors carry it in "loc"; the contract's
// cross-field rules have an empty loc and name the field in the message instead.
function errorField(err) {
  const loc = (err.loc || []).filter((part) => part !== "body");
  for (const part of [...loc].reverse()) if (FIELD_LABELS[part]) return part;
  const msg = err.msg || "";
  let best = null;
  let at = Infinity;
  for (const name of Object.keys(FIELD_LABELS)) {
    const i = msg.indexOf(name);
    if (i >= 0 && i < at) { at = i; best = name; }
  }
  return best;
}

function humanMessage(msg) {
  let text = String(msg || "Invalid value").replace(/^Value error,\s*/, "");
  const names = Object.keys(FIELD_LABELS).sort((a, b) => b.length - a.length);
  for (const name of names) text = text.replaceAll(`customer_claims.${name}`, name).replaceAll(name, FIELD_LABELS[name].toLowerCase());
  text = text.replace(/ >= /g, " on or after ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function serverErrorItems(errors) {
  return (errors || []).map((err) => {
    const field = errorField(err);
    const loc = (err.loc || []).join(".");
    return { field, message: humanMessage(err.msg), where: field ? null : loc || "request" };
  });
}

// ---------- Submitting ----------

let submitting = false;

function setSubmitting(on) {
  submitting = on;
  const btn = $("submit-btn");
  btn.disabled = on;
  btn.innerHTML = on ? '<span class="spinner" aria-hidden="true"></span>Submitting…' : "Submit dispute";
}

async function submitDispute({ skipChecks = false } = {}) {
  if (submitting) return;
  clearAllErrors();
  hideSummary();
  $("submit-note").textContent = "";
  const payload = buildPayload();

  if (!skipChecks) {
    const errors = clientChecks(payload);
    const names = Object.keys(errors);
    if (names.length) {
      for (const name of names) setFieldError(name, errors[name]);
      showSummary("danger", `<h3>${icon("alert")}Check ${names.length === 1 ? "1 field" : `${names.length} fields`} before submitting</h3>
        <ul>${names.map((n) => `<li>${fieldLink(n)}: ${esc(errors[n])}</li>`).join("")}</ul>
        <p>These are quick checks in your browser. The server validates every request and quarantines any it rejects.</p>
        <button type="button" class="btn btn--secondary btn--small" data-action="send-anyway">Send to the server anyway</button>`);
      return;
    }
  }

  setSubmitting(true);
  let res;
  let body = {};
  try {
    res = await fetch("/disputes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    body = await res.json().catch(() => ({}));
  } catch (err) {
    showSummary("danger", `<h3>${icon("alert")}Could not reach the service</h3><p>${esc(err.message)}. Nothing was submitted; check the connection and try again.</p>`);
    return;
  } finally {
    setSubmitting(false);
  }

  if (res.status === 422) return showRejected(body);
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    showSummary("danger", `<h3>${icon("alert")}The service did not accept the request</h3><p>${esc(detail)} (HTTP ${res.status}). Nothing was started; try again shortly.</p>`);
    return;
  }

  rememberRun({
    run_id: body.run_id,
    submitted_at: Date.now(),
    customer_id: payload.customer_id,
    transaction_id: payload.transaction_id,
    amount: payload.transaction_amount_inr,
    status: body.status || "queued",
  });
  $("submit-note").textContent = "";
  openRun(body.run_id, { submittedAt: Date.now() });
}

let lastQuarantineId = null;

function showRejected(body) {
  lastQuarantineId = body.quarantine_id ?? null;
  const items = serverErrorItems(body.detail);
  for (const item of items) if (item.field) setFieldError(item.field, item.message);
  const list = items.map((item) => item.field
    ? `<li>${fieldLink(item.field)}: ${esc(item.message)}</li>`
    : `<li><code>${esc(item.where)}</code>: ${esc(item.message)}</li>`).join("");
  const qid = lastQuarantineId !== null ? `<strong>#${esc(lastQuarantineId)}</strong>` : "an entry";
  showSummary("danger", `<h3>${icon("alert")}Request rejected and quarantined</h3>
    <p>The server rejected this request against the data contract, so no run was started. It was stored in quarantine as ${qid}.</p>
    <ul>${list}</ul>
    <button type="button" class="btn btn--secondary btn--small" data-action="show-rejected">View in Rejected requests</button>`);
  rejectedLoaded = false;
  // A rejected request has no run: never leave an earlier run's result on screen beside it.
  closeRun();
  $("result").innerHTML = `<div class="empty"><strong>No run started</strong>The last request was rejected and quarantined as #${esc(lastQuarantineId ?? "–")}. Correct the fields shown in the form and submit again.</div>`;
  announce(`Request rejected with ${items.length} error${items.length === 1 ? "" : "s"}; quarantine ${lastQuarantineId ?? ""}`);
}

// ---------- Run polling and the result panel ----------

let current = null; // { runId, token, startedAt, view, ... }
let pollToken = 0;
let ticker = null;

function openRun(runId, { submittedAt = null } = {}) {
  closeRun();
  current = { runId, token: pollToken, startedAt: submittedAt, pollStartedAt: Date.now(), view: null, renderedStatus: null };
  selectTab("tab-result");
  renderRecent();
  renderResult();
  ticker = setInterval(tick, 1000);
  pollRun(current);
  // Single-column layout: the result sits below the form, so bring it into view.
  const panel = document.querySelector(".panel--side");
  if (panel.getBoundingClientRect().top > window.innerHeight * 0.6) panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeRun() {
  pollToken += 1;
  clearInterval(ticker);
  ticker = null;
  current = null;
  renderRecent();
}

async function pollRun(run) {
  for (;;) {
    if (run.token !== pollToken) return;
    try {
      const res = await fetch(`/runs/${encodeURIComponent(run.runId)}`);
      if (run.token !== pollToken) return;
      if (res.status === 404) {
        run.notFound = true;
        forgetRun(run.runId);
        renderResult();
        return stopTicker();
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      run.view = await res.json();
      run.pollError = null;
      if (run.startedAt === null) run.startedAt = parseServerTime(run.view.created_at);
      updateRememberedRun(run.view);
      if (run.view.status !== run.renderedStatus) {
        renderResult();
        announce(statusAnnouncement(run.view));
      }
      if (TERMINAL.has(run.view.status)) return stopTicker();
    } catch (err) {
      if (run.token !== pollToken) return;
      run.pollError = err.message;
      renderWaitNote();
    }
    if (Date.now() - run.pollStartedAt > GIVE_UP_AFTER_MS) {
      run.gaveUp = true;
      renderWaitNote();
      return stopTicker();
    }
    await sleep(POLL_MS);
  }
}

function stopTicker() {
  clearInterval(ticker);
  ticker = null;
  tick();
}

function statusAnnouncement(view) {
  const r = view.result;
  if (view.status === "completed" && r) return `Run completed. Recommended route: ${ROUTES[r.route] || r.route}.`;
  if (view.status === "escalated") return "Run escalated. Needs analyst review.";
  if (view.status === "failed") return "Run failed. No recommendation was produced.";
  return `Run ${STATUS_LABELS[view.status] || view.status}.`;
}

function elapsedMs(run) {
  const v = run.view;
  if (v && TERMINAL.has(v.status)) {
    return parseServerTime(v.updated_at) - parseServerTime(v.created_at);
  }
  return run.startedAt ? Date.now() - run.startedAt : 0;
}

function tick() {
  if (!current) return;
  const el = $("elapsed");
  if (el) {
    const done = current.view && TERMINAL.has(current.view.status);
    el.textContent = `${done ? "Took" : "Elapsed"} ${duration(elapsedMs(current))}`;
  }
  renderWaitNote();
}

function renderTracker(status) {
  const order = ["queued", "running", "final"];
  const at = TERMINAL.has(status) ? 2 : Math.max(0, order.indexOf(status));
  const steps = [
    { label: "Queued", sub: "Waiting for a worker" },
    { label: "Running", sub: "Agents reviewing" },
    TERMINAL.has(status)
      ? { label: STATUS_LABELS[status], sub: status === "failed" ? "No recommendation" : status === "escalated" ? "Analyst review" : "Recommendation ready" }
      : { label: "Outcome", sub: "Completed, escalated or failed" },
  ];
  const finalTerminal = TERMINAL.has(status);
  return `<ol class="tracker" aria-label="Run progress">${steps.map((step, i) => {
    let cls = "";
    let mark = String(i + 1);
    let state = "not started";
    if (i < at || (i === 2 && finalTerminal)) { cls = "is-done"; mark = icon("check"); state = "done"; }
    else if (i === at) { cls = "is-current"; mark = '<span class="spinner" aria-hidden="true"></span>'; state = "in progress"; }
    if (i === 2 && finalTerminal && status !== "completed") {
      cls += ` tone-final-${status}`;
      mark = icon(status === "failed" ? "cross" : "alert");
    }
    return `<li class="tracker__step ${cls}"${i === at && !finalTerminal ? ' aria-current="step"' : ""}>
      <span class="tracker__mark">${mark}</span>
      <span class="tracker__label">${esc(step.label)}<span class="visually-hidden"> (${state})</span><small>${esc(step.sub)}</small></span>
    </li>`;
  }).join("")}</ol>`;
}

function renderWaitNote() {
  const box = $("wait-note");
  if (!box || !current) return;
  const run = current;
  const done = run.view && TERMINAL.has(run.view.status);
  let html = "";
  let tone = "info";
  if (run.gaveUp) {
    tone = "warn";
    html = `<p>No final status after ${duration(GIVE_UP_AFTER_MS)}. The run may still finish.</p>
      <button type="button" class="btn btn--secondary btn--small" data-action="check-again">Check again</button>`;
  } else if (!done && run.pollError) {
    tone = "warn";
    html = `<p>Lost contact with the service (${esc(run.pollError)}). Retrying…</p>`;
  } else if (!done && elapsedMs(run) > SLOW_AFTER_MS) {
    html = "<p>Still working. Runs that call a real model can take up to about 90 seconds. This page keeps checking; you can leave it open.</p>";
  }
  box.className = html ? `callout wait-note tone-${tone}` : "wait-note";
  if (box.dataset.html !== html) {
    box.innerHTML = html;
    box.dataset.html = html;
  }
}

function renderResult() {
  const root = $("result");
  const run = current;
  if (!run) {
    root.innerHTML = `<div class="empty"><strong>No dispute submitted yet</strong>Fill in the form, or choose a case from “Load example”, then submit. The recommendation appears here.</div>`;
    return;
  }
  if (run.notFound) {
    root.innerHTML = `<div class="callout tone-neutral"><h3>${icon("info")}Run not found</h3><p>The service has no run <code>${esc(run.runId)}</code>. It may have been removed when the database was reset.</p></div>`;
    return;
  }
  const v = run.view;
  const status = v?.status ?? "queued";
  run.renderedStatus = v?.status ?? null;
  const submitted = v ? parseServerTime(v.created_at) : run.startedAt;

  let body = "";
  if (v && TERMINAL.has(status)) {
    body = status === "failed" ? renderFailed(v) : renderOutcome(v);
    body += renderDetails(v);
    if (status !== "failed") body += renderFeedback(v);
  }

  root.innerHTML = `
    <div class="run-head">
      <h2 class="run-head__title">Run <span class="mono">${esc(run.runId.slice(0, 8))}</span></h2>
      <span class="run-head__meta">Submitted ${esc(Number.isFinite(submitted) ? fmtTime.format(submitted) : "–")} IST · <span id="elapsed"></span></span>
    </div>
    ${renderTracker(status)}
    <div id="wait-note" class="wait-note"></div>
    ${body}`;
  tick();
}

// Honest labels for results no Verifier has checked: the placeholder ("fake-0") and the
// Investigator's unverified draft ("v1+unverified").
function renderFakeBanner(r) {
  const version = r?.prompt_version || "";
  if (version.startsWith("fake")) {
    return `<div class="callout tone-neutral fake-banner" role="note">${icon("info")}
      <p><strong>Placeholder agent:</strong> this result is not real reasoning; citations are stand-ins.</p></div>`;
  }
  if (version.includes("+unverified")) {
    return `<div class="callout tone-neutral fake-banner" role="note">${icon("info")}
      <p><strong>Investigator draft:</strong> not yet checked by the Verifier.</p></div>`;
  }
  return "";
}

function renderOutcome(v) {
  const r = v.result || {};
  const route = r.route || (v.status === "escalated" ? "escalate" : null);
  const escalation = v.status === "escalated"
    ? `<div class="callout tone-warn" role="note"><h3>${icon("alert")}Needs analyst review</h3>
        <p>${esc(r.escalation_reason || "The service could not settle this case; a disputes analyst should review it.")}</p></div>`
    : "";
  const badge = route
    ? `<div class="route route--${esc(route)}">
        <span class="route__icon">${icon(ROUTE_ICONS[route] || "info")}</span>
        <span><span class="route__label">Recommended route</span><br><span class="route__value">${esc(ROUTES[route] || route)}</span></span>
      </div>`
    : "";
  const facts = [
    ["Fraud type", FRAUD_TYPES[r.fraud_type] || r.fraud_type || "–"],
    ["Liability position", r.liability_position || "–"],
    ["Basis", r.basis || "–"],
  ];
  const citations = (r.citations || []).map((c) => `
    <li class="citation">
      <div class="citation__head">
        <span class="citation__doc">${esc(DOC_NAMES[c.doc_id] || c.doc_id)}</span>
        <span class="citation__clause" title="${esc(c.doc_id)}:${esc(c.clause_id)}">Clause ${esc(c.clause_id)}</span>
      </div>
      <blockquote>${esc(c.text)}</blockquote>
    </li>`).join("");
  return `${renderFakeBanner(r)}${escalation}${badge}
    <dl class="facts">${facts.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val)}</dd>`).join("")}</dl>
    <h3 class="section-title">Cited provisions (${(r.citations || []).length})</h3>
    ${citations ? `<ol class="citations">${citations}</ol>` : '<p class="hint">No provisions cited.</p>'}
    ${r.error ? `<div class="callout tone-neutral"><p>${esc(r.error)}</p></div>` : ""}`;
}

function renderFailed(v) {
  const error = v.error || v.result?.error || "No error message was recorded.";
  return `${renderFakeBanner(v.result)}<div class="callout tone-danger" role="alert">
      <h3>${icon("cross")}Run failed: no recommendation</h3>
      <p>The service could not produce a recommendation for this dispute. Handle it manually or resubmit it.</p>
      <p><code>${esc(error)}</code></p>
    </div>`;
}

function renderDetails(v) {
  const r = v.result;
  const trace = r?.trace || [];
  const rows = trace.map((step) => {
    const { step: name, latency_ms: latency, ...rest } = step;
    const extra = Object.entries(rest).map(([k, val]) => `${k}: ${typeof val === "object" ? JSON.stringify(val) : val}`).join("; ");
    return `<tr><td>${esc(name ?? "–")}</td><td class="num">${latency != null ? `${esc(Math.round(latency).toLocaleString("en-IN"))} ms` : "–"}</td><td>${esc(extra)}</td></tr>`;
  }).join("");
  return `<details class="details">
    <summary>Run details</summary>
    <div class="details__body">
      <dl class="kv">
        <dt>Run ID</dt><dd><code id="run-id-full">${esc(v.run_id)}</code>
          <button type="button" class="btn btn--quiet btn--small" data-action="copy-run-id" aria-describedby="run-id-full">Copy</button></dd>
        <dt>Status</dt><dd>${esc(STATUS_LABELS[v.status] || v.status)}</dd>
        <dt>Prompt version</dt><dd><code>${esc(r?.prompt_version ?? "–")}</code></dd>
        <dt>Created</dt><dd>${esc(showTime(parseServerTime(v.created_at)))}</dd>
        <dt>Last updated</dt><dd>${esc(showTime(parseServerTime(v.updated_at)))}</dd>
      </dl>
      ${rows
        ? `<div class="table-wrap"><table><caption class="visually-hidden">Trace steps</caption>
            <thead><tr><th scope="col">Step</th><th scope="col" class="num">Latency</th><th scope="col">Details</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`
        : '<p class="hint">No trace steps recorded.</p>'}
    </div>
  </details>`;
}

function renderFeedback(v) {
  const sent = recentMemory.find((e) => e.run_id === v.run_id)?.feedback_id ?? current.feedbackId;
  if (sent != null) {
    return `<div class="feedback"><p class="confirm">${icon("check")}Feedback recorded (#${esc(sent)}). Thank you.</p></div>`;
  }
  return `<form class="feedback" id="feedback-form" novalidate>
    <fieldset>
      <legend>Was this recommendation useful?</legend>
      <div class="segmented">
        <input type="radio" id="fb-yes" name="fb-useful" value="true"><label for="fb-yes">Yes</label>
        <input type="radio" id="fb-no" name="fb-useful" value="false"><label for="fb-no">No</label>
      </div>
    </fieldset>
    <div class="field">
      <label for="fb-reason">Reason <span class="optional">optional</span></label>
      <textarea id="fb-reason" maxlength="2000" rows="2"></textarea>
    </div>
    <div class="actions">
      <button type="submit" class="btn btn--secondary">Send feedback</button>
      <span class="actions__note" id="fb-status" aria-live="polite"></span>
    </div>
  </form>`;
}

async function sendFeedback(feedbackForm) {
  if (!current) return;
  const choice = feedbackForm.querySelector('input[name="fb-useful"]:checked');
  const status = $("fb-status");
  if (!choice) {
    status.textContent = "Choose Yes or No first.";
    feedbackForm.querySelector("#fb-yes").focus();
    return;
  }
  const button = feedbackForm.querySelector("button[type=submit]");
  button.disabled = true;
  status.textContent = "Sending…";
  const runId = current.runId;
  try {
    const res = await fetch(`/runs/${encodeURIComponent(runId)}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        satisfactory: choice.value === "true",
        reason: $("fb-reason").value.trim() || FEEDBACK_DEFAULT_REASON,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    current.feedbackId = body.feedback_id;
    updateRememberedRun({ run_id: runId, feedback_id: body.feedback_id });
    feedbackForm.outerHTML = `<div class="feedback"><p class="confirm" tabindex="-1" id="fb-confirm">${icon("check")}Feedback recorded (#${esc(body.feedback_id)}). Thank you.</p></div>`;
    $("fb-confirm").focus();
  } catch (err) {
    button.disabled = false;
    status.textContent = `Could not send feedback (${err.message}). Try again.`;
  }
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy"; }, 1500);
}

function setupResult() {
  const root = $("result");
  root.addEventListener("click", (ev) => {
    const action = ev.target.closest("[data-action]")?.dataset.action;
    if (action === "copy-run-id" && current?.view) copyText(current.view.run_id, ev.target.closest("button"));
    if (action === "check-again" && current) openRun(current.runId, { submittedAt: null });
  });
  root.addEventListener("submit", (ev) => {
    if (ev.target.id !== "feedback-form") return;
    ev.preventDefault();
    sendFeedback(ev.target);
  });
  renderResult();
}

// ---------- Recent runs (this browser) ----------

let recentMemory = storage.read();

function rememberRun(entry) {
  recentMemory = [entry, ...recentMemory.filter((e) => e.run_id !== entry.run_id)].slice(0, RECENT_MAX);
  storage.write(recentMemory);
  renderRecent();
}

function updateRememberedRun(update) {
  const entry = recentMemory.find((e) => e.run_id === update.run_id);
  if (!entry) return;
  if (update.status) entry.status = update.status;
  if (update.result?.route) entry.route = update.result.route;
  if (update.feedback_id != null) entry.feedback_id = update.feedback_id;
  storage.write(recentMemory);
  renderRecent();
}

function forgetRun(runId) {
  recentMemory = recentMemory.filter((e) => e.run_id !== runId);
  storage.write(recentMemory);
  renderRecent();
}

function runPill(entry) {
  if (entry.status === "completed" && entry.route) {
    return `<span class="pill pill--${esc(entry.route)}">${esc(ROUTES[entry.route] || entry.route)}</span>`;
  }
  const label = entry.status === "escalated" ? "Needs review" : STATUS_LABELS[entry.status] || entry.status;
  return `<span class="pill pill--${esc(entry.status)}">${esc(label)}</span>`;
}

function renderRecent() {
  const list = $("recent-list");
  $("recent-count").textContent = recentMemory.length ? String(recentMemory.length) : "";
  $("recent-clear").hidden = !recentMemory.length;
  if (!recentMemory.length) {
    list.innerHTML = '<li class="empty"><strong>No runs yet</strong>Runs you submit from this browser are listed here.</li>';
    return;
  }
  list.innerHTML = recentMemory.map((e) => {
    const amount = Number.isFinite(e.amount) ? fmtINR.format(e.amount) : "";
    const isCurrent = current?.runId === e.run_id;
    return `<li><button type="button" class="run-item" data-run="${esc(e.run_id)}"${isCurrent ? ' aria-current="true"' : ""}>
      <span class="run-item__title">${esc(e.customer_id || "–")} · ${esc(e.transaction_id || "–")}</span>
      ${runPill(e)}
      <span class="run-item__meta">${esc(amount)}${amount ? " · " : ""}${esc(fmtDateTime.format(e.submitted_at))} IST · run ${esc(e.run_id.slice(0, 8))}</span>
    </button></li>`;
  }).join("");
}

function setupRecent() {
  $("recent-list").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-run]");
    if (btn) openRun(btn.dataset.run);
  });
  $("recent-clear").addEventListener("click", () => {
    recentMemory = [];
    storage.write(recentMemory);
    renderRecent();
  });
  renderRecent();
}

// ---------- Rejected requests (GET /quarantine) ----------

let rejectedLoaded = false;

async function loadRejected() {
  const root = $("rejected-list");
  root.innerHTML = '<p class="hint"><span class="spinner" aria-hidden="true" style="display:inline-block;vertical-align:-2px"></span> Loading…</p>';
  try {
    const res = await fetch("/quarantine?limit=20");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const rows = await res.json();
    rejectedLoaded = true;
    if (!rows.length) {
      root.innerHTML = '<div class="empty"><strong>No rejected requests</strong>Requests that fail validation appear here.</div>';
      return;
    }
    root.innerHTML = rows.map(renderRejectedRow).join("");
  } catch (err) {
    root.innerHTML = `<div class="callout tone-danger"><p>Could not load rejected requests (${esc(err.message)}).</p></div>`;
  }
}

function renderRejectedRow(row) {
  let raw = row.raw_payload;
  let who = "";
  try {
    const parsed = JSON.parse(raw);
    raw = JSON.stringify(parsed, null, 2);
    who = [parsed.customer_id, parsed.transaction_id].filter((x) => typeof x === "string").join(" · ");
  } catch { /* not JSON: show as received */ }
  const errors = serverErrorItems(row.errors).map((item) =>
    `<li>${item.field ? `<strong>${esc(FIELD_LABELS[item.field])}</strong>` : `<code>${esc(item.where)}</code>`}: ${esc(item.message)}</li>`).join("");
  const count = (row.errors || []).length;
  const highlight = row.id === lastQuarantineId ? " is-highlight" : "";
  return `<article class="reject${highlight}">
    <div class="reject__head">
      <span class="reject__id">Quarantine #${esc(row.id)}${highlight ? ' <span class="pill pill--rejected">Latest from this form</span>' : ""}</span>
      <span class="reject__meta">${esc(showTime(parseServerTime(row.received_at)))}</span>
    </div>
    <p class="reject__meta">${esc(who || "Unparsed payload")} · ${count} error${count === 1 ? "" : "s"} · ${esc(row.endpoint)}</p>
    <ul>${errors}</ul>
    <details><summary>Raw payload</summary><pre>${esc(raw)}</pre></details>
  </article>`;
}

function setupRejected() {
  $("rejected-refresh").addEventListener("click", loadRejected);
}

// ---------- Tabs ----------

const TAB_IDS = ["tab-result", "tab-recent", "tab-rejected"];

function selectTab(id, { focus = false } = {}) {
  for (const tabId of TAB_IDS) {
    const tab = $(tabId);
    const selected = tabId === id;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !selected;
  }
  if (focus) $(id).focus();
  if (id === "tab-rejected" && !rejectedLoaded) loadRejected();
}

function setupTabs() {
  for (const id of TAB_IDS) $(id).addEventListener("click", () => selectTab(id));
  document.querySelector(".tabs").addEventListener("keydown", (ev) => {
    const i = TAB_IDS.indexOf(document.activeElement.id);
    if (i < 0) return;
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TAB_IDS.length - 1 }[ev.key];
    if (next === undefined) return;
    ev.preventDefault();
    selectTab(TAB_IDS[(next + TAB_IDS.length) % TAB_IDS.length], { focus: true });
  });
}

// ---------- Health (GET /readyz) ----------

async function checkHealth() {
  const box = $("health");
  const text = box.querySelector(".health__text");
  let state = "down";
  let label = "Not reachable";
  let title = "The API did not respond.";
  try {
    const res = await fetch("/readyz", { cache: "no-store" });
    const body = await res.json().catch(() => ({}));
    const checks = Object.entries(body.checks || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
    state = res.ok ? "ready" : "down";
    label = res.ok ? "Ready" : "Not ready";
    title = checks || `HTTP ${res.status}`;
  } catch { /* keep "Not reachable" */ }
  if (box.dataset.state !== state || text.textContent !== label) {
    box.dataset.state = state;
    text.textContent = label;
  }
  box.title = title;
  box.setAttribute("aria-label", `Service status: ${label}. ${title}`);
}

// ---------- Start ----------

setupForm();
setupTabs();
setupResult();
setupRecent();
setupRejected();
checkHealth();
setInterval(checkHealth, HEALTH_MS);
