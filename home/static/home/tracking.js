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

    function attachDeleteConfirmation(form) {
        form.addEventListener("submit", event => {
            if (!window.confirm(form.dataset.confirmMessage)) {
                event.preventDefault();
            }
        });
    }

    orderTracking.querySelectorAll("[data-delete-cancelled-order]").forEach(
        attachDeleteConfirmation
    );

    function renderDeliveredOrder(card, order) {
        const summary = document.createElement("div");
        summary.className = "delivered-order-summary";
        summary.setAttribute("aria-live", "polite");

        const check = document.createElement("span");
        check.className = "delivered-order-check";
        check.setAttribute("aria-hidden", "true");
        check.textContent = "✓";

        const info = document.createElement("div");
        info.className = "delivered-order-info";
        order.product_names.forEach(productName => {
            const name = document.createElement("p");
            name.className = "delivered-product-name";
            name.textContent = productName;
            info.append(name);
        });

        const status = document.createElement("p");
        status.className = "delivered-order-status";
        status.textContent = "Status: Delivered";
        info.append(status);

        if (order.delivered_at) {
            const deliveredAt = document.createElement("time");
            deliveredAt.className = "delivered-order-time";
            deliveredAt.dateTime = order.delivered_at;
            deliveredAt.textContent =
                `Delivered Date & Time: ${new Date(order.delivered_at).toLocaleString(
                    undefined,
                    { dateStyle: "medium", timeStyle: "short" }
                )}`;
            info.append(deliveredAt);
        }

        summary.append(check, info);
        card.classList.add("account-order-delivered");
        card.dataset.orderDelivered = "true";
        card.replaceChildren(summary);
    }

    function renderCancelledOrder(card, order) {
        const csrfInput = card.querySelector("[name=csrfmiddlewaretoken]");
        const summary = document.createElement("div");
        summary.className = "delivered-order-summary cancelled-order-summary";
        summary.setAttribute("aria-live", "polite");

        const cross = document.createElement("span");
        cross.className = "delivered-order-check cancelled-order-cross";
        cross.setAttribute("aria-hidden", "true");
        cross.textContent = "✕";

        const info = document.createElement("div");
        info.className = "delivered-order-info";
        order.product_names.forEach(productName => {
            const name = document.createElement("p");
            name.className = "delivered-product-name";
            name.textContent = productName;
            info.append(name);
        });

        const status = document.createElement("p");
        status.className = "delivered-order-status";
        status.textContent = "Status: Cancelled";
        info.append(status);

        [
            ["Booking Date & Time", order.created_at],
            ["Cancellation Date & Time", order.cancelled_at]
        ].forEach(([label, date]) => {
            if (!date) {
                return;
            }
            const time = document.createElement("time");
            time.className = "delivered-order-time";
            time.dateTime = date;
            time.textContent = `${label}: ${new Date(date).toLocaleString(
                undefined,
                { dateStyle: "medium", timeStyle: "short" }
            )}`;
            info.append(time);
        });

        const form = document.createElement("form");
        form.method = "post";
        form.action = order.delete_url;
        form.className = "delete-cancelled-order-form";
        form.dataset.deleteCancelledOrder = "";
        form.dataset.confirmMessage =
            `Delete cancelled order #${order.id} from your order history?`;
        if (csrfInput) {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "csrfmiddlewaretoken";
            input.value = csrfInput.value;
            form.append(input);
        }
        const button = document.createElement("button");
        button.type = "submit";
        button.className = "button button-delete-order";
        button.textContent = "Delete Order";
        form.append(button);
        attachDeleteConfirmation(form);

        info.append(form);
        summary.append(cross, info);
        card.classList.add("account-order-cancelled");
        card.dataset.orderCancelledCard = "true";
        card.replaceChildren(summary);
    }

    function updateOrder(order) {
        const card = orderTracking.querySelector(`[data-order-id="${order.id}"]`);
        if (!card) {
            return;
        }

        if (order.status === "delivered") {
            if (!card.hasAttribute("data-order-delivered")) {
                renderDeliveredOrder(card, order);
            }
            return;
        }
        if (order.status === "cancelled") {
            if (!card.hasAttribute("data-order-cancelled-card")) {
                renderCancelledOrder(card, order);
            }
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

    function hasTrackableOrders() {
        return Boolean(
            orderTracking.querySelector(
                "[data-order-id]:not([data-order-delivered]):not([data-order-cancelled-card])"
            )
        );
    }

    if (hasTrackableOrders()) {
        window.setInterval(() => {
            if (!document.hidden && hasTrackableOrders()) {
                refreshOrderTracking();
            }
        }, 10000);
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden && hasTrackableOrders()) {
                refreshOrderTracking();
            }
        });
    }
}
