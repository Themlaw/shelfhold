// Passkey sign-in of the login page. Served from the same origin (`script-src 'self'`): the page
// has no other script than its hashed one. The server draws the challenge and checks everything;
// this file only moves bytes between the server and `navigator.credentials`.
(() => {
  "use strict";
  const button = document.getElementById("passkey");
  if (!button) return;
  if (!window.PublicKeyCredential || !navigator.credentials || !window.isSecureContext) {
    button.hidden = true;
    return;
  }
  const message = document.getElementById("message");
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
    button.disabled = true;
    message.textContent = "";
    let text = button.dataset.error;
    try {
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
        remember: document.querySelector("input[name=remember]").checked,
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
        window.location.assign("/");
        return;
      }
      const problem = await response.json();
      if (problem && problem.title) text = problem.title;
    } catch (error) {
      // cancelled gesture or network failure: the generic message stays
    }
    message.textContent = text;
    button.disabled = false;
  });
})();
