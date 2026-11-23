// Project Mu New Tab: put the cursor in the search box. Chromium gives the
// address bar focus on a new tab; typing "/" or clicking the page moves it here.
const q = document.getElementById("q");
q.focus();
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== q) { e.preventDefault(); q.focus(); }
});
