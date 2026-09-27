"use strict";

const $ = id => document.getElementById(id);
const state = {
  session: null, team: [], overviews: new Map(), me: null, calendar: null,
  view: "overview", bucket: "upcoming", month: null, today: "", quote: null,
  requestOpen: false, selectedStart: "", selectedEnd: "", calendarDrag: null, suppressCalendarClick: false,
  requestEmployeeFilter: "all", projection: null, projectionEmployee: "",
  holidayEntries: [], saving: false, initialized: false,
  portalLoadGeneration: 0, calendarLoadGeneration: 0, projectionLoadGeneration: 0,
  commandRefreshFailed: false,
};
const persona = $("persona");
const pendingOperations = readOperationKeys();

function readOperationKeys() {
  try { return new Map(JSON.parse(sessionStorage.getItem("timeoff-operation-keys") || "[]")); }
  catch (_) { return new Map(); }
}
function saveOperationKeys() {
  try { sessionStorage.setItem("timeoff-operation-keys", JSON.stringify([...pendingOperations])); }
  catch (_) { /* The server still validates each action if storage is unavailable. */ }
}
function api(path, options) {
  const settings = options || {};
  const headers = new Headers(settings.headers || {});
  headers.set("Authorization", "Bearer " + persona.value);
  if (settings.body) headers.set("Content-Type", "application/json");
  return fetch(path, {...settings, headers}).then(async response => {
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw {network:true, status:response.status}; }
    if (!response.ok) throw {network:false, status:response.status, payload};
    return payload;
  }).catch(error => {
    if (error && error.network === false) throw error;
    throw {network:true, message:"The service connection was interrupted."};
  });
}
function makeKey() {
  return globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2);
}
function dayISO(value) { return String(value || "").slice(0, 10); }
function dayDate(value) { return new Date(dayISO(value) + "T12:00:00Z"); }
function isoDay(value) { return value.toISOString().slice(0, 10); }
function shiftDay(value, days) {
  const date = dayDate(value);
  date.setUTCDate(date.getUTCDate() + days);
  return isoDay(date);
}
function monthFirst(value) { return dayISO(value).slice(0, 7) + "-01"; }
function monthLast(value) {
  const date = dayDate(monthFirst(value));
  date.setUTCMonth(date.getUTCMonth() + 1, 0);
  return isoDay(date);
}
function parseNumber(value) { const n = Number(value); return Number.isFinite(n) ? n : 0; }
function hours(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  const value = parseNumber(minutes) / 60;
  return (value < 0 ? "−" : "") + Math.abs(value).toLocaleString(undefined, {maximumFractionDigits:2}) + " h";
}
function daysText(minutes, dayMinutes) {
  const oneDay = parseNumber(dayMinutes);
  if (!oneDay) return "";
  const amount = parseNumber(minutes) / oneDay;
  return formatDays(amount);
}
function formatDays(value) {
  const amount = parseNumber(value);
  const label = amount.toLocaleString(undefined, {maximumFractionDigits:2});
  return label + (amount !== 0 && Math.abs(amount) <= 1 ? " day" : " days");
}
function dateText(value, options) {
  if (!value) return "—";
  const parsed = dayDate(value);
  if (Number.isNaN(parsed.valueOf())) return String(value);
  return parsed.toLocaleDateString("en-US", {...(options || {month:"short", day:"numeric", year:"numeric"}), timeZone:"UTC"});
}
function dateRange(request) {
  const start = dayISO(request.start);
  const end = requestLastDate(request);
  if (start === end) return dateText(start);
  return dateText(start, {month:"short", day:"numeric"}) + " – " + dateText(end, {month:"short", day:"numeric", year:"numeric"});
}
function requestLastDate(request) {
  const start = dayISO(request.start);
  let end = dayISO(request.end || request.start);
  if (request.end && String(request.end).slice(11, 19) === "00:00:00" && end > start) end = shiftDay(end, -1);
  return end;
}
function requestPortion(request) {
  if (request.half === "morning") return "Morning half";
  if (request.half === "afternoon") return "Afternoon half";
  return "";
}
function requestDateText(request) {
  const portion = requestPortion(request);
  return dateRange(request) + (portion ? " · " + portion : "");
}
function isApprovedCurrentOrFuture(request) {
  return request.status === "approved" && requestLastDate(request) >= state.today;
}
function isApprovedInProgress(request) {
  return isApprovedCurrentOrFuture(request) && dayISO(request.start) < state.today;
}
function timeStampText(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString("en-US", {dateStyle:"medium", timeStyle:"short", timeZone:"UTC"}) + " UTC";
}
function initials(name) {
  return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0].toUpperCase()).join("");
}
function isManager() { return Boolean(state.session && state.session.actor.role === "manager"); }
function actorEmployeeId() { return (state.session && state.session.actor.employee_id) || ""; }
function canDecideRequest(employeeId) {
  return isManager() && employeeId !== actorEmployeeId() &&
    state.team.some(member => member.employee_id === employeeId && member.manager_id === actorEmployeeId());
}
function displayName(employeeId) {
  const member = state.team.find(item => item.employee_id === employeeId);
  return member ? member.name : employeeId;
}
function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function setNotice(message, kind) {
  const box = $("status");
  box.textContent = message || "";
  box.className = "notice" + (kind ? " " + kind : "");
  box.hidden = !message;
}
function describeError(error) {
  if (error && error.network) return "The service could not confirm the result. Retry the action to check the original attempt.";
  const code = error && error.payload && error.payload.code;
  const messages = {
    forbidden:"Your account cannot make that change.",
    borrowing_limit_exceeded:"This request exceeds the borrowing limit.",
    employee_leave_overlap:"These dates overlap another active request.",
    overlapping_request:"These dates overlap another active request.",
    stale_quote:"The leave rules changed. Review the request again before submitting.",
    idempotency_conflict:"That retry key belongs to a different action. Refresh and try again.",
    pending_deadline_passed:"This request can no longer be approved because its start time has passed.",
    retroactive_schedule_change:"Choose an effective date after today.",
    retroactive_calendar_change:"Choose an effective date after today.",
    invalid_date_range:"Check that the end date is on or after the start date.",
    request_has_no_chargeable_time:"These dates do not include scheduled working time.",
    invalid_half_day:"Choose a morning or afternoon half-day on one date.",
    timezone_migration_unsupported:"Changing the schedule timezone is not supported in this demo.",
    configuration_conflict:"A setting already exists for that effective date. Choose a later date.",
    employee_not_found:"The employee record could not be found.",
    projection_date_out_of_range:"Choose a date within the next year.",
  };
  return messages[code] || "We could not save that change. Check the values and request status, then try again.";
}
function setSaving(value) {
  state.saving = value;
  persona.disabled = value;
  document.querySelectorAll("[data-mutation]").forEach(button => { button.disabled = value; });
  document.querySelectorAll("#request-panel input, #request-panel select, #manager-view input, #manager-view select, #manager-view textarea")
    .forEach(input => { input.disabled = value; });
  $("submit-button").disabled = value || !state.quote;
  $("calculate-button").disabled = value || !requestFormReady();
  $("save-schedule").disabled = value;
  $("add-holiday").disabled = value;
  $("add-us-holidays").disabled = value;
  document.querySelectorAll("#manager-view .holiday-remove").forEach(button => { button.disabled = value; });
  const policy = state.me && latestPolicyVersion();
  $("borrow-days").disabled = value || Boolean(policy && policy.mode === "unlimited");
  $("save-policy").disabled = value || Boolean(policy && policy.mode === "unlimited");
  $("save-holidays").disabled = value;
}
function queryOverview(employeeId) {
  return api("/api/overview?" + new URLSearchParams({employee_id:employeeId, category:"vacation"}).toString());
}
function todayPlus(days) { return shiftDay(state.today, days); }
function nextWorkday() {
  let result = todayPlus(1);
  while ([0, 6].includes(dayDate(result).getUTCDay())) result = shiftDay(result, 1);
  return result;
}
function latestEffective(items) {
  return (items || []).map(item => dayISO(item.effective_from)).filter(Boolean).sort().slice(-1)[0] || state.today;
}
function nextEffective(items) {
  const latest = latestEffective(items);
  return shiftDay(latest > state.today ? latest : state.today, 1);
}
function latestPolicyVersion() {
  const versions = state.me && state.me.configuration && state.me.configuration.policy_versions || [];
  return versions.slice().sort((a, b) => dayISO(a.effective_from).localeCompare(dayISO(b.effective_from))).slice(-1)[0] || (state.me && state.me.policy);
}
function uniqueVersion(prefix) { return prefix + "-" + Date.now().toString(36); }

