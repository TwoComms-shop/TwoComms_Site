(function () {
  "use strict";

  var root = document.querySelector(".f5-sg");
  var bootNode = document.getElementById("f5-size-grid-bootstrap");
  if (!root || !bootNode) return;

  var boot;
  try {
    boot = JSON.parse(bootNode.textContent || "{}");
  } catch (error) {
    boot = {};
  }

  var urls = boot.urls || {};
  var catalogs = Array.isArray(boot.catalogs) ? boot.catalogs : [];
  var form = root.querySelector("[data-grid-form]");
  var gridList = root.querySelector("[data-grid-list]");
  var libraryState = root.querySelector("[data-library-state]");
  var columnList = root.querySelector("[data-column-list]");
  var measurementHead = root.querySelector("[data-measurement-head]");
  var measurementBody = root.querySelector("[data-measurement-body]");
  var readiness = root.querySelector(".f5-sg__readiness");
  var statusText = root.querySelector("[data-status-text]");
  var editState = root.querySelector("[data-edit-state]");
  var saveButton = root.querySelector("[data-action='save-grid']");
  var saveLabel = root.querySelector("[data-save-label]");
  var toastNode = root.querySelector("[data-toast]");
  var columnTemplate = document.getElementById("f5-sg-column-template");
  var rowTemplate = document.getElementById("f5-sg-row-template");
  var toastTimer = null;
  var sequence = 0;

  var state = {
    grids: [],
    selectedId: null,
    columns: [],
    rows: [],
    dirty: false,
    busy: false,
    filters: { search: "", catalog: "", garment: "", fit: "" }
  };

  function uid(prefix) {
    sequence += 1;
    return prefix + "-" + sequence;
  }

  function escapeHTML(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function csrfToken() {
    var input = document.querySelector("input[name='csrfmiddlewaretoken']");
    if (input && input.value) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function responseJSON(response) {
    var data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    if (!response.ok || data.ok === false) {
      var failure = new Error(data.error || "Сервер не зміг виконати операцію.");
      failure.status = response.status;
      failure.code = data.code || "";
      throw failure;
    }
    return data;
  }

  function getJSON(url) {
    return fetch(url, { headers: { "X-CSRFToken": csrfToken() } }).then(responseJSON);
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken()
      },
      body: JSON.stringify(payload || {})
    }).then(responseJSON);
  }

  function toast(message, isError) {
    toastNode.textContent = message;
    toastNode.classList.toggle("is-error", Boolean(isError));
    toastNode.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastNode.hidden = true;
    }, 4200);
  }

  function setReadiness(kind, message) {
    readiness.dataset.readiness = kind;
    statusText.textContent = message;
  }

  function setBusy(busy, label) {
    state.busy = busy;
    saveButton.disabled = busy;
    saveLabel.textContent = busy ? (label || "Збереження…") : "Зберегти сітку";
    root.querySelectorAll("[data-action='new-grid']").forEach(function (button) {
      button.disabled = busy;
    });
  }

  function markDirty() {
    if (state.busy) return;
    state.dirty = true;
    editState.textContent = "Не збережено";
    editState.className = "f5-sg__edit-state is-dirty";
    setReadiness("dirty", "Є незбережені зміни");
  }

  function markSaved() {
    state.dirty = false;
    editState.textContent = state.selectedId ? "Збережено" : "Чернетка";
    editState.className = "f5-sg__edit-state" + (state.selectedId ? " is-saved" : "");
    setReadiness(state.selectedId ? "saved" : "ready", state.selectedId ? "Сітка синхронізована" : "Бібліотека готова");
  }

  function fitFromKey(optionKey) {
    var part = String(optionKey || "").split(";").find(function (item) {
      return item.indexOf("fit=") === 0;
    });
    return part ? part.slice(4) : "";
  }

  function fitLabel(optionKey) {
    var fit = fitFromKey(optionKey);
    var known = { classic: "Класика", oversize: "Oversize", regular: "Regular", slim: "Slim" };
    return known[fit] || fit || "Без посадки";
  }

  function catalogName(catalogId) {
    var catalog = catalogs.find(function (item) { return String(item.id) === String(catalogId); });
    return catalog ? catalog.name : "Каталог";
  }

  function setupCatalogs() {
    var filter = root.querySelector("[data-filter='catalog']");
    var editor = form.elements.catalog_id;
    catalogs.forEach(function (catalog) {
      var filterOption = document.createElement("option");
      filterOption.value = catalog.id;
      filterOption.textContent = catalog.name;
      filter.appendChild(filterOption);
      var editorOption = filterOption.cloneNode(true);
      editor.appendChild(editorOption);
    });
  }

  function defaultColumns() {
    return [
      { uid: uid("col"), key: "size", label: "Розмір" },
      { uid: uid("col"), key: "width", label: "Ширина, см" },
      { uid: uid("col"), key: "length", label: "Довжина, см" }
    ];
  }

  function defaultRows(columns) {
    return ["S", "M", "L"].map(function (size) {
      var values = {};
      columns.forEach(function (column) { values[column.uid] = column.key === "size" ? size : ""; });
      return { uid: uid("row"), values: values };
    });
  }

  function hydrateGuide(guide) {
    var sourceColumns = Array.isArray(guide.columns) && guide.columns.length ? guide.columns : [
      { key: "size", label: "Розмір" }
    ];
    state.columns = sourceColumns.map(function (column) {
      return { uid: uid("col"), key: column.key || "", label: column.label || "" };
    });
    var sourceRows = Array.isArray(guide.rows) && guide.rows.length ? guide.rows : [{ size: "S" }];
    state.rows = sourceRows.map(function (sourceRow) {
      var values = {};
      state.columns.forEach(function (column) {
        values[column.uid] = column.key === "size"
          ? (sourceRow.display_size || sourceRow.size || "")
          : (sourceRow[column.key] || "");
      });
      return { uid: uid("row"), values: values };
    });
  }

  function setFormValue(name, value) {
    if (form.elements[name]) form.elements[name].value = value == null ? "" : value;
  }

  function resetForm() {
    state.selectedId = null;
    form.reset();
    setFormValue("id", "");
    setFormValue("order", 0);
    form.elements.catalog_id.disabled = false;
    state.columns = defaultColumns();
    state.rows = defaultRows(state.columns);
    root.querySelector("[data-editor-heading]").textContent = "Нова розмірна сітка";
    renderLibrary();
    renderEditor();
    clearErrors();
    markSaved();
    window.requestAnimationFrame(function () { form.elements.name.focus(); });
  }

  function loadGridIntoEditor(grid) {
    state.selectedId = grid.id;
    setFormValue("id", grid.id);
    setFormValue("name", grid.name);
    setFormValue("catalog_id", grid.catalog_id);
    setFormValue("garment_code", grid.profile && grid.profile.garment_code);
    setFormValue("option_key", grid.profile && grid.profile.option_key);
    setFormValue("order", grid.order || 0);
    setFormValue("description", grid.description || "");
    setFormValue("eyebrow", grid.guide_data && grid.guide_data.eyebrow);
    setFormValue("guide_title", grid.guide_data && grid.guide_data.title);
    setFormValue("intro", grid.guide_data && grid.guide_data.intro);
    form.elements.catalog_id.disabled = true;
    hydrateGuide(grid.guide_data || {});
    root.querySelector("[data-editor-heading]").textContent = grid.name;
    clearErrors();
    renderLibrary();
    renderEditor();
    markSaved();
    fetchPersistedPreview(grid.id);
  }

  async function fetchPersistedPreview(id) {
    if (!urls.preview) return;
    try {
      var data = await getJSON(urls.preview + "?id=" + encodeURIComponent(id));
      if (state.selectedId === id && !state.dirty && data.preview) {
        renderPreview(data.preview);
      }
    } catch (error) {
      /* The local preview is still fully available if this optional check fails. */
    }
  }

  function renderColumns() {
    columnList.textContent = "";
    state.columns.forEach(function (column, index) {
      var node = columnTemplate.content.firstElementChild.cloneNode(true);
      node.dataset.columnIndex = index;
      node.querySelector("[data-column-key]").value = column.key;
      node.querySelector("[data-column-label]").value = column.label;
      if (index === 0) {
        node.querySelector("[data-column-key]").readOnly = true;
        node.querySelector("[data-column-key]").title = "Системна колонка розміру";
      }
      columnList.appendChild(node);
    });
  }

  function renderMeasurementTable() {
    var headerRow = document.createElement("tr");
    var controlsHeader = document.createElement("th");
    controlsHeader.scope = "col";
    controlsHeader.textContent = "Порядок";
    headerRow.appendChild(controlsHeader);
    state.columns.forEach(function (column) {
      var th = document.createElement("th");
      th.scope = "col";
      th.textContent = column.label || column.key || "Колонка";
      headerRow.appendChild(th);
    });
    var removeHeader = document.createElement("th");
    removeHeader.scope = "col";
    removeHeader.innerHTML = '<span class="f5-sg__sr-only">Видалити</span>';
    headerRow.appendChild(removeHeader);
    measurementHead.textContent = "";
    measurementHead.appendChild(headerRow);

    measurementBody.textContent = "";
    state.rows.forEach(function (row, rowIndex) {
      var tr = rowTemplate.content.firstElementChild.cloneNode(true);
      tr.dataset.rowUid = row.uid;
      var removeCell = tr.querySelector(".f5-sg__row-remove-cell");
      state.columns.forEach(function (column) {
        var td = document.createElement("td");
        var input = document.createElement("input");
        input.type = "text";
        input.value = row.values[column.uid] || "";
        input.dataset.cellKey = column.key;
        input.dataset.columnUid = column.uid;
        input.setAttribute("aria-label", (column.label || column.key) + ", рядок " + (rowIndex + 1));
        if (column.key === "size") input.placeholder = "S";
        else input.placeholder = "—";
        td.appendChild(input);
        tr.insertBefore(td, removeCell);
      });
      tr.querySelector("[data-action='move-row-up']").disabled = rowIndex === 0;
      tr.querySelector("[data-action='move-row-down']").disabled = rowIndex === state.rows.length - 1;
      measurementBody.appendChild(tr);
    });
  }

  function refreshColumnMetadata(index) {
    var column = state.columns[index];
    if (!column) return;
    var header = measurementHead.querySelector("th:nth-child(" + (index + 2) + ")");
    if (header) header.textContent = column.label || column.key || "Колонка";
    measurementBody.querySelectorAll("tr").forEach(function (rowNode) {
      var input = rowNode.querySelector("[data-column-uid='" + column.uid + "']");
      if (input) {
        input.dataset.cellKey = column.key;
        input.setAttribute("aria-label", (column.label || column.key || "Колонка") + ", рядок " + (rowNode.sectionRowIndex + 1));
      }
    });
  }

  function currentGuide() {
    return {
      title: form.elements.guide_title.value.trim() || form.elements.name.value.trim() || "Нова розмірна сітка",
      eyebrow: form.elements.eyebrow.value.trim() || "Гід посадки",
      intro: form.elements.intro.value.trim() || "Заміри готового виробу у сантиметрах.",
      columns: state.columns.map(function (column) {
        return { key: column.key.trim(), label: column.label.trim() || column.key.trim() };
      }),
      rows: state.rows.map(function (row) {
        var payload = {};
        state.columns.forEach(function (column) {
          payload[column.key.trim()] = (row.values[column.uid] || "").trim();
        });
        payload.display_size = payload.size || "";
        return payload;
      })
    };
  }

  function renderPreview(guide) {
    guide = guide || currentGuide();
    root.querySelector("[data-preview-eyebrow]").textContent = guide.eyebrow || "Гід посадки";
    root.querySelector("[data-preview-title]").textContent = guide.title || form.elements.name.value.trim() || "Нова розмірна сітка";
    var intro = root.querySelector("[data-preview-intro]");
    intro.textContent = guide.intro || "";
    intro.hidden = !guide.intro;

    var previewHead = root.querySelector("[data-preview-head]");
    var previewBody = root.querySelector("[data-preview-body]");
    var headRow = document.createElement("tr");
    (guide.columns || []).forEach(function (column) {
      var th = document.createElement("th");
      th.scope = "col";
      th.textContent = column.label || column.key || "—";
      headRow.appendChild(th);
    });
    previewHead.textContent = "";
    previewHead.appendChild(headRow);
    previewBody.textContent = "";
    (guide.rows || []).forEach(function (row) {
      var tr = document.createElement("tr");
      (guide.columns || []).forEach(function (column) {
        var td = document.createElement("td");
        if (column.key === "size") td.className = "tc-size-table-key";
        td.textContent = column.key === "size" ? (row.display_size || row.size || "—") : (row[column.key] || "—");
        tr.appendChild(td);
      });
      previewBody.appendChild(tr);
    });
  }

  function renderEditor() {
    renderColumns();
    renderMeasurementTable();
    renderPreview();
  }

  function filteredGrids() {
    var search = state.filters.search.toLocaleLowerCase("uk");
    return state.grids.filter(function (grid) {
      var profile = grid.profile || {};
      var haystack = [grid.name, grid.description, grid.catalog_name, profile.garment_code, profile.option_key].join(" ").toLocaleLowerCase("uk");
      if (search && haystack.indexOf(search) === -1) return false;
      if (state.filters.catalog && String(grid.catalog_id) !== state.filters.catalog) return false;
      if (state.filters.garment && profile.garment_code !== state.filters.garment) return false;
      if (state.filters.fit && fitFromKey(profile.option_key) !== state.filters.fit) return false;
      return true;
    });
  }

  function gridCardHTML(grid) {
    var guide = grid.guide_data || {};
    var profile = grid.profile || {};
    var rows = Array.isArray(guide.rows) ? guide.rows.length : 0;
    var columns = Array.isArray(guide.columns) ? guide.columns.length : 0;
    var selected = state.selectedId === grid.id ? " is-selected" : "";
    return '<article class="f5-sg__grid-card' + selected + '" data-grid-id="' + Number(grid.id) + '">' +
      '<div class="f5-sg__grid-card-head">' +
        '<button class="f5-sg__grid-card-title" type="button" data-action="select-grid">' + escapeHTML(grid.name) + '</button>' +
        '<span class="f5-sg__grid-status">Активна</span>' +
      '</div>' +
      '<div class="f5-sg__grid-meta">' +
        '<span><strong>' + escapeHTML(grid.catalog_name || catalogName(grid.catalog_id)) + '</strong></span>' +
        '<span>' + escapeHTML(fitLabel(profile.option_key)) + '</span>' +
        '<span>' + rows + ' × ' + columns + '</span>' +
        (grid.assigned_count ? '<span>У ' + Number(grid.assigned_count) + ' товарах</span>' : '') +
      '</div>' +
      '<div class="f5-sg__grid-actions">' +
        '<button type="button" data-action="select-grid" title="Редагувати сітку" aria-label="Редагувати ' + escapeHTML(grid.name) + '">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.5-1 10-10-3.5-3.5-10 10L4 20ZM13.5 7l3.5 3.5"/></svg>' +
        '</button>' +
        '<button type="button" data-action="duplicate-grid" title="Створити копію" aria-label="Дублювати ' + escapeHTML(grid.name) + '">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="11"/><path d="M16 8V5H5v11h3"/></svg>' +
        '</button>' +
        '<button type="button" data-action="archive-grid" title="Архівувати сітку" aria-label="Архівувати ' + escapeHTML(grid.name) + '">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16v13H4zM3 4h18v3H3zM9 11h6"/></svg>' +
        '</button>' +
      '</div>' +
    '</article>';
  }

  function renderLibrary() {
    var grids = filteredGrids();
    root.querySelector("[data-grid-count]").textContent = grids.length;
    gridList.innerHTML = grids.map(gridCardHTML).join("");
    if (grids.length) {
      libraryState.hidden = true;
      libraryState.className = "f5-sg__library-state";
    } else {
      libraryState.hidden = false;
      libraryState.className = "f5-sg__library-state is-empty";
      libraryState.textContent = state.grids.length
        ? "За цими фільтрами сіток немає. Змініть умови пошуку."
        : "Бібліотека порожня. Створіть першу еталонну сітку для каталогу.";
    }
  }

  async function loadGrids(selectId) {
    libraryState.hidden = false;
    libraryState.className = "f5-sg__library-state";
    libraryState.innerHTML = '<div class="f5-sg__skeleton" aria-hidden="true"><i></i><i></i><i></i></div>';
    gridList.textContent = "";
    try {
      var data = await getJSON(urls.list);
      state.grids = Array.isArray(data.grids) ? data.grids : [];
      root.dataset.state = "ready";
      renderLibrary();
      if (selectId != null) {
        var selected = state.grids.find(function (grid) { return grid.id === selectId; });
        if (selected) loadGridIntoEditor(selected);
      }
      return data;
    } catch (error) {
      root.dataset.state = "error";
      libraryState.hidden = false;
      libraryState.className = "f5-sg__library-state is-error";
      libraryState.textContent = "Не вдалося завантажити бібліотеку. Оновіть сторінку або перевірте з’єднання.";
      setReadiness("error", "Помилка завантаження");
      toast(error.message, true);
      throw error;
    }
  }

  function clearErrors() {
    root.querySelectorAll("[data-error-for]").forEach(function (node) { node.textContent = ""; });
    form.querySelectorAll("[aria-invalid='true']").forEach(function (input) { input.removeAttribute("aria-invalid"); });
  }

  function showError(name, message) {
    var output = root.querySelector("[data-error-for='" + name + "']");
    if (output) output.textContent = message;
    if (form.elements[name]) form.elements[name].setAttribute("aria-invalid", "true");
  }

  function validate() {
    clearErrors();
    var valid = true;
    if (!form.elements.name.value.trim()) { showError("name", "Вкажіть зрозумілу назву сітки."); valid = false; }
    if (!form.elements.catalog_id.value) { showError("catalog_id", "Оберіть каталог."); valid = false; }
    if (!form.elements.option_key.value.trim()) { showError("option_key", "Вкажіть посадку, наприклад fit=classic."); valid = false; }

    var keys = state.columns.map(function (column) { return column.key.trim(); });
    if (!keys.length || keys[0] !== "size" || keys.some(function (key) { return !/^[a-z][a-z0-9_-]{0,49}$/.test(key); })) {
      showError("columns", "Коди мають починатися з латинської літери; перша колонка — size.");
      valid = false;
    } else if (new Set(keys).size !== keys.length) {
      showError("columns", "Коди колонок не повинні повторюватися.");
      valid = false;
    }

    var sizeColumn = state.columns.find(function (column) { return column.key.trim() === "size"; });
    var sizes = sizeColumn ? state.rows.map(function (row) { return (row.values[sizeColumn.uid] || "").trim().toUpperCase(); }) : [];
    if (!state.rows.length || sizes.some(function (size) { return !size; })) {
      showError("rows", "Кожен рядок повинен мати розмір.");
      valid = false;
    } else if (new Set(sizes).size !== sizes.length) {
      showError("rows", "Розміри не повинні повторюватися.");
      valid = false;
    }
    return valid;
  }

  function payload() {
    var guide = currentGuide();
    guide.rows = guide.rows.map(function (row) {
      delete row.display_size;
      return row;
    });
    return {
      id: state.selectedId,
      catalog_id: Number(form.elements.catalog_id.value),
      name: form.elements.name.value.trim(),
      description: form.elements.description.value.trim(),
      order: Number(form.elements.order.value || 0),
      profile: {
        garment_code: form.elements.garment_code.value.trim(),
        option_key: form.elements.option_key.value.trim(),
        is_active: true
      },
      guide_data: guide,
      is_active: true
    };
  }

  async function saveGrid() {
    if (state.busy || !validate()) {
      if (!state.busy) {
        setReadiness("error", "Перевірте поля редактора");
        var firstInvalid = form.querySelector("[aria-invalid='true']");
        if (firstInvalid) firstInvalid.focus();
      }
      return;
    }
    setBusy(true, "Збереження…");
    setReadiness("loading", "Зберігаємо та перевіряємо…");
    try {
      var data = await postJSON(urls.save, payload());
      await loadGrids(data.grid.id);
      toast("Розмірну сітку збережено та синхронізовано.");
    } catch (error) {
      setReadiness("error", "Не вдалося зберегти");
      toast(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function duplicateGrid(id) {
    if (state.busy) return;
    setBusy(true, "Створення копії…");
    setReadiness("loading", "Створюємо незалежну копію…");
    try {
      var data = await postJSON(urls.duplicate, { id: id });
      await loadGrids(data.grid.id);
      toast("Копію створено. Можна безпечно змінювати її окремо.");
    } catch (error) {
      setReadiness("error", "Не вдалося створити копію");
      toast(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function archiveGrid(id) {
    if (state.busy) return;
    var grid = state.grids.find(function (item) { return item.id === id; });
    if (!grid) return;
    if (!window.confirm('Архівувати сітку «' + grid.name + '»? Вона зникне зі списку доступних для нових товарів.')) return;
    setBusy(true, "Архівування…");
    setReadiness("loading", "Перевіряємо використання сітки…");
    try {
      await postJSON(urls.archive, { id: id });
      if (state.selectedId === id) resetForm();
      await loadGrids();
      markSaved();
      toast("Сітку перенесено до архіву.");
    } catch (error) {
      if (error.status === 409 || error.code === "size_grid_in_use") {
        toast("Сітка вже призначена товарам. Спочатку перепризначте їх на іншу сітку.", true);
      } else {
        toast(error.message, true);
      }
      setReadiness("error", "Архівування заблоковано");
    } finally {
      setBusy(false);
    }
  }

  function selectGrid(id) {
    if (state.dirty && !window.confirm("Незбережені зміни буде втрачено. Відкрити іншу сітку?")) return;
    var grid = state.grids.find(function (item) { return item.id === id; });
    if (grid) loadGridIntoEditor(grid);
  }

  function addColumn() {
    var column = { uid: uid("col"), key: "measurement_" + state.columns.length, label: "Новий замір" };
    state.columns.push(column);
    state.rows.forEach(function (row) { row.values[column.uid] = ""; });
    renderEditor();
    markDirty();
    var inputs = columnList.querySelectorAll("[data-column-key]");
    inputs[inputs.length - 1].select();
  }

  function removeColumn(index) {
    if (index === 0 || state.columns.length <= 1) return;
    var removed = state.columns.splice(index, 1)[0];
    state.rows.forEach(function (row) { delete row.values[removed.uid]; });
    renderEditor();
    markDirty();
  }

  function addRow() {
    var values = {};
    state.columns.forEach(function (column) { values[column.uid] = ""; });
    var row = { uid: uid("row"), values: values };
    state.rows.push(row);
    renderMeasurementTable();
    renderPreview();
    markDirty();
    var input = measurementBody.querySelector("tr:last-child [data-cell-key='size']");
    if (input) input.focus();
  }

  function removeRow(rowUid) {
    if (state.rows.length <= 1) {
      showError("rows", "Сітка повинна містити хоча б один розмір.");
      return;
    }
    state.rows = state.rows.filter(function (row) { return row.uid !== rowUid; });
    renderMeasurementTable();
    renderPreview();
    markDirty();
  }

  function moveRow(rowUid, direction) {
    var index = state.rows.findIndex(function (row) { return row.uid === rowUid; });
    var target = index + direction;
    if (index < 0 || target < 0 || target >= state.rows.length) return;
    var moved = state.rows.splice(index, 1)[0];
    state.rows.splice(target, 0, moved);
    renderMeasurementTable();
    renderPreview();
    markDirty();
    var focused = measurementBody.querySelector("[data-row-uid='" + rowUid + "'] [data-action='move-row-" + (direction < 0 ? "up" : "down") + "']");
    if (focused) focused.focus();
  }

  root.addEventListener("click", function (event) {
    var target = event.target.closest("button");
    if (!target || !root.contains(target)) return;
    var action = target.dataset.action;
    var card = target.closest("[data-grid-id]");
    var id = card ? Number(card.dataset.gridId) : null;
    if (action === "new-grid") {
      if (!state.dirty || window.confirm("Очистити незбережені зміни та створити нову сітку?")) resetForm();
    } else if (action === "select-grid") {
      selectGrid(id);
    } else if (action === "duplicate-grid") {
      duplicateGrid(id);
    } else if (action === "archive-grid") {
      archiveGrid(id);
    } else if (action === "add-column") {
      addColumn();
    } else if (action === "remove-column") {
      var columnNode = target.closest("[data-column-index]");
      removeColumn(Number(columnNode.dataset.columnIndex));
    } else if (action === "add-row") {
      addRow();
    } else if (action === "remove-row") {
      removeRow(target.closest("[data-row-uid]").dataset.rowUid);
    } else if (action === "move-row-up") {
      moveRow(target.closest("[data-row-uid]").dataset.rowUid, -1);
    } else if (action === "move-row-down") {
      moveRow(target.closest("[data-row-uid]").dataset.rowUid, 1);
    }

    if (target.dataset.viewport) {
      var viewport = target.dataset.viewport;
      root.querySelectorAll("[data-viewport]").forEach(function (button) {
        var active = button.dataset.viewport === viewport;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      root.querySelector("[data-preview-stage]").classList.toggle("is-mobile", viewport === "mobile");
      root.querySelector("[data-viewport-readout]").textContent = viewport === "mobile" ? "375 px" : "Авто";
    }

    if (target.dataset.fit != null) {
      state.filters.fit = target.dataset.fit;
      root.querySelectorAll("[data-fit]").forEach(function (button) {
        var active = button === target;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      renderLibrary();
    }

    if (target.dataset.garment != null) {
      state.filters.garment = target.dataset.garment;
      root.querySelectorAll("[data-garment]").forEach(function (button) {
        var active = button === target;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      renderLibrary();
    }
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    saveGrid();
  });

  form.addEventListener("input", function (event) {
    var columnNode = event.target.closest("[data-column-index]");
    if (columnNode) {
      var column = state.columns[Number(columnNode.dataset.columnIndex)];
      if (event.target.matches("[data-column-key]")) column.key = event.target.value.toLowerCase().replace(/\s+/g, "_");
      if (event.target.matches("[data-column-label]")) column.label = event.target.value;
      refreshColumnMetadata(Number(columnNode.dataset.columnIndex));
    }
    var rowNode = event.target.closest("[data-row-uid]");
    if (rowNode && event.target.dataset.columnUid) {
      var row = state.rows.find(function (item) { return item.uid === rowNode.dataset.rowUid; });
      if (row) row.values[event.target.dataset.columnUid] = event.target.value;
    }
    markDirty();
    renderPreview();
  });

  root.querySelector("[data-filter='search']").addEventListener("input", function (event) {
    state.filters.search = event.target.value.trim();
    renderLibrary();
  });

  root.querySelector("[data-filter='catalog']").addEventListener("change", function (event) {
    state.filters.catalog = event.target.value;
    renderLibrary();
  });

  window.addEventListener("beforeunload", function (event) {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  setupCatalogs();
  resetForm();
  loadGrids().catch(function () { /* Error state is already rendered. */ });
})();
