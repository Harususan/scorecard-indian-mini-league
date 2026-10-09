/* Browser-only captain preferences; no server storage or external dependency. */
(function (root) {
  function validChoices(value) {
    const choices = Object.create(null);
    if (!value || typeof value !== "object" || Array.isArray(value)) return choices;
    for (const [club, id] of Object.entries(value)) {
      if (id === null || typeof id === "string") choices[club] = id;
    }
    return choices;
  }
  function createStore(storage, namespace) {
    const key = "iml:captains:v1:" + namespace;
    let memory = Object.create(null);
    const unsaved = Object.create(null);
    return {
      key,
      read() {
        try {
          const raw = storage.getItem(key);
          memory = {...(raw === null ? {} : validChoices(JSON.parse(raw))), ...unsaved};
          return {choices: {...memory}, error: Object.keys(unsaved).length ? "Browser storage could not save this choice. It will last only for this visit." : null};
        } catch (_) {
          return {choices: {...memory}, error: "Browser storage is unavailable or unreadable. Choices will last only for this visit."};
        }
      },
      save(club, id) {
        // Merge only the edited club with the latest saved choices from other tabs.
        const latest = this.read();
        memory = {...latest.choices, [club]: id};
        try {
          storage.setItem(key, JSON.stringify(memory));
          for (const club of Object.keys(unsaved)) delete unsaved[club];
          return {choices: {...memory}, error: null};
        } catch (_) {
          unsaved[club] = id;
          return {choices: {...memory}, error: "Browser storage could not save this choice. It will last only for this visit."};
        }
      }
    };
  }
  const api = {createStore, validChoices};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CaptainStorage = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
