const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class ClassList {
  constructor() { this.values = new Set(); }
  toggle(name, enabled) {
    if (enabled) this.values.add(name);
    else this.values.delete(name);
  }
  contains(name) { return this.values.has(name); }
}

function element() {
  return {
    classList: new ClassList(),
    hidden: true,
    parentNode: null,
    dataset: {},
    style: {},
    addEventListener() {},
  };
}

const appbar = element();
const mobileBar = element();
const exitButton = element();
const managerButton = element();
const stepLabel = element();
const progress = element();
const root = element();
root.querySelector = (selector) => ({
  "[data-studio-appbar]": appbar,
  "[data-studio-exit]": exitButton,
  "[data-manager-open]": managerButton,
  "[data-appbar-step]": stepLabel,
  "[data-appbar-progress]": progress,
}[selector] || null);
root.querySelectorAll = () => [];

const body = element();
body.append = (node) => { node.parentNode = body; };
body.appendChild = body.append;
const context = {
  document: { body },
  globalThis: {},
};
context.globalThis = context;
vm.createContext(context);
const source = fs.readFileSync(path.join(__dirname, "../twocomms/twocomms_django_theme/static/js/custom-print-mobile-shell.js"), "utf8");
vm.runInContext(source, context);

const shell = context.CustomPrintMobileShell.create({
  root,
  mobileBar,
  onExit() {},
  onManager() {},
  onPreview() {},
});

assert.equal(appbar.parentNode, body, "top app bar must escape the page scroll container");
assert.equal(mobileBar.parentNode, body, "bottom action bar must escape the page scroll container");
shell.setActive(true);
assert.equal(appbar.hidden, false);
assert.equal(mobileBar.hidden, false);
assert.equal(body.classList.contains("cp-studio-active"), true);
shell.setActive(false);
assert.equal(appbar.hidden, true);
assert.equal(mobileBar.hidden, true);

console.log("custom print mobile shell contract: ok");
