import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = await fs.readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const formReadyMatch = source.match(/function requestFormReady\(\) \{[\s\S]*?\n\}/);
assert.ok(formReadyMatch, 'the request form has a single readiness check');
const formFields = {
  'start-date': {value:'2025-02-03'}, 'end-date': {value:'2025-02-03'},
  'leave-unit': {value:'full_day'}, 'half-period': {value:'morning'},
  'request-reason': {value:''},
};
const formContext = vm.createContext({$: id => formFields[id]});
vm.runInContext(formReadyMatch[0], formContext);
assert.equal(vm.runInContext('requestFormReady()', formContext), false,
  'a request cannot be reviewed without a reason');
formFields['request-reason'].value = 'Personal plans';
assert.equal(vm.runInContext('requestFormReady()', formContext), true,
  'a complete request with a brief reason is ready for review');

const match = source.match(/async function runCommand\(name, data, button\) \{[\s\S]*?\n\}\n\nasync function loadProjection/);
assert.ok(match, 'the standalone browser app still exposes its request-command lifecycle');
const commandSource = match[0].replace(/\n\nasync function loadProjection$/, '');
const button = {
  disabled: false,
  attributes: new Map(),
  setAttribute(name, value) { this.attributes.set(name, value); },
  removeAttribute(name) { this.attributes.delete(name); },
};
const status = {textContent: ''};
const state = {saving: false, commandRefreshFailed: false};
const pendingOperations = new Map();
const posted = [];
let nextKey = 0;
let fail = true;
let release;

const context = vm.createContext({
  JSON, Map, state, pendingOperations,
  persona: {value: 'demo-avery'},
  makeKey: () => `attempt-${++nextKey}`,
  saveOperationKeys() {},
  setSaving(value) { state.saving = value; },
  setNotice(message) { status.textContent = message; },
  describeError(error) {
    return error.network
      ? 'The service could not confirm the result. Retry the action to check the original attempt.'
      : 'The change could not be saved.';
  },
  refreshPortal: async () => {},
  renderEverything() {},
  isManager: () => true,
  actorEmployeeId: () => 'lee',
  displayName: id => id === 'avery' ? 'Avery Morgan' : id,
  $: id => id === 'status' ? status : button,
  api: async (path, options) => {
    assert.equal(path, '/api/commands');
    posted.push({key: options.headers['Idempotency-Key'], body: JSON.parse(options.body)});
    await new Promise(resolve => { release = resolve; });
    if (fail) throw {network: true};
    return {ok: true, request_id: 'saved-once'};
  },
});

vm.runInContext(commandSource, context);
const data = {employee_id: 'avery', category: 'vacation', start: '2025-02-03', end: '2025-02-03'};
context.data = data;
context.button = button;
const invoke = () => vm.runInContext('runCommand("request.submit", data, button)', context);
const first = invoke();
assert.equal(state.saving, true);
assert.equal(button.disabled, true);
await invoke();
assert.equal(posted.length, 1, 'a second click while the request is pending sends nothing');
release();
assert.equal(await first, null);
assert.match(status.textContent, /original attempt/);
assert.equal(button.disabled, false, 'an uncertain failure can be retried');

fail = false;
const retry = invoke();
assert.equal(posted.length, 2);
assert.equal(posted[0].key, posted[1].key, 'a retry reuses the original operation key');
release();
assert.deepEqual(await retry, {ok: true, request_id: 'saved-once'});
assert.equal(button.disabled, true, 'a confirmed success leaves submission disabled until the form refreshes');

const headerRequestMatch = source.match(/function handleRequestOpen\(\) \{[\s\S]*?\n\}/);
assert.ok(headerRequestMatch, 'the main request action has an explicit calendar-first route');
const headerRequestState = {view:'overview'};
let requestedView = '';
let manualFormOpened = false;
const headerContext = vm.createContext({
  state: headerRequestState,
  setView(view) { requestedView = view; headerRequestState.view = view; },
  openRequest() { manualFormOpened = true; },
});
vm.runInContext(headerRequestMatch[0], headerContext);
vm.runInContext('handleRequestOpen()', headerContext);
assert.equal(requestedView, 'calendar', 'the main request action opens the calendar first');
assert.equal(manualFormOpened, false, 'the main action does not skip to typed date fields');
vm.runInContext('handleRequestOpen()', headerContext);
assert.equal(manualFormOpened, true, 'manual date entry remains available from the calendar');

const actionMatch = source.match(/async function runRequestAction\(command, employeeId, request\) \{[\s\S]*?\n\}\nasync function runCommand/);
assert.ok(actionMatch, 'request actions use the same guarded command lifecycle');
vm.runInContext(actionMatch[0].replace(/\nasync function runCommand$/, ''), context);
const approval = vm.runInContext(
  'runRequestAction("request.approve", "avery", {request_id:"request-1", category:"vacation"})', context);
