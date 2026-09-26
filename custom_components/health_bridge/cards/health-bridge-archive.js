/* Originals are sensitive. All archive data uses HA administrator authentication. */
class HealthBridgeArchive extends HTMLElement {
  setConfig(config) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(config.user_id || "")) {
      throw new Error("Set user_id to the exact Health Bridge person ID.");
    }
    this._config = config;
    this._generation = (this._generation || 0) + 1;
    this._cursor = null;
    this._ready = false;
    this._build();
    if (this._hass) this._start();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._graph) this._graph.hass = hass;
    if (this._config && !this._ready) this._start();
  }

  getCardSize() { return 8; }

  _build() {
    if (!this.shadowRoot) this.attachShadow({mode: "open"});
    // Only static markup enters innerHTML. All archive strings use textContent.
    this.shadowRoot.innerHTML = `
      <style>
        ha-card {padding:20px} h2 {margin-top:0} label {display:block;margin:8px 0}
        input,select,button {font:inherit;padding:8px;max-width:100%;box-sizing:border-box}
        button {cursor:pointer;margin:4px} .filters {display:flex;flex-wrap:wrap;gap:12px}
        pre {white-space:pre-wrap;overflow-wrap:anywhere} li {margin:8px 0}
        .muted {color:var(--secondary-text-color)} #notice {white-space:pre-wrap}
        #danger {border-top:1px solid var(--divider-color);margin-top:20px;padding-top:12px}
        #timeline {padding-left:20px} #detail {max-height:30em;overflow:auto}
      </style>
      <ha-card>
        <h2>Health archive</h2><p id="person"></p>
        <p class="muted">Administrator access. Original samples are separate from recorder history.</p>
        <div class="filters">
          <label>Sample type <select id="type"></select></label>
          <label>From (UTC, inclusive) <input id="start" type="date"></label>
          <label>To (UTC, exclusive) <input id="end" type="date"></label>
        </div>
        <button id="load">Load / refresh</button><button id="next" disabled>Next page</button>
        <button id="export">Export JSON Lines</button><button id="retry">Retry failed statistics</button>
        <p id="notice" role="status" aria-live="polite"></p>
        <ul id="status"></ul><div id="graph"></div>
        <p class="muted">Pages show up to 200 originals in start-time / UUID order. Concurrent imports or corrections can change pages; pause import for a consistent export. Tombstones are exported for the entire selected person and type because they have no original date.</p>
        <ol id="timeline"></ol><pre id="detail"></pre>
        <section id="danger">
          <h3>Delete this person's archive</h3>
          <p>This deletes originals, tombstones, import receipts, and archive progress for this person. Recorder history, external statistics, backups, and prior exports are NOT deleted. This is not a privacy wipe. Pause imports first; future uploads can add records again.</p>
          <label>Type the exact person ID <input id="confirm" autocomplete="off"></label>
          <button id="delete">Delete archive…</button>
        </section>
      </ha-card>`;
    this._el("person").textContent = this._config.user_id;
    const today = new Date();
    this._el("end").value = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() + 1)).toISOString().slice(0, 10);
    this._el("start").value = `${today.getUTCFullYear() - 1}-01-01`;
    this._el("load").onclick = () => this._run(() => this._load(false));
    this._el("next").onclick = () => this._run(() => this._load(true));
    this._el("retry").onclick = () => this._run(async () => {
      await this._api("retry", "POST", {});
      await this._status();
      this._notice("Failed statistics queued for retry; visibility is not yet confirmed.");
    });
    this._el("delete").onclick = () => this._run(() => this._delete());
    this._el("export").onclick = () => this._run(() => this._export());
    for (const name of ["type", "start", "end"]) {
      this._el(name).onchange = () => {
        this._cursor = null;
        this._el("next").disabled = true;
        this._el("timeline").replaceChildren();
        this._el("detail").textContent = "";
        this._notice("Filters changed. Load the selected range.");
      };
    }
  }

  _el(id) { return this.shadowRoot.getElementById(id); }
  _notice(message) { this._el("notice").textContent = message; }
  _path(operation) { return `health_bridge/archive/${encodeURIComponent(this._config.user_id)}/${operation}`; }
  _api(operation, method = "GET", body) { return this._hass.callApi(method, this._path(operation), body); }

  _start() {
    this._ready = true;
    if (!this._hass.user?.is_admin) {
      this._notice("Home Assistant administrator access is required. Person-to-account pairing is not configured.");
      this.shadowRoot.querySelectorAll("button,input,select").forEach(el => { el.disabled = true; });
      return;
    }
    this._run(() => this._load(false));
  }

  async _run(action) {
    if (this._busy) return;
    this._busy = true;
    this.shadowRoot.querySelectorAll("button,input,select").forEach(el => { el.disabled = true; });
    try { await action(); }
    catch (_) { this._notice("Archive operation failed or was cancelled. Check administrator access and archive availability. No health details were logged."); }
    finally {
      this._busy = false;
      this.shadowRoot.querySelectorAll("button,input,select").forEach(el => { el.disabled = false; });
      this._el("next").disabled = !this._cursor;
    }
  }

  _params() {
    const start = this._el("start").value, end = this._el("end").value;
    if (!start || !end || start >= end) throw new Error("invalid_range");
    return new URLSearchParams({sample_type: this._el("type").value, start: `${start}T00:00:00Z`, end: `${end}T00:00:00Z`, limit: "200"});
  }

  async _status() {
    const generation = this._generation;
    const status = await this._api("status");
    if (generation !== this._generation) return;
    const selected = this._el("type").value;
    this._el("type").replaceChildren(...status.sample_types.map(type => {
      const option = document.createElement("option"); option.value = type;
      option.textContent = type.replace(/^HK(?:Quantity|Category)TypeIdentifier/, "");
      return option;
    }));
    if (status.sample_types.includes(selected)) this._el("type").value = selected;
    const list = this._el("status"); list.replaceChildren();
    for (const metric of status.metrics) {
      const item = document.createElement("li");
      const states = {pending: "Statistics pending", current: "Statistics current (read back)", failed: "Statistics failed — retry available"};
      item.textContent = `${metric.metric}: ${metric.timeline_only ? "Timeline only; no numeric statistics" : states[metric.state] || "Statistics pending"}. `;
      if (metric.statistic_id) {
        const button = document.createElement("button"); button.textContent = "Multiyear trend";
        button.onclick = () => this._run(async () => {
          // HA's hui-statistics-graph-card reads integration-owned statistic IDs.
          const helpers = await window.loadCardHelpers();
          const days = Math.min(36600, Math.max(1, Math.ceil((Date.now() - new Date(this._el("start").value).getTime()) / 86400000)));
          this._graph = helpers.createCardElement({type: "statistics-graph", title: `${metric.metric} — selected start through today`, entities: [{entity: metric.statistic_id, name: metric.metric}], days_to_show: days, period: "month", stat_types: [metric.statistic_type]});
          this._graph.hass = this._hass; this._el("graph").replaceChildren(this._graph);
        });
        item.append(button);
        const link = document.createElement("a"); link.href = "/developer-tools/statistics";
        link.textContent = "Statistics settings"; link.title = metric.statistic_id; item.append(link);
      }
      if (metric.entity_id) {
        const live = document.createElement("a"); live.href = `/history?entity_id=${encodeURIComponent(metric.entity_id)}`;
        live.textContent = " Live sensor history"; item.append(live);
      }
      list.append(item);
    }
    if (!status.statistics_available) list.append(document.createTextNode("Statistics unavailable; archived originals remain browsable."));
  }

  async _load(next) {
    const generation = this._generation;
    this._notice("Reading archive…");
    if (!next) { this._cursor = null; await this._status(); }
    if (generation !== this._generation) return;
    const params = this._params();
    if (next && this._cursor) params.set("cursor", this._cursor);
    const result = await this._api(`samples?${params}`);
    if (generation !== this._generation) return;
    this._cursor = result.next_cursor;
    this._el("detail").textContent = "";
    this._el("timeline").replaceChildren(...result.samples.map(sample => {
      const row = document.createElement("li"), button = document.createElement("button");
      const payload = sample.payload;
      const sleep = ["In bed", "Asleep (unspecified)", "Awake", "Core sleep", "Deep sleep", "REM sleep"];
      const category = params.get("sample_type") === "HKCategoryTypeIdentifierSleepAnalysis" ? `Sleep: ${sleep[payload.value] || payload.value}` : params.get("sample_type") === "HKCategoryTypeIdentifierMindfulSession" ? "Mindful session" : `Category: ${payload.value}`;
      const summary = payload.kind === "quantity" ? `${payload.canonical_value} ${payload.canonical_unit}` : payload.kind === "workout" ? `Workout: ${payload.activity_type}; ${payload.duration_seconds}s` : category;
      button.textContent = `${sample.start} – ${sample.end}: ${summary}`;
      button.onclick = () => this._run(async () => {
        const detail = await this._api(`sample?${new URLSearchParams({sample_type: params.get("sample_type"), uuid: sample.uuid})}`);
        if (generation === this._generation) this._el("detail").textContent = JSON.stringify(detail.sample, null, 2);
      });
      row.append(button); return row;
    }));
    this._notice(`${result.samples.length} archived samples on this page. ${this._cursor ? "More pages available." : "End of selected range."}`);
  }

  async _export() {
    if (!window.showSaveFilePicker) {
      this._notice("Streaming file export requires a desktop browser with the File System Access API (for example Chrome or Edge). The authenticated JSON Lines API is also available. This browser cannot safely save an unbounded archive in memory.");
      return;
    }
    const params = this._params();
    const handle = await window.showSaveFilePicker({suggestedName: "health-bridge-archive.jsonl", types: [{description: "JSON Lines", accept: {"application/x-ndjson": [".jsonl"]}}]});
    const response = await this._hass.fetchWithAuth(`/api/health_bridge/archive/${encodeURIComponent(this._config.user_id)}/export?${params}`, {cache: "no-store"});
    if (!response.ok || !response.body) throw new Error("export_failed");
    const destination = await handle.createWritable();
    const decoder = new TextDecoder();
    let tail = "";
    // Retain only enough bytes to validate the small footer; never buffer records.
    const checkCompletion = new TransformStream({
      transform(chunk, controller) {
        tail = (tail + decoder.decode(chunk, {stream: true})).slice(-2048);
        controller.enqueue(chunk);
      },
      flush() {
        tail += decoder.decode();
        const last = tail.trimEnd().split("\n").pop();
        if (JSON.parse(last).kind !== "complete") throw new Error("incomplete_export");
      }
    });
    // Backpressure bounds memory; a missing completion footer aborts the file.
    await response.body.pipeThrough(checkCompletion).pipeTo(destination);
    this._notice("Complete export saved. Keep this sensitive copy protected.");
  }

  async _delete() {
    const user = this._config.user_id;
    if (this._el("confirm").value !== user) { this._notice("Type the exact person ID before deleting."); return; }
    if (!window.confirm(`Permanently delete the archive for ${user}? Recorder, statistics, backups, and exports will remain.`)) return;
    await this._api("delete", "POST", {confirm_user_id: user, confirm: "DELETE"});
    this._el("confirm").value = "";
    this._el("graph").replaceChildren(); this._graph = null;
    await this._load(false);
    this._notice("Archive deleted for this person. Recorder, statistics, backups, and exports remain.");
  }
}

if (!customElements.get("health-bridge-archive")) customElements.define("health-bridge-archive", HealthBridgeArchive);
window.customCards = window.customCards || [];
if (!window.customCards.some(card => card.type === "health-bridge-archive")) {
  window.customCards.push({type: "health-bridge-archive", name: "Health Bridge Archive", description: "Administrator browser for original samples, multiyear statistics and streaming export. Configure user_id."});
}
