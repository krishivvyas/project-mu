// Project Mu New Tab: put the cursor in the search box. Chromium gives the
// address bar focus on a new tab; typing "/" or clicking the page moves it here.
const q = document.getElementById("q");
q.focus();
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== q) { e.preventDefault(); q.focus(); }
});

// Return to Mu: hold the inkan seal until it fills, and every tab and window
// closes, this session's browsing data is cleared, and one fresh page
// remains (background.js does the work). A press shorter than the hold does
// nothing but show the hint, so it is deliberate, never an accident.
const seal = document.getElementById("seal");
const hint = document.getElementById("seal-hint");
const line = document.getElementById("line");
const HOLD_MS = 1100;  // keep in step with --hold in newtab.css
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
let timer = 0;
let started = 0;
let done = false;

function startHold() {
  if (done || timer) return;
  started = performance.now();
  seal.classList.add("holding");
  timer = setTimeout(returnToMu, HOLD_MS);
}

function cancelHold() {
  if (!timer) return;
  clearTimeout(timer);
  timer = 0;
  seal.classList.remove("holding");
  if (performance.now() - started < 300) {  // a click, not a hold: say how
    hint.classList.add("nudge");
    setTimeout(() => hint.classList.remove("nudge"), 2200);
  }
}

seal.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  seal.setPointerCapture(e.pointerId);
  startHold();
});
for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
  seal.addEventListener(type, cancelHold);
}
seal.addEventListener("keydown", (e) => {
  if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); startHold(); }
});
seal.addEventListener("keyup", (e) => {
  if (e.key === " " || e.key === "Enter") cancelHold();
});
seal.addEventListener("blur", cancelHold);
seal.addEventListener("contextmenu", (e) => e.preventDefault());

async function returnToMu() {
  timer = 0;
  done = true;
  document.body.classList.add("stamped");
  // Let the seal land before the tabs go (the background closes this one).
  await new Promise((r) => setTimeout(r, reducedMotion ? 150 : 650));
  let reply;
  try {
    reply = await chrome.runtime.sendMessage({ type: "return-to-mu" });
  } catch (err) {
    reply = { ok: false, error: String(err) };
  }
  if (!reply?.ok) {  // still here: something stopped it
    document.body.classList.remove("stamped");
    seal.classList.remove("holding");
    done = false;
    line.textContent = "Could not return to Mu. Nothing was cleared.";
  }
}

// The fresh page left after returning: let the ink settle back in.
chrome.storage.session.get("returned").then(({ returned }) => {
  if (!returned || Date.now() - returned > 10000) return;
  chrome.storage.session.remove("returned");
  document.body.classList.add("returned");
  line.textContent = "Returned to nothing.";
}).catch(() => {});