async function loadPortal() {
  const generation = ++state.portalLoadGeneration;
  const identity = persona.value;
  const isCurrent = () => generation === state.portalLoadGeneration && identity === persona.value;
  state.calendarLoadGeneration += 1;
  state.projectionLoadGeneration += 1;
  state.initialized = false;
  state.requestOpen = false;
  state.requestEmployeeFilter = "all";
  $("request-panel").hidden = true;
  $("quote-card").hidden = true;
  state.quote = null;
  state.overviews.clear();
  state.calendar = null;
  state.projection = null;
  state.calendarFocusDate = "";
  setNotice("Loading your time-off account…");
  try {
    const session = await api("/api/session");
    if (!isCurrent()) return;
    state.session = session;
    state.today = dayISO(state.session.clock);
    state.month = dayDate(state.today);
    state.month.setUTCDate(1);
    const team = await api("/api/team");
    if (!isCurrent()) return;
    state.team = team.members || [];
    const ids = isManager() ? state.team.map(item => item.employee_id) : [actorEmployeeId()];
    const results = await Promise.all(ids.map(async id => [id, await queryOverview(id)]));
    if (!isCurrent()) return;
    for (const pair of results) state.overviews.set(pair[0], pair[1]);
    state.me = state.overviews.get(actorEmployeeId()) || await queryOverview(actorEmployeeId());
    if (!isCurrent()) return;
    state.overviews.set(actorEmployeeId(), state.me);
    state.projectionEmployee = actorEmployeeId();
    state.initialized = true;
    configureIdentity();
    if (isManager()) configureManagerForm();
    initializeRequestDates();
    initializeProjectionDate();
    renderEverything();
    await Promise.all([loadCalendar(), loadProjection()]);
    if (isCurrent()) setNotice("");
  } catch (error) {
    if (isCurrent()) setNotice(describeError(error), "error");
  }
}
async function refreshPortal() {
  const generation = state.portalLoadGeneration;
  const identity = persona.value;
  const isCurrent = () => generation === state.portalLoadGeneration && identity === persona.value;
  const manager = isManager();
  const employeeId = actorEmployeeId();
  const ids = manager ? state.team.map(item => item.employee_id) : [employeeId];
  const results = await Promise.all(ids.map(async id => [id, await queryOverview(id)]));
  if (!isCurrent()) return;
  state.overviews = new Map(results);
  state.me = state.overviews.get(employeeId);
  renderEverything();
  await Promise.all([loadCalendar(), loadProjection()]);
}
function configureIdentity() {
  const employee = state.me.employee || {};
  const manager = isManager();
  $("company-name").textContent = "Demo";
  $("person-name").textContent = employee.name || actorEmployeeId();
  $("person-role").textContent = manager ? "Manager" : "Employee";
  $("person-avatar").textContent = initials(employee.name || actorEmployeeId());
  $("manager-nav").hidden = !manager;
  $("history-panel").hidden = !manager;
  $("request-open").textContent = manager ? "Book time off" : "Request time off";
  $("request-heading").textContent = manager ? "Book time off" : "Request time off";
  $("request-help").textContent = manager
    ? "Full days follow your work schedule. Your own booking is approved immediately; your balance and borrowing limit still apply."
    : "Full days follow your work schedule. Weekends and company holidays are not charged.";
  $("team-overview").hidden = !manager;
  $("projection-employee-field").hidden = !manager;
  $("employee-filter-wrap").hidden = !manager;
  $("request-scope").textContent = manager
    ? "All company requests are visible. You can decide pending requests for your direct reports."
    : "Your requests and decisions.";
  $("request-employee-filter").replaceChildren();
  addOption($("request-employee-filter"), "all", "All employees");
  if (manager) state.team.forEach(member => addOption($("request-employee-filter"), member.employee_id, member.name));
  $("projection-employee").replaceChildren();
  if (manager) state.team.forEach(member => addOption($("projection-employee"), member.employee_id, member.name));
  $("projection-employee").value = state.projectionEmployee;
}
function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.appendChild(option);
}
function initializeRequestDates() {
  const day = nextWorkday();
  $("start-date").value = day;
  $("end-date").value = day;
  $("start-date").min = state.today;
  $("end-date").min = state.today;
  $("leave-unit").value = "full_day";
  $("half-period").value = "morning";
  renderRequestMode();
}
function initializeProjectionDate() {
  $("projection-date").value = state.today;
  $("projection-date").min = state.today;
  $("projection-date").max = todayPlus(366);
}
function activeSchedule(overview) {
  return overview && overview.configuration && overview.configuration.active_schedule || {};
}
function finiteBalance(balance) { return Boolean(balance && balance.mode !== "unlimited" && balance.available !== null); }
function renderEverything() {
  if (!state.me) return;
  renderHeader();
  renderBalance();
  renderUpcoming();
  renderTeamSnapshot();
  renderPeopleDirectory();
  renderRequests();
  renderHistory();
  if (isManager()) renderManagerTeam();
  setSaving(state.saving);
}
function renderHeader() {
  const page = {
    overview:["TIME OFF", "My time off", "Your balance and upcoming leave."],
    people:["PEOPLE", "People & reporting", isManager() ? "Company directory and reporting structure." : "Your teammates and reporting structure."],
    calendar:["TEAM TIME OFF", "Time-off calendar", isManager() ? "See who's away across the company." : "See who's away on your team and choose dates to request time off."],
    requests:["REQUESTS", "Time off requests", isManager() ? "Company-wide visibility; your decisions are limited to direct reports." : "Track your upcoming and past time away."],
    manager:["MANAGER WORKSPACE", "Team & settings", "Manage schedules, borrowing limits, holidays, and team requests."],
  }[state.view] || ["TIME OFF", "My time off", "Your balance and upcoming leave."];
  $("eyebrow").textContent = page[0];
  $("page-title").textContent = page[1];
  $("page-subtitle").textContent = page[2];
  $("request-open").hidden = state.view === "manager";
  const calendarAction = state.view === "calendar";
  $("request-open").textContent = calendarAction
    ? "Manual dates"
    : (isManager() ? "Book time off" : "Request time off");
  $("request-open").classList.toggle("secondary", calendarAction);
  $("upcoming-description").textContent = isManager() ? "Approved time away across the company." : "Your approved time away.";
  document.querySelectorAll(".nav-item[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === state.view));
  const count = allRequests().filter(item => item.request.status === "pending" && canDecideRequest(item.employee.employee_id)).length;
  $("pending-nav-count").textContent = count > 99 ? "99+" : String(count);
  $("pending-nav-count").hidden = !isManager() || count === 0;
}
function renderBalance() {
  const balance = state.me.balance;
  const employee = state.me.employee || {};
  const schedule = activeSchedule(state.me);
  const policy = state.me.policy;
  const dayMinutes = schedule.day_minutes || state.me.day_minutes;
  $("balance-asof").textContent = "As of " + dateText(state.me.as_of || state.session.clock);
  $("workday-value").textContent = parseNumber(dayMinutes) ? hours(dayMinutes) : "Not set";
  $("accrual-value").removeAttribute("title");
  if (!policy) $("accrual-value").textContent = "No leave plan";
  else if (policy.mode === "unlimited") $("accrual-value").textContent = "Unlimited";
  else if (policy.mode === "worked") $("accrual-value").textContent = "Based on work";
  else {
    const policyMinutes = policy.unit === "days"
      ? parseNumber(policy.amount) * parseNumber(dayMinutes)
      : parseNumber(policy.amount) * 60;
    const perMonth = policyMinutes / (policy.period === "year" ? 12 : 1);
    $("accrual-value").textContent = formatDays(perMonth / dayMinutes) + " / month";
    $("accrual-value").title = "Monthly installment based on " + policy.amount + " " + policy.unit + " per " + policy.period + ".";
  }
  $("accrual-label").textContent = policy && policy.mode === "worked" ? "Earned by" : "Accrual";
  const value = $("balance-value");
  const note = $("balance-note");
  value.classList.remove("negative"); note.classList.remove("negative");
  if (!balance) {
    value.textContent = "—"; $("balance-caption").textContent = "No active balance"; note.textContent = "Ask a manager to review your leave plan.";
    return;
  }
  if (balance.mode === "unlimited") {
    value.textContent = "Unlimited"; $("balance-caption").textContent = "No numerical limit";
    note.textContent = daysText(balance.used_minutes || 0, dayMinutes) + " used · usage is still tracked.";
    return;
  }
  const available = parseNumber(balance.available);
  value.textContent = daysText(balance.available, dayMinutes);
  const negative = available < 0;
  if (negative) {
    value.classList.add("negative"); note.classList.add("negative");
    $("balance-caption").textContent = "Borrowing is in use";
    note.textContent = daysText(balance.consumed_debt || 0, dayMinutes) + " borrowed and used · " + daysText(balance.reserved_borrowing || 0, dayMinutes) +
      " reserved · " + daysText(balance.borrowing_limit || 0, dayMinutes) + " limit" + (balance.over_limit ? " · existing commitment exceeds the current limit" : "");
  } else {
    $("balance-caption").textContent = "available after active requests";
    const details = [];
    if (parseNumber(balance.reserved_credit)) details.push(daysText(balance.reserved_credit, dayMinutes) + " held for requests");
    if (parseNumber(balance.reserved_borrowing)) details.push(daysText(balance.reserved_borrowing, dayMinutes) + " borrowing capacity held");
    if (parseNumber(balance.consumed_debt)) details.push(daysText(balance.consumed_debt, dayMinutes) + " borrowed and used");
    if (parseNumber(balance.used_minutes)) details.push(daysText(balance.used_minutes, dayMinutes) + " taken");
    note.textContent = details.join(" · ") || "Pending and approved time is reserved until cancelled or taken.";
  }
}
function allRequests() {
  const records = [];
  const sources = isManager() ? [...state.overviews.entries()] : [[actorEmployeeId(), state.me]];
  for (const pair of sources) {
    const employeeId = pair[0];
    const overview = pair[1];
    if (!overview) continue;
    const employee = overview.employee || {employee_id:employeeId, name:displayName(employeeId)};
    for (const request of overview.requests || []) records.push({employee, request:{...request, employee_id:employeeId}});
  }
  return records.sort((a, b) => String(a.request.start || "").localeCompare(String(b.request.start || "")));
}
function bucketFor(request) {
  if (request.status === "pending") return "pending";
  if (isApprovedCurrentOrFuture(request)) return "upcoming";
  return "past";
}
function renderUpcoming() {
  const host = $("upcoming-list");
  host.replaceChildren();
  const upcoming = allRequests().filter(item => isApprovedCurrentOrFuture(item.request));
  upcoming.sort((a, b) => String(a.request.start).localeCompare(String(b.request.start)));
  const selected = isManager() ? upcoming.slice(0, 6) : upcoming.filter(item => item.employee.employee_id === actorEmployeeId()).slice(0, 4);
  if (!selected.length) {
    host.appendChild(make("p", "empty-inline", "No approved time off is coming up."));
    return;
  }
  for (const item of selected) {
    const row = make("div", "upcoming-item");
    row.appendChild(make("span", "upcoming-date", requestDateText(item.request)));
    const person = make("div", "upcoming-person");
    person.appendChild(make("span", "mini-avatar", initials(item.employee.name)));
    const label = make("span");
    label.appendChild(make("strong", "", isManager() ? item.employee.name : "Vacation"));
    label.appendChild(make("small", "", isApprovedInProgress(item.request)
      ? "In progress" : (isManager() ? "Vacation" : statusText(item.request.status))));
    person.appendChild(label);
    row.appendChild(person);
    row.appendChild(make("span", "upcoming-duration", formatDays(item.request.days)));
    host.appendChild(row);
  }
}
function statusText(status) {
  return ({pending:"Pending review", approved:"Approved", consumed:"Taken", taken:"Taken", cancelled:"Cancelled", rejected:"Declined", expired:"Expired"})[status] || "Status unavailable";
}
function renderTeamSnapshot() {
  if (!isManager()) return;
  const body = $("team-summary");
  body.replaceChildren();
  for (const member of state.team) {
    const overview = state.overviews.get(member.employee_id);
    const row = make("tr");
    const name = make("td"); name.appendChild(make("strong", "", member.name)); row.appendChild(name);
    const balance = overview && overview.balance;
    const balanceCell = make("td", balance && parseNumber(balance.available) < 0 ? "negative-text" : "",
      !balance ? "—" : balance.mode === "unlimited" ? "Unlimited" : daysText(balance.available, overview.day_minutes));
    row.appendChild(balanceCell);
    const schedule = overview && activeSchedule(overview);
    row.appendChild(make("td", "", schedule && schedule.day_minutes ? hours(schedule.day_minutes) : "—"));
    const requests = overview && overview.requests || [];
    row.appendChild(make("td", "", String(requests.filter(isApprovedCurrentOrFuture).length)));
    row.appendChild(make("td", "", String(requests.filter(item => item.status === "pending").length)));
    body.appendChild(row);
  }
}
function renderManagerTeam() {
  if (!isManager()) return;
  const body = $("manager-team-rows");
  body.replaceChildren();
  for (const member of state.team) {
    const overview = state.overviews.get(member.employee_id);
    const row = make("tr");
    row.appendChild(make("td", "", member.name));
    const roleCell = make("td", "");
    roleCell.appendChild(make("span", "", member.title || "Team member"));
    roleCell.appendChild(make("small", "muted", " · " + (member.job_level || "IC1")));
    row.appendChild(roleCell);
    const manager = state.team.find(item => item.employee_id === member.manager_id);
    row.appendChild(make("td", "muted", manager ? manager.name : (member.manager_id ? displayName(member.manager_id) : "—")));
    const balance = overview && overview.balance;
    row.appendChild(make("td", balance && parseNumber(balance.available) < 0 ? "negative-text" : "",
      !balance ? "—" : balance.mode === "unlimited" ? "Unlimited" : daysText(balance.available, overview.day_minutes)));
    const schedule = overview && activeSchedule(overview);
    row.appendChild(make("td", "", schedule && schedule.day_minutes ? hours(schedule.day_minutes) : "—"));
    row.appendChild(make("td", "", String(overview && overview.requests ? overview.requests.length : 0)));
    body.appendChild(row);
  }
}
function renderPeopleDirectory() {
  const members = state.team.slice();
  const host = $("org-chart");
  host.replaceChildren();
  $("directory-scope").textContent = isManager() ? "Everyone in this demo, grouped by reporting line." : "Your manager and teammates in the same reporting group.";
  $("directory-count").textContent = members.length + (members.length === 1 ? " person" : " people");
  const byId = new Map(members.map(member => [member.employee_id, member]));
  const children = new Map(members.map(member => [member.employee_id, []]));
  for (const member of members) {
    if (member.manager_id && member.manager_id !== member.employee_id && byId.has(member.manager_id)) {
      children.get(member.manager_id).push(member);
    }
  }
  for (const reports of children.values()) reports.sort((a, b) => a.name.localeCompare(b.name));
  let roots = members.filter(member => !member.manager_id || member.manager_id === member.employee_id || !byId.has(member.manager_id));
  if (!roots.length) roots = members.slice(); // Keep a malformed legacy cycle visible instead of hiding the directory.
  roots.sort((a, b) => a.name.localeCompare(b.name));
  const rendered = new Set();
  function personCard(member, manager, depth) {
    const reports = children.get(member.employee_id) || [];
    const card = make("article", "org-person-card" + (depth === 0 ? " org-leader" : ""));
    card.appendChild(make("span", "org-avatar", initials(member.name)));
    const identity = make("div", "org-person-identity");
    identity.appendChild(make("strong", "org-person-name", member.name));
    identity.appendChild(make("span", "org-person-title", member.title || "Team member"));
    const meta = make("div", "org-person-meta");
    meta.appendChild(make("span", "org-department", member.department || "Unassigned"));
    meta.appendChild(make("span", "job-level", member.job_level || "IC1"));
    identity.appendChild(meta);
    identity.appendChild(make("small", "org-reporting-line", manager ? "Reports to " + manager.name : (reports.length ? "Reporting group lead" : "No manager listed")));
    card.append(identity, make("span", "org-report-count", reports.length ? reports.length + (reports.length === 1 ? " report" : " reports") : ""));
    return card;
  }
  function appendBranch(parent, member, manager, depth, path) {
    if (path.has(member.employee_id) || rendered.has(member.employee_id)) return;
    rendered.add(member.employee_id);
    const branch = make("div", "org-branch");
    branch.appendChild(personCard(member, manager, depth));
    const reports = children.get(member.employee_id) || [];
    if (reports.length) {
      const list = make("div", "org-children");
      const nextPath = new Set(path); nextPath.add(member.employee_id);
      for (const report of reports) appendBranch(list, report, member, depth + 1, nextPath);
      if (list.childElementCount) branch.appendChild(list);
    }
    parent.appendChild(branch);
  }
  for (const root of roots) appendBranch(host, root, null, 0, new Set());
  for (const member of members) if (!rendered.has(member.employee_id)) appendBranch(host, member, null, 0, new Set());
  if (!members.length) host.appendChild(make("p", "empty-inline", "No team members are available for this account."));
}
function renderRequests() {
  const items = allRequests();
  const filtered = items.filter(item => state.requestEmployeeFilter === "all" || item.employee.employee_id === state.requestEmployeeFilter);
  const counts = {upcoming:0, pending:0, past:0};
  for (const item of filtered) counts[bucketFor(item.request)] += 1;
  $("count-upcoming").textContent = String(counts.upcoming);
  $("count-pending").textContent = String(counts.pending);
  $("count-past").textContent = String(counts.past);
  const pendingCount = filtered.filter(item => item.request.status === "pending" && canDecideRequest(item.employee.employee_id)).length;
  $("pending-nav-count").textContent = pendingCount > 99 ? "99+" : String(pendingCount);
  $("pending-nav-count").hidden = !isManager() || pendingCount === 0;
  const rows = $("request-rows"); rows.replaceChildren();
  const shown = filtered.filter(item => bucketFor(item.request) === state.bucket)
    .sort((a, b) => String(a.request.start || "").localeCompare(String(b.request.start || "")));
  for (const item of shown) {
    const {employee, request} = item;
    const row = make("tr");
    row.appendChild(make("td", "", isManager() ? employee.name : "Vacation"));
    const dateCell = make("td", "request-date-cell");
    dateCell.appendChild(make("span", "", requestDateText(request)));
    if (request.reason) dateCell.appendChild(make("small", "request-reason", request.reason));
    row.appendChild(dateCell);
    const duration = make("td", "");
    duration.appendChild(make("strong", "", formatDays(request.days)));
    row.appendChild(duration);
    const statusCell = make("td", "");
    statusCell.appendChild(make("span", "status-pill " + request.status,
      isApprovedInProgress(request) ? "In progress" : statusText(request.status)));
    row.appendChild(statusCell);
    const actionCell = make("td", "");
    const actions = make("div", "request-row-actions");
    if (canDecideRequest(employee.employee_id) && request.status === "pending") {
      actions.appendChild(actionButton("Approve", "button tiny approve", () => runRequestAction("request.approve", employee.employee_id, request)));
      actions.appendChild(actionButton("Decline", "button tiny decline", () => runRequestAction("request.reject", employee.employee_id, request)));
    } else if (isManager() && request.status === "pending" && employee.employee_id !== actorEmployeeId()) {
      const rosterEntry = state.team.find(member => member.employee_id === employee.employee_id);
      const assignedManager = state.team.find(member => member.employee_id === (rosterEntry || {}).manager_id);
      actions.appendChild(make("small", "muted", assignedManager ? "Assigned to " + assignedManager.name : "Assigned manager"));
    }
    const mayCancel = (employee.employee_id === actorEmployeeId()) &&
      (request.status === "pending" || request.status === "approved") && new Date(request.start) > new Date(state.session.clock);
    if (mayCancel) actions.appendChild(actionButton("Cancel", "button tiny cancel", () => runRequestAction("request.cancel", employee.employee_id, request)));
    actionCell.appendChild(actions); row.appendChild(actionCell); rows.appendChild(row);
  }
  const empty = shown.length === 0;
  $("request-empty").hidden = !empty;
  if (empty) {
    $("empty-title").textContent = state.bucket === "upcoming" ? "No upcoming time off" : state.bucket === "pending" ? "No requests waiting for review" : "No past requests";
    $("empty-copy").textContent = isManager() ? "Requests for your team will appear here." : "Requests will appear here when you book time away.";
  }
}
function actionButton(label, className, callback) {
  const button = make("button", className, label);
  button.type = "button";
  button.dataset.mutation = "true";
  button.addEventListener("click", callback);
  return button;
}
function renderHistory() {
  const all = [];
  const overviews = isManager() ? [...state.overviews.values()] : [state.me];
  const seen = new Set();
  for (const overview of overviews) for (const record of overview && overview.history || []) {
    const key = String(record.id);
    if (seen.has(key)) continue;
    seen.add(key);
    all.push({...record, employee_name:(overview.employee || {}).name || "Employee",
      employee_day_minutes:overview.day_minutes});
  }
  all.sort((a, b) => String(b.recorded_at).localeCompare(String(a.recorded_at)));
  const list = $("history-list"); list.replaceChildren();
  $("history-empty").hidden = all.length > 0;
  for (const record of all.slice(0, 12)) {
    const item = make("li");
    const title = make("span", "history-title", activityName(record.kind) + (isManager() ? " · " + record.employee_name : ""));
    const detail = make("span", "history-detail", activityDetail(record));
    const time = make("time", "history-time", "Recorded " + timeStampText(record.recorded_at));
    item.append(title, detail, time); list.appendChild(item);
  }
}
function activityName(kind) {
  return ({"request.submit":"Time off booked", "request.approve":"Request approved", "request.reject":"Request declined",
    "request.cancel":"Request cancelled", "accrual.run":"Monthly accrual", "policy.publish":"Policy updated",
    "employee.schedule.publish":"Work schedule updated", "company.calendar.publish":"Company holidays updated"})[kind] || "Account updated";
}
function activityDetail(record) {
  const result = record.result || {};
  const payload = record.payload || {};
  if (result.ok === false) return "Not completed: " + (result.code || "request rejected").replaceAll("_", " ");
  if (record.kind === "request.submit") {
    const days = result.days === undefined
      ? parseNumber(result.minutes) / parseNumber(record.employee_day_minutes)
      : result.days;
    return formatDays(days) + " requested";
  }
  if (record.kind === "request.cancel") return "Unused reservation released.";
  if (record.kind === "request.approve") return "Reservation remains in place.";
  if (record.kind === "request.reject") return "Reservation released.";
  if (record.kind === "accrual.run") {
    const months = (result.calculation || []).map(item => dateText(item.period_start, {month:"long", year:"numeric"}));
    return months.length
      ? hours(result.posted_minutes) + " posted for " + months.join(", ") + "."
      : "No completed month was ready to post through " + payload.through_date + ".";
  }
  if (record.kind === "policy.publish") return result.policy && result.policy.mode === "unlimited" ? "Unlimited vacation policy." : "Borrowing and accrual terms changed.";
  if (record.kind === "employee.schedule.publish") return "New hours take effect " + payload.schedule.effective_from + ".";
  if (record.kind === "company.calendar.publish") return "Holiday calendar takes effect " + payload.effective_from + ".";
  return "Change saved.";
}

function setView(view) {
  if (view === "manager" && !isManager()) return;
  if (view !== "calendar") {
    state.selectedStart = ""; state.selectedEnd = "";
  }
  state.view = view;
  document.querySelectorAll(".view").forEach(section => { section.hidden = section.id !== view + "-view"; });
  if (view === "calendar") {
    const history = document.querySelector(".history-panel");
    if (history) history.open = false;
  }
  renderHeader();
  if (view === "calendar") loadCalendar().catch(error => setNotice(describeError(error), "error"));
  if (view === "overview") loadProjection().catch(() => {});
}
async function loadCalendar() {
  if (!state.month) return;
  const generation = ++state.calendarLoadGeneration;
  const portalGeneration = state.portalLoadGeneration;
  const identity = persona.value;
  const first = monthFirst(isoDay(state.month));
  const last = monthLast(first);
  const calendar = await api("/api/calendar?" + new URLSearchParams({start:first, end:last}).toString());
  if (generation !== state.calendarLoadGeneration || portalGeneration !== state.portalLoadGeneration ||
      identity !== persona.value || first !== monthFirst(isoDay(state.month))) return;
  state.calendar = calendar;
  renderCalendar();
}
function eventCovers(event, date) {
  const start = dayISO(event.start);
  const end = requestLastDate(event);
  return start <= date && date <= end;
}
function eventOverlapsMonth(event, month) {
  const first = monthFirst(month);
  const last = monthLast(first);
  return dayISO(event.start) <= last && requestLastDate(event) >= first;
}
function calendarEventDateText(event) {
  const start = dayISO(event.start);
  const end = requestLastDate(event);
  const format = {weekday:"short", month:"short", day:"numeric"};
  if (start.slice(0, 4) !== end.slice(0, 4)) format.year = "numeric";
  const startText = dateText(start, format);
  return start === end ? startText : startText + " – " + dateText(end, format);
}
function renderCalendar() {
  if (!state.calendar || !state.month) return;
  $("calendar-view").classList.toggle("has-date-selection", Boolean(state.selectedStart));
  const month = isoDay(state.month);
  $("month-label").textContent = dayDate(month).toLocaleDateString("en-US", {month:"long", year:"numeric", timeZone:"UTC"});
  const grid = $("calendar-grid"); grid.replaceChildren();
  ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].forEach(day => {
    const heading = make("div", "calendar-weekday", day); heading.setAttribute("aria-hidden", "true"); grid.appendChild(heading);
  });
  const first = dayDate(monthFirst(month));
  const start = new Date(first); start.setUTCDate(first.getUTCDate() - first.getUTCDay());
  const holidaySet = new Set((state.calendar.holidays || []).map(dayISO));
  const preferredFocusDate = state.calendarFocusDate && state.calendarFocusDate.slice(0, 7) === month.slice(0, 7)
    ? state.calendarFocusDate
    : (state.selectedStart && state.selectedStart >= state.today && state.selectedStart.slice(0, 7) === month.slice(0, 7)
      ? state.selectedStart
      : (month >= monthFirst(state.today) ? (month === monthFirst(state.today) ? state.today : month) : ""));
  let firstAvailableDate = "";
  let focusButton = null;
  for (let index = 0; index < 42; index += 1) {
    const date = new Date(start); date.setUTCDate(start.getUTCDate() + index);
    const dateKey = isoDay(date);
    const outside = dateKey.slice(0, 7) !== month.slice(0, 7);
    const cell = make("div", "calendar-cell" + (outside ? " outside" : "") + (dateKey === state.today ? " today" : "") +
      (dateKey === state.selectedStart || (state.selectedEnd && dateKey >= state.selectedStart && dateKey <= state.selectedEnd) ? " selected" : ""));
    cell.dataset.date = dateKey;
    const selected = isCalendarDateSelected(dateKey);
    const dateButton = make("button", "day-number", String(date.getUTCDate()));
    dateButton.type = "button";
    dateButton.setAttribute("aria-label", dateText(dateKey) + (selected ? ", selected date" : "") + (dateKey < state.today ? ", unavailable for a new request" : ", available for a new request"));
    dateButton.setAttribute("aria-pressed", String(selected));
    if (dateKey === state.today) dateButton.setAttribute("aria-current", "date");
    dateButton.disabled = dateKey < state.today;
    dateButton.dataset.date = dateKey;
    if (!dateButton.disabled && !firstAvailableDate && dateKey.slice(0, 7) === month.slice(0, 7)) firstAvailableDate = dateKey;
    dateButton.tabIndex = dateKey === preferredFocusDate && !dateButton.disabled ? 0 : -1;
    if (dateButton.tabIndex === 0) focusButton = dateButton;
    dateButton.addEventListener("click", () => {
      if (state.suppressCalendarClick) { state.suppressCalendarClick = false; return; }
      pickCalendarDate(dateKey);
    });
    cell.appendChild(dateButton);
    if (holidaySet.has(dateKey)) {
      const holidayName = (state.calendar.holiday_names || {})[dateKey] || "Company holiday";
      const holidayLabel = make("span", "holiday-label", holidayName);
      holidayLabel.title = holidayName + " · Company holiday";
      holidayLabel.setAttribute("aria-label", holidayLabel.title);
      cell.appendChild(holidayLabel);
    }
    const events = (state.calendar.events || []).filter(event => eventCovers(event, dateKey));
    events.slice(0, 3).forEach(event => {
      const detail = (event.category || "Vacation") + (event.status === "pending" ? " · Pending" : "");
      const portion = requestPortion(event);
      const compactPortion = event.half === "morning" ? " · AM" : event.half === "afternoon" ? " · PM" : "";
      const chip = make("span", "calendar-event" + (event.status === "pending" ? " pending" : ""),
        (event.name || detail) + (event.name ? compactPortion : (portion ? " · " + portion : "")));
      chip.title = (event.name ? event.name + " · " : "") + detail + (portion ? " · " + portion : "");
      chip.setAttribute("aria-label", chip.title);
      cell.appendChild(chip);
    });
    if (events.length > 3) cell.appendChild(make("span", "holiday-label", "+" + (events.length - 3) + " more"));
    grid.appendChild(cell);
  }
  if (!focusButton && firstAvailableDate) {
    focusButton = [...grid.querySelectorAll(".day-number")].find(button => button.dataset.date === firstAvailableDate);
    if (focusButton) focusButton.tabIndex = 0;
  }
  if (state.calendarFocusDate) {
    if (focusButton && focusButton.dataset.date === state.calendarFocusDate) focusButton.focus();
    state.calendarFocusDate = "";
  }
  renderCalendarSelection();
  renderCalendarAgenda();
}
function isCalendarDateSelected(value) {
  return Boolean(state.selectedStart && (value === state.selectedStart ||
    (state.selectedEnd && value >= state.selectedStart && value <= state.selectedEnd)));
}
function renderCalendarSelection() {
  const hasStart = Boolean(state.selectedStart);
  const hasRange = Boolean(state.selectedEnd && state.selectedEnd !== state.selectedStart);
  $("calendar-view").classList.toggle("has-date-selection", hasStart);
  $("calendar-hint").hidden = hasStart;
  $("calendar-selection-actions").hidden = !hasStart;
  $("calendar-prompt").textContent = hasRange
    ? "Date range selected. Continue to the request form to check scheduled days and holidays."
    : "One day selected. Click another date or drag to extend the range, or continue with this day.";
  $("selected-range").textContent = hasStart
    ? (hasRange
      ? dateText(state.selectedStart) + " – " + dateText(state.selectedEnd) + " · " + calendarDayCount(state.selectedStart, state.selectedEnd) + " days"
      : dateText(state.selectedStart) + " · 1 day")
    : "";
  $("request-selected").disabled = !hasStart;
  updateCalendarSelectionCells();
}
function calendarDayCount(start, end) {
  return Math.round((dayDate(end) - dayDate(start)) / 86400000) + 1;
}
function updateCalendarSelectionCells() {
  document.querySelectorAll("#calendar-grid .calendar-cell[data-date]").forEach(cell => {
    const selected = isCalendarDateSelected(cell.dataset.date);
    cell.classList.toggle("selected", selected);
    const button = cell.querySelector(".day-number");
    if (button) {
      button.setAttribute("aria-label", dateText(cell.dataset.date) + (selected ? ", selected date" : "") + (cell.dataset.date < state.today ? ", unavailable for a new request" : ", available for a new request"));
      button.setAttribute("aria-pressed", String(selected));
    }
  });
}
function renderCalendarAgenda() {
  const list = $("calendar-agenda-list"); list.replaceChildren();
  const month = monthFirst(isoDay(state.month));
  const entries = [];
  for (const event of state.calendar.events || []) {
    if (!eventOverlapsMonth(event, month)) continue;
    const portion = requestPortion(event);
    entries.push({date:dayISO(event.start), date_text:calendarEventDateText(event), label:(event.name ? event.name + " · " : "") +
      (event.category || "Vacation") + (portion ? " · " + portion : ""), detail:statusText(event.status)});
  }
  const holidayNames = state.calendar.holiday_names || {};
  for (const holiday of state.calendar.holidays || []) {
    const date = dayISO(holiday);
    entries.push({date, label:holidayNames[date] || "Company holiday", detail:"Office closed"});
  }
  entries.sort((a,b) => a.date.localeCompare(b.date) || a.label.localeCompare(b.label));
  $("calendar-agenda-empty").hidden = entries.length > 0;
  for (const entry of entries) {
    const row = make("li", "agenda-item");
    row.append(make("time", "agenda-date", entry.date_text || dateText(entry.date, {weekday:"short", month:"short", day:"numeric"})),
      make("strong", "agenda-label", entry.label), make("span", "agenda-detail", entry.detail));
    list.appendChild(row);
  }
}
function pickCalendarDate(value) {
  if (!state.selectedStart || state.selectedEnd) {
    state.selectedStart = value; state.selectedEnd = "";
  } else if (value < state.selectedStart) {
    state.selectedStart = value; state.selectedEnd = "";
  } else {
    state.selectedEnd = value;
  }
  renderCalendarSelection();
}
function calendarCellAtPoint(x, y) {
  const target = document.elementFromPoint(x, y);
  const cell = target && target.closest(".calendar-cell[data-date]");
  const button = cell && cell.querySelector(".day-number");
  return button && !button.disabled ? {cell, date:cell.dataset.date} : null;
}
function previewCalendarDrag(start, current) {
  state.selectedStart = start <= current ? start : current;
  state.selectedEnd = start <= current ? current : start;
  if (state.selectedEnd === state.selectedStart) state.selectedEnd = "";
  updateCalendarSelectionCells();
}
function setUpCalendarDragSelection() {
  const grid = $("calendar-grid");
  grid.addEventListener("keydown", event => {
    const current = event.target.closest(".day-number");
    if (!current) return;
    const offset = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7}[event.key];
    if (!offset) return;
    event.preventDefault();
    const nextDate = shiftDay(current.dataset.date, offset);
    if (nextDate < state.today) return;
    const target = [...grid.querySelectorAll(".day-number")].find(button => button.dataset.date === nextDate);
    if (target && !target.disabled) {
      grid.querySelectorAll(".day-number").forEach(button => { button.tabIndex = button === target ? 0 : -1; });
      target.focus();
      return;
    }
    state.calendarFocusDate = nextDate;
    state.month = dayDate(nextDate);
    state.month.setUTCDate(1);
    loadCalendar().catch(error => setNotice(describeError(error), "error"));
  });
  grid.addEventListener("pointerdown", event => {
    if (event.pointerType === "touch" || event.button !== 0) return;
    const cell = event.target.closest(".calendar-cell[data-date]");
    const button = cell && cell.querySelector(".day-number");
    if (!button || button.disabled) return;
    state.calendarDrag = {pointerId:event.pointerId, start:cell.dataset.date, current:cell.dataset.date,
      x:event.clientX, y:event.clientY, moved:false};
  });
  document.addEventListener("pointermove", event => {
    const drag = state.calendarDrag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!drag.moved && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 8) return;
    drag.moved = true;
    const target = calendarCellAtPoint(event.clientX, event.clientY);
    if (!target || target.date === drag.current) return;
    drag.current = target.date;
    previewCalendarDrag(drag.start, drag.current);
  });
  const finish = event => {
    const drag = state.calendarDrag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    state.calendarDrag = null;
    if (!drag.moved) return;
    previewCalendarDrag(drag.start, drag.current);
    renderCalendarSelection();
    state.suppressCalendarClick = true;
    window.setTimeout(() => { state.suppressCalendarClick = false; }, 0);
  };
  document.addEventListener("pointerup", finish);
  document.addEventListener("pointercancel", finish);
}
function setMonthOffset(offset) {
  state.month.setUTCMonth(state.month.getUTCMonth() + offset, 1);
  loadCalendar().catch(error => setNotice(describeError(error), "error"));
}

