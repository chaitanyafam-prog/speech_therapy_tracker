const entries = document.querySelector("#clinician-entries");
const entryTemplate = document.querySelector("#clinician-entry-template");
const addButton = document.querySelector("#add-clinician");

function updateEntryControls() {
  const rows = entries.querySelectorAll(".clinician-entry");
  rows.forEach((row, index) => {
    row.querySelector("legend").textContent = `Clinician ${index + 1}`;
    row.querySelector(".remove-clinician").disabled = rows.length === 1;
  });
  addButton.disabled = rows.length >= 50;
}

addButton.addEventListener("click", () => {
  if (entries.querySelectorAll(".clinician-entry").length >= 50) return;
  entries.append(entryTemplate.content.cloneNode(true));
  updateEntryControls();
});

entries.addEventListener("click", (event) => {
  if (event.target.matches(".remove-clinician")) {
    event.target.closest(".clinician-entry").remove();
    updateEntryControls();
  }
});

updateEntryControls();
