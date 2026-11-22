async function refresh(path, id) {
  try {
    const response = await fetch(path, {credentials: 'same-origin'});
    if (response.status !== 200) return;
    const html = await response.text();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const fresh = parsed.getElementById(id);
    const current = document.getElementById(id);
    // a swap must not pull focus or a disclosure state from under the visitor: skip the
    // refresh while focus is inside, and reopen the disclosures that were open
    if (fresh && current && !current.contains(document.activeElement)) {
      const open = Array.from(current.querySelectorAll('details')).map((item) => item.open);
      current.innerHTML = fresh.innerHTML;
      current.querySelectorAll('details').forEach((item, index) => {
        if (open[index]) item.open = true;
      });
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

setInterval(() => refresh('/fragments/limits' + location.search, 'limits'), 30000);
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
  const percent = document.getElementById('percent');
  const resetDate = document.getElementById('resets_date');
  const resetTime = document.getElementById('resets_time');
  const resetsIn = document.getElementById('resets_in');
  const preview = document.getElementById('calibration-preview');
  if (!provider || !target || !field || !percent || !resetDate || !resetTime || !resetsIn) return;
  const groups = Array.from(target.querySelectorAll('optgroup')).map((group) => group.cloneNode(true));
  const placeholder = target.querySelector('option[value=""]');
  const initialTarget = target.value;
  const initialGroup = target.selectedOptions[0]?.closest('optgroup');
  if (initialGroup) provider.value = initialGroup.dataset.pool;
  const resets = [resetDate, resetTime, resetsIn];
  const hint = document.getElementById('rolling-hint');
  // a rolling window disables the reset fields, and a disabled field is not submitted
  const value = (input) => (input.disabled ? '' : input.value);
  // the dashboard time zone offset, so dates mean what the server reads, not the browser zone
  const offsetMs = Number(target.form?.dataset.utcOffsetMinutes || 0) * 60 * 1000;
  // the grammar of periods.parse_span: \d there matches any Unicode decimal digit
  const SPAN = /^(?:(\p{Nd}+)d)?(?:(\p{Nd}+)h)?(?:(\p{Nd}+)m)?$/u;

  // null for a malformed span, NaN when only the server can convert its digits
  const spanMilliseconds = (text) => {
    const match = text ? SPAN.exec(text) : null;
    if (!match) return null;
    const [days, hours, minutes] = match.slice(1).map((part) => Number(part || 0));
    return ((days * 24 + hours) * 60 + minutes) * 60 * 1000;
  };

  const resetMoment = () => {
    if (value(resetsIn)) {
      const span = spanMilliseconds(value(resetsIn));
      return Number.isFinite(span) && span > 0 ? new Date(Date.now() + span) : null;
    }
    const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value(resetDate));
    if (!day) return null;
    const [hour, minute] = (value(resetTime) || '00:00').split(':').map(Number);
    const wall = Date.UTC(Number(day[1]), Number(day[2]) - 1, Number(day[3]), hour, minute);
    return Number.isFinite(wall) ? new Date(wall - offsetMs) : null;
  };

  // the start of a fixed window that resets at `reset`: calendar months in UTC with the day
  // clamped to the month end, as periods.shift counts them, then days and hours
  const windowStart = (reset, option) => {
    const months = Number(option.dataset.months || 0);
    const total = reset.getUTCFullYear() * 12 + reset.getUTCMonth() - months;
    const year = Math.floor(total / 12);
    const month = total - year * 12;
    const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    const start = Date.UTC(
      year, month, Math.min(reset.getUTCDate(), lastDay),
      reset.getUTCHours(), reset.getUTCMinutes(), reset.getUTCSeconds(),
    );
    const hours = Number(option.dataset.days || 0) * 24 + Number(option.dataset.hours || 0);
    return new Date(start - hours * 60 * 60 * 1000);
  };

  const updatePreview = () => {
    if (!preview) return;
    const lines = [];
    if (percent.value.trim()) {
      const used = Number(percent.value);
      if (Number.isFinite(used)) lines.push(`used ${used}%`);
    }
    const reset = resetMoment();
    const chosen = target.selectedOptions[0];
    if (reset && chosen?.dataset.kind === 'fixed') {
      const start = windowStart(reset, chosen);
      const length = reset.getTime() - start.getTime();
      if (length > 0) {
        const expected = Math.min(100, Math.max(0, (Date.now() - start.getTime()) / length * 100));
        lines.push(`expected ${expected.toFixed(1)}%`);
      }
    }
    if (reset) {
      const wall = new Date(reset.getTime() + offsetMs);
      const pad = (part) => String(part).padStart(2, '0');
      const date = `${pad(wall.getUTCDate())}.${pad(wall.getUTCMonth() + 1)}`;
      const time = `${pad(wall.getUTCHours())}:${pad(wall.getUTCMinutes())}`;
      lines.push(`resets ${date} ${time}`);
    }
    preview.textContent = lines.join('\n');
    preview.hidden = lines.length === 0;
  };

  // a field shows its message once the visitor has changed it, or every field on submit
  const touched = new Set(['target', 'percent', 'resets_date', 'resets_in']
    .filter((name) => document.getElementById(`${name}-error`)?.hidden === false));
  const showErrors = (errors, all) => {
    const controls = {target, percent, resets_date: resetDate, resets_in: resetsIn};
    Object.entries(controls).forEach(([name, control]) => {
      const message = all || touched.has(name) ? errors[name] || '' : '';
      const error = document.getElementById(`${name}-error`);
      if (error) {
        error.textContent = message;
        error.hidden = !message;
      }
      control.setAttribute('aria-describedby', `${name}-hint${message ? ` ${name}-error` : ''}`);
      if (message) control.setAttribute('aria-invalid', 'true');
      else control.removeAttribute('aria-invalid');
    });
  };

  // only checks that do not depend on the clock or time zone block a submit;
  // the server decides whether a reset is in the past or too far ahead
  const validate = (all = false) => {
    const errors = {};
    if (!target.value) errors.target = 'choose a pool and window';
    const used = Number(percent.value);
    if (!percent.value.trim() || !Number.isFinite(used) || used < 0 || used > 100) {
      errors.percent = 'enter a number from 0 to 100';
    }
    const date = value(resetDate);
    const span = value(resetsIn);
    if (value(resetTime) && !date) errors.resets_date = 'set the reset date';
    if (date && span) errors.resets_in = 'set either the reset date or reset in, not both';
    if (span) {
      const length = spanMilliseconds(span);
      if (length === null) {
        errors.resets_in = `invalid span: '${span}' (expected 3h20m, 45m, or 2d)`;
      } else if (length <= 0) {
        errors.resets_in = `invalid span: '${span}' (must be positive)`;
      }
    }
    showErrors(errors, all);
    updatePreview();
    return errors;
  };

  const syncReset = () => {
    const chosen = target.selectedOptions[0];
    const rolling = Boolean(chosen && chosen.dataset.kind === 'rolling');
    resets.forEach((input) => { input.disabled = rolling; });
    if (hint) {
      hint.hidden = !rolling;
      hint.textContent = 'rolling window: no reset time';
    }
  };
  const show = (selected = '') => {
    const group = groups.find((item) => item.dataset.pool === provider.value);
    const options = group ? Array.from(group.children) : [];
    target.replaceChildren(placeholder, ...options.map((option) => option.cloneNode(true)));
    target.value = selected;
    syncReset();
  };
  target.addEventListener('change', syncReset);
  [target, percent, resetDate, resetTime, resetsIn].forEach((control) => {
    const name = control === resetTime ? 'resets_date' : control.id;
    const check = () => {
      touched.add(name);
      validate();
    };
    control.addEventListener('input', check);
    control.addEventListener('change', check);
  });
  provider.addEventListener('change', () => {
    show();
    validate();
  });
  if (target.form) target.form.noValidate = true;
  target.form?.addEventListener('submit', (event) => {
    const errors = validate(true);
    const first = ['target', 'percent', 'resets_date', 'resets_in']
      .find((name) => errors[name]);
    if (!first) return;
    event.preventDefault();
    document.getElementById(first)?.focus();
  });
  field.hidden = false;
  show(initialTarget);
  updatePreview();
}

setupCalibration();
