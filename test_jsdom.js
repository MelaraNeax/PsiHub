const fs = require('fs');
const jsdom = require('jsdom');
const { JSDOM } = jsdom;

const html = fs.readFileSync('index.html', 'utf8');
const script = fs.readFileSync('app.js', 'utf8');

const virtualConsole = new jsdom.VirtualConsole();
virtualConsole.on("error", () => { console.log("Error:", ...arguments); });
virtualConsole.on("warn", () => { console.log("Warn:", ...arguments); });
virtualConsole.on("info", () => { console.log("Info:", ...arguments); });
virtualConsole.on("dir", () => { console.log("Dir:", ...arguments); });

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  virtualConsole
});
const window = dom.window;
const document = window.document;

const scriptEl = document.createElement('script');
scriptEl.type = 'module';
scriptEl.textContent = script;

try {
  document.body.appendChild(scriptEl);
} catch (e) {
  console.error("DOM error:", e);
}

setTimeout(() => {
  console.log("JSDOM finished test.");
}, 2000);
