function openDevicePairing() {
  const dialog = document.getElementById("device-pairing");
  if (!dialog?.open || !dialog.showModal || dialog.matches(":modal")) return;
  dialog.removeAttribute("open");
  dialog.showModal();
}

openDevicePairing();
document.addEventListener("htmx:load", openDevicePairing);

document.addEventListener("click", (event) => {
  const dialog = event.target.closest(".pairing-close")?.closest("dialog:modal");
  if (dialog) {
    event.preventDefault();
    dialog.close();
  }
});

document.addEventListener("close", (event) => {
  if (event.target.id !== "device-pairing") return;
  event.target.remove();
  if (new URLSearchParams(location.search).has("add")) {
    history.replaceState(history.state, "", "/admin/devices");
  }
  document.getElementById("add-device").focus();
}, true);
