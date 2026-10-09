// One place to track unsaved editor state across project tabs. Tab switches do
// not discard changes; navigation and background refreshes must preserve them.
var projectEdits = (function () {
  var readers = new Map();
  var submitting = null;
  var initialDisabled = new WeakMap();
  function dirty() {
    return Array.from(readers).some(function (entry) {
      return entry[0] !== submitting && entry[1]();
    });
  }
  function update() {
    var changed = dirty();
    document.querySelectorAll('[data-requires-saved-manual]').forEach(function (button) {
      if (!initialDisabled.has(button)) initialDisabled.set(button, button.disabled);
      button.disabled = initialDisabled.get(button) || changed;
    });
    var notice = document.getElementById('unsaved-changes');
    if (notice) notice.classList.toggle('hidden', !changed);
  }
  document.addEventListener('input', function () { queueMicrotask(update); });
  document.addEventListener('change', function () { queueMicrotask(update); });
  document.addEventListener('click', function () { queueMicrotask(update); }, true);
  document.addEventListener('submit', function (event) {
    queueMicrotask(function () {
      if (!event.defaultPrevented) submitting = event.target;
    });
  });
  window.addEventListener('beforeunload', function (event) {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = '';
    // If navigation is cancelled, the next attempt must check every form again.
    submitting = null;
  });
  return {
    register: function (form, reader) { readers.set(form, reader); update(); },
    update: update,
    dirty: dirty,
    refresh: function () {
      if (!dirty()) { window.location.reload(); return; }
      var notice = document.getElementById('background-update');
      if (notice) notice.classList.remove('hidden');
    }
  };
})();

// Theme toggle (System/Light/Dark). Mirrors the inline pre-paint script in
// base.html so the two never disagree about resolution logic; this instance
// also wires up the dropdown's click handlers and keeps them synced when the
// OS preference changes while "System" is selected.
(function () {
  var THEME_KEY = "meta-coder-theme";
  var systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

  function savedTheme() {
    var value = "system";
    try {
      value = localStorage.getItem(THEME_KEY) || "system";
    } catch (_) {
      /* private browsing / storage disabled */
    }
    return ["system", "light", "dark"].indexOf(value) >= 0 ? value : "system";
  }

  function applyTheme(preference, persist) {
    var resolved = preference === "system" ? (systemTheme.matches ? "dark" : "light") : preference;
    document.documentElement.classList.toggle("dark", resolved === "dark");
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = resolved;
    if (persist) {
      try {
        localStorage.setItem(THEME_KEY, preference);
      } catch (_) {
        /* ignore */
      }
    }
    document.querySelectorAll(".js-theme-option").forEach(function (option) {
      option.setAttribute("aria-checked", option.dataset.themeValue === preference ? "true" : "false");
    });
  }

  applyTheme(savedTheme(), false);
  document.querySelectorAll(".js-theme-option").forEach(function (option) {
    option.addEventListener("click", function () {
      applyTheme(option.dataset.themeValue, true);
    });
  });
  if (systemTheme.addEventListener) {
    systemTheme.addEventListener("change", function () {
      if (savedTheme() === "system") applyTheme("system", false);
    });
  }
})();

// Dropzone behavior for file inputs. Progressive enhancement: each dropzone is a
// <label> wrapping a real <input type="file">, so click-to-browse works with no JS
// at all. Choosing or dropping a file submits the zone's form immediately — no
// separate upload button — via requestSubmit() so any onsubmit confirm() dialog
// and native required-field validation still run (unlike the older .submit()).
(function () {
  function updateLabel(input, zone) {
    var preview = zone.querySelector("[data-dropzone-filename]");
    if (!preview) return;
    var files = input.files;
    if (!files || files.length === 0) {
      preview.textContent = "";
    } else if (files.length === 1) {
      preview.textContent = files[0].name;
    } else {
      preview.textContent = files.length + " files selected";
    }
  }

  function filesChosen(input, zone) {
    if (input.disabled) return;
    updateLabel(input, zone);
    if (!input.files || !input.files.length) return;
    var form = zone.closest("form");
    if (form) form.requestSubmit();
  }

  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector('input[type="file"]');
    if (!input) return;

    updateLabel(input, zone);
    input.addEventListener("change", function () {
      filesChosen(input, zone);
    });

    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (!input.disabled) zone.classList.add("dragging");
      });
    });

    ["dragleave", "dragend", "drop"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove("dragging");
      });
    });

    zone.addEventListener("drop", function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!input.disabled && files && files.length) {
        input.files = files;
        filesChosen(input, zone);
      }
    });
  });
})();

