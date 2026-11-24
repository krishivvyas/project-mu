// Project Mu New Tab: the window Chromium opens at startup can be created
// before this extension finishes loading, so its first tab shows Chromium's
// own New Tab page. Once loaded, replace any such tab with Mu's page. Tabs
// already showing Mu's page are left alone.

const NEW_TAB = /^chrome:\/\/(newtab|new-tab-page)/;
const handled = new Set();  // tab ids already replaced (or being replaced)
let running = null;         // one pass at a time: several events fire at startup

async function claimNewTabs() {
  const ours = new Set(
    (await chrome.runtime.getContexts({ contextTypes: ["TAB"] })).map((c) => c.tabId));
  for (const tab of await chrome.tabs.query({})) {
    const url = tab.pendingUrl || tab.url || "";
    if (!NEW_TAB.test(url) || ours.has(tab.id) || handled.has(tab.id)) continue;
    handled.add(tab.id);
    // Replace the tab rather than navigating it, so Back does not lead to
    // Chromium's page. Create first: closing a window's last tab closes it.
    const fresh = await chrome.tabs.create({ windowId: tab.windowId, index: tab.index,
                                             active: tab.active, url: "chrome://newtab/" });
    handled.add(fresh.id);
    await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

function claimOnce() {
  running = (running || Promise.resolve()).then(claimNewTabs).catch(() => {});
  return running;
}

chrome.runtime.onStartup.addListener(claimOnce);
chrome.runtime.onInstalled.addListener(claimOnce);
claimOnce();

// Return to Mu: asked for by the inkan seal on Mu's New Tab page (newtab.js).
// Open one fresh New Tab page, close every other tab and window, then clear
// the session's browsing data, as if Mu had just started. Clearing history
// also forgets the closed tabs, so Ctrl+Shift+T brings nothing back. Kage
// (private) windows are not touched: this extension does not run there.
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg?.type !== "return-to-mu" || sender.id !== chrome.runtime.id || !sender.tab) return;
  returnToMu(sender.tab).then(() => reply({ ok: true }),
                              (err) => reply({ ok: false, error: String(err) }));
  return true;  // reply is sent asynchronously
});

async function returnToMu(from) {
  await chrome.storage.session.set({ returned: Date.now() });  // the fresh page settles in
  const fresh = await chrome.tabs.create({ windowId: from.windowId, url: "chrome://newtab/",
                                           active: true });
  handled.add(fresh.id);
  const others = (await chrome.tabs.query({})).filter((t) => t.id !== fresh.id).map((t) => t.id);
  if (others.length) await chrome.tabs.remove(others);
  // Close the tabs first, so no page can write anything back afterwards.
  // Passwords stay; site data, cookies, cache, history and downloads go.
  await chrome.browsingData.remove({ since: 0 }, {
    cache: true, cacheStorage: true, cookies: true, downloads: true, fileSystems: true,
    formData: true, history: true, indexedDB: true, localStorage: true,
    serviceWorkers: true, webSQL: true,
  });
}
