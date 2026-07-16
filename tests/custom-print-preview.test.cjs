const assert = require("node:assert/strict");
const test = require("node:test");

require("../twocomms/twocomms_django_theme/static/js/custom-print-preview.js");

const { computeZoneBox } = globalThis.CustomPrintPreview;

const formats = {
  A6: { width_mm: 105, height_mm: 148 },
  A5: { width_mm: 148, height_mm: 210 },
  A4: { width_mm: 210, height_mm: 297 },
  A3: { width_mm: 297, height_mm: 420 },
  A2: { width_mm: 420, height_mm: 594 },
};

test("ISO preview zones preserve physical aspect ratio within 0.5 percent", () => {
  const calibration = {
    garment_width_mm: 600,
    zones: { body: { width: 50 } },
  };

  for (const [name, dimensions] of Object.entries(formats)) {
    const box = computeZoneBox(dimensions, calibration, { width: 1200, height: 1400 });
    const renderedRatio = (box.width * 1200) / (box.height * 1400);
    const physicalRatio = dimensions.width_mm / dimensions.height_mm;
    const error = Math.abs(renderedRatio - physicalRatio) / physicalRatio;
    assert.ok(error <= 0.005, `${name} ratio error was ${(error * 100).toFixed(4)}%`);
  }
});
