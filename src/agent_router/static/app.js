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
setInterval(() => refresh('/fragments/jobs', 'jobs'), 5000);

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
