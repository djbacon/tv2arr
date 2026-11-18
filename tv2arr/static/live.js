const refreshHeaders = {"X-TV2ARR-Refresh": "1"};
const regions = new Map(
  Array.from(document.querySelectorAll("[data-refresh]"), region => [region.dataset.refresh, region])
);
const scanForm = document.querySelector("[data-scan-form]");
const scanButton = regions.get("scan");
const scanStatus = document.querySelector("[data-scan-status]");
let timer;
let refreshing = false;
let startingScan = false;

function schedule(delay = 3000) {
  clearTimeout(timer);
  if (!document.hidden && !refreshing) {
    timer = setTimeout(refresh, delay);
  }
}

async function refresh() {
  if (document.hidden || refreshing) return;

  refreshing = true;
  try {
    const response = await fetch(location.href, {
      headers: refreshHeaders,
      redirect: "error",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok || !response.headers.get("content-type")?.startsWith("text/html")) return;

    const html = await response.text();
    if (document.hidden) return;
    // The same-origin fragment contains server-rendered, escaped markup.
    const fragment = document.createElement("template");
    fragment.innerHTML = html;

    for (const next of fragment.content.querySelectorAll("[data-refresh]")) {
      const current = regions.get(next.dataset.refresh);
      if (!current) continue;

      if (current === scanButton) {
        if (startingScan) continue;
        const nextState = next.getAttribute("aria-disabled");
        if (current.getAttribute("aria-disabled") === "true" && nextState === "false") {
          scanStatus.textContent = "TV3 scan finished.";
        }
        current.setAttribute("aria-disabled", nextState);
      } else if (current.matches(":focus-within") || current.querySelector(":active")) {
        // Let the user finish interacting before replacing this region.
        continue;
      }

      if (current.innerHTML !== next.innerHTML) {
        current.replaceChildren(...next.childNodes);
      }
    }
  } catch {
    // Leave the current content in place until the next successful poll.
  } finally {
    refreshing = false;
    schedule();
  }
}

scanForm?.addEventListener("submit", async event => {
  event.preventDefault();
  if (startingScan || scanButton.getAttribute("aria-disabled") === "true") return;

  startingScan = true;
  scanButton.setAttribute("aria-disabled", "true");
  try {
    const response = await fetch(scanForm.action, {
      method: "POST",
      body: new URLSearchParams(new FormData(scanForm)),
      headers: refreshHeaders,
      redirect: "error",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error("Scan start failed");
    const {started} = await response.json();
    scanStatus.textContent = started ? "TV3 scan started." : "A scan is already running.";
  } catch {
    scanButton.setAttribute("aria-disabled", "false");
    scanStatus.textContent = "Could not confirm the scan. If it doesn't start, try again.";
  } finally {
    startingScan = false;
    schedule(0);
  }
});

document.addEventListener("visibilitychange", () => schedule(0));
schedule();