// Structured coding-manual editor. The manual is only ever edited through this
// UI — see plan.md "Problem 1": "I do not want the user to edit the yaml schema
// directly". This module renders form controls bound to an in-memory JS object
// (seeded from the server) and, on submit, serializes that object to JSON into a
// hidden field; the server rebuilds the CodingManual and writes the YAML file —
// this script never generates YAML itself.
(function () {
  var dataEl = document.getElementById("manual-editor-data");
  if (!dataEl) return; // manual couldn't be loaded — reset-to-default UI shown instead

  var state = JSON.parse(dataEl.textContent);
  state.effects = state.effects || [];

  // Matches meta_coder/manual.py's NOTES_FIELD_NAME — the server forces this
  // field onto every manual and discards any edits to it, so the editor locks
  // it too rather than letting a user "successfully" edit something that gets
  // silently reverted on save.
  var RESERVED_EFFECT_FIELD_NAME = "notes";

  function cloneTemplate(id) {
    return document.getElementById(id).content.firstElementChild.cloneNode(true);
  }

  function bindText(container, key, obj, multiline) {
    var input = container.querySelector('[data-field="' + key + '"]');
    if (!input) return;
    input.value = obj[key] == null ? "" : obj[key];
    input.addEventListener("input", function () {
      obj[key] = input.value;
    });
  }

  function bindCheckbox(container, key, obj, defaultValue) {
    var input = container.querySelector('[data-field="' + key + '"]');
    if (!input) return;
    input.checked = obj[key] === undefined ? defaultValue : !!obj[key];
    obj[key] = input.checked;
    input.addEventListener("change", function () {
      obj[key] = input.checked;
    });
  }

  function renderLevel(field, level, listEl) {
    var row = cloneTemplate("tmpl-level");
    bindText(row, "value", level);
    bindText(row, "description", level);
    row.querySelector("[data-remove-row]").addEventListener("click", function () {
      var idx = field.levels.indexOf(level);
      if (idx >= 0) field.levels.splice(idx, 1);
      row.remove();
    });
    listEl.appendChild(row);
  }

  // `openByDefault` is true only for a field just added this session — a manual
  // with dozens of fields (a real coding manual easily has 30+) should load with
  // every field collapsed, not one huge scroll of open blocks.
  function renderEffectField(field, openByDefault) {
    field.levels = field.levels || [];
    var block = cloneTemplate("tmpl-effect-field");
    var locked = field.name === RESERVED_EFFECT_FIELD_NAME;
    if (openByDefault) block.open = true;

    var summaryName = block.querySelector("[data-summary-name]");
    var summaryType = block.querySelector("[data-summary-type]");
    function syncSummary() {
      summaryName.textContent = field.name || "Untitled field";
      summaryType.textContent = field.type || "string";
    }

    bindText(block, "name", field);
    block.querySelector('[data-field="name"]').addEventListener("input", syncSummary);
    bindText(block, "description", field);
    bindCheckbox(block, "evidence_required", field, true);

    var typeSelect = block.querySelector('[data-field="type"]');
    typeSelect.value = field.type || "string";
    field.type = typeSelect.value;
    var levelsSection = block.querySelector("[data-levels-section]");
    var levelsList = block.querySelector("[data-levels-list]");

    function refreshLevelsVisibility() {
      if (typeSelect.value === "string") {
        levelsSection.classList.remove("hidden");
      } else {
        levelsSection.classList.add("hidden");
        field.levels = [];
        levelsList.innerHTML = "";
      }
    }

    typeSelect.addEventListener("change", function () {
      field.type = typeSelect.value;
      refreshLevelsVisibility();
      syncSummary();
    });

    field.levels.forEach(function (level) {
      renderLevel(field, level, levelsList);
    });
    refreshLevelsVisibility();
    syncSummary();

    var addLevelBtn = block.querySelector("[data-add-level]");
    addLevelBtn.addEventListener("click", function () {
      var level = { value: "", description: "" };
      field.levels.push(level);
      renderLevel(field, level, levelsList);
    });

    var removeBtn = block.querySelector("[data-remove-row]");
    if (locked) {
      [
        block.querySelector('[data-field="name"]'),
        block.querySelector('[data-field="description"]'),
        block.querySelector('[data-field="evidence_required"]'),
        typeSelect,
        addLevelBtn,
      ].forEach(function (el) {
        el.disabled = true;
      });
      levelsSection.classList.add("hidden");
      removeBtn.remove();

      var lockBadge = document.createElement("span");
      lockBadge.className = "badge";
      lockBadge.setAttribute("data-variant", "outline");
      lockBadge.style.marginLeft = "var(--sp-2)";
      lockBadge.textContent = "auto";
      summaryType.insertAdjacentElement("afterend", lockBadge);

      var lockNote = document.createElement("p");
      lockNote.className = "u-xs u-muted";
      lockNote.style.marginTop = "var(--sp-2)";
      lockNote.textContent =
        "Added automatically to every coding manual — the model uses it to explain " +
        "any issues it had coding this row. Can't be edited or removed.";
      block.querySelector(".effect-field-body").appendChild(lockNote);
    } else {
      removeBtn.addEventListener("click", function (e) {
        e.preventDefault(); // inside a <summary> — don't toggle collapse on remove
        var idx = state.effects.indexOf(field);
        if (idx >= 0) state.effects.splice(idx, 1);
        block.remove();
      });
    }

    return block;
  }

  var effectsList = document.getElementById("effect-fields-list");

  document.getElementById("add-effect-field").addEventListener("click", function () {
    var field = { name: "", type: "string", evidence_required: true, description: "", levels: [] };
    state.effects.push(field);
    var addedField = renderEffectField(field, true);
    effectsList.appendChild(addedField);
    addedField.querySelector('[data-field="name"]').focus();
  });

  ["name", "description", "effect_definition"].forEach(function (key) {
    var input = document.getElementById("manual-" + key.replace(/_/g, "-"));
    if (!input) return;
    input.addEventListener("input", function () {
      state[key] = input.value;
    });
  });

  function replaceManualState(nextState) {
    state = nextState;
    effectsList.innerHTML = "";
    state.effects.forEach(function (field) {
      effectsList.appendChild(renderEffectField(field, false));
    });
    ["name", "description", "effect_definition"].forEach(function (key) {
      var input = document.getElementById("manual-" + key.replace(/_/g, "-"));
      if (input) input.value = state[key] || "";
    });
  }

  replaceManualState(state);

  var form = document.getElementById("manual-form");
  var hiddenInput = document.getElementById("manual-json-input");
  var savedManual = JSON.stringify(state);
  var unsavedDraft = false;
  if (form) {
    projectEdits.register(form, function () {
      return unsavedDraft || JSON.stringify(state) !== savedManual;
    });
    form.addEventListener("submit", function () {
      hiddenInput.value = JSON.stringify(state);
    });
  }

  // Document drafting stays on this page: the generic dropzone above triggers
  // requestSubmit(), this handler uploads with fetch, and the validated candidate
  // replaces only the editor's in-memory state. The saved YAML remains untouched
  // until the user submits `manual-form`.
  var draftForm = document.getElementById("manual-draft-form");
  if (draftForm) {
    var draftInput = draftForm.querySelector('input[type="file"]');
    var draftZone = draftForm.querySelector("[data-dropzone]");
    var draftStatus = document.getElementById("manual-draft-status");
    var drafting = false;

    var DRAFT_STATUS_VARIANT = { info: "info", success: "success", error: "destructive" };
    function showDraftStatus(kind, message) {
      if (!draftStatus) return;
      draftStatus.className = "alert";
      draftStatus.setAttribute("data-variant", DRAFT_STATUS_VARIANT[kind]);
      draftStatus.style.marginTop = "var(--sp-2)";
      draftStatus.textContent = message;
    }

    draftForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (drafting || !draftInput || draftInput.disabled || !draftInput.files || !draftInput.files.length) return;
      drafting = true;
      var draftData = new FormData(draftForm);
      draftInput.disabled = true;
      if (draftZone) draftZone.setAttribute("aria-busy", "true");
      showDraftStatus("info", "Uploading and drafting the coding manual… This may take a minute.");

      fetch(draftForm.action, { method: "POST", body: draftData })
        .then(function (response) {
          return response.text().then(function (text) {
            var payload;
            try {
              payload = JSON.parse(text);
            } catch (_error) {
              payload = {};
            }
            if (!response.ok) {
              throw new Error(payload.error || payload.detail || "The coding-manual draft failed.");
            }
            if (!payload || !payload.manual || typeof payload.manual !== "object" ||
                !Array.isArray(payload.manual.effects) || typeof payload.yaml !== "string" ||
                !payload.manual.effects.every(function (field) {
                  return field && typeof field === "object" &&
                    (field.levels == null || (Array.isArray(field.levels) &&
                      field.levels.every(function (level) { return level && typeof level === "object"; })));
                })) {
              throw new Error("The coding-manual draft response was incomplete. Your edits were preserved.");
            }
            return payload;
          });
        })
        .then(function (payload) {
          replaceManualState(payload.manual);
          unsavedDraft = true;
          projectEdits.update();

          var badge = document.getElementById("manual-status-badge");
          if (badge) {
            badge.className = "badge";
            badge.setAttribute("data-variant", "info");
            badge.textContent = "Draft · not saved";
          }
          var sidebarBadge = document.getElementById("manual-sidebar-status");
          if (sidebarBadge) {
            sidebarBadge.setAttribute("data-state", "info");
            sidebarBadge.textContent = "Draft · not saved";
          }

          var notice = document.getElementById("manual-draft-notice");
          if (notice) {
            notice.classList.remove("hidden");
            var filename = notice.querySelector("[data-manual-draft-filename]");
            if (filename) filename.textContent = payload.filename || "manual document";
          }
          var incompleteWarning = document.getElementById("manual-incomplete-warning");
          if (incompleteWarning) incompleteWarning.classList.add("hidden");

          document.querySelectorAll("[data-manual-save]").forEach(function (button) {
            button.textContent = "Validate and save draft";
          });
          var yamlKind = document.getElementById("manual-yaml-kind");
          if (yamlKind) yamlKind.textContent = "drafted";
          var yamlPreview = document.getElementById("manual-yaml-preview");
          if (yamlPreview) yamlPreview.textContent = payload.yaml || "";


          var runWarning = document.getElementById("manual-draft-run-warning");
          if (runWarning) runWarning.classList.remove("hidden");

          showDraftStatus(
            "success",
            "Draft ready from " + (payload.filename || "the uploaded document") +
              ". Review the effect definition and coding fields before saving."
          );
        })
        .catch(function (error) {
          showDraftStatus("error", error.message || "The coding-manual draft failed.");
        })
        .finally(function () {
          drafting = false;
          draftInput.disabled = false;
          draftInput.value = "";
          if (draftZone) draftZone.removeAttribute("aria-busy");
          var filenamePreview = draftZone && draftZone.querySelector("[data-dropzone-filename]");
          if (filenamePreview) filenamePreview.textContent = "";
        });
    });
  }
})();

