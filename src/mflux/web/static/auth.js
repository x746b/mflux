"use strict";

(() => {
  const form = document.getElementById("login-form") || document.getElementById("setup-form");
  const error = document.getElementById("auth-error");
  const isSetup = form.id === "setup-form";

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await MF.ready;
      const key = document.getElementById("api-key").value;
      if (isSetup) {
        await MF.api("/api/setup", { method: "POST", json: { api_key: key, api_key_confirm: document.getElementById("api-key-confirm").value }, redirectOn401: false });
      } else {
        await MF.api("/api/login", { method: "POST", json: { api_key: key, remember: document.getElementById("remember").checked }, redirectOn401: false });
      }
      window.location.href = "/";
    } catch (exc) {
      error.textContent = exc.message;
      error.hidden = false;
      button.disabled = false;
    }
  });
})();
