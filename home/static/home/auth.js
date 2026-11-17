function createEyeIcon(isVisible) {
    const namespace = "http://www.w3.org/2000/svg";
    const icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");

    const eye = document.createElementNS(namespace, "path");
    eye.setAttribute("d", "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z");
    icon.append(eye);

    const pupil = document.createElementNS(namespace, "circle");
    pupil.setAttribute("cx", "12");
    pupil.setAttribute("cy", "12");
    pupil.setAttribute("r", "3");
    icon.append(pupil);

    if (isVisible) {
        const slash = document.createElementNS(namespace, "path");
        slash.setAttribute("d", "m3 3 18 18");
        icon.append(slash);
    }

    return icon;
}

document.querySelectorAll('input[type="password"]').forEach(passwordInput => {
    const wrapper = document.createElement("div");
    wrapper.className = "password-input-wrap";
    passwordInput.parentNode.insertBefore(wrapper, passwordInput);
    wrapper.append(passwordInput);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "password-toggle";
    toggle.setAttribute("aria-label", "Show password");
    toggle.setAttribute("aria-controls", passwordInput.id);
    toggle.setAttribute("aria-pressed", "false");
    toggle.title = "Show password";
    toggle.append(createEyeIcon(false));
    wrapper.append(toggle);

    toggle.addEventListener("click", () => {
        const isVisible = passwordInput.type === "password";
        passwordInput.type = isVisible ? "text" : "password";
        toggle.setAttribute("aria-label", isVisible ? "Hide password" : "Show password");
        toggle.setAttribute("aria-pressed", String(isVisible));
        toggle.title = isVisible ? "Hide password" : "Show password";
        toggle.replaceChildren(createEyeIcon(isVisible));
    });
});

document.querySelectorAll("[data-password-panel-toggle]").forEach(toggle => {
    const panel = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!panel) {
        return;
    }

    const openLabel = toggle.getAttribute("data-open-label");
    const closeLabel = toggle.getAttribute("data-close-label");
    toggle.addEventListener("click", () => {
        const isExpanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!isExpanded));
        panel.hidden = isExpanded;
        toggle.textContent = isExpanded ? openLabel : closeLabel;
    });
});
