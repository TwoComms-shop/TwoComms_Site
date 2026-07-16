(function (global) {
  const groups = [
    { key: "format", internal: ["mode"] },
    { key: "garment", internal: ["product", "config"] },
    { key: "placement", internal: ["zones"] },
    { key: "artwork", internal: ["artwork"] },
    { key: "quantity", internal: ["quantity", "gift"] },
    { key: "contact", internal: ["contact"] },
  ];

  function fromInternal(step) {
    return groups.find((group) => group.internal.includes(step))?.key || "format";
  }

  function firstInternal(studioStep) {
    return groups.find((group) => group.key === studioStep)?.internal[0] || "mode";
  }

  function isComplete(studioStep, doneSteps) {
    const group = groups.find((item) => item.key === studioStep);
    return !!group && group.internal.every((step) => doneSteps.has(step));
  }

  function progressIndex(internalStep) {
    return Math.max(0, groups.findIndex((group) => group.internal.includes(internalStep)));
  }

  global.CustomPrintStateTools = { groups, fromInternal, firstInternal, isComplete, progressIndex };
})(globalThis);
