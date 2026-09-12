(() => {
  "use strict";

  const STATUS_LABELS = {
    succeeded: "Completed",
    completed: "Completed",
    running: "Running",
    failed: "Failed",
    cancelled: "Cancelled",
    canceled: "Cancelled",
    timed_out: "Timed out",
    timeout: "Timed out",
    queued: "Queued",
    pending: "Pending",
  };

  function number(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatDuration(seconds) {
    const value = Math.max(0, number(seconds) ?? 0);
    if (value < 1) return `${value.toFixed(1)}s`;
    if (value < 60) return `${Math.round(value)}s`;
    const minutes = Math.floor(value / 60);
    const remainder = Math.floor(value % 60);
    if (minutes < 60) return `${minutes}m ${remainder}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }


  function formatLiveDuration(seconds) {
    const value = Math.max(0, Math.floor(number(seconds) ?? 0));
    if (value < 60) return `${value}s`;
    const minutes = Math.floor(value / 60);
    const remainder = value % 60;
    if (minutes < 60) return `${minutes}m ${remainder}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m ${remainder}s`;
  }

  function formatClock(epochSeconds) {
    const value = number(epochSeconds);
    if (value === null) return "";
    return new Date(value * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function normalizeStatus(status) {
    const raw = String(status || "completed").toLowerCase();
    if (raw === "succeeded") return "completed";
    if (raw === "canceled") return "cancelled";
    if (raw === "timeout") return "timed_out";
    return raw;
  }

  function statusLabel(status) {
    const normalized = normalizeStatus(status);
    return STATUS_LABELS[normalized] || normalized.replaceAll("_", " ");
  }

  function statusIcon(status) {
    const normalized = normalizeStatus(status);
    if (normalized === "running") return "●";
    if (normalized === "failed" || normalized === "timed_out") return "!";
    if (normalized === "cancelled") return "×";
    if (normalized === "queued" || normalized === "pending" || normalized === "waiting") return "○";
    return "✓";
  }

  function isRunning(status) {
    return normalizeStatus(status) === "running";
  }

  function appendText(parent, text, className = "") {
    const node = document.createElement("span");
    if (className) node.className = className;
    node.textContent = text;
    parent.append(node);
    return node;
  }

  function renderValue(value, depth = 0) {
    if (value === null || value === undefined) {
      const node = document.createElement("span");
      node.className = "activity-scalar";
      node.textContent = value === null ? "null" : "undefined";
      return node;
    }

    if (typeof value === "boolean" || typeof value === "number") {
      const node = document.createElement("span");
      node.className = "activity-scalar";
      node.textContent = String(value);
      return node;
    }

    if (typeof value === "string") {
      if (value.includes("\n")) {
        const wrapper = document.createElement("div");
        wrapper.className = "activity-pre-wrap";
        const pre = document.createElement("pre");
        pre.className = "activity-pre";
        pre.textContent = value;
        wrapper.append(pre);
        if (navigator.clipboard) {
          const copy = document.createElement("button");
          copy.type = "button";
          copy.className = "activity-copy";
          copy.textContent = "Copy";
          copy.addEventListener("click", async () => {
            try {
              await navigator.clipboard.writeText(value);
              copy.textContent = "Copied";
            } catch (_) {
              copy.textContent = "Copy failed";
            }
          });
          wrapper.append(copy);
        }
        return wrapper;
      }
      const node = document.createElement("span");
      node.className = "activity-text";
      node.textContent = value;
      return node;
    }

    if (depth >= 8) {
      const pre = document.createElement("pre");
      pre.className = "activity-pre";
      pre.textContent = JSON.stringify(value, null, 2);
      return pre;
    }

    if (Array.isArray(value) || typeof value === "object") {
      const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item]) : Object.entries(value);
      if (entries.length === 0) {
        const node = document.createElement("span");
        node.className = "activity-scalar";
        node.textContent = Array.isArray(value) ? "[]" : "{}";
        return node;
      }

      const list = document.createElement("dl");
      list.className = "activity-fields";
      for (const [key, item] of entries) {
        const term = document.createElement("dt");
        term.textContent = key;
        const definition = document.createElement("dd");
        definition.append(renderValue(item, depth + 1));
        list.append(term, definition);
      }
      return list;
    }

    const fallback = document.createElement("span");
    fallback.className = "activity-text";
    fallback.textContent = String(value);
    return fallback;
  }

  class ActivityPanel {
    constructor(root, options = {}) {
      if (!(root instanceof Element)) throw new TypeError("ActivityPanel requires a DOM root element");
      this.root = root;
      this.options = options;
      this.snapshot = options.previousSnapshot || null;
      this.collapsed = Boolean(options.initialCollapsed);
      this.expandedEntryId = options.expandedEntryId || null;
      this.otherJobsExpanded = false;
      this.mediaCache = new Map();
      this.expandedCallDetailKey = null;
      this.liveNodes = [];
      this.hasRenderedRows = false;
      this.retired = false;
      this.summaryFlashTimer = null;
      this._build();
    }

    _build() {
      this.root.replaceChildren();
      this.root.className = "serena-activity-panel";

      this.header = document.createElement("button");
      this.header.type = "button";
      this.header.className = "activity-header";
      this.header.setAttribute("aria-expanded", String(!this.collapsed));

      const logo = document.createElement("span");
      logo.className = "activity-logo";
      logo.setAttribute("aria-hidden", "true");
      logo.innerHTML = '<svg viewBox="0 0 256 256" focusable="false"><rect x="24" y="24" width="208" height="208" rx="48" fill="#fff" stroke="currentColor" stroke-width="12"/><g class="activity-logo-mark"><path class="activity-logo-bracket-left" d="M104 76 64 128l40 52" fill="none" stroke="currentColor" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/><path class="activity-logo-bracket-right" d="M152 76l40 52-40 52" fill="none" stroke="currentColor" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/><path d="M116 128h24" fill="none" stroke="currentColor" stroke-width="18" stroke-linecap="round"/><circle cx="128" cy="128" r="9" fill="currentColor"/></g></svg>';

      const heading = document.createElement("span");
      heading.className = "activity-heading";
      this.titleNode = appendText(heading, "Serena", "activity-title");
      this.summaryStack = document.createElement("span");
      this.summaryStack.className = "activity-summary-stack";
      this.summaryNode = appendText(this.summaryStack, "Waiting for activity", "activity-summary activity-summary-normal");
      this.summaryFlashNode = appendText(this.summaryStack, "", "activity-summary activity-summary-flash");
      heading.append(this.summaryStack);

      const meta = document.createElement("span");
      meta.className = "activity-header-meta";
      this.clockNode = appendText(meta, "", "activity-clock");
      this.durationNode = appendText(meta, "", "activity-header-duration");

      this.chevron = appendText(this.header, this.options.summaryMode ? "›" : "⌄", "activity-chevron");
      this.header.prepend(logo, heading, meta);
      this.root.append(this.header);

      this.header.addEventListener("click", () => {
        if (this.options.summaryMode && typeof this.options.onOpen === "function" && this.snapshot) {
          this.options.onOpen(this.snapshot);
          return;
        }
        this.setCollapsed(!this.collapsed);
        if (this.collapsed && typeof this.options.onCollapse === "function") this.options.onCollapse(this.snapshot);
      });

      if (this.options.summaryMode) {
        this.header.setAttribute("aria-expanded", "false");
        return;
      }

      this._buildBody();
    }

    _buildBody() {
      this.body = document.createElement("div");
      this.body.className = "activity-body";
      this.list = document.createElement("ol");
      this.list.className = "activity-list";
      this.empty = document.createElement("div");
      this.empty.className = "activity-empty";
      this.empty.textContent = "No activity yet.";

      this.otherJobsButton = document.createElement("button");
      this.otherJobsButton.type = "button";
      this.otherJobsButton.className = "activity-other-jobs";
      this.otherJobsButton.hidden = true;
      this.otherJobsList = document.createElement("ol");
      this.otherJobsList.className = "activity-list activity-background-jobs";
      this.otherJobsList.hidden = true;

      this.body.append(this.empty, this.list, this.otherJobsButton, this.otherJobsList);
      this.root.append(this.body);

      this.body.addEventListener("click", event => {
        if (!(event.target instanceof Element)) return;
        const button = event.target.closest(".activity-row-button");
        const row = button?.closest(".activity-row");
        if (!button || !row || !this.body.contains(row)) return;
        const id = row.dataset.entryId;
        if (!id) return;
        const next = this.expandedEntryId === id ? null : id;
        this.setExpandedEntryId(next, { notify: true });
      });
      this.otherJobsButton.addEventListener("click", () => {
        this.otherJobsExpanded = !this.otherJobsExpanded;
        this._renderRows();
        this._notifyHeight();
      });
      this._syncCollapsed();
    }

    promote(options = {}) {
      if (!this.options.summaryMode) return;
      this.options = { ...this.options, ...options, summaryMode: false };
      this.collapsed = false;
      this.chevron.textContent = "⌄";
      this.header.setAttribute("aria-expanded", "true");
      this._buildBody();
      this._notifyHeight();
    }

    demote(options = {}) {
      if (this.options.summaryMode) return;
      this.options = { ...this.options, ...options, summaryMode: true };
      this.collapsed = true;
      this.expandedEntryId = null;
      this.expandedCallDetailKey = null;
      this._clearMediaCache();
      this.otherJobsExpanded = false;
      this.liveNodes = [];
      this.hasRenderedRows = false;
      this.body?.remove();
      this.chevron.textContent = "›";
      this.header.setAttribute("aria-expanded", "false");
      this._notifyHeight();
    }

    setCollapsed(collapsed) {
      if (this.options.summaryMode) return;
      const next = Boolean(collapsed);
      if (this.collapsed === next) return;
      this.collapsed = next;
      this._syncCollapsed();
      if (!next) this._renderRows();
      this._notifyHeight();
    }

    setExpandedEntryId(entryId, { notify = false } = {}) {
      const next = entryId || null;
      if (this.expandedEntryId === next) return;
      const previous = this.expandedEntryId;
      this.expandedEntryId = next;
      this.expandedCallDetailKey = null;
      if (previous !== next) this._releaseMedia(previous);
      if (!this.collapsed && !this.options.summaryMode && this.hasRenderedRows) {
        this._patchExpandedRow(previous, false);
        this._patchExpandedRow(next, true);
      }
      if (notify && typeof this.options.onExpandedChange === "function") {
        this.options.onExpandedChange(next, this._entryForId(next));
      }
      this._notifyHeight();
    }


    _findRow(entryId) {
      if (!entryId) return null;
      for (const list of [this.list, this.otherJobsList]) {
        if (!list) continue;
        for (const row of list.children) {
          if (row.dataset?.entryId === entryId) return row;
        }
      }
      return null;
    }

    _patchExpandedRow(entryId, expanded) {
      const row = this._findRow(entryId);
      if (!row) return;
      const button = row.querySelector(".activity-row-button");
      const chevron = row.querySelector(".activity-row-chevron");
      button?.setAttribute("aria-expanded", String(expanded));
      if (chevron) chevron.textContent = expanded ? "⌄" : "›";
      row.querySelector(".activity-detail")?.remove();
      if (!expanded) return;
      const detailContainer = document.createElement("div");
      detailContainer.className = "activity-detail";
      row.append(detailContainer);
      this._renderExpandedDetail(detailContainer, row.dataset.kind, entryId);
    }

    _refreshExpandedDetail() {
      if (!this.expandedEntryId) return;
      const row = this._findRow(this.expandedEntryId);
      const detailContainer = row?.querySelector(".activity-detail");
      if (!row || !detailContainer) return;

      if (row.dataset.kind === "job") {
        const detail = this.snapshot?.expanded_job;
        if (detail?.job_id === this.expandedEntryId && this._refreshJobDetail(detailContainer, detail)) return;
      } else {
        const detail = this.snapshot?.expanded_call;
        if (detail?.call_id === this.expandedEntryId && JSON.stringify(detail) === this.expandedCallDetailKey) return;
      }

      detailContainer.replaceChildren();
      this._renderExpandedDetail(detailContainer, row.dataset.kind, this.expandedEntryId);
    }

    _hasRunningSessionActivity() {
      if (!this.snapshot) return false;
      if (this.snapshot.active) return true;
      if ((this.snapshot.calls || []).some(call => isRunning(call.status))) return true;
      return (this.snapshot.jobs || []).some(job => {
        if (!isRunning(job.status)) return false;
        if (job.panel_id) return job.panel_id === this.snapshot.panel_id;
        return Boolean(job.current_turn);
      });
    }

    hasLiveActivity() {
      return this._runningEntries().length > 0 || this.liveNodes.length > 0;
    }

    render(snapshot) {
      const previous = this.snapshot;
      this.snapshot = snapshot || {};
      if (this.retired) return;
      this.root.classList.toggle("activity-running", this._hasRunningSessionActivity());
      this._renderHeader();
      this._flashNewActivity(previous);
      if (this.options.summaryMode) {
        this.tick(Date.now() / 1000);
        this._notifyHeight();
        return;
      }
      if (!this.collapsed) {
        const rowsUnchanged = Boolean(
          this.hasRenderedRows
          && previous
          && this._rowsKey(previous) === this._rowsKey(this.snapshot)
        );
        if (rowsUnchanged) this._refreshExpandedDetail();
        else this._renderRows();
      } else {
        this.list.replaceChildren();
        this.otherJobsList.replaceChildren();
        this.liveNodes = [];
        this.hasRenderedRows = false;
      }
      this.tick(Date.now() / 1000);
      this._notifyHeight();
    }

    tick(nowSeconds) {
      const now = number(nowSeconds) ?? Date.now() / 1000;
      for (const item of this.liveNodes) {
        item.node.textContent = formatLiveDuration(now - item.startedAt);
      }
    }

    retire() {
      this.retired = true;
      this.root.classList.add("retired");
      this._notifyHeight();
    }

_releaseMedia(callId) {
      if (!callId) return;
      const asset = this.mediaCache.get(callId);
      if (!asset) return;
      this.mediaCache.delete(callId);
      Promise.resolve(asset)
        .then(resolved => resolved?.dispose?.())
        .catch(() => {});
    }

    _clearMediaCache() {
      for (const callId of [...this.mediaCache.keys()]) this._releaseMedia(callId);
    }

    destroy() {
      if (this.summaryFlashTimer !== null) window.clearTimeout(this.summaryFlashTimer);
      this.summaryFlashTimer = null;
      this.liveNodes = [];
      this._clearMediaCache();
      this.root.replaceChildren();
    }

    _syncCollapsed() {
      this.root.classList.toggle("collapsed", this.collapsed);
      this.header.setAttribute("aria-expanded", String(!this.collapsed));
      this.body.hidden = this.collapsed;
      this.chevron.textContent = this.collapsed ? "›" : "⌄";
    }

    _renderHeader() {
      const snapshot = this.snapshot || {};
      const calls = Array.isArray(snapshot.calls) ? snapshot.calls : [];
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const toolCount = number(snapshot.tool_count) ?? calls.length;
      const jobCount = number(snapshot.job_count) ?? jobs.filter(job => job.current_turn !== false || !snapshot.run_id).length;
      const title = snapshot.session_title || snapshot.display_name || "Serena";
      this.titleNode.textContent = title;

      const latest = snapshot.latest_activity || null;
      const additions = number(snapshot.git_additions) ?? 0;
      const deletions = number(snapshot.git_deletions) ?? 0;
      const ahead = number(snapshot.git_ahead_commits) ?? 0;
      this.summaryNode.replaceChildren();
      appendText(this.summaryNode, `${toolCount} ${toolCount === 1 ? "tool" : "tools"} · ${jobCount} ${jobCount === 1 ? "job" : "jobs"}`);
      if (additions || deletions) {
        appendText(this.summaryNode, " · ");
        appendText(this.summaryNode, `+${additions}`, "activity-git-additions");
        appendText(this.summaryNode, " ");
        appendText(this.summaryNode, `-${deletions}`, "activity-git-deletions");
      }
      if (ahead) {
        appendText(this.summaryNode, " ");
        appendText(this.summaryNode, `(+${ahead})`, "activity-git-ahead");
      }

      this.clockNode.textContent = latest ? formatClock(latest.started_at) : formatClock(snapshot.started_at);
      const span = number(snapshot.submission_span_seconds);
      this.durationNode.textContent = span === null ? "" : formatDuration(span);
      this.durationNode.title = span === null ? "" : "Time between first and latest submitted tool";
    }

    _flashNewActivity(previous) {
      if (!previous) return;

      const previousActivity = previous.latest_activity || null;
      const activity = this.snapshot?.latest_activity || null;
      if (!activity) return;

      const identity = item => [
        number(item?.started_at) ?? null,
        item?.label || "",
        item?.detail || "",
        item?.scope || "",
      ].join("\u0000");
      if (previousActivity && identity(previousActivity) === identity(activity)) return;
      const previousStartedAt = number(previousActivity?.started_at);
      const startedAt = number(activity.started_at);
      if (previousStartedAt !== null && startedAt !== null && startedAt <= previousStartedAt) return;

      const detail = activity.detail || activity.scope || "";
      this.summaryFlashNode.replaceChildren();
      appendText(this.summaryFlashNode, activity.label || "Activity", "activity-summary-flash-label");
      if (detail) appendText(this.summaryFlashNode, ` · ${detail}`, "activity-summary-flash-detail");

      if (this.summaryFlashTimer !== null) window.clearTimeout(this.summaryFlashTimer);
      this.summaryStack.classList.add("is-activity-flash");
      this.summaryFlashTimer = window.setTimeout(() => {
        this.summaryStack.classList.remove("is-activity-flash");
        this.summaryFlashTimer = null;
      }, 1000);
    }

    _rowKey(kind, item) {
      const startedAt = number(item.started_at ?? item.submitted_at);
      const finishedAt = number(item.finished_at);
      if (kind === "job") {
        return JSON.stringify([
          "job",
          item.job_id,
          item.label,
          item.project,
          normalizeStatus(item.status),
          item.status_message,
          startedAt,
          finishedAt,
        ]);
      }
      return JSON.stringify([
        "call",
        item.call_id,
        item.tool_name,
        normalizeStatus(item.status),
        item.scope || item.project_name || "",
        item.detail,
        startedAt,
        finishedAt,
      ]);
    }

    _reconcileRows(list, desiredRows) {
      let cursor = list.firstChild;
      for (const row of desiredRows) {
        if (row === cursor) {
          cursor = cursor.nextSibling;
          continue;
        }
        list.insertBefore(row, cursor);
      }
      while (cursor) {
        const next = cursor.nextSibling;
        cursor.remove();
        cursor = next;
      }
    }

    _rowsKey(snapshot) {
      const calls = Array.isArray(snapshot?.calls) ? snapshot.calls : [];
      const jobs = Array.isArray(snapshot?.jobs) ? snapshot.jobs : [];
      return JSON.stringify({
        calls: calls.map(call => [
          call.call_id,
          call.tool_name,
          normalizeStatus(call.status),
          call.scope,
          call.project_name,
          call.detail,
          call.job_id,
          number(call.started_at ?? call.submitted_at),
          number(call.finished_at),
        ]),
        jobs: jobs.map(job => [
          job.job_id,
          job.label,
          job.project,
          normalizeStatus(job.status),
          job.status_message,
          number(job.started_at ?? job.submitted_at),
          number(job.finished_at),
          Boolean(job.current_turn),
          job.panel_id,
        ]),
      });
    }

    _renderRows() {
      const previousScroll = this.list.scrollTop;
      const followLatest = !this.hasRenderedRows || this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight <= 32;
      const focusedElement = this.root.contains(document.activeElement) ? document.activeElement : null;
      const focusedRow = focusedElement?.closest?.(".activity-row") || null;
      const focusedEntryId = focusedRow?.dataset.entryId || null;
      this.liveNodes = [];

      const snapshot = this.snapshot || {};
      const calls = Array.isArray(snapshot.calls) ? snapshot.calls : [];
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const runMode = Boolean(snapshot.run_id);
      const primaryJobs = runMode ? jobs.filter(job => job.current_turn) : jobs;
      const primaryJobIds = new Set(primaryJobs.map(job => job.job_id));
      const visibleCalls = calls.filter(call => !(call.tool_name === "start_job" && call.job_id && primaryJobIds.has(call.job_id)));
      const backgroundJobs = runMode ? jobs.filter(job => !job.current_turn && isRunning(job.status)) : [];
      const primary = [
        ...visibleCalls.map(call => ({ kind: "call", id: call.call_id, item: call })),
        ...primaryJobs.map(job => ({ kind: "job", id: job.job_id, item: job })),
      ].sort((a, b) => (number(a.item.started_at ?? a.item.submitted_at) ?? 0) - (number(b.item.started_at ?? b.item.submitted_at) ?? 0));

      const existingRows = new Map();
      for (const list of [this.list, this.otherJobsList]) {
        for (const row of list.children) {
          if (!row.dataset?.entryId || !row.dataset?.kind) continue;
          existingRows.set(`${row.dataset.kind}:${row.dataset.entryId}`, row);
        }
      }
      const rowFor = (kind, id, item) => {
        const existing = existingRows.get(`${kind}:${id}`) || null;
        const renderKey = this._rowKey(kind, item);
        if (!existing || existing.dataset.renderKey !== renderKey) return this._renderRow(kind, id, item);

        const startedAt = number(item.started_at ?? item.submitted_at);
        const finishedAt = number(item.finished_at);
        if (startedAt !== null && isRunning(item.status) && finishedAt === null) {
          const elapsed = existing.querySelector(".activity-row-elapsed");
          if (elapsed) this.liveNodes.push({ node: elapsed, startedAt });
        }
        return existing;
      };

      this.empty.hidden = primary.length > 0 || backgroundJobs.length > 0;
      const primaryRows = primary.map(entry => rowFor(entry.kind, entry.id, entry.item));
      this._reconcileRows(this.list, primaryRows);
      if (followLatest) this.list.scrollTop = this.list.scrollHeight;
      else this.list.scrollTop = Math.min(previousScroll, Math.max(0, this.list.scrollHeight - this.list.clientHeight));
      this.hasRenderedRows = true;

      this.otherJobsButton.hidden = backgroundJobs.length === 0;
      this.otherJobsButton.textContent = backgroundJobs.length === 1 ? "1 other job running" : `${backgroundJobs.length} other jobs running`;
      this.otherJobsButton.setAttribute("aria-expanded", String(this.otherJobsExpanded));
      this.otherJobsList.hidden = !this.otherJobsExpanded || backgroundJobs.length === 0;
      const backgroundRows = this.otherJobsExpanded
        ? backgroundJobs.map(job => rowFor("job", job.job_id, job))
        : [];
      this._reconcileRows(this.otherJobsList, backgroundRows);
      this._refreshExpandedDetail();

      if (focusedElement && !focusedElement.isConnected && focusedEntryId) {
        this._findRow(focusedEntryId)?.querySelector(".activity-row-button")?.focus({ preventScroll: true });
      }
    }

    _renderRow(kind, id, item) {
      const row = document.createElement("li");
      row.className = "activity-row";
      row.dataset.entryId = id;
      row.dataset.kind = kind;
      row.dataset.renderKey = this._rowKey(kind, item);
      const normalizedStatus = normalizeStatus(item.status);
      row.dataset.status = normalizedStatus;

      const button = document.createElement("button");
      button.type = "button";
      button.className = "activity-row-button";
      button.setAttribute("aria-expanded", String(this.expandedEntryId === id));

      const status = document.createElement("span");
      status.className = "activity-status";
      status.textContent = statusIcon(normalizedStatus);
      status.setAttribute("aria-label", statusLabel(normalizedStatus));
      status.title = statusLabel(normalizedStatus);

      const copy = document.createElement("span");
      copy.className = "activity-row-copy";
      const titleLine = document.createElement("span");
      titleLine.className = "activity-row-title-line";
      const label = kind === "job" ? (item.label || "Job") : (item.tool_name || "Tool");
      appendText(titleLine, label, "activity-row-title");
      const scope = kind === "job" ? (item.project || "") : (item.scope || item.project_name || "");
      if (scope) appendText(titleLine, scope, "activity-row-scope");
      const detail = kind === "job" ? (item.status_message || "JOB") : (item.detail || "");
      const detailNode = appendText(copy, detail, "activity-row-detail");
      copy.prepend(titleLine);

      const timing = document.createElement("span");
      timing.className = "activity-row-timing";
      const startedAt = number(item.started_at ?? item.submitted_at);
      appendText(timing, formatClock(startedAt), "activity-row-clock");
      const elapsed = appendText(timing, "", "activity-row-elapsed");
      const finishedAt = number(item.finished_at);
      if (startedAt !== null) {
        if (isRunning(normalizedStatus) && finishedAt === null) {
          this.liveNodes.push({ node: elapsed, startedAt });
        } else if (finishedAt !== null) {
          elapsed.textContent = formatDuration(finishedAt - startedAt);
        }
      }

      const chevron = appendText(button, this.expandedEntryId === id ? "⌄" : "›", "activity-row-chevron");
      button.prepend(status, copy, timing);
      row.append(button);


      if (this.expandedEntryId === id) {
        const detailContainer = document.createElement("div");
        detailContainer.className = "activity-detail";
        row.append(detailContainer);
        this._renderExpandedDetail(detailContainer, kind, id);
      }
      return row;
    }

    _renderExpandedDetail(container, kind, id) {
      const snapshot = this.snapshot || {};
      const detail = kind === "call" ? snapshot.expanded_call : snapshot.expanded_job;
      const detailId = kind === "call" ? detail?.call_id : detail?.job_id;
      if (!detail || detailId !== id) {
        if (kind === "call") this.expandedCallDetailKey = null;
        container.textContent = "Loading…";
        return;
      }

      if (kind === "call") {
        this._renderCallDetail(container, detail);
        this.expandedCallDetailKey = JSON.stringify(detail);
      } else {
        this.expandedCallDetailKey = null;
        this._renderJobDetail(container, detail);
      }
    }

    _renderCallDetail(container, detail) {
      const parameters = this._appendDetailSection(container, "Parameters", renderValue(detail.arguments ?? {}));
      parameters.classList.add("activity-detail-parameters");

      if (detail.error) {
        const error = document.createElement("pre");
        error.className = "activity-pre activity-error";
        error.textContent = detail.error;
        this._appendDetailSection(container, "Error", error);
      } else if (detail.structured_result !== null && detail.structured_result !== undefined) {
        const result = this._appendDetailSection(container, "Result", renderValue(detail.structured_result));
        result.classList.add("activity-detail-result");
      } else if (detail.result !== null && detail.result !== undefined && detail.result !== "") {
        const result = this._appendDetailSection(container, "Result", renderValue(detail.result));
        result.classList.add("activity-detail-result");
      }

      if (detail.media) {
        const mediaContainer = document.createElement("div");
        mediaContainer.className = "activity-media";
        this._appendDetailSection(container, "Media", mediaContainer);
        this._loadMedia(detail.call_id, detail.media, mediaContainer);
      }
    }

    _jobMetadata(detail) {
      const metadata = {
        status: detail.status_message || detail.status,
        cwd: detail.cwd,
        return_code: detail.return_code,
        timeout_seconds: detail.timeout_seconds,
        memory_bytes: detail.memory_bytes,
        cpu_seconds: detail.cpu_seconds,
        process_count: detail.process_count,
      };
      const compact = Object.fromEntries(Object.entries(metadata).filter(([, value]) => value !== null && value !== undefined && value !== ""));
      const content = renderValue(compact);
      content.classList.add("activity-job-metadata");
      return content;
    }

    _renderJobDetail(container, detail) {
      this._appendDetailSection(container, "Job", this._jobMetadata(detail));
      if (detail.output) {
        const output = document.createElement("pre");
        output.className = "activity-pre activity-job-output";
        output.textContent = detail.output;
        const section = this._appendDetailSection(container, detail.earlier_output_omitted ? "Recent output" : "Output", output);
        section.classList.add("activity-job-output-section");
      }
    }

    _refreshJobDetail(container, detail) {
      const metadata = container.querySelector(".activity-job-metadata");
      if (!metadata) return false;
      metadata.replaceWith(this._jobMetadata(detail));

      const outputSection = container.querySelector(".activity-job-output-section");
      const output = outputSection?.querySelector(".activity-job-output");
      if (!detail.output) {
        outputSection?.remove();
        return true;
      }

      if (!outputSection || !output) {
        const nextOutput = document.createElement("pre");
        nextOutput.className = "activity-pre activity-job-output";
        nextOutput.textContent = detail.output;
        const nextSection = this._appendDetailSection(
          container,
          detail.earlier_output_omitted ? "Recent output" : "Output",
          nextOutput,
        );
        nextSection.classList.add("activity-job-output-section");
        return true;
      }

      const heading = outputSection.querySelector("h4");
      if (heading) heading.textContent = detail.earlier_output_omitted ? "Recent output" : "Output";
      const previousOutput = output.textContent || "";
      if (detail.output !== previousOutput) {
        if (detail.output.startsWith(previousOutput)) {
          output.append(document.createTextNode(detail.output.slice(previousOutput.length)));
        } else {
          output.textContent = detail.output;
        }
      }
      return true;
    }

    _appendDetailSection(container, title, content) {
      const section = document.createElement("section");
      section.className = "activity-detail-section";
      const heading = document.createElement("h4");
      heading.textContent = title;
      section.append(heading, content);
      container.append(section);
      return section;
    }

    async _loadMedia(callId, media, container) {
      if (typeof this.options.loadMedia !== "function") {
        container.textContent = media.name || media.mime_type || "Media available";
        return;
      }
      let asset = this.mediaCache.get(callId);
      if (!asset) {
        asset = Promise.resolve(this.options.loadMedia(callId, media));
        this.mediaCache.set(callId, asset);
      }

      try {
        const resolved = await asset;
        if (this.expandedEntryId !== callId || !container.isConnected) return;
        container.replaceChildren(this._mediaNode(resolved, media));
        this._notifyHeight();
      } catch (error) {
        if (this.mediaCache.get(callId) === asset) this.mediaCache.delete(callId);
        container.textContent = error instanceof Error ? error.message : "Could not load media";
      }
    }

    _mediaNode(asset, media) {
      const type = asset?.type || media?.media_type || "file";
      const src = asset?.src || asset?.url || "";
      if (type === "image" && src) {
        const image = document.createElement("img");
        image.className = "activity-image";
        image.src = src;
        image.alt = asset?.name || media?.name || "Serena result image";
        image.addEventListener("load", () => this._notifyHeight(), { once: true });
        return image;
      }
      if (type === "audio" && src) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.src = src;
        return audio;
      }
      const link = document.createElement("a");
      link.className = "activity-file";
      link.href = src || media?.uri || "#";
      link.textContent = asset?.name || media?.name || "Open file";
      link.target = "_blank";
      link.rel = "noreferrer";
      return link;
    }

    _entryForId(id) {
      if (!id || !this.snapshot) return null;
      const call = (this.snapshot.calls || []).find(item => item.call_id === id);
      if (call) return { kind: "call", item: call };
      const job = (this.snapshot.jobs || []).find(item => item.job_id === id);
      if (job) return { kind: "job", item: job };
      return null;
    }

    _runningEntries() {
      if (!this.snapshot) return [];
      const entries = [...(this.snapshot.calls || []), ...(this.snapshot.jobs || [])];
      if (this.snapshot.latest_activity) entries.push(this.snapshot.latest_activity);
      return entries.filter(item => isRunning(item.status));
    }

    _notifyHeight() {
      if (typeof this.options.onHeightChange === "function") this.options.onHeightChange();
    }
  }

  window.SerenaActivity = Object.freeze({ ActivityPanel, formatDuration, formatLiveDuration, formatClock, renderValue });
})();