function requestFormReady() {
  return Boolean($("start-date").value && $("end-date").value &&
    $("request-reason").value.trim() && $("request-reason").value.trim().length <= 240 &&
    ($("leave-unit").value !== "half_day" || $("half-period").value));
}
function renderRequestMode() {
  const half = $("leave-unit").value === "half_day";
  $("end-field").hidden = half;
  $("half-field").hidden = !half;
  $("start-label").textContent = half ? "Date" : "First day";
  $("end-date").min = $("start-date").value || state.today;
  if (half) $("end-date").value = $("start-date").value;
  $("request-help").textContent = half
    ? "A half day covers one morning or afternoon based on the work schedule."
    : (isManager() ? "Full days follow your work schedule. Your own booking is approved immediately; your balance and borrowing limit still apply." : "Full days follow your work schedule. Weekends and company holidays are not charged.");
  $("calculate-button").disabled = state.saving || !requestFormReady();
  $("submit-button").disabled = state.saving || !state.quote;
}
function requestPayload() {
  const start = $("start-date").value;
  const half = $("leave-unit").value === "half_day";
  return {employee_id:actorEmployeeId(), category:$("request-category").value || "vacation",
    start_date:start, end_date:half ? start : $("end-date").value,
    unit:half ? "half_day" : "full_day", reason:$("request-reason").value.trim(),
    ...(half ? {half:$("half-period").value} : {})};
}
function clearQuote() {
  state.quote = null;
  $("quote-card").hidden = true;
  $("submit-button").disabled = true;
}
function openRequest(start, end) {
  if (state.saving) return;
  state.requestOpen = true;
  $("request-reason").value = "";
  if (start) $("start-date").value = start;
  if (end) $("end-date").value = end;
  if (start && !end) $("end-date").value = start;
  $("leave-unit").value = "full_day";
  renderRequestMode(); clearQuote();
  $("request-panel").hidden = false;
  $("request-panel").scrollIntoView({behavior:"smooth", block:"start"});
  $("start-date").focus({preventScroll:true});
}
function handleRequestOpen() {
  if (state.view !== "calendar") {
    setView("calendar");
    return;
  }
  openRequest();
}
function closeRequest() {
  if (state.saving) return;
  state.requestOpen = false; clearQuote(); $("request-panel").hidden = true;
  state.selectedStart = ""; state.selectedEnd = "";
  if (state.view === "calendar") renderCalendar();
}
async function calculateRequest() {
  if (state.saving || !requestFormReady()) return;
  clearQuote(); setSaving(true); setNotice("Checking the work schedule and holiday calendar…");
  try {
    const payload = requestPayload();
    state.quote = await api("/api/quote", {method:"POST", body:JSON.stringify(payload)});
    const dayCount = parseNumber(state.quote.days);
    $("quote-total").textContent = formatDays(dayCount);
    const portion = requestPortion(state.quote);
    $("quote-detail").textContent = portion
      ? portion + " · " + hours(state.quote.minutes) + " scheduled hours."
      : "Scheduled workdays. Weekends and company holidays are excluded.";
    $("quote-funding").textContent = "Submitting reserves time. The balance and borrowing limit are checked again when saved.";
    $("quote-card").hidden = false;
    $("submit-button").disabled = false;
    setNotice("Review the scheduled days, then submit to reserve the time.", "success");
  } catch (error) { setNotice(describeError(error), "error"); }
  finally { setSaving(false); }
}
async function submitRequest() {
  if (!state.quote || state.saving) return;
  const data = {...requestPayload(), quote_version:state.quote.quote_version};
  const result = await runCommand("request.submit", data, $("submit-button"));
  if (result) {
    state.requestOpen = false; $("request-panel").hidden = true; clearQuote();
    if (!state.commandRefreshFailed) {
      setNotice(isManager() ? "Time off booked and approved." : "Request submitted. Time is now reserved.", "success");
    }
  }
}
async function runRequestAction(command, employeeId, request) {
  const data = {employee_id:employeeId, category:request.category || "vacation", request_id:request.request_id};
  const result = await runCommand(command, data);
  if (!result || state.commandRefreshFailed) return;
  const employee = employeeId === actorEmployeeId() ? "Your" : displayName(employeeId) + "’s";
  const messages = {
    "request.approve": employee + " time off was approved.",
    "request.reject": employee + " time off was declined.",
    "request.cancel": employee + " time off was cancelled.",
  };
  setNotice(messages[command] || "Request updated.", "success");
}
async function runCommand(name, data, button) {
  if (state.saving) return null;
  state.commandRefreshFailed = false;
  const identity = JSON.stringify([persona.value, name, data]);
  const operationKey = pendingOperations.get(identity) || makeKey();
  pendingOperations.set(identity, operationKey); saveOperationKeys();
  setSaving(true); if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  setNotice("Saving…");
  let saved = false;
  try {
    const result = await api("/api/commands", {method:"POST", headers:{"Idempotency-Key":operationKey},
      body:JSON.stringify({command:name, data})});
    saved = true; pendingOperations.delete(identity); saveOperationKeys();
    try { await refreshPortal(); }
    catch (_) {
      state.commandRefreshFailed = true;
      setNotice("The change was saved, but the screen could not refresh. Reload to see the latest state.", "error");
    }
    return result;
  } catch (error) {
    const uncertain = error.network || error.status >= 500;
    if (!uncertain) { pendingOperations.delete(identity); saveOperationKeys(); }
    setNotice(describeError(error), "error");
    return null;
  } finally {
    setSaving(false); if (button && !saved) { button.disabled = false; button.removeAttribute("aria-busy"); }
    if (button && saved) button.removeAttribute("aria-busy");
    renderEverything();
  }
}

