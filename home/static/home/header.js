document.querySelectorAll(".site-header").forEach(header => {
    const menuButton = header.querySelector(".menu-toggle");
    const menuPanel = header.querySelector(".nav-panel");
    if (!menuButton || !menuPanel) return;

    const closeMenu = () => {
        header.classList.remove("menu-open");
        menuButton.setAttribute("aria-expanded", "false");
        menuButton.setAttribute("aria-label", "Open navigation menu");
    };

    menuButton.addEventListener("click", () => {
        const isOpen = header.classList.toggle("menu-open");
        menuButton.setAttribute("aria-expanded", String(isOpen));
        menuButton.setAttribute(
            "aria-label",
            isOpen ? "Close navigation menu" : "Open navigation menu"
        );
    });

    menuPanel.addEventListener("click", event => {
        if (event.target.closest("a")) closeMenu();
    });
    document.addEventListener("click", event => {
        if (!header.contains(event.target)) closeMenu();
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape" && header.classList.contains("menu-open")) {
            closeMenu();
            menuButton.focus();
        }
    });
    window.addEventListener("resize", () => {
        if (window.innerWidth > 720) closeMenu();
    });
});

document.querySelectorAll(".dashboard-header").forEach(header => {
    const menuButton = header.querySelector(".dashboard-menu-toggle");
    const menuPanel = header.querySelector(".dashboard-nav");
    if (!menuButton || !menuPanel) return;

    const closeMenu = () => {
        header.classList.remove("menu-open");
        menuButton.setAttribute("aria-expanded", "false");
        menuButton.setAttribute("aria-label", "Open account navigation");
    };

    menuButton.addEventListener("click", () => {
        const isOpen = header.classList.toggle("menu-open");
        menuButton.setAttribute("aria-expanded", String(isOpen));
        menuButton.setAttribute(
            "aria-label",
            isOpen ? "Close account navigation" : "Open account navigation"
        );
    });

    menuPanel.querySelectorAll("a").forEach(link => {
        link.addEventListener("click", closeMenu);
    });
    document.addEventListener("click", event => {
        if (!header.contains(event.target) && header.classList.contains("menu-open")) {
            closeMenu();
        }
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape" && header.classList.contains("menu-open")) {
            closeMenu();
            menuButton.focus();
        }
    });
    window.addEventListener("resize", () => {
        if (window.innerWidth > 720) closeMenu();
    });
});

document.querySelectorAll(".staff-header").forEach(header => {
    const menuButton = header.querySelector(".staff-menu-toggle");
    const menuPanel = header.querySelector(".staff-header-user");
    if (!menuButton || !menuPanel) return;

    const closeMenu = () => {
        header.classList.remove("menu-open");
        menuButton.setAttribute("aria-expanded", "false");
        menuButton.setAttribute("aria-label", "Open staff navigation");
    };

    menuButton.addEventListener("click", () => {
        const isOpen = header.classList.toggle("menu-open");
        menuButton.setAttribute("aria-expanded", String(isOpen));
        menuButton.setAttribute(
            "aria-label",
            isOpen ? "Close staff navigation" : "Open staff navigation"
        );
    });

    menuPanel.querySelectorAll("a").forEach(link => {
        link.addEventListener("click", closeMenu);
    });
    document.addEventListener("click", event => {
        if (!header.contains(event.target) && header.classList.contains("menu-open")) {
            closeMenu();
        }
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape" && header.classList.contains("menu-open")) {
            closeMenu();
            menuButton.focus();
        }
    });
    window.addEventListener("resize", () => {
        if (window.innerWidth > 760) closeMenu();
    });
});
