(() => {
    const button = document.querySelector(".gh-mobile-menu");
    const sidebar = document.querySelector(".gh-sidebar");
    if (!button || !sidebar) return;

    button.addEventListener("click", () => {
        const isOpen = document.body.classList.toggle("gh-nav-open");
        button.setAttribute("aria-expanded", String(isOpen));
        button.setAttribute("aria-label", isOpen ? "Close navigation" : "Open navigation");
        button.textContent = isOpen ? "×" : "☰";
    });

    document.addEventListener("click", (event) => {
        if (!document.body.classList.contains("gh-nav-open")) return;
        if (sidebar.contains(event.target) || button.contains(event.target)) return;
        document.body.classList.remove("gh-nav-open");
        button.setAttribute("aria-expanded", "false");
        button.setAttribute("aria-label", "Open navigation");
        button.textContent = "☰";
    });
})();