async function loadProjection() {
  if (!state.initialized) return;
  const generation = ++state.projectionLoadGeneration;
  const portalGeneration = state.portalLoadGeneration;
  const identity = persona.value;
  const employeeId = isManager() ? ($("projection-employee").value || actorEmployeeId()) : actorEmployeeId();
  const on = $("projection-date").value || state.today;
  state.projectionEmployee = employeeId;
  try {
    const projection = await api("/api/projection?" + new URLSearchParams({employee_id:employeeId, category:"vacation", on}).toString());
    if (generation !== state.projectionLoadGeneration || portalGeneration !== state.portalLoadGeneration ||
        identity !== persona.value || employeeId !== (isManager() ? ($("projection-employee").value || actorEmployeeId()) : actorEmployeeId()) ||
        on !== ($("projection-date").value || state.today)) return;
    state.projection = projection;
    renderProjection();
  } catch (error) {
    if (generation === state.projectionLoadGeneration && portalGeneration === state.portalLoadGeneration && identity === persona.value)
      $("projection-result").replaceChildren(make("p", "muted", describeError(error)));
  }
}
function renderProjection() {
  const host = $("projection-result"); host.replaceChildren();
  const projection = state.projection;
  if (!projection) { host.appendChild(make("p", "muted", "Choose a date to see your estimate.")); return; }
  const overview = state.overviews.get(projection.employee_id);
  if (overview && overview.balance && overview.balance.mode === "unlimited") {
    host.appendChild(make("strong", "projected-value", "Unlimited"));
    host.appendChild(make("p", "muted", "Usage is tracked, but there is no numerical balance limit."));
    return;
  }
  if (projection.projected_available === null || projection.projected_available === undefined) {
    host.appendChild(make("strong", "projected-value", "Not available yet"));
    host.appendChild(make("p", "muted", "Future worked-time accrual depends on payroll and cannot be forecast."));
    return;
  }
  host.appendChild(make("span", "muted", "Estimated available on " + dateText(projection.on) + ":"));
  const projectionDay = parseNumber(projection.day_minutes) || parseNumber(overview && overview.day_minutes);
  host.appendChild(make("strong", "projected-value", daysText(projection.projected_available, projectionDay)));
  host.appendChild(make("p", "projection-plan-note", daysText(projection.planned_leave_through_date, projectionDay) +
    " of planned leave through this date is already held from your available balance."));
  const details = make("div", "projection-breakdown");
  const entries = [
    ["Available now", daysText(projection.current_available, projectionDay)],
    ["Completed-month accrual", "+" + daysText(projection.forecast_accrued, projectionDay)],
    ["Requests after date returned", "+" + daysText(projection.planned_leave_after_date || 0, projectionDay)],
    ["Credits expiring by date", parseNumber(projection.credit_expiring) ? "−" + daysText(projection.credit_expiring, projectionDay) : "0 days"],
  ];
  for (const item of entries) { details.append(make("span", "", item[0]), make("strong", "", item[1])); }
  host.appendChild(details);
}

