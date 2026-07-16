(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.f5Inventory = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function resolveInventoryRule(rules, fitCode, size) {
    const normalizedFit = String(fitCode || '');
    const normalizedSize = String(size || '');
    let general = null;
    let exact = null;
    (rules || []).forEach((rule) => {
      if (String(rule && rule.size || '') !== normalizedSize) return;
      const ruleFit = String(rule && rule.fit_code || '');
      if (!ruleFit) general = rule;
      if (ruleFit === normalizedFit) exact = rule;
    });
    return exact || general || null;
  }

  function normalizedRow(row) {
    return {
      fit_code: String(row && row.fit_code || ''),
      size: String(row && row.size || ''),
      is_enabled: !row || row.is_enabled !== false,
      stock: !row || row.stock == null || row.stock === '' ? null : Number(row.stock),
      note: String(row && row.note || ''),
    };
  }

  function rowSignature(row) {
    return JSON.stringify([row.is_enabled, row.stock, row.note]);
  }

  function canonicalizeInventoryRows(rows) {
    const groups = new Map();
    (rows || []).map(normalizedRow).filter((row) => row.size).forEach((row) => {
      if (!groups.has(row.size)) groups.set(row.size, []);
      groups.get(row.size).push(row);
    });

    const result = [];
    groups.forEach((sizeRows, size) => {
      const signatures = new Set(sizeRows.map(rowSignature));
      if (signatures.size === 1) {
        result.push(Object.assign({}, sizeRows[0], { fit_code: '', size }));
      } else {
        result.push(...sizeRows);
      }
    });
    return result;
  }

  function snapshotInventoryDraft(variant) {
    return canonicalizeInventoryRows((variant && variant.sizes) || [])
      .map((row) => Object.assign({}, row));
  }

  function replaceInventoryDraft(variant, rows) {
    if (!variant) return [];
    variant.sizes = canonicalizeInventoryRows(rows)
      .map((row) => Object.assign({}, row));
    variant._sizesRevision = (variant._sizesRevision || 0) + 1;
    variant._revision = (variant._revision || 0) + 1;
    variant._sizesDirty = true;
    variant._dirty = true;
    return snapshotInventoryDraft(variant);
  }

  return {
    canonicalizeInventoryRows,
    replaceInventoryDraft,
    resolveInventoryRule,
    snapshotInventoryDraft,
  };
}));
