async function refresh(path, id) {
  try {
    const response = await fetch(path, {credentials: 'same-origin'});
    if (response.status !== 200) return;
    const html = await response.text();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const fresh = parsed.getElementById(id);
    const current = document.getElementById(id);
    if (fresh && current) {
      current.innerHTML = fresh.innerHTML;
      if (fresh.hasAttribute('data-terminal')) {
        current.dataset.terminal = fresh.dataset.terminal;
      }
    }
  } catch (_error) {
    return;
  }
}

setInterval(() => refresh('/fragments/limits', 'limits'), 30000);
setInterval(() => refresh('/fragments/advice', 'advice'), 30000);
setInterval(() => refresh('/fragments/jobs', 'jobs'), 5000);
setInterval(() => refresh('/fragments/approvals', 'approvals'), 5000);

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
