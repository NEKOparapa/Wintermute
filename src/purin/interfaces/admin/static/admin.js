document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  input.select();
  try {
    await navigator.clipboard.writeText(input.value);
    button.textContent = "已复制";
  } catch {
    button.textContent = "请手动复制";
  }
});
