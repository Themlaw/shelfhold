// Passkey sign-in of the login page. Served from the same origin (`script-src 'self'`), like the
// other script of the page: nothing is inline. The server draws the challenge and checks everything;
// this file only moves bytes between the server and `navigator.credentials`.
(() => {
  "use strict";
  const button = document.getElementById("passkey");
  if (!button) return;
  if (!window.PublicKeyCredential || !navigator.credentials || !window.isSecureContext) {
    button.hidden = true;
    const alternatives = button.closest(".alt");
    if (alternatives && !alternatives.querySelector(".btn:not([hidden])")) alternatives.hidden = true;
    return;
  }
  // the refusals are said by the page's own script (`login.js`), in the same place as the others
  const problem = (title, action) =>
    document.dispatchEvent(new CustomEvent("login:problem", { detail: { title, action } }));
  const toBytes = (text) => {
    const padded = text.replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  };
  const toText = (buffer) => {
    let raw = "";
    for (const byte of new Uint8Array(buffer)) raw += String.fromCharCode(byte);
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const post = async (url, body) =>
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  button.addEventListener("click", async () => {
    if (button.getAttribute("aria-disabled") === "true") return;
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("aria-busy", "true");
    document.dispatchEvent(new CustomEvent("login:clear"));
    let title = button.dataset.error;
    let action = "";
    try {
      const remember = document.querySelector("input[name=remember]").checked;
      const started = await post(button.dataset.options, {});
      if (!started.ok) throw new Error("options");
      const { flow, options } = await started.json();
      const credential = await navigator.credentials.get({
        publicKey: {
          challenge: toBytes(options.challenge),
          rpId: options.rpId,
          timeout: options.timeout,
          userVerification: "required",
        },
      });
      if (!credential) throw new Error("cancelled");
      const answer = credential.response;
      const response = await post(button.dataset.login, {
        flow,
        remember: remember,
        confirm_remember: remember, // the ticked box is the explicit request (see `login.js`)
        credential: {
          id: credential.id,
          rawId: toText(credential.rawId),
          type: credential.type,
          response: {
            clientDataJSON: toText(answer.clientDataJSON),
            authenticatorData: toText(answer.authenticatorData),
            signature: toText(answer.signature),
            userHandle: answer.userHandle ? toText(answer.userHandle) : null,
          },
          clientExtensionResults: {},
        },
      });
      if (response.ok) {
        // the server already filtered it: the same check as the password form, all the same
        const next = button.dataset.next || "/";
        const plain = next.startsWith("/") && !next.startsWith("//") && !/[\\\u0000-\u001f\u007f]/.test(next);
        window.location.assign(plain ? next : "/");
        return;
      }
      const refusal = await response.json();
      if (refusal && refusal.title) title = refusal.title;
      if (refusal && refusal.action) action = refusal.action;
    } catch (error) {
      // cancelled gesture or network failure: the generic message stays
    }
    problem(title, action);
    button.removeAttribute("aria-disabled");
    button.removeAttribute("aria-busy");
  });
})();
