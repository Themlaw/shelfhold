// Confirmation of the link with an identity provider: one answer, yes or no, sent as JSON with the
// token of the flow (a cookie the page reads, never a value written in the page).
(() => {
  "use strict";
  const root = document.getElementById("confirm");
  const message = document.getElementById("message");
  const buttons = root.querySelectorAll("button");

  const show = (title, action) => {
    message.querySelector(".alert__title").textContent = title;
    message.querySelector(".alert__body").textContent = action || "";
    message.hidden = false;
  };
  const csrf = () => {
    const wanted = root.dataset.csrfCookie + "=";
    for (const part of document.cookie.split("; ")) {
      if (part.startsWith(wanted)) return decodeURIComponent(part.slice(wanted.length));
    }
    return "";
  };
  const lock = (locked) => {
    buttons.forEach((button) => {
      if (locked) {
        button.setAttribute("aria-disabled", "true");
      } else {
        button.removeAttribute("aria-disabled");
      }
    });
    root.toggleAttribute("aria-busy", locked);
  };
  const answer = async (confirm) => {
    if (root.hasAttribute("aria-busy")) return;
    message.hidden = true;
    lock(true);
    let title = root.dataset.error;
    let action = "";
    try {
      const response = await fetch(root.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ confirm }),
      });
      if (response.ok) {
        window.location.assign(confirm ? root.dataset.done : root.dataset.declined);
        return;
      }
      const problem = await response.json();
      if (problem && problem.title) title = problem.title;
      if (problem && problem.action) action = problem.action;
    } catch (error) {
      // network failure: the generic message stays
    }
    show(title, action);
    lock(false);
  };
  document.getElementById("yes").addEventListener("click", () => answer(true));
  document.getElementById("no").addEventListener("click", () => answer(false));
})();