// Generic tab switching, reused for both the project page's sidebar and the
// PDF matching sub-tabs. Every
// tab's content is already server-rendered on the page (no fetch/partial-load)
// — this just shows one panel at a time within `root`.
function initTabGroup(root, opts) {
  if (!root) return;
  var panels = root.querySelectorAll("[" + opts.panelAttr + "]");
  var links = root.querySelectorAll("[" + opts.linkAttr + "]");
  if (!panels.length) return;

  function activate(tab) {
    if (opts.linkAttr === "data-tab-link" && tab === "metadata") tab = "analysis";
    var match = null;
    panels.forEach(function (panel) {
      if (panel.getAttribute(opts.panelAttr) === tab) match = panel;
    });
    if (!match) match = panels[0];
    tab = match.getAttribute(opts.panelAttr);
    panels.forEach(function (panel) {
      panel.classList.toggle("hidden", panel !== match);
      var panelTab = panel.getAttribute(opts.panelAttr);
      panel.id = opts.panelAttr + "-" + panelTab;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", opts.linkAttr + "-" + panelTab);
    });
    links.forEach(function (link) {
      var linkTab = link.getAttribute(opts.linkAttr);
      var selected = linkTab === tab;
      link.classList.toggle(opts.activeClass, selected);
      link.id = opts.linkAttr + "-" + linkTab;
      link.setAttribute("aria-controls", opts.panelAttr + "-" + linkTab);
      link.setAttribute("aria-selected", String(selected));
      link.tabIndex = selected ? 0 : -1;
    });
    if (tab && opts.storageKey) {
      try {
        sessionStorage.setItem(opts.storageKey, tab);
      } catch (e) {
        /* private browsing / storage disabled — tab memory just won't persist */
      }
    }
  }

  links.forEach(function (link, index) {
    link.addEventListener("keydown", function (e) {
      var vertical = link.closest('[role="tablist"]').getAttribute("aria-orientation") === "vertical";
      var next = vertical ? "ArrowDown" : "ArrowRight";
      var previous = vertical ? "ArrowUp" : "ArrowLeft";
      var target;
      if (e.key === next) target = (index + 1) % links.length;
      else if (e.key === previous) target = (index + links.length - 1) % links.length;
      else if (e.key === "Home") target = 0;
      else if (e.key === "End") target = links.length - 1;
      else return;
      e.preventDefault();
      links[target].click();
      links[target].focus();
    });
    link.addEventListener("click", function (e) {
      e.preventDefault();
      activate(link.getAttribute(opts.linkAttr));
    });
  });

  // A server-forced tab (e.g. a validation error on this exact response) always
  // wins over whatever was remembered from before.
  var remembered = null;
  if (opts.storageKey) {
    try {
      remembered = sessionStorage.getItem(opts.storageKey);
    } catch (e) {
      /* ignore */
    }
  }
  activate(opts.forceTab || remembered || links[0] && links[0].getAttribute(opts.linkAttr));
}

