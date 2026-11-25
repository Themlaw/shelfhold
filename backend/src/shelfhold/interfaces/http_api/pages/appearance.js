// The choice of the person for the light or the dark paper, read from the device the way the app
// does (`shelfhold.appearance`), before the page is drawn. Nothing else is read, nothing is sent.
(() => {
  try {
    const saved = JSON.parse(localStorage.getItem("shelfhold.appearance") || "null");
    if (saved && (saved.colorScheme === "light" || saved.colorScheme === "dark")) {
      document.documentElement.dataset.colorScheme = saved.colorScheme;
    }
  } catch (error) {
    // storage refused or unreadable: the system choice stays
  }
})();
