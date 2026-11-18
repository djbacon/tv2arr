const badge = document.querySelector("[data-version-url]");
const current = badge.textContent.trim();
const updateParam = "_tv2arr_version";
let timer;
let checking = false;
let navigating = false;

const pageUrl = new URL(location.href);
if (pageUrl.searchParams.get(updateParam) === current) {
  pageUrl.searchParams.delete(updateParam);
  history.replaceState(history.state, "", pageUrl);
}

function schedule(delay = 10000) {
  clearTimeout(timer);
  if (!document.hidden && !checking && !navigating) {
    timer = setTimeout(checkVersion, delay);
  }
}

async function checkVersion() {
  if (document.hidden || checking || navigating) return;

  checking = true;
  try {
    const response = await fetch(badge.dataset.versionUrl, {
      cache: "no-store",
      headers: {"X-TV2ARR-Version": current},
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) return;
    const {version} = await response.json();
    if (document.hidden || version === current || !/^v\d+\.\d+\.\d+$/.test(version)) return;

    // A fresh GET keeps filters and avoids resubmitting any previous form.
    const target = new URL(location.href);
    if (target.searchParams.get(updateParam) === version) return;
    target.searchParams.set(updateParam, version);
    location.replace(target);
    navigating = true;
  } catch {
    // Retry after transient failures, including while the container restarts.
  } finally {
    checking = false;
    schedule();
  }
}

document.addEventListener("visibilitychange", () => schedule(0));
schedule(0);