// Live run progress: polls the /status JSON endpoint (already existed for the
// old <meta http-equiv="refresh"> era's status-line; nothing previously read
// it client-side) instead of reloading the whole page every 2s. Only updates
// rows already present in the DOM — sort/pagination stay server-rendered, so a
// PDF on another page of the run table just won't visibly update until the
// page reloads at the end of the run.
(function () {
  var root = document.getElementById("run-progress");
  if (!root || root.dataset.running !== "true") return;

  var statusUrl = root.dataset.statusUrl;
  var bar = root.querySelector("[data-progress-bar]");
  var summary = root.querySelector("[data-progress-summary]");
  var rowsByPdf = {};
  document.querySelectorAll("[data-run-row]").forEach(function (tr) {
    rowsByPdf[tr.getAttribute("data-run-row")] = tr;
  });

  var STATUS_BADGE = {
    ok: '<span class="badge" data-variant="success">ok</span>',
    needs_review: '<span class="badge" data-variant="warning">needs review</span>',
    error: '<span class="badge" data-variant="destructive">error</span>',
    running: '<span class="badge" data-variant="info">running</span>',
    cancelled: '<span class="badge" data-variant="outline">cancelled</span>',
    pending: '<span class="badge" data-variant="outline">pending</span>',
  };

  function applySnapshot(data) {
    if (bar) {
      var total = data.total || 1;
      var processed = data.processed || 0;
      var fill = bar.querySelector("span") || bar;
      fill.style.width = Math.min(100, (processed / total) * 100) + "%";
      bar.setAttribute("aria-valuemax", total);
      bar.setAttribute("aria-valuenow", processed);
    }
    if (summary) {
      summary.textContent = data.processed + " / " + data.total + " PDFs processed — " + data.status;
    }
    (data.pdfs || []).forEach(function (pdf) {
      var tr = rowsByPdf[pdf.source_pdf];
      if (!tr) return;
      var statusCell = tr.querySelector("[data-run-status-cell]");
      if (statusCell) {
        var badge = STATUS_BADGE[pdf.status] || pdf.status;
        if (pdf.status === "ok" && pdf.json_repaired) badge = badge.replace("</span>", " · repaired</span>");
        statusCell.innerHTML = badge;
      }
      var inTok = tr.querySelector("[data-run-input-tokens]");
      if (inTok) inTok.textContent = pdf.input_tokens != null ? pdf.input_tokens : "—";
      var outTok = tr.querySelector("[data-run-output-tokens]");
      if (outTok) outTok.textContent = pdf.output_tokens != null ? pdf.output_tokens : "—";
      var detail = tr.querySelector("[data-run-detail]");
      if (detail) {
        var parts = [];
        if (pdf.error) parts.push(pdf.error);
        if (pdf.missing_ids && pdf.missing_ids.length) parts.push("missing: " + pdf.missing_ids.join(", "));
        if (pdf.extra_ids && pdf.extra_ids.length) parts.push("unexpected: " + pdf.extra_ids.join(", "));
        detail.textContent = parts.join(" ");
      }
    });
  }

  function poll() {
    fetch(statusUrl, { headers: { Accept: "application/json" } })
      .then(function (res) {
        if (res.ok === false) throw new Error("Status request failed.");
        return res.json();
      })
      .then(function (data) {
        if (!data || !["running", "cancelling", "complete", "failed", "cancelled", "idle"].includes(data.status)) {
          throw new Error("Invalid run status.");
        }
        applySnapshot(data);
        if (data.status === "running" || data.status === "cancelling") {
          setTimeout(poll, 1500);
        } else {
          // Terminal state reached (complete/failed/cancelled) — reload once
          // to pick up the freshly server-rendered table (correct sort/paging,
          // retry buttons, the Results tab appearing, etc.) rather than trying
          // to replicate all of that in JS.
          projectEdits.refresh();
        }
      })
      .catch(function () {
        if (summary) summary.textContent = "Connection lost. Retrying…";
        setTimeout(poll, 3000); // transient fetch failure — keep trying
      });
  }

  setTimeout(poll, 1000);
})();

