const form = document.getElementById("search-form");
const submitBtn = document.getElementById("submit-btn");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");

const STORE_ORDER = ["Checkers", "Pick n Pay", "Woolworths"];

let currentSource = null;

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const product = document.getElementById("product").value.trim();
  const address = document.getElementById("address").value.trim();
  if (!product || !address) return;

  startSearch(product, address);
});

function startSearch(product, address) {
  if (currentSource) currentSource.close();

  submitBtn.disabled = true;
  submitBtn.textContent = "Comparing...";
  statusEl.hidden = false;
  statusEl.innerHTML = "";
  resultsEl.innerHTML = "";

  for (const store of STORE_ORDER) {
    addStatusLine(`checking-${store}`, `Checking ${store} ...`);
  }

  const url = `/compare/stream?product=${encodeURIComponent(product)}&address=${encodeURIComponent(address)}`;
  const source = new EventSource(url);
  currentSource = source;

  source.onmessage = (e) => {
    const event = JSON.parse(e.data);
    if (event.type === "status") {
      updateStatusLine(
        `checking-${event.retailer}`,
        `${event.retailer} done — ${event.offers} offer${event.offers === 1 ? "" : "s"} found`,
        true
      );
    } else if (event.type === "result") {
      renderResult(event);
    } else if (event.type === "done") {
      addStatusLine("done", `All stores checked in ${event.elapsed.toFixed(1)}s`, true);
      finishSearch();
    }
  };

  source.onerror = () => {
    addStatusLine("error", "Connection lost — try again.", true);
    finishSearch();
  };
}

function finishSearch() {
  submitBtn.disabled = false;
  submitBtn.textContent = "Compare prices";
  if (currentSource) {
    currentSource.close();
    currentSource = null;
  }
}

function addStatusLine(id, text, done = false) {
  const line = document.createElement("div");
  line.className = "line" + (done ? " done" : "");
  line.id = `status-${id}`;
  line.textContent = text;
  statusEl.appendChild(line);
}

function updateStatusLine(id, text, done = false) {
  const line = document.getElementById(`status-${id}`);
  if (line) {
    line.textContent = text;
    if (done) line.classList.add("done");
  } else {
    addStatusLine(id, text, done);
  }
}

function formatPrice(price) {
  return `R${price.toFixed(2)}`;
}

function renderResult(result) {
  const card = document.createElement("div");
  card.className = "result-card";

  const heading = document.createElement("h2");
  heading.textContent = result.label;
  card.appendChild(heading);

  const table = document.createElement("table");
  const cheapestRetailer = result.cheapest ? result.cheapest.retailer : null;

  for (const store of STORE_ORDER) {
    const offer = result.matches[store];
    const row = document.createElement("tr");

    if (!offer) {
      row.className = "no-match";
      row.innerHTML = `<td>${store}</td><td colspan="2">no confident match found</td>`;
      table.appendChild(row);
      continue;
    }

    if (store === cheapestRetailer) row.className = "cheapest";

    const dealHtml = offer.deal_label
      ? `<br><span class="deal-badge">${escapeHtml(offer.deal_label)}</span>`
      : "";

    row.innerHTML = `
      <td>${store}</td>
      <td class="price">${formatPrice(offer.price)}${dealHtml}</td>
      <td>${offer.url ? `<a href="${offer.url}" target="_blank" rel="noopener">${escapeHtml(offer.name)}</a>` : escapeHtml(offer.name)}</td>
    `;
    table.appendChild(row);
  }

  card.appendChild(table);
  resultsEl.appendChild(card);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
