(function (global, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else global.CustomPrintCollection = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const MAX_ITEMS = 10;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  function create() {
    let entries = [];
    return {
      list: () => entries.slice(),
      get: (id) => entries.find((item) => item.id === id),
      save(item) {
        const index = entries.findIndex((entry) => entry.id === item.id);
        if (!item.id || !item.snapshot?.product?.type) throw new Error("invalid_item");
        if (index < 0 && entries.length >= MAX_ITEMS) throw new Error("item_limit");
        const saved = {
          id: item.id, snapshot: clone(item.snapshot), state: clone(item.state),
          files: new Map(Array.from(item.files || [], ([key, files]) => [key, files.slice()])),
          photo: item.photo || null,
        };
        if (index >= 0) entries[index] = saved;
        else entries.push(saved);
        return saved;
      },
      remove(id) { entries = entries.filter((item) => item.id !== id); },
      clear() { entries = []; },
      serialize: () => entries.map(({ id, snapshot, state }) => ({ id, snapshot: clone(snapshot), state: clone(state) })),
      restore(saved) {
        entries = [];
        for (const item of (Array.isArray(saved) ? saved : []).slice(0, MAX_ITEMS)) {
          if (!/^[a-zA-Z0-9_-]{1,64}$/.test(item?.id || "") || !item.snapshot?.product?.type || !item.state || entries.some((entry) => entry.id === item.id)) continue;
          entries.push({ id: item.id, snapshot: clone(item.snapshot), state: clone(item.state), files: new Map(), photo: null });
        }
      },
    };
  }
  function missingFiles(item) {
    const references = (item.snapshot.artwork.files || []).filter((file) => file.role !== "garment_reference");
    if ((item.snapshot.artwork.files || []).some((file) => file.role === "garment_reference") && !item.photo) return true;
    if (references.some((file) => !(item.files?.get(file.placement_key) || []).some((upload) => upload.name === file.name))) return true;
    if (!["ready", "adjust"].includes(item.snapshot?.artwork?.service_kind)) return false;
    const specs = item.snapshot.placement_specs;
    if (Array.isArray(specs)) return specs.some((spec) => spec.requires_artwork_file && !(item.files?.get(spec.placement_key) || []).length);
    return (item.snapshot.print?.zones || []).some((zone) => {
      const options = item.snapshot.print.zone_options?.[zone] || {};
      if (zone === "sleeve") return ["left", "right"].some((side) => options[`${side}_enabled`] && options[`${side}_mode`] !== "full_text" && !(item.files?.get(`sleeve_${side}`) || []).length);
      if (zone === "hem") return options.mode !== "text" && !(item.files?.get(`hem_${options.side}`) || []).length;
      if (zone === "shoulder") return ["left", "right"].some((side) => options[`${side}_enabled`] && !(item.files?.get(`shoulder_${side}`) || []).length);
      return !(item.files?.get(zone) || []).length;
    });
  }
  function pricing(items, giftPrice = 0) {
    const estimate = items.some((item) => item.snapshot?.pricing?.estimate_required || !Number.isFinite(item.snapshot?.pricing?.final_total));
    const quantity = items.reduce((sum, item) => sum + Number(item.snapshot.order?.quantity || 0), 0);
    const knownTotal = items.reduce((sum, item) => sum + (Number.isFinite(item.snapshot?.pricing?.final_total) ? item.snapshot.pricing.final_total : 0), 0);
    return { quantity, final_total: estimate ? null : knownTotal + giftPrice, known_total: knownTotal, gift_price: giftPrice, estimate_required: estimate, base_price: estimate ? null : knownTotal, unit_total: null };
  }
  return { create, pricing, missingFiles, MAX_ITEMS };
});