// PDF match-scan progress: same polling/reload-on-terminal shape as the run
// progress bar above, but there's no per-row table to update — just the bar
// and summary line — since the scan only feeds the (server-rendered) match
// suggestions panel once it's done.
(function () {
  var root = document.getElementById("pdf-scan-progress");
  if (!root || root.dataset.running !== "true") return;

  var statusUrl = root.dataset.statusUrl;
  var bar = root.querySelector("[data-progress-bar]");
  var summary = root.querySelector("[data-progress-summary]");

  function applySnapshot(data) {
    if (bar) {
      var total = data.total || 1;
      var processed = data.processed || 0;
      var fill = bar.querySelector("span") || bar;
      fill.style.width = Math.min(100, (processed / total) * 100) + "%";
      bar.setAttribute("aria-valuemax", total);
      bar.setAttribute("aria-valuenow", processed);
    }
    if (summary) {
      summary.textContent = "Scanning uploaded PDFs for matches — " + (data.processed || 0) + " / " + (data.total || 0);
    }
  }

  function poll() {
    fetch(statusUrl, { headers: { Accept: "application/json" } })
      .then(function (res) {
        if (res.ok === false) throw new Error("Status request failed.");
        return res.json();
      })
      .then(function (data) {
        if (!data || !["running", "complete", "idle"].includes(data.status)) {
          throw new Error("Invalid scan status.");
        }
        if (data.status === "running") {
          applySnapshot(data);
          setTimeout(poll, 1500);
        } else {
          projectEdits.refresh();
        }
      })
      .catch(function () {
        if (summary) summary.textContent = "Connection lost. Retrying…";
        setTimeout(poll, 3000); // transient fetch failure — keep trying
      });
  }

  setTimeout(poll, 1000);
})();

