"use strict";

(() => {
  const names = {
    "axon-heuristic": "Regex",
    laya: "Laya",
    strands: "Strands",
    "llm-router": "GPT-4o Mini",
  };
  const search = document.querySelector("#case-search");
  const task = document.querySelector("#case-task");
  const variant = document.querySelector("#case-variant");
  const errors = document.querySelector("#case-errors");
  const status = document.querySelector("#case-status");
  const list = document.querySelector("#case-list");
  const more = document.querySelector("#case-more");
  let cases = [];
  let limit = 12;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }

  function render() {
    const query = search.value.trim().toLowerCase();
    const matches = cases.filter((item) =>
      (!query || item.prompt.toLowerCase().includes(query))
      && (!task.value || item.expected_task === task.value)
      && (!variant.value || item.variant === variant.value)
      && (!errors.checked || Object.values(item.predictions).some((p) => p !== item.expected_task))
    );
    list.replaceChildren();
    matches.slice(0, limit).forEach((item) => {
      const article = element("article", "case", "");
      article.dataset.caseId = item.id;
      article.append(element("p", "case-meta",
        `Expected: ${item.expected_task} · ${item.variant.replaceAll("_", " ")} · ${item.domain.replaceAll("_", " ")}`));
      article.append(element("p", "case-prompt", item.prompt));
      const predictions = element("div", "predictions", "");
      Object.entries(names).forEach(([strategy, name]) => {
        const predicted = item.predictions[strategy];
        const correct = predicted === item.expected_task;
        predictions.append(element("span", `prediction${correct ? "" : " incorrect"}`,
          `${name}: ${predicted}${correct ? "" : " · incorrect"}`));
      });
      article.append(predictions);
      list.append(article);
    });
    status.textContent = `${matches.length} matching test prompts · showing ${Math.min(limit, matches.length)}`;
    more.hidden = limit >= matches.length;
  }

  [search, task, variant, errors].forEach((control) => {
    control.addEventListener("input", () => { limit = 12; render(); });
  });
  more.addEventListener("click", () => { limit += 12; render(); });
  fetch("benchmark-cases.json", { credentials: "omit" })
    .then((response) => {
      if (!response.ok) throw new Error("Static dataset unavailable");
      return response.json();
    })
    .then((data) => {
      cases = data.cases;
      render();
    })
    .catch(() => {
      status.textContent = "The static dataset could not be loaded. Use the dataset download link below.";
      more.hidden = true;
    });
})();
