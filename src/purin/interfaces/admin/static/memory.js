const compactDirectory = window.matchMedia("(max-width: 52rem)");

function sizeMemoryDirectory() {
  const directory = document.getElementById("memory-directory");
  if (directory) directory.open = !compactDirectory.matches;
}

sizeMemoryDirectory();
compactDirectory.addEventListener("change", sizeMemoryDirectory);
document.addEventListener("htmx:historyRestore", sizeMemoryDirectory);
document.addEventListener("htmx:afterSwap", (event) => {
  if (event.target.id !== "memory-content") return;
  sizeMemoryDirectory();
  const reader = document.getElementById("memory-preview");
  if (!reader) return;
  reader.focus({ preventScroll: true });
  const top = reader.getBoundingClientRect().top;
  if (top < 0 || top >= window.innerHeight) reader.scrollIntoView({ block: "start" });
});