(function () {
  var provider = document.getElementById("run-provider");
  if (!provider) return;
  var model = document.getElementById("run-model");
  var keyStatus = document.getElementById("run-key-status");
  var defaults = {};
  var statuses = {};
  var labels = {};
  var defaultsInput = document.getElementById("run-provider-defaults");
  try {
    if (keyStatus) {
      statuses = JSON.parse(keyStatus.dataset.providerStatus || "{}");
      labels = JSON.parse(keyStatus.dataset.providerLabels || "{}");
    }
    if (defaultsInput) defaults = JSON.parse(defaultsInput.textContent || "{}");
  } catch (_) {
    // The server still validates the provider and supplies its default on save.
  }

  function updateProviderSettings(resetModel) {
    document.querySelectorAll("[data-provider-setting]").forEach(function (setting) {
      setting.hidden = !setting.getAttribute("data-provider-setting").split(" ").includes(provider.value);
    });
    if (resetModel && model) model.value = defaults[provider.value] || "";

    if (!keyStatus) return;
    var status = statuses[provider.value] || "missing";
    var label = labels[provider.value] || provider.value;
    var message = keyStatus.querySelector("[data-run-key-message]");
    keyStatus.setAttribute("data-variant", (status === "saved" || status === "optional") ? "success" : "warning");
    if (message) {
      message.textContent = status === "unconfigured"
        ? "Set the OpenAI-compatible API base URL in Settings."
        : status === "optional"
        ? "OpenAI-compatible endpoint is configured (API key optional)."
        : status === "saved"
        ? label + " API key is saved. Keychain access will be requested when needed."
        : "No " + label + " API key set yet.";
    }

  }

  provider.addEventListener("change", function () { updateProviderSettings(true); });
  updateProviderSettings(false);
  var settingsForm = provider.form;
  var savedSettings = JSON.stringify(Array.from(new FormData(settingsForm)));
  projectEdits.register(settingsForm, function () {
    return JSON.stringify(Array.from(new FormData(settingsForm))) !== savedSettings;
  });
})();

(function () {
  var tabRoot = document.getElementById("tab-root");
  if (tabRoot) {
    // Top-level tabs survive this app's full-page-reload actions (form
    // submits, the in-progress-run auto-refresh) without any server plumbing —
    // remembered per project so switching projects doesn't leak the tab choice.
    var navigation = tabRoot.querySelector('.project-tabs');
    var compactNavigation = matchMedia('(max-width: 48rem)');
    function updateNavigationOrientation() {
      navigation.setAttribute('aria-orientation', compactNavigation.matches ? 'horizontal' : 'vertical');
    }
    updateNavigationOrientation();
    compactNavigation.addEventListener('change', updateNavigationOrientation);
    initTabGroup(tabRoot, {
      linkAttr: "data-tab-link",
      panelAttr: "data-tab-panel",
      activeClass: "is-active",
      storageKey: "metaCoderActiveTab:" + (tabRoot.dataset.projectId || "default"),
      forceTab: tabRoot.dataset.forceTab,
    });
  }

  document.querySelectorAll("[data-method-group]").forEach(function (group) {
    initTabGroup(group, {
      linkAttr: "data-method-link",
      panelAttr: "data-method-panel",
      activeClass: "is-active",
    });
  });

  var identifySubtabs = document.getElementById("identify-subtabs");
  if (identifySubtabs) {
    initTabGroup(identifySubtabs, {
      linkAttr: "data-subtab-link",
      panelAttr: "data-subtab-panel",
      activeClass: "is-active",
      storageKey: "metaCoderIdentifySubtab:" + (tabRoot ? tabRoot.dataset.projectId : "default"),
    });
  }

  document.addEventListener("invalid", function (event) {
    var ancestor = event.target.closest("details");
    while (ancestor) {
      ancestor.open = true;
      ancestor = ancestor.parentElement.closest("details");
    }
    if (event.target.form !== document.getElementById("manual-form")) return;
    var firstInvalid = Array.from(event.target.form.elements).find(function (input) {
      return input.willValidate && !input.validity.valid;
    });
    if (event.target !== firstInvalid) return;
    var panel = event.target.closest("[data-tab-panel]");
    if (panel) document.querySelector('[data-tab-link="' + panel.dataset.tabPanel + '"]').click();
    var details = event.target.closest("details");
    while (details) {
      details.open = true;
      details = details.parentElement.closest("details");
    }
    event.target.focus();
  }, true);

  // Cross-references from one tab's content to another (e.g. the coding
  // sheet's "see the PDF matching tab" notice) — clicks the matching
  // top-level tab button so its own listener (registered above) does the
  // actual switch.
  document.addEventListener("click", function (e) {
    var jump = e.target.closest("[data-tab-jump]");
    if (!jump) return;
    e.preventDefault();
    var target = document.querySelector('[data-tab-link="' + jump.getAttribute("data-tab-jump") + '"]');
    if (target) target.click();
  });
})();

