// Minimal client: submit a dispute, poll the run, render route, liability, citations, escalation.
const POLL_MS = 1000;
const TERMINAL = new Set(["completed", "escalated", "failed"]);

const form = document.getElementById("dispute-form");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");

function localInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// Default datetimes: transaction two days ago, alert one minute later, reported yesterday.
(function setDefaults() {
  const t = new Date(Date.now() - 2 * 86400e3);
  form.transaction_datetime.value = localInputValue(t);
  form.bank_alert_datetime.value = localInputValue(new Date(t.getTime() + 60e3));
  form.reported_datetime.value = localInputValue(new Date(Date.now() - 86400e3));
})();

// datetime-local has no zone; send it with the browser's UTC offset so the contract accepts it.
function withOffset(value) {
  if (!value) return null;
  const off = -new Date(value).getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const abs = Math.abs(off);
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  return `${value}:00${sign}${hh}:${mm}`;
}

function tri(value) {
  return value === "" ? null : value === "true";
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function buildPayload() {
  const f = form;
  return {
    customer_id: f.customer_id.value,
    account_type: f.account_type.value,
    channel: f.channel.value,
    transaction_id: f.transaction_id.value,
    transaction_amount_inr: Number(f.transaction_amount_inr.value),
    transaction_datetime: withOffset(f.transaction_datetime.value),
    bank_alert_datetime: withOffset(f.bank_alert_datetime.value),
    reported_datetime: withOffset(f.reported_datetime.value),
    home_branch_state: f.home_branch_state.value,
    complaint_text: f.complaint_text.value,
    customer_claims: {
      shared_otp_or_pin: tri(f.shared_otp_or_pin.value),
      card_in_possession: tri(f.card_in_possession.value),
      received_alert: tri(f.received_alert.value),
    },
  };
}

function renderErrors(body) {
  const items = (body.detail || []).map(
    (e) => `<li><code>${escapeHtml((e.loc || []).join("."))}</code>: ${escapeHtml(e.msg)}</li>`
  );
  statusEl.innerHTML = `<p class="error">Rejected (HTTP 422), quarantine id ${escapeHtml(body.quarantine_id)}. No run was started.</p>`;
  resultEl.innerHTML = `<ul>${items.join("")}</ul>`;
}

function renderRun(view) {
  statusEl.innerHTML = `Run <code>${escapeHtml(view.run_id)}</code>: <strong>${escapeHtml(view.status)}</strong>`;
  const r = view.result;
  if (!r) {
    resultEl.innerHTML = view.error ? `<p class="error">Error: ${escapeHtml(view.error)}</p>` : "";
    return;
  }
  const citations = r.citations
    .map((c) => `<li><strong>${escapeHtml(c.doc_id)}</strong> ${escapeHtml(c.clause_id)}<blockquote>${escapeHtml(c.text)}</blockquote></li>`)
    .join("");
  const escalation = r.escalation_reason
    ? `<p class="escalated">Escalated to a disputes analyst: ${escapeHtml(r.escalation_reason)}</p>`
    : "";
  resultEl.innerHTML = `
    ${escalation}
    <p><strong>Route:</strong> ${escapeHtml(r.route)}</p>
    <p><strong>Fraud type:</strong> ${escapeHtml(r.fraud_type)}</p>
    <p><strong>Liability position:</strong> ${escapeHtml(r.liability_position)}</p>
    <p><strong>Basis:</strong> ${escapeHtml(r.basis)}</p>
    <p><strong>Citations:</strong></p><ol>${citations}</ol>
    <p><small>prompt_version: ${escapeHtml(r.prompt_version)}</small></p>
    ${r.error ? `<p class="error">${escapeHtml(r.error)}</p>` : ""}
    <details><summary>Raw result</summary><pre>${escapeHtml(JSON.stringify(view, null, 2))}</pre></details>`;
}

async function poll(statusUrl) {
  for (;;) {
    const res = await fetch(statusUrl);
    const view = await res.json();
    renderRun(view);
    if (TERMINAL.has(view.status)) return;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  resultEl.innerHTML = "";
  statusEl.textContent = "Submitting...";
  try {
    const res = await fetch("/disputes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    const body = await res.json();
    if (res.status === 422) return renderErrors(body);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${JSON.stringify(body)}`);
    await poll(body.status_url);
  } catch (err) {
    statusEl.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
  }
});
