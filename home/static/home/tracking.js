const orderTracking = document.querySelector("[data-order-tracking]");

if (orderTracking) {
    const cancelForms = orderTracking.querySelectorAll("[data-cancel-order]");

    cancelForms.forEach(form => {
        form.addEventListener("submit", event => {
            if (!window.confirm(form.dataset.confirmMessage)) {
                event.preventDefault();
            }
        });
    });

    function updateOrder(order) {
        const card = orderTracking.querySelector(`[data-order-id="${order.id}"]`);
        if (!card) {
            return;
        }

        const status = card.querySelector(".order-status-pill");
        status.className = `order-status-pill status-${order.status}`;
        status.textContent = order.status_label;

        const progress = card.querySelector("[data-order-progress]");
        progress.hidden = order.status === "cancelled";
        order.tracking_steps.forEach(step => {
            const element = progress.querySelector(
                `[data-tracking-step="${step.status}"]`
            );
            element.classList.toggle("is-complete", step.complete);
            element.classList.toggle("is-current", step.current);
        });

        const cancelled = card.querySelector("[data-order-cancelled]");
        cancelled.hidden = order.status !== "cancelled";

        const latest = card.querySelector("[data-tracking-latest]");
        latest.textContent = order.events.length
            ? order.events[order.events.length - 1].description
            : `Current status: ${order.status_label}. New tracking updates will appear here.`;

        const eventList = card.querySelector("[data-order-events]");
        eventList.replaceChildren();
        order.events.forEach(event => {
            const item = document.createElement("li");
            const marker = document.createElement("span");
            marker.className = "tracking-event-marker";
            marker.setAttribute("aria-hidden", "true");

            const detail = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = event.status_label;
            const time = document.createElement("time");
            time.dateTime = event.created_at;
            time.textContent = new Date(event.created_at).toLocaleString(
                undefined,
                { dateStyle: "medium", timeStyle: "short" }
            );
            const description = document.createElement("p");
            description.textContent = event.description;

            detail.append(title, time, description);
            item.append(marker, detail);
            eventList.append(item);
        });

        if (!order.events.length) {
            const empty = document.createElement("li");
            empty.className = "tracking-empty";
            empty.textContent = "New tracking updates will appear here as your order progresses.";
            eventList.append(empty);
        }

        card.querySelector("[data-tracking-summary]").textContent =
            `Tracking updates (${order.events.length})`;
        card.querySelector("[data-order-cancel-actions]").hidden =
            !order.can_cancel;
        card.querySelector("[data-cancellation-expired]").hidden =
            !["pending", "confirmed"].includes(order.status) || order.can_cancel;
        card.querySelector("[data-cancellation-shipped]").hidden =
            !["shipped", "out_for_delivery"].includes(order.status);
    }

    async function refreshOrderTracking() {
        try {
            const response = await fetch(orderTracking.dataset.trackingUrl, {
                cache: "no-store",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                throw new Error(`Tracking request failed (${response.status}).`);
            }

            const result = await response.json();
            result.orders.forEach(updateOrder);
        } catch (error) {
            console.error("Unable to refresh order tracking.", error);
        }
    }

    if (orderTracking.querySelector("[data-order-id]")) {
        window.setInterval(() => {
            if (!document.hidden) {
                refreshOrderTracking();
            }
        }, 10000);
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
                refreshOrderTracking();
            }
        });
    }
}
