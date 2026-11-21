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