function fillEmployeeSelect(select, members) {
  select.replaceChildren();
  members.forEach(member => addOption(select, member.employee_id, member.name));
}
function configureManagerForm() {
  const editable = state.team;
  fillEmployeeSelect($("schedule-employee"), editable);
  if (editable.some(member => member.employee_id === actorEmployeeId())) $("schedule-employee").value = actorEmployeeId();
  const policy = latestPolicyVersion();
  const legacyNote = $("legacy-borrowing-note");
  legacyNote.hidden = true;
  if (policy && policy.mode !== "unlimited") {
    if (policy.borrowing_limit_days !== null && policy.borrowing_limit_days !== undefined) {
      $("borrow-days").value = String(parseNumber(policy.borrowing_limit_days));
    } else if (parseNumber(policy.borrowing_limit) > 0) {
      $("borrow-days").value = "";
      legacyNote.textContent = "This policy version uses a fixed minutes limit. Enter a workday limit to publish a schedule-aware version.";
      legacyNote.hidden = false;
    } else {
      $("borrow-days").value = "0";
    }
  }
  $("unlimited-note").hidden = !(policy && policy.mode === "unlimited");
  $("borrow-days").disabled = Boolean(policy && policy.mode === "unlimited");
  $("save-policy").disabled = Boolean(policy && policy.mode === "unlimited");
  const calendarVersions = state.me.configuration.calendar_versions || [];
  const latestCalendar = calendarVersions.slice().sort((a, b) =>
    dayISO(a.effective_from).localeCompare(dayISO(b.effective_from))).slice(-1)[0];
  state.holidayEntries = normalizeHolidayEntries(latestCalendar ? latestCalendar.holidays : state.me.configuration.holidays);
  populateHolidayYears();
  renderHolidayEntries();
  $("holiday-date").value = "";
  $("holiday-name").value = "";
  const policyVersions = state.me.configuration.policy_versions || [];
  $("holiday-effective").value = nextEffective(calendarVersions);
  $("policy-effective").value = nextEffective(policyVersions.length ? policyVersions : [policy || {}]);
  configureWorkdayStartOptions();
  renderWeekdayOptions();
  loadScheduleForSelection();
}
function normalizeHolidayEntries(values) {
  return (values || []).map(value => value && typeof value === "object"
    ? {date:dayISO(value.date), name:String(value.name || "Company holiday")}
    : {date:dayISO(value), name:"Company holiday"})
    .filter(value => value.date).sort((a,b) => a.date.localeCompare(b.date) || a.name.localeCompare(b.name));
}
function populateHolidayYears() {
  const select = $("holiday-year"); select.replaceChildren();
  const current = dayDate(state.today).getUTCFullYear();
  const years = new Set();
  for (let year = current - 1; year <= current + 5; year += 1) years.add(year);
  state.holidayEntries.forEach(entry => { const year = Number(entry.date.slice(0, 4)); if (year) years.add(year); });
  [...years].sort((a,b) => a-b).forEach(year => addOption(select, String(year), String(year)));
  select.value = String(current);
}
function renderHolidayEntries() {
  const list = $("holiday-entry-list"); list.replaceChildren();
  $("holiday-list-empty").hidden = state.holidayEntries.length > 0;
  state.holidayEntries.forEach((entry, index) => {
    const row = make("li", "holiday-entry");
    const date = document.createElement("input");
    date.type = "date"; date.value = entry.date;
    date.setAttribute("aria-label", "Date for " + entry.name);
    date.addEventListener("change", () => {
      state.holidayEntries[index].date = date.value;
      name.setAttribute("aria-label", "Holiday name for " + date.value);
    });
    const name = document.createElement("input");
    name.type = "text"; name.maxLength = 100; name.value = entry.name;
    name.setAttribute("aria-label", "Holiday name for " + entry.date);
    name.addEventListener("change", () => {
      state.holidayEntries[index].name = name.value.trim();
      date.setAttribute("aria-label", "Date for " + name.value.trim());
      remove.setAttribute("aria-label", "Remove " + name.value.trim());
    });
    const remove = make("button", "text-button holiday-remove", "Remove");
    remove.type = "button"; remove.disabled = state.saving; remove.setAttribute("aria-label", "Remove " + entry.name);
    remove.addEventListener("click", () => {
      state.holidayEntries.splice(index, 1); renderHolidayEntries();
    });
    row.append(date, name, remove); list.appendChild(row);
  });
}
function addManualHoliday() {
  const date = $("holiday-date").value;
  const name = $("holiday-name").value.trim();
  if (!date || !name) { setNotice("Choose a date and enter its holiday name.", "error"); return; }
  if (state.holidayEntries.some(entry => entry.date === date)) {
    setNotice("That date is already listed. Edit its name or remove it first.", "error"); return;
  }
  state.holidayEntries.push({date, name});
  state.holidayEntries.sort((a,b) => a.date.localeCompare(b.date));
  renderHolidayEntries();
  $("holiday-date").value = ""; $("holiday-name").value = "";
  setNotice(name + " added. Save holidays to publish the change.", "success");
}
async function addUsFederalHolidays() {
  const year = Number($("holiday-year").value);
  if (!Number.isInteger(year)) { setNotice("Choose a calendar year.", "error"); return; }
  const button = $("add-us-holidays"); button.disabled = true;
  try {
    const preset = await api("/api/holiday-presets/us-federal?" + new URLSearchParams({year:String(year)}).toString());
    const byDate = new Map(state.holidayEntries.map(entry => [entry.date, entry]));
    let added = 0, labeled = 0;
    for (const entry of preset.holidays || []) {
      const existing = byDate.get(entry.date);
      if (!existing) { byDate.set(entry.date, {date:entry.date, name:entry.name}); added += 1; }
      else if (existing.name === "Company holiday") { byDate.set(entry.date, {date:entry.date, name:entry.name}); labeled += 1; }
    }
    state.holidayEntries = [...byDate.values()].sort((a,b) => a.date.localeCompare(b.date));
    renderHolidayEntries();
    setNotice(added + " dates added" + (labeled ? "; " + labeled + " existing dates labeled" : "") +
      " from the " + year + " U.S. federal calendar. Review before saving.", "success");
  } catch (error) { setNotice(describeError(error), "error"); }
  finally { button.disabled = state.saving; }
}
function configureWorkdayStartOptions() {
  const select = $("schedule-start"); select.replaceChildren();
  for (let hour = 0; hour < 24; hour += 1) {
    const suffix = hour < 12 ? "AM" : "PM";
    const display = (hour % 12 || 12) + ":00 " + suffix;
    addOption(select, String(hour * 60), display);
  }
}
function renderWeekdayOptions(selectedDays) {
  const host = $("weekday-options"); host.replaceChildren();
  const days = selectedDays || [0, 1, 2, 3, 4];
  ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((label, index) => {
    // The domain uses datetime.weekday(): Monday=0 through Sunday=6.
    const weekday = index;
    const wrapper = make("label", "weekday-option");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.value = String(weekday); checkbox.checked = days.includes(weekday);
    checkbox.setAttribute("aria-label", label);
    wrapper.append(checkbox, make("span", "", label)); host.appendChild(wrapper);
  });
}
function loadScheduleForSelection() {
  const employeeId = $("schedule-employee").value || actorEmployeeId();
  const overview = state.overviews.get(employeeId);
  if (!overview) return;
  const schedule = activeSchedule(overview);
  $("schedule-hours").value = (parseNumber(schedule.day_minutes || 480) / 60).toString();
  $("schedule-start").value = String(schedule.start_minute === undefined ? 540 : schedule.start_minute);
  renderWeekdayOptions((schedule.weekdays || [1, 2, 3, 4, 5]).map(Number));
  const timeline = overview.configuration && overview.configuration.schedules || [];
  $("schedule-effective").value = nextEffective(timeline);
}
function setNextEffective(inputId) {
  const input = $(inputId);
  if (input.value) input.value = shiftDay(input.value, 1);
}
async function saveSchedule() {
  const employeeId = $("schedule-employee").value;
  const weekdays = [...$("weekday-options").querySelectorAll("input:checked")].map(input => Number(input.value)).sort((a,b) => a-b);
  const hoursPerDay = parseNumber($("schedule-hours").value);
  const dayMinutes = Math.round(hoursPerDay * 60);
  if (!employeeId || !weekdays.length || hoursPerDay <= 0 || hoursPerDay > 16 || !$("schedule-effective").value) {
    setNotice("Choose at least one workday, between 1 and 16 hours per day, and an effective date.", "error"); return;
  }
  const selected = state.overviews.get(employeeId);
  const schedule = {version_id:uniqueVersion("schedule"), effective_from:$("schedule-effective").value,
    weekdays, start_minute:Number($("schedule-start").value), day_minutes:dayMinutes,
    timezone:(activeSchedule(selected).timezone || "UTC")};
  const result = await runCommand("employee.schedule.publish", {employee_id:employeeId, schedule}, $("save-schedule"));
  if (result) {
    setNextEffective("schedule-effective");
    if (!state.commandRefreshFailed) setNotice("Work schedule saved. Pending requests were recalculated; approved leave stays as agreed.", "success");
  }
}
async function savePolicy() {
  const current = latestPolicyVersion();
  if (!current || current.mode === "unlimited") return;
  const borrowDaysRaw = $("borrow-days").value.trim();
  const borrowDays = Number(borrowDaysRaw);
  if (!borrowDaysRaw || !Number.isFinite(borrowDays) || borrowDays < 0 || !$("policy-effective").value) {
    setNotice("Enter a non-negative workday limit and an effective date.", "error"); return;
  }
  const policy = {policy_id:current.policy_id, version_id:uniqueVersion("policy"), category:current.category,
    effective_from:$("policy-effective").value, mode:current.mode,
    amount:String(current.amount || "0"), unit:current.unit || "hours", period:current.period || "year",
    borrowing_limit:"0", borrowing_limit_days:String(borrowDays),
    tenure_tiers:(current.tenure_tiers || []).map(item => ({completed_years:item.completed_years, amount:String(item.amount)}))};
  if (current.worked_denominator_days) policy.worked_denominator_days = String(current.worked_denominator_days);
  if (current.worked_denominator_minutes) policy.worked_denominator_minutes = String(current.worked_denominator_minutes);
  const result = await runCommand("policy.publish", policy, $("save-policy"));
  if (result) {
    setNextEffective("policy-effective");
    if (!state.commandRefreshFailed) setNotice("Borrowing limit saved in workdays. Existing commitments are preserved.", "success");
  }
}
async function saveHolidays() {
  const effective = $("holiday-effective").value;
  const holidays = state.holidayEntries.map(entry => ({date:entry.date, name:entry.name.trim()}))
    .sort((a,b) => a.date.localeCompare(b.date));
  const dates = new Set();
  let invalidHoliday = false;
  for (const entry of holidays) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(entry.date) || Number.isNaN(dayDate(entry.date).valueOf()) ||
        isoDay(dayDate(entry.date)) !== entry.date || !entry.name || entry.name.length > 100 || dates.has(entry.date)) {
      invalidHoliday = true; break;
    }
    dates.add(entry.date);
  }
  if (!effective || invalidHoliday) {
    setNotice("Check that every holiday has one valid date and a name, then choose an effective date.", "error"); return;
  }
  const result = await runCommand("company.calendar.publish", {version_id:uniqueVersion("calendar"), effective_from:effective, holidays}, $("save-holidays"));
  if (result) {
    setNextEffective("holiday-effective");
    if (!state.commandRefreshFailed) setNotice("Company calendar saved. Holiday names now appear on the calendar; pending requests were recalculated.", "success");
  }
}

