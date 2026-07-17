(function (global) {
  function formatCm(valueMm) {
    return (valueMm / 10).toLocaleString(document.documentElement.lang || "uk-UA", {
      maximumFractionDigits: 1,
    });
  }

  function profileKey(state) {
    const type = state.product.type || "hoodie";
    if (type === "longsleeve") return "longsleeve:regular";
    const fit = state.product.fit || "regular";
    return `${type}:${fit}`;
  }

  function computeZoneBox(dimensions, calibration, canvas = { width: 1200, height: 1400 }) {
    const bodyWidth = calibration.zones.body.width;
    const width = (dimensions.width_mm / calibration.garment_width_mm) * bodyWidth;
    const height = (dimensions.height_mm / calibration.garment_width_mm) * bodyWidth * (canvas.width / canvas.height);
    return { width, height, dimensions };
  }

  function create({ root, config, getState }) {
    const previewNodes = Array.from(root.querySelectorAll("[data-png-preview]"));

    function zoneBox(format, calibration) {
      const dimensions = config.format_dimensions?.[format];
      if (!dimensions) return null;
      return computeZoneBox(dimensions, calibration, calibration.canvas);
    }

    function appendZone(container, placement, calibration, view) {
      const isBody = placement.zone === "front" || placement.zone === "back";
      if (isBody && placement.zone !== view) return;
      if (placement.zone === "sleeve" && view !== "front") return;
      const format = placement.zone === "sleeve" ? "A6" : (placement.size_preset || "A4");
      const box = zoneBox(format, calibration);
      if (!box) return;
      const anchorKey = placement.zone === "sleeve"
        ? `sleeve_${placement.side || "left"}`
        : placement.zone;
      const anchor = calibration.zones[anchorKey] || calibration.zones[view];
      if (!anchor) return;

      const zone = document.createElement("div");
      zone.className = "cp-preview-zone";
      zone.style.left = `${anchor.x}%`;
      zone.style.top = `${anchor.y}%`;
      zone.style.width = `${box.width}%`;
      zone.style.height = `${box.height}%`;
      if (anchor.rotate) zone.style.rotate = `${anchor.rotate}deg`;
      zone.innerHTML = `<strong>${format}</strong><span>${formatCm(box.dimensions.width_mm)} × ${formatCm(box.dimensions.height_mm)} см</span>`;
      container.appendChild(zone);
    }

    function expandedPlacements(state) {
      const result = [];
      for (const zone of state.print.zones || []) {
        const options = state.print.zone_options?.[zone] || {};
        if (zone === "sleeve") {
          if (options.left_enabled !== false) result.push({ zone, side: "left" });
          if (options.right_enabled) result.push({ zone, side: "right" });
        } else {
          result.push({ zone, size_preset: options.size_preset || "A4" });
        }
      }
      return result;
    }

    function render() {
      const state = getState();
      const key = profileKey(state);
      const assets = config.preview_assets?.[key] || config.preview_assets?.["hoodie:regular"];
      const calibration = config.preview_calibration?.[key] || config.preview_calibration?.["hoodie:regular"];
      if (!assets || !calibration) return;
      const view = state.ui.stage_view === "back" ? "back" : "front";
      const productConfig = config.products?.[state.product.type] || {};
      const selectedFabric = (productConfig.fabrics?.[state.product.fit] || []).find((item) => item.value === state.product.fabric);
      const palette = selectedFabric?.colors || productConfig.colors || [];
      const color = palette.find((item) => item.value === state.product.color)?.hex || "#303036";
      const placements = expandedPlacements(state);

      previewNodes.forEach((preview) => {
        preview.classList.remove("is-refreshing");
        requestAnimationFrame(() => preview.classList.add("is-refreshing"));
        const garment = preview.querySelector("[data-preview-garment]");
        const tint = preview.querySelector("[data-preview-color]");
        const lacing = preview.querySelector("[data-preview-lacing]");
        const zones = preview.querySelector("[data-preview-zones]");
        const asset = assets[view] || assets.front;
        if (garment) {
          garment.src = asset;
          garment.alt = state.product.type ? `${state.product.type} · ${view}` : "";
        }
        if (tint) {
          tint.style.backgroundColor = color;
          tint.style.setProperty("--cp-preview-mask", `url("${asset}")`);
        }
        if (lacing) {
          lacing.src = assets.lacing || "";
          lacing.hidden = !(assets.lacing && view === "front" && state.print.add_ons?.includes("lacing"));
        }
        if (zones) {
          zones.replaceChildren();
          placements.forEach((placement) => appendZone(zones, placement, calibration, view));
        }
      });
    }

    function bindPreviewMotion(preview) {
      const motionQuery = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
      if (motionQuery?.matches) return;
      preview.addEventListener("pointermove", (event) => {
        const rect = preview.getBoundingClientRect();
        const x = ((event.clientX - rect.left) / Math.max(rect.width, 1) - 0.5) * 2;
        const y = ((event.clientY - rect.top) / Math.max(rect.height, 1) - 0.5) * 2;
        preview.style.setProperty("--cp-preview-tilt-x", `${(y * -1.8).toFixed(2)}deg`);
        preview.style.setProperty("--cp-preview-tilt-y", `${(x * 2.4).toFixed(2)}deg`);
      });
      preview.addEventListener("pointerleave", () => {
        preview.style.removeProperty("--cp-preview-tilt-x");
        preview.style.removeProperty("--cp-preview-tilt-y");
      });
    }

    previewNodes.forEach(bindPreviewMotion);

    return { render };
  }

  global.CustomPrintPreview = { create, computeZoneBox };
})(globalThis);
