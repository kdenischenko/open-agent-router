async function refresh(path, id) {
  try {
    const response = await fetch(path, {credentials: 'same-origin'});
    if (response.status !== 200) return;
    const html = await response.text();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const fresh = parsed.getElementById(id);
    const current = document.getElementById(id);
    // a swap must not pull focus or a disclosure state from under the visitor: skip the
    // refresh while focus is inside or a confirmation dialog is open, since closing the dialog
    // returns focus to the link that opened it; reopen the disclosures that were open
    if (fresh && current && !current.contains(document.activeElement)
        && !document.querySelector('dialog[open]')) {
      const open = Array.from(current.querySelectorAll('details')).map((item) => item.open);
      current.innerHTML = fresh.innerHTML;
      current.querySelectorAll('details').forEach((item, index) => {
        if (open[index]) item.open = true;
      });
      // the wrapper's own attributes change too, e.g. the inbox loses its heading reference
      // when it empties
      Array.from(current.attributes).forEach((attr) => {
        if (!fresh.hasAttribute(attr.name)) current.removeAttribute(attr.name);
      });
      Array.from(fresh.attributes).forEach((attr) => current.setAttribute(attr.name, attr.value));
      if (id === 'limits') {
        setupLimitCards(current);
      }
    }
  } catch (_error) {
    return;
  }
}

setInterval(() => refresh('/fragments/limits', 'limits'), 30000);
setInterval(() => refresh('/fragments/advice', 'advice'), 30000);
// the job list keeps its filters and page from the query string
setInterval(() => refresh('/fragments/jobs' + location.search, 'jobs'), 5000);
setInterval(() => refresh('/fragments/approvals', 'approvals'), 5000);

// limits cards enhance the server markup progressively: pace markers appear and
// relative reset countdowns update only with scripts on
function setupLimitCards(root) {
  if (!root) return;
  root.querySelectorAll('.usage-pace[data-expected]').forEach((marker) => {
    const value = Number.parseFloat(marker.dataset.expected);
    if (!Number.isFinite(value)) return;
    const clamped = Math.min(100, Math.max(0, value));
    marker.style.setProperty('--expected', `${clamped}%`);
    marker.hidden = false;
  });
  updateLimitCountdowns(root);
}

function formatCountdown(ms) {
  const seconds = Math.floor(ms / 1000);
  if (seconds <= 0) return 'now';
  if (seconds < 60) return `in ${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    return rest ? `in ${hours} h ${rest} min` : `in ${hours} h`;
  }
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  return rest ? `in ${days} d ${rest} h` : `in ${days} d`;
}

function updateLimitCountdowns(root) {
  if (!root) return;
  root.querySelectorAll('time[data-countdown][datetime]').forEach((time) => {
    const target = Date.parse(time.getAttribute('datetime'));
    const relative = time.querySelector('[data-countdown-relative]');
    if (!Number.isFinite(target) || !relative) return;
    relative.textContent = formatCountdown(target - Date.now());
  });
}

function setupLimitCountdowns() {
  const limits = document.getElementById('limits');
  if (!limits) return;
  setupLimitCards(limits);
  const tick = () => {
    updateLimitCountdowns(limits);
    setTimeout(tick, 30000);
  };
  setTimeout(tick, 30000);
}

setupLimitCountdowns();

// a job page polls its own fragment and stops once the job reaches a terminal status;
// the next request starts only after the previous one is done, so answers never arrive
// out of order
function setupJobRefresh() {
  const job = document.getElementById('job');
  if (!job || job.dataset.terminal === 'true') return;
  const poll = async () => {
    await refresh('/fragments/jobs/' + job.dataset.jobId, 'job');
    if (job.dataset.terminal !== 'true') setTimeout(poll, 5000);
  };
  setTimeout(poll, 5000);
}

setupJobRefresh();

// with scripts on, the calibration form asks for the provider first and lists only its windows;
// the window stays unset until the visitor picks one, and a rolling window takes no reset time
function setupCalibration() {
  const provider = document.getElementById('provider');
  const target = document.getElementById('target');
  const field = document.getElementById('provider-field');
  if (!provider || !target || !field) return;
  const groups = Array.from(target.querySelectorAll('optgroup')).map((group) => group.cloneNode(true));
  const placeholder = target.querySelector('option[value=""]');
  const resets = ['resets_date', 'resets_time', 'resets_in']
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  const hint = document.getElementById('rolling-hint');
  const syncReset = () => {
    const chosen = target.selectedOptions[0];
    const rolling = Boolean(chosen && chosen.dataset.kind === 'rolling');
    resets.forEach((input) => { input.disabled = rolling; });
    if (hint) {
      hint.hidden = !rolling;
      hint.textContent = 'rolling window: no reset time';
    }
  };
  const show = () => {
    const group = groups.find((item) => item.dataset.pool === provider.value);
    const options = group ? Array.from(group.children) : [];
    target.replaceChildren(placeholder, ...options.map((option) => option.cloneNode(true)));
    target.value = '';
    syncReset();
  };
  target.addEventListener('change', syncReset);
  provider.addEventListener('change', show);
  field.hidden = false;
  show();
}

setupCalibration();

// approval deadlines count down between refreshes; the server text stays without scripts
function approvalCountdown(ms) {
  const seconds = Math.floor(ms / 1000);
  if (seconds <= 0) return 'expired';
  if (seconds < 60) return `in ${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  return `in ${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function setupApprovalCountdowns() {
  setInterval(() => {
    document.querySelectorAll('time[data-approval-countdown][datetime]').forEach((time) => {
      const target = Date.parse(time.getAttribute('datetime'));
      if (Number.isFinite(target)) time.textContent = approvalCountdown(target - Date.now());
    });
  }, 1000);
}

setupApprovalCountdowns();

// Deny and Cancel are links to a confirmation page; with scripts on, a dialog asks instead
// and posts the same form, so the page reload is skipped but the routes and CSRF stay
function setupConfirmDialog() {
  const csrf = document.querySelector('input[name="csrf"]');
  if (!csrf || typeof HTMLDialogElement === 'undefined') return;
  const dialog = document.createElement('dialog');
  dialog.className = 'confirm-dialog';
  dialog.setAttribute('aria-labelledby', 'confirm-dialog-title');
  const title = document.createElement('h2');
  title.id = 'confirm-dialog-title';
  const form = document.createElement('form');
  form.method = 'post';
  form.className = 'inline';
  const token = document.createElement('input');
  token.type = 'hidden';
  token.name = 'csrf';
  token.value = csrf.value;
  const confirm = document.createElement('button');
  confirm.type = 'submit';
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'button-secondary';
  back.textContent = 'Keep';
  back.addEventListener('click', () => dialog.close());
  form.append(token, confirm, back);
  dialog.append(title, form);
  document.body.append(dialog);
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[data-confirm-action]');
    if (!link) return;
    event.preventDefault();
    const action = link.dataset.confirmAction;
    const job = link.getAttribute('href').split('/')[2];
    title.textContent = action === 'deny' ? `Deny the route for ${job}?` : `Cancel ${job}?`;
    confirm.textContent = action === 'deny' ? 'Deny' : 'Cancel the job';
    form.action = link.getAttribute('href');
    dialog.showModal();
    back.focus();  // the safe choice takes focus, so Enter does not deny by accident
  });
}

setupConfirmDialog();