function renderRequestModeAndQuote() {
  renderRequestMode(); clearQuote();
}
function setUpEvents() {
  setUpCalendarDragSelection();
  persona.addEventListener("change", loadPortal);
  document.querySelectorAll(".nav-item[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelectorAll("[data-go]").forEach(button => button.addEventListener("click", () => setView(button.dataset.go)));
  $("request-open").addEventListener("click", handleRequestOpen);
  $("request-close").addEventListener("click", closeRequest);
  $("start-date").addEventListener("change", () => {
    $("end-date").min = $("start-date").value || state.today;
    if ($("leave-unit").value === "half_day" || $("end-date").value < $("start-date").value) $("end-date").value = $("start-date").value;
    renderRequestModeAndQuote();
  });
  $("end-date").addEventListener("change", renderRequestModeAndQuote);
  $("leave-unit").addEventListener("change", renderRequestModeAndQuote);
  $("half-period").addEventListener("change", renderRequestModeAndQuote);
  $("request-category").addEventListener("change", renderRequestModeAndQuote);
  $("request-reason").addEventListener("input", renderRequestModeAndQuote);
  $("calculate-button").addEventListener("click", calculateRequest);
  $("submit-button").addEventListener("click", submitRequest);
  $("projection-date").addEventListener("change", loadProjection);
  $("projection-employee").addEventListener("change", loadProjection);
  $("month-prev").addEventListener("click", () => setMonthOffset(-1));
  $("month-next").addEventListener("click", () => setMonthOffset(1));
  $("month-today").addEventListener("click", () => { state.month = dayDate(state.today); state.month.setUTCDate(1); loadCalendar(); });
  $("clear-calendar-selection").addEventListener("click", () => {
    state.selectedStart = ""; state.selectedEnd = ""; renderCalendar();
  });
  $("request-selected").addEventListener("click", () => {
    if (!state.selectedStart) return;
    const start = state.selectedStart, end = state.selectedEnd || state.selectedStart;
    state.selectedStart = ""; state.selectedEnd = "";
    renderCalendar(); openRequest(start, end);
  });
  document.querySelectorAll(".request-tab").forEach(button => button.addEventListener("click", () => {
    state.bucket = button.dataset.bucket;
    document.querySelectorAll(".request-tab").forEach(tab => { const selected = tab === button; tab.classList.toggle("active", selected); tab.setAttribute("aria-pressed", String(selected)); });
    renderRequests();
  }));
  $("request-employee-filter").addEventListener("change", event => { state.requestEmployeeFilter = event.target.value; renderRequests(); });
  $("schedule-employee").addEventListener("change", loadScheduleForSelection);
  $("save-schedule").addEventListener("click", saveSchedule);
  $("save-policy").addEventListener("click", savePolicy);
  $("add-holiday").addEventListener("click", addManualHoliday);
  $("add-us-holidays").addEventListener("click", addUsFederalHolidays);
  $("save-holidays").addEventListener("click", saveHolidays);
}

setUpEvents();
loadPortal();