await Promise.resolve();
release();
await approval;
assert.equal(status.textContent, 'Avery Morgan’s time off was approved.',
  'confirmed manager decisions get a clear completion message');

const dateHelpers = source.slice(source.indexOf('function dayISO('), source.indexOf('function timeStampText('));
const bucketMatch = source.match(/function bucketFor\(request\) \{[\s\S]*?\n\}/);
const eventOverlapMatch = source.match(/function eventOverlapsMonth\(event, month\) \{[\s\S]*?\n\}/);
const eventDateTextMatch = source.match(/function calendarEventDateText\(event\) \{[\s\S]*?\n\}/);
assert.ok(bucketMatch, 'request activity uses the production bucket classifier');
assert.ok(eventOverlapMatch && eventDateTextMatch, 'the calendar agenda uses the same exclusive-end date rules');
const calendarContext = vm.createContext({state: {today: '2025-02-05'}, request:{}});
vm.runInContext(dateHelpers + bucketMatch[0] + eventOverlapMatch[0] + eventDateTextMatch[0], calendarContext);
const bucket = request => {
  calendarContext.request = request;
  return vm.runInContext('bucketFor(request)', calendarContext);
};
assert.equal(bucket({status:'approved', start:'2025-02-03T00:00:00Z', end:'2025-02-08T00:00:00Z'}), 'upcoming',
  'a multi-day approved request remains upcoming through its last included day');
assert.equal(bucket({status:'approved', start:'2025-02-03T00:00:00Z', end:'2025-02-05T00:00:00Z'}), 'past',
  'an exclusive midnight request end is not counted as a leave day');
assert.equal(vm.runInContext('requestDateText({start:"2025-02-03",end:"2025-02-04T00:00:00Z",half:"morning"})', calendarContext)
  .includes('Morning half'), true, 'date labels preserve half-day intent');
calendarContext.event = {start:'2025-01-31T09:00:00Z', end:'2025-02-01T00:00:00Z'};
calendarContext.month = '2025-02-01';
assert.equal(vm.runInContext('eventOverlapsMonth(event, month)', calendarContext), false,
  'an event ending at midnight on the first does not leak into the new month agenda');
calendarContext.event = {start:'2025-01-31T09:00:00Z', end:'2025-02-04T00:00:00Z'};
assert.equal(vm.runInContext('eventOverlapsMonth(event, month)', calendarContext), true,
  'a leave interval crossing the month boundary remains visible in the agenda');
assert.match(vm.runInContext('calendarEventDateText(event)', calendarContext), /Jan 31.*Feb 3/,
  'the agenda shows the full included date range for a cross-month request');

const projectionMatch = source.match(/async function loadProjection\(\) \{[\s\S]*?\n\}\nfunction renderProjection/);
assert.ok(projectionMatch, 'date projections are fetched through the guarded loader');
const projectionRequests = [];
const projectionState = {initialized:true, projectionLoadGeneration:0, portalLoadGeneration:3, today:'2025-02-01'};
const projectionFields = {
  'projection-employee': {value:'avery'},
  'projection-date': {value:'2025-02-03'},
  'projection-result': {replaceChildren() {}},
};
const projectionContext = vm.createContext({
  state: projectionState,
  persona: {value:'demo-manager'},
  $: id => projectionFields[id],
  isManager: () => true,
  actorEmployeeId: () => 'lee',
  api: path => new Promise(resolve => projectionRequests.push({path, resolve})),
  URLSearchParams,
  renderProjection() {},
  describeError: error => String(error),
  make: () => ({}),
});
vm.runInContext(projectionMatch[0].replace('\nfunction renderProjection', ''), projectionContext);
const oldProjection = vm.runInContext('loadProjection()', projectionContext);
projectionFields['projection-date'].value = '2025-02-04';
const newProjection = vm.runInContext('loadProjection()', projectionContext);
assert.equal(projectionRequests.length, 2);
projectionRequests[1].resolve({on:'2025-02-04'});
await newProjection;
projectionRequests[0].resolve({on:'2025-02-03'});
await oldProjection;
assert.equal(projectionState.projection.on, '2025-02-04',
  'a slower response for an old date cannot replace the current projection');

const record = {
  method: 'Executed production browser functions copied from the standalone app in Node VMs with a minimal mock API and DOM surface.',
  checks: [
    'a second click while saving sends no duplicate request',
    'uncertain failure tells the user to retry the original attempt',
    'retry after an uncertain outcome reuses its idempotency key',
    'confirmed success leaves the submit action disabled until refreshed',
    'main request action starts with calendar selection while manual date entry remains available',
    'manager approval displays its confirmed result',
    'approved requests stay upcoming through their inclusive end date',
    'exclusive-midnight request ends are interpreted correctly',
    'half-day intent remains in date labels',
    'month agenda excludes exclusive-end spillover and preserves crossing ranges',
    'stale balance-projection responses are ignored after the selected date changes',
  ],
  passed: true,
};
console.log(JSON.stringify(record));