// Manual drafting has its own model, independent of per-project extraction.
(function () {
  var provider = document.getElementById("manual-generator-provider");
  var model = document.getElementById("manual-generator-model");
  var defaultsInput = document.getElementById("manual-provider-defaults");
  if (!provider || !model || !defaultsInput) return;
  var defaults = JSON.parse(defaultsInput.textContent);
  var models = {};
  var previous = provider.value;
  provider.addEventListener("change", function () {
    models[previous] = model.value;
    model.value = models[provider.value] || defaults[provider.value] || "";
    model.placeholder = provider.value === "openai_compatible" ? "Model ID served by your endpoint" : "Model ID";
    previous = provider.value;
  });
})();

// Coding-sheet conversion is a reviewable draft; saving is a separate action.
(function () {
  var form = document.getElementById("sheet-draft-form");
  if (!form) return;
  var preview = document.getElementById("sheet-draft-preview");
  var status = document.getElementById("sheet-draft-status");
  var saveForm = document.getElementById("sheet-draft-save-form");
  var csvInput = document.getElementById("sheet-draft-csv");
  var busy = false;

  var draftRows = [];
  var draftPage = 1;

  function renderDraftPage() {
    var body = document.querySelector("#sheet-draft-table tbody");
    body.replaceChildren();
    draftRows.slice((draftPage - 1) * 10, draftPage * 10).forEach(function (row) {
      var tr = document.createElement("tr");
      ["row_id", "source_pdf", "locator", "authors", "year", "title", "doi"].forEach(function (key) {
        var cell = document.createElement("td");
        cell.textContent = row[key];
        tr.appendChild(cell);
      });
      body.appendChild(tr);
    });
    var totalPages = Math.max(1, Math.ceil(draftRows.length / 10));
    document.getElementById("sheet-draft-pager").classList.toggle("hidden", totalPages <= 1);
    document.getElementById("sheet-draft-page-summary").textContent =
      "Page " + draftPage + " of " + totalPages + " (" + draftRows.length + " rows)";
    document.getElementById("sheet-draft-previous").disabled = draftPage <= 1;
    document.getElementById("sheet-draft-next").disabled = draftPage >= totalPages;
  }

  document.getElementById("sheet-draft-previous").addEventListener("click", function () {
    if (draftPage > 1) { draftPage--; renderDraftPage(); }
  });
  document.getElementById("sheet-draft-next").addEventListener("click", function () {
    if (draftPage * 10 < draftRows.length) { draftPage++; renderDraftPage(); }
  });

  function showStatus(message, variant) {
    status.textContent = message;
    status.dataset.variant = variant;
    status.classList.remove("hidden");
  }

  function post(target) {
    return fetch(target.action, { method: "POST", body: new FormData(target) })
      .then(function (response) {
        return response.text().then(function (text) {
          var payload;
          try { payload = JSON.parse(text); } catch (_) { payload = {}; }
          if (!response.ok) throw new Error(payload.error || "The request failed. Please try again.");
          return payload;
        });
      });
  }

  function validateSheetDraft(payload) {
    var validCount = function (value) { return Number.isInteger(value) && value >= 0; };
    if (!payload || typeof payload.csv !== "string" || !Array.isArray(payload.rows) ||
        !payload.rows.every(function (row) {
          return row && typeof row === "object" &&
            ["row_id", "source_pdf", "locator", "authors", "year", "title", "doi"].every(function (key) {
              return typeof row[key] === "string";
            });
        }) || !Array.isArray(payload.warnings) ||
        !payload.warnings.every(function (warning) { return typeof warning === "string"; }) ||
        !validCount(payload.source_row_count) || !validCount(payload.row_count)) {
      throw new Error("The coding-sheet draft response was incomplete. Your previous draft was preserved.");
    }
    return payload;
  }

  function setBusy(value) {
    busy = value;
    form.setAttribute("aria-busy", String(value));
    // Preserve disabled states set by the server (missing setup or active run).
    [form, saveForm].forEach(function (target) {
      target.querySelectorAll("button").forEach(function (button) {
        if (value) {
          button.dataset.wasDisabled = String(button.disabled);
          button.disabled = true;
        } else {
          button.disabled = button.dataset.wasDisabled === "true";
        }
      });
    });
  }

  var sheetDraftUnsaved = false;
  projectEdits.register(saveForm, function () { return sheetDraftUnsaved; });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    showStatus("Converting the CSV using your saved manual and notes…", "info");
    post(form).then(validateSheetDraft).then(function (payload) {
      csvInput.value = payload.csv;
      sheetDraftUnsaved = true;
      projectEdits.update();
      document.getElementById("sheet-draft-summary").textContent =
        payload.source_row_count + " source rows → " + payload.row_count + " coding rows. Review all rows before saving.";
      var warnings = document.getElementById("sheet-draft-warnings");
      warnings.replaceChildren();
      payload.warnings.forEach(function (message) {
        var item = document.createElement("li");
        item.textContent = message;
        warnings.appendChild(item);
      });
      draftRows = payload.rows;
      draftPage = 1;
      renderDraftPage();
      preview.classList.remove("hidden");
      showStatus("Draft ready. Your saved sheet and results have not changed.", "success");
    }).catch(function (error) {
      showStatus(error.message, "destructive");
    }).finally(function () { setBusy(false); });
  });

  saveForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    showStatus("Validating and saving the converted sheet…", "info");
    post(saveForm).then(function (payload) {
      sheetDraftUnsaved = false;
      projectEdits.update();
      window.location.assign(payload.redirect);
    }).catch(function (error) {
      showStatus(error.message, "destructive");
    }).finally(function () { setBusy(false); });
  });

  document.getElementById("sheet-draft-discard").addEventListener("click", function () {
    if (busy) return;
    csvInput.value = "";
    sheetDraftUnsaved = false;
    projectEdits.update();
    preview.classList.add("hidden");
    showStatus("Draft discarded. Your saved coding sheet is unchanged.", "info");
  });
})();

