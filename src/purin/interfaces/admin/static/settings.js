for (const form of document.querySelectorAll(".setting-row")) {
  const field = form.querySelector("input:not([type=hidden]), select");
  const error = form.querySelector(".save-error");
  let saved = field.value;
  let saving = false;
  let timer;

  async function save() {
    clearTimeout(timer);
    if (saving || !field.validity.valid) return;
    const value = field.value;
    if (value === saved) return;

    saving = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "text/plain" },
        keepalive: true,
      });
      if (response.redirected) {
        location.assign(response.url);
        return;
      }
      if (response.status !== 204) {
        const message = response.headers.get("Content-Type")?.startsWith("text/plain")
          && [400, 403].includes(response.status) ? await response.text() : "";
        throw new Error(message);
      }
      saved = value;
      error.hidden = true;
    } catch (cause) {
      // A lost response can follow a successful write. Retry even if the field is reverted.
      saved = null;
      error.hidden = false;
      error.querySelector('[role="alert"]').textContent =
        cause instanceof TypeError || !cause.message ? "保存失败，请重试。" : cause.message;
    } finally {
      saving = false;
      if (field.value !== value) save();
    }
  }

  field.disabled = false;
  field.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(save, 350);
  });
  field.addEventListener("change", () => {
    if (field.reportValidity()) save();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) save();
  });
}
