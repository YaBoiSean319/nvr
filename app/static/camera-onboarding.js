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

  return { parseCsvRecords, buildCsvForStatuses, buildRetryCsv, mountAddCameraModes };
});