// Live project search keeps the input (and its cursor) in place while replacing
// only the server-rendered results. Invalidate requests as soon as typing resumes.
(function () {
  var form = document.querySelector('.project-search');
  if (!form) return;
  var query = document.getElementById('project-search');
  var sort = document.getElementById('project-sort');
  var results = document.getElementById('project-search-results');
  var status = document.getElementById('project-search-status');
  var timer;
  var controller;
  var revision = 0;

  function schedule(delay) {
    clearTimeout(timer);
    if (controller) controller.abort();
    var current = ++revision;
    results.setAttribute('aria-busy', 'true');
    timer = setTimeout(async function () {
      controller = new AbortController();
      var url = new URL(form.action, window.location.href);
      if (query.value.trim()) url.searchParams.set('q', query.value.trim());
      url.searchParams.set('sort', sort.value);
      try {
        var response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error('Search failed');
        var html = await response.text();
        if (current !== revision) return;
        var next = new DOMParser().parseFromString(html, 'text/html').getElementById('project-search-results');
        if (!next) throw new Error('Missing project results');
        results.replaceChildren(...Array.from(next.childNodes));
        results.dataset.resultCount = next.dataset.resultCount;
        window.history.replaceState(null, '', url);
        status.classList.add('u-visually-hidden');
        status.textContent = next.dataset.resultCount + ' matching projects';
      } catch (error) {
        if (current !== revision || error.name === 'AbortError') return;
        status.classList.remove('u-visually-hidden');
        status.textContent = 'Could not update projects. Try typing again or press Enter to retry.';
      } finally {
        if (current === revision) results.setAttribute('aria-busy', 'false');
      }
    }, delay);
  }

  query.addEventListener('input', function (event) {
    if (!event.isComposing) schedule(250);
  });
  query.addEventListener('compositionend', function () { schedule(250); });
  sort.addEventListener('change', function () { schedule(0); });
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    schedule(0);
  });
})();
