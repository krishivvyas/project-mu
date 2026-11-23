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
