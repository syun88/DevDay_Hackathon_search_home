/* Server-authoritative search state. Future chat tools use the same patch contract. */
(() => {
'use strict';
class SearchSession extends EventTarget {
  constructor() {
    super();
    this.snapshot = null;
    this.criteria = {};
    this.draft = '';
    this.pending = {};
    this.status = 'loading';
    this.saving = false;
    this.generation = 0;
  }
  emit() { this.dispatchEvent(new Event('change')); }
  async request(options = {}) {
    const response = await fetch('/api/session', {
      credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(8000), ...options,
    });
    const data = await response.json();
    if (response.status === 409) {
      const error = new Error('conflict'); error.conflict = data.session; throw error;
    }
    if (!response.ok) throw new Error('session_request_failed');
    return data;
  }
  async load() {
    clearTimeout(this.timer);
    const generation = ++this.generation;
    this.status = 'loading'; this.emit();
    try {
      const data = await this.request();
      if (generation !== this.generation) return;
      this.snapshot = data; this.criteria = structuredClone(data.criteria);
      this.draft = data.draft_message; this.pending = {}; this.status = 'saved';
    } catch {
      if (generation !== this.generation) return;
      this.status = 'unavailable';
    }
    this.emit();
  }
  patch(criteria = {}, draft) {
    if (this.status === 'loading') return;
    for (const [key, value] of Object.entries(criteria)) {
      if (JSON.stringify(value) !== JSON.stringify(this.criteria[key])) {
        this.criteria[key] = structuredClone(value);
        this.pending.criteria = {...this.pending.criteria, [key]: structuredClone(value)};
      }
    }
    if (draft !== undefined && draft !== this.draft) {
      this.draft = draft; this.pending.draft_message = draft;
    }
    if (!Object.keys(this.pending).length) return;
    if (!this.snapshot) { this.emit(); return; }
    if (this.status !== 'conflict') {
      this.status = 'saving';
      clearTimeout(this.timer); this.timer = setTimeout(() => this.flush(), 250);
    }
    this.emit();
  }
  async flush() {
    if (this.saving || !this.snapshot || ['loading', 'conflict'].includes(this.status) || !Object.keys(this.pending).length) return;
    this.saving = true;
    const generation = this.generation;
    const pending = this.pending;
    this.pending = {};
    try {
      const data = await this.request({method: 'POST', headers: {'Content-Type': 'application/json'},
        keepalive: true, body: JSON.stringify({expected_revision: this.snapshot.revision, ...pending})});
      if (generation !== this.generation) return;
      this.snapshot = data;
      // Edits made during the request win in the draft and are sent next in order.
      this.criteria = {...structuredClone(data.criteria), ...this.pending.criteria};
      this.draft = this.pending.draft_message ?? data.draft_message;
      this.status = Object.keys(this.pending).length ? 'saving' : 'saved';
    } catch (error) {
      if (generation !== this.generation) return;
      this.pending = {...pending, ...this.pending,
        ...(pending.criteria || this.pending.criteria ? {criteria: {...pending.criteria, ...this.pending.criteria}} : {})};
      this.status = error.conflict ? 'conflict' : 'error';
    } finally {
      this.saving = false;
      if (generation === this.generation) {
        this.emit();
        }
      if (this.status === 'saving') this.flush();
    }
  }
  accept(data) {
    if (this.saving || Object.keys(this.pending).length || (this.snapshot && data.revision < this.snapshot.revision)) return false;
    this.snapshot = data; this.criteria = structuredClone(data.criteria); this.draft = data.draft_message;
    this.status = 'saved'; this.emit(); return true;
  }
  retry() { this.status = 'saving'; this.emit(); return this.flush(); }
}
window.SearchSession = SearchSession;
})();
