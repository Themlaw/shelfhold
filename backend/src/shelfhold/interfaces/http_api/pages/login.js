// Password sign-in of the login page: the form is sent as JSON to the endpoint it names, so the
// password never travels in a URL. A refusal is said with the title of the problem the server
// returned and, when it gives one, the way out (`action`).
(() => {
  "use strict";
  const form = document.getElementById("login");
  const message = document.getElementById("message");
  const submit = form.querySelector("button[type=submit]");

  const show = (title, action) => {
    message.querySelector(".alert__title").textContent = title;
    message.querySelector(".alert__body").textContent = action || "";
    message.hidden = false;
  };
  const clear = () => {
    message.hidden = true;
    message.querySelector(".alert__title").textContent = "";
    message.querySelector(".alert__body").textContent = "";
  };
  // the passkey script reports its own refusals the same way
  document.addEventListener("login:problem", (event) => {
    show(event.detail.title || form.dataset.error, event.detail.action);
  });
  document.addEventListener("login:clear", clear);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.getAttribute("aria-disabled") === "true") return;
    clear();
    submit.setAttribute("aria-disabled", "true");
    submit.setAttribute("aria-busy", "true");
    const data = new FormData(form);
    // The box is the explicit request of the hardened profile: shown ticked or not, with what it
    // does written under it, so a person who leaves it ticked has asked for a long session, and
    // the profile has no reason to refuse the first attempt.
    const remember = data.get("remember") === "on";
    const body = {
      username: data.get("username"),
      password: data.get("password"),
      remember,
      confirm_remember: remember,
    };
    let title = form.dataset.error;
    let action = "";
    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (response.ok) {
        window.location.assign("/");
        return;
      }
      const problem = await response.json();
      if (problem && problem.title) title = problem.title;
      if (problem && problem.action) action = problem.action;
    } catch (error) {
      // network failure: the generic message stays
    }
    show(title, action);
    submit.removeAttribute("aria-disabled");
    submit.removeAttribute("aria-busy");
  });
})();
