// Exercise the actual card export in a browser-like stream environment.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");

function cardFor(lines) {
  let registered;
  const writes = [];
  const outcome = {closed: false, aborted: false, paths: []};
  const context = {
    HTMLElement: class {}, URLSearchParams, TextDecoder, TransformStream,
    customElements: {get() {}, define(name, element) { registered = element; }},
    window: {showSaveFilePicker: async () => ({createWritable: async () => new WritableStream({
      write(chunk) { writes.push(new TextDecoder().decode(chunk)); },
      close() { outcome.closed = true; },
      abort() { outcome.aborted = true; },
    })})},
  };
  vm.runInNewContext(readFileSync("custom_components/health_bridge/cards/health-bridge-archive.js", "utf8"), context);
  const card = new registered();
  card._config = {user_id: "person-1"};
  card._params = () => new URLSearchParams({sample_type: "HKWorkoutType"});
  card._notice = text => { outcome.notice = text; };
  card._hass = {fetchWithAuth: async path => {
    outcome.paths.push(path);
    return {ok: true, body: new ReadableStream({start(controller) {
      for (const line of lines) controller.enqueue(new TextEncoder().encode(line));
      controller.close();
    }})};
  }};
  return {card, outcome, writes};
}

test("export uses HA authenticated fetch and closes only with a complete footer", async () => {
  const lines = ['{"kind":"sample","value":12}\n', '{"kind":"complete","samples":1,"tombstones":0}\n'];
  const {card, outcome, writes} = cardFor(lines);
  await card._export();
  assert.deepEqual(outcome.paths, ["/api/health_bridge/archive/person-1/export?sample_type=HKWorkoutType"]);
  assert.equal(writes.join(""), lines.join(""));
  assert.equal(outcome.closed, true);
  assert.equal(outcome.aborted, false);
});

test("truncated or server-error exports abort the file", async () => {
  for (const ending of ['', '{"kind":"error","complete":false}\n']) {
    const {card, outcome} = cardFor(['{"kind":"sample"}\n', ending]);
    await assert.rejects(card._export());
    assert.equal(outcome.closed, false);
    assert.equal(outcome.aborted, true);
  }
});
