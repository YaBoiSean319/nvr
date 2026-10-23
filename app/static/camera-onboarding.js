(function installCameraOnboarding(root, createModule) {
  const onboarding = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = onboarding;
  root.NvrCameraOnboarding = onboarding;
})(typeof window === "undefined" ? globalThis : window, function createCameraOnboarding() {
  function parseCsvRecords(value) {
    const text = String(value || "").replace(/^\uFEFF/, "");
    const records = [];
    let fields = [];
    let field = "";
    let quoted = false;
    let afterQuote = false;

    function finishField() {
      fields.push(field);
      field = "";
      afterQuote = false;
    }

    function finishRecord() {
      finishField();
      if (fields.some(item => item.trim())) records.push(fields);
      fields = [];
    }

    for (let index = 0; index < text.length; index += 1) {
      const character = text[index];
      if (quoted) {
        if (character === '"') {
          if (text[index + 1] === '"') {
            field += '"';
            index += 1;
          } else {
            quoted = false;
            afterQuote = true;
          }
        } else {
          field += character;
        }
        continue;
      }

      if (character === ",") {
        finishField();
      } else if (character === "\n" || character === "\r") {
        if (character === "\r" && text[index + 1] === "\n") index += 1;
        finishRecord();
      } else if (character === '"' && !field && !afterQuote) {
        quoted = true;
      } else if (afterQuote) {
        if (character === " " || character === "\t") continue;
        throw new Error("Malformed CSV: unexpected text after a quoted field.");
      } else {
        field += character;
      }
    }

    if (quoted) throw new Error("Malformed CSV: an opening quote is not closed.");
    if (field || fields.length || afterQuote) finishRecord();
    return records;
  }

  function encodeCsvRecord(record) {
    return record.map(value => {
      const text = String(value ?? "");
      return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
    }).join(",");
  }

  function buildCsvForStatuses(csvText, resultRows, statuses) {
    const records = parseCsvRecords(csvText);
    if (records.length < 2) throw new Error("CSV must contain a header and at least one camera row.");
    const selectedStatuses = new Set(statuses || []);
    const selectedRows = new Set((resultRows || [])
      .filter(result => selectedStatuses.has(result.status))
      .map(result => Number(result.row))
      .filter(row => Number.isInteger(row) && row >= 2));
    if (!selectedRows.size) return "";

    const selected = records.slice(1).filter((record, index) => selectedRows.has(index + 2));
    if (!selected.length) return "";
    return [encodeCsvRecord(records[0]), ...selected.map(encodeCsvRecord)].join("\n");
  }

  function buildRetryCsv(csvText, resultRows) {
    return buildCsvForStatuses(csvText, resultRows, ["failed"]);
  }

  function mountAddCameraModes(root, { discovery, manualForm, csvPanel, manualStatus } = {}, callbacks = {}) {
    if (!root) return null;
    const modes = [
      { id: "discover", label: "Discover", panelId: "camera-discovery-mode", content: discovery },
      { id: "manual", label: "Manual Add", panelId: "camera-manual-mode", content: manualForm },
      { id: "csv", label: "CSV Import", panelId: "camera-csv-mode", content: csvPanel },
    ];
    const navigation = document.createElement("nav");
    navigation.id = "camera-add-mode-nav";
    navigation.className = "camera-add-mode-nav";
    navigation.setAttribute("role", "tablist");
    navigation.setAttribute("aria-label", "Add cameras by discovery, manual setup, or CSV import");
    const buttons = new Map();
    const panels = new Map();
    const createInventoryAction = id => {
      const wrapper = document.createElement("div");
      wrapper.className = "camera-add-view-actions";
      const button = document.createElement("button");
      button.id = id;
      button.type = "button";
      button.textContent = "View Cameras";
      button.classList.add("hidden");
      button.addEventListener("click", () => callbacks.onOpenInventory?.());
      wrapper.append(button);
      return { wrapper, button };
    };

    for (const mode of modes) {
      const button = document.createElement("button");
      button.type = "button";
      button.id = `camera-add-tab-${mode.id}`;
      button.textContent = mode.label;
      button.dataset.cameraAddMode = mode.id;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", mode.panelId);
      button.setAttribute("aria-selected", "false");
      button.tabIndex = -1;
      buttons.set(mode.id, button);
      navigation.append(button);

      const panel = document.createElement("section");
      panel.id = mode.panelId;
      panel.className = "camera-add-mode-panel";
      panel.dataset.cameraAddModePanel = mode.id;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", button.id);
      panel.tabIndex = 0;
      panels.set(mode.id, panel);
      if (mode.content) panel.append(mode.content);
    }

    const manualPanel = panels.get("manual");
    if (manualStatus) manualPanel.append(manualStatus);
    const importMessage = document.createElement("p");
    importMessage.id = "camera-import-message";
    importMessage.setAttribute("role", "status");
    importMessage.setAttribute("aria-live", "polite");
    const csvActions = createInventoryAction("camera-csv-open-inventory");
    const manualActions = createInventoryAction("camera-manual-open-inventory");
    panels.get("csv").append(importMessage, csvActions.wrapper);
    manualPanel.append(manualActions.wrapper);

    root.append(navigation, ...panels.values());
    let activeMode = "discover";

    function setMode(id) {
      if (!panels.has(id)) return false;
      for (const [modeId, button] of buttons) {
        const active = modeId === id;
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
        panels.get(modeId).classList.toggle("hidden", !active);
      }
      activeMode = id;
      csvActions.button.classList.add("hidden");
      manualActions.button.classList.add("hidden");
      return true;
    }

    async function requestMode(id) {
      if (!panels.has(id) || id === activeMode) return false;
      if (callbacks.onBeforeModeChange && !await callbacks.onBeforeModeChange(activeMode, id, panels.get(activeMode))) return false;
      setMode(id);
      return true;
    }

    for (const [id, button] of buttons) {
      button.addEventListener("click", () => void requestMode(id));
      button.addEventListener("keydown", event => {
        const order = modes.map(mode => mode.id);
        const index = order.indexOf(id);
        const next = event.key === "ArrowRight" ? order[(index + 1) % order.length]
          : event.key === "ArrowLeft" ? order[(index + order.length - 1) % order.length]
            : event.key === "Home" ? order[0]
              : event.key === "End" ? order.at(-1) : null;
        if (!next) return;
        event.preventDefault();
        buttons.get(next).focus();
        void requestMode(next);
      });
    }

    setMode("discover");
    return {
      setMode,
      requestMode,
      get activeMode() { return activeMode; },
      getPanel(mode) { return panels.get(mode) || null; },
      showInventoryAction(mode) {
        const target = mode === "manual" ? manualActions.button : csvActions.button;
        target.classList.remove("hidden");
      },
      clearInventoryActions() {
        csvActions.button.classList.add("hidden");
        manualActions.button.classList.add("hidden");
      },
    };
  }

  function mountCsvImport(root, { message, request, refresh, clearDraft, showInventoryAction } = {}) {
    if (!root) return null;
    const input = root.querySelector("#camera-import-csv");
    const previewButton = root.querySelector("#preview-import");
    const applyButton = root.querySelector("#apply-import");
    const retryButton = root.querySelector("#retry-import-failed");
    const reconcileButton = root.querySelector("#reconcile-import");
    const results = root.querySelector("#import-preview");
    const rowDetails = root.querySelector("#import-row-details");
    if (!input || !previewButton || !applyButton || !retryButton || !reconcileButton || !results) return null;

    const status = message || document.querySelector("#camera-import-message");
    let requestGeneration = 0;
    let applyInProgress = false;
    let rows = [];
    let sourceCsv = "";
    let pendingCsv = "";
    let pendingRowMap = [];
    let outcomeUnknown = false;
    let retryPrepared = false;
    let previewFresh = false;
    let canApply = false;
    let lastPreviewCsv = "";

    function importStatusLabel(state) {
      return ({ accepted: "Ready to import", created: "Added", duplicate: "Already exists", invalid: "Needs correction", failed: "Import failed" })[state] || state;
    }

    function mergeRows(current, updates) {
      const merged = new Map((current || []).map(row => [Number(row.row), row]));
      for (const row of updates || []) merged.set(Number(row.row), row);
      return [...merged.values()].sort((left, right) => Number(left.row) - Number(right.row));
    }

    function showRowDetails(row) {
      if (!rowDetails) return;
      rowDetails.replaceChildren();
      const heading = document.createElement("h4"); heading.textContent = `CSV row ${row.row} · ${row.name || "Unnamed"}`; rowDetails.append(heading);
      const state = document.createElement("p"); state.textContent = importStatusLabel(row.status); rowDetails.append(state);
      const errors = Array.isArray(row.errors) ? row.errors : [];
      if (errors.length) {
        const list = document.createElement("ul");
        for (const error of errors) { const item = document.createElement("li"); item.textContent = error; list.append(item); }
        rowDetails.append(list);
      } else {
        const note = document.createElement("p"); note.textContent = "No additional row detail."; rowDetails.append(note);
      }
    }

    function renderRows() {
      results.replaceChildren();
      const summary = document.createElement("p"); summary.id = "import-summary"; summary.setAttribute("role", "status"); summary.setAttribute("aria-live", "polite");
      const counts = { created: 0, failed: 0, accepted: 0, duplicate: 0, invalid: 0 };
      for (const row of rows) if (Object.hasOwn(counts, row.status)) counts[row.status] += 1;
      summary.textContent = rows.length
        ? `${counts.created} added · ${counts.failed} failed · ${counts.accepted} ready · ${counts.duplicate} duplicates · ${counts.invalid} invalid`
        : "Preview CSV rows to see eligible, duplicate, and invalid entries.";
      results.append(summary);
      if (!rows.length) return;
      const scroll = document.createElement("div"); scroll.className = "camera-import-results-scroll"; scroll.tabIndex = 0; scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", "Camera CSV import results");
      const table = document.createElement("table"); table.className = "camera-import-results-table";
      const caption = document.createElement("caption"); caption.textContent = "Camera CSV import results"; table.append(caption);
      const head = document.createElement("thead"), header = document.createElement("tr");
      for (const label of ["CSV row", "Camera", "Group", "Result", "Details"]) { const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = label; header.append(cell); }
      head.append(header); table.append(head);
      const body = document.createElement("tbody");
      for (const row of rows) {
        const tr = document.createElement("tr"); tr.dataset.importStatus = row.status;
        const number = document.createElement("th"); number.scope = "row"; number.textContent = String(row.row); tr.append(number);
        const name = document.createElement("td"); name.textContent = row.name || "Unnamed"; tr.append(name);
        const group = document.createElement("td"); group.textContent = row.group_name || "No group"; tr.append(group);
        const result = document.createElement("td"); result.textContent = importStatusLabel(row.status); result.dataset.state = row.status; tr.append(result);
        const detail = document.createElement("td"), button = document.createElement("button"); button.type = "button"; button.textContent = "View details"; button.setAttribute("aria-label", `View details for CSV row ${row.row}, ${row.name || "Unnamed"}`); button.addEventListener("click", () => showRowDetails(row)); detail.append(button); tr.append(detail);
        body.append(tr);
      }
      table.append(body); scroll.append(table); results.append(scroll);
    }

    function setActions({ applying = false, retrying = false, unknown = outcomeUnknown } = {}) {
      input.disabled = unknown || applying || retrying;
      previewButton.disabled = applying || retrying || unknown;
      applyButton.disabled = applying || retrying || unknown || !canApply;
      const failed = rows.some(row => row.status === "failed");
      retryButton.classList.toggle("hidden", unknown || !(retryPrepared || previewFresh && failed));
      retryButton.disabled = applying || retrying;
      reconcileButton.classList.toggle("hidden", !unknown);
      reconcileButton.disabled = applying || retrying;
    }

    async function preview({ csvText = input.value, reconcile = false, rowMap = null } = {}) {
      if (applyInProgress) return false;
      if (outcomeUnknown && !reconcile) { status.textContent = "Reconcile the previous import outcome before starting another preview."; return false; }
      const generation = ++requestGeneration;
      status.textContent = reconcile ? "Checking the prior import against the current inventory…" : "Validating CSV rows…";
      setActions({ applying: true, unknown: outcomeUnknown });
      try {
        const response = await request("/api/cameras/import/preview", { method: "POST", body: JSON.stringify({ csv_text: csvText }) });
        if (generation !== requestGeneration) return false;
        if (!reconcile && csvText !== input.value) return false;
        const mapping = rowMap || response.rows.map(row => row.row);
        const mapped = response.rows.map((row, index) => ({ ...row, row: mapping[index] ?? row.row }));
        if (reconcile) {
          rows = mergeRows(rows, mapped);
          outcomeUnknown = false;
          const accepted = response.rows.map((row, index) => ({ row, index })).filter(item => item.row.status === "accepted");
          pendingCsv = buildCsvForStatuses(csvText, response.rows, ["accepted"]);
          pendingRowMap = accepted.map(item => mapping[item.index] ?? item.row.row);
          canApply = false;
          retryPrepared = Boolean(pendingCsv); previewFresh = true;
          status.textContent = pendingCsv
            ? "Inventory checked. Only rows still eligible for addition are ready to retry."
            : "Inventory checked. No unconfirmed row is eligible to add again.";
          if (mapped.some(row => row.status === "duplicate")) {
            showInventoryAction?.();
            try { await refresh?.(); } catch {}
          }
          if (!pendingCsv && !rows.some(row => ["accepted", "failed", "invalid"].includes(row.status))) {
            input.value = ""; sourceCsv = ""; lastPreviewCsv = "";
            clearDraft?.(root);
          }
          retryButton.textContent = "Retry unconfirmed rows";
          retryButton.classList.toggle("hidden", !pendingCsv);
          applyButton.disabled = true;
        } else {
          sourceCsv = csvText;
          rows = mapped;
          lastPreviewCsv = csvText;
          retryPrepared = false; previewFresh = true;
          const accepted = response.rows.filter(row => row.status === "accepted");
          pendingCsv = buildCsvForStatuses(csvText, response.rows, ["accepted"]);
          pendingRowMap = accepted.map(row => row.row);
          canApply = Boolean(pendingCsv);
          status.textContent = `Preview: ${response.accepted_count} eligible, ${response.rejected_count} duplicate or invalid. Only eligible rows will be sent for testing and import.`;
          retryButton.textContent = "Retry failed rows";
          retryButton.classList.add("hidden");
          if (csvText !== "" && response.accepted_count === 0) status.textContent += " No rows are eligible to add.";
        }
        renderRows();
        setActions({ unknown: false });
        return true;
      } catch (error) {
        if (generation === requestGeneration) { status.textContent = error.message; if (!reconcile) previewFresh = false; }
        return false;
      } finally {
        if (generation === requestGeneration) setActions({ unknown: outcomeUnknown });
      }
    }

    async function apply({ duringGuard = false } = {}) {
      if (applyInProgress) return false;
      const csvText = pendingCsv, rowMap = [...pendingRowMap];
      if (!canApply || !csvText) { status.textContent = "Preview the current CSV again before importing."; return false; }
      if (!duringGuard && !outcomeUnknown && lastPreviewCsv !== input.value) { status.textContent = "The CSV changed after preview. Preview the current text before importing."; return false; }
      const generation = ++requestGeneration;
      setActions({ applying: true, unknown: false });
      applyInProgress = true;
      status.textContent = `Testing and importing ${rowMap.length} reviewed eligible camera row(s)…`;
      try {
        const response = await request("/api/cameras/import/apply", { method: "POST", body: JSON.stringify({ csv_text: csvText }) });
        if (generation !== requestGeneration) return false;
        const mapped = response.rows.map((row, index) => ({ ...row, row: rowMap[index] ?? row.row }));
        rows = mergeRows(rows, mapped);
        pendingCsv = ""; pendingRowMap = []; canApply = false; outcomeUnknown = false;
        retryPrepared = false; previewFresh = true;
        const attention = rows.filter(row => !["created", "duplicate"].includes(row.status));
        status.textContent = `Added ${response.created_count} camera(s); ${attention.length} row(s) still need attention. Stream credentials are not shown.`;
        if (response.created_count > 0) { showInventoryAction?.(); await refresh?.(); }
        if (!attention.length) {
          clearDraft?.(root);
          input.value = ""; sourceCsv = ""; lastPreviewCsv = "";
          status.textContent += " The source CSV was cleared; row results are retained.";
        }
        retryButton.textContent = "Retry failed rows";
        renderRows(); setActions({ unknown: false });
        return attention.length === 0;
      } catch (error) {
        if (generation === requestGeneration) {
          if (error instanceof TypeError) {
            outcomeUnknown = true;
            retryPrepared = false; previewFresh = false;
            reconcileButton.textContent = "Reconcile import outcome";
            status.textContent = "The import response was interrupted. Its outcome is unknown. Reconcile against the current inventory before retrying.";
          } else status.textContent = error.message;
          setActions({ unknown: outcomeUnknown });
        }
        return false;
      } finally {
        applyInProgress = false;
        if (generation === requestGeneration) setActions({ unknown: outcomeUnknown });
      }
    }

    async function retryFailed() {
      if (outcomeUnknown) { status.textContent = "Reconcile the previous import outcome before retrying."; return false; }
      if (retryPrepared) canApply = Boolean(pendingCsv);
      else if (!pendingCsv) {
        pendingCsv = buildRetryCsv(sourceCsv, rows);
        pendingRowMap = rows.filter(row => row.status === "failed").map(row => row.row);
        canApply = Boolean(pendingCsv);
      }
      return apply();
    }

    function discardDraft() {
      requestGeneration += 1;
      input.value = ""; rows = []; sourceCsv = ""; pendingCsv = ""; pendingRowMap = [];
      canApply = false; outcomeUnknown = false; retryPrepared = false; previewFresh = false; lastPreviewCsv = "";
      clearDraft?.(root);
      input.disabled = false;
      results.replaceChildren(); rowDetails?.replaceChildren();
      retryButton.classList.add("hidden"); reconcileButton.classList.add("hidden");
      applyButton.disabled = true;
      status.textContent = "CSV import draft discarded.";
    }

    previewButton.addEventListener("click", () => void preview());
    applyButton.addEventListener("click", () => void apply());
    retryButton.addEventListener("click", () => void retryFailed());
    reconcileButton.addEventListener("click", () => void preview({ csvText: pendingCsv, reconcile: true, rowMap: pendingRowMap }));
    input.addEventListener("input", () => {
      requestGeneration += 1;
      if (outcomeUnknown) return;
      canApply = false; pendingCsv = ""; pendingRowMap = []; lastPreviewCsv = ""; retryPrepared = false; previewFresh = false;
      status.textContent = "CSV changed. Preview this text before importing.";
      setActions({ unknown: false });
    });

    setActions();
    return {
      get applying() { return applyInProgress; },
      get outcomeUnknown() { return outcomeUnknown; },
      isPreviewReadyForCurrentInput() { return canApply && lastPreviewCsv === input.value; },
      preview,
      apply,
      discardDraft,
    };
  }

  return { parseCsvRecords, buildCsvForStatuses, buildRetryCsv, mountAddCameraModes, mountCsvImport };
});
