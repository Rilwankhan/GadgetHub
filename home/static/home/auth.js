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

document.querySelectorAll("[data-personal-details-toggle]").forEach(toggle => {
    const panel = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!panel) {
        return;
    }

    toggle.addEventListener("click", () => {
        const isExpanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!isExpanded));
        panel.hidden = isExpanded;
    });

    const summary = panel.querySelector("[data-profile-summary]");
    const editor = panel.querySelector("[data-profile-editor]");
    const form = editor.querySelector("form");

    panel.querySelector("[data-profile-edit]").addEventListener("click", () => {
        summary.hidden = true;
        editor.hidden = false;
        editor.querySelector("input:not([type='hidden'])").focus();
    });

    panel.querySelector("[data-profile-cancel]").addEventListener("click", () => {
        form.reset();
        editor.hidden = true;
        summary.hidden = false;
    });
});

document.querySelectorAll("[data-otp-form]").forEach(form => {
    const inputs = Array.from(form.querySelectorAll("[data-otp-input]"));
    const otpValue = form.querySelector("[data-otp-value]");

    if (!inputs.length || !otpValue) {
        return;
    }

    const syncOtp = () => {
        otpValue.value = inputs.map(input => input.value).join("");
    };

    inputs.forEach((input, index) => {
        input.addEventListener("input", () => {
            input.value = input.value.replace(/\D/g, "").slice(-1);
            syncOtp();
            if (input.value && index < inputs.length - 1) {
                inputs[index + 1].focus();
                inputs[index + 1].select();
            }
        });

        input.addEventListener("keydown", event => {
            if (event.key === "Backspace" && !input.value && index > 0) {
                inputs[index - 1].focus();
                inputs[index - 1].select();
            }
            if (event.key === "ArrowLeft" && index > 0) {
                event.preventDefault();
                inputs[index - 1].focus();
            }
            if (event.key === "ArrowRight" && index < inputs.length - 1) {
                event.preventDefault();
                inputs[index + 1].focus();
            }
        });

        input.addEventListener("paste", event => {
            event.preventDefault();
            const digits = event.clipboardData
                .getData("text")
                .replace(/\D/g, "")
                .slice(0, inputs.length - index);

            digits.split("").forEach((digit, offset) => {
                inputs[index + offset].value = digit;
            });
            syncOtp();

            const nextIndex = Math.min(index + digits.length, inputs.length - 1);
            inputs[nextIndex].focus();
        });
    });

    form.addEventListener("submit", event => {
        syncOtp();
        if (!/^\d{6}$/.test(otpValue.value)) {
            event.preventDefault();
            inputs.find(input => !input.value)?.focus();
        }
    });

    inputs[0].focus();
});
