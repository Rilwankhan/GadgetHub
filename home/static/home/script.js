const WHATSAPP_NUMBER = "918608865811";

const products = JSON.parse(document.getElementById("store-products").textContent);
const storeConfig = document.getElementById("store-config").dataset;
window.GADGETHUB = {
    authenticated: storeConfig.authenticated === "true",
    loginUrl: storeConfig.loginUrl,
    accountUrl: storeConfig.accountUrl,
    placeOrderUrl: storeConfig.placeOrderUrl
};

let selectedCategory = "All";
let cart = [];
try {
    const savedCart = JSON.parse(sessionStorage.getItem("gadgethub-cart") || "[]");
    cart = savedCart
        .map(savedItem => {
            const product = products.find(item => item.id === savedItem.id);
            return product && Number.isInteger(savedItem.quantity) && savedItem.quantity > 0
                ? { ...product, quantity: savedItem.quantity }
                : null;
        })
        .filter(Boolean);
} catch (error) {
    sessionStorage.removeItem("gadgethub-cart");
}
let liveLocation = null;

function formatPrice(amount) {
    return "₹" + amount.toLocaleString("en-IN");
}

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[character]);
}

function getWhatsAppLink(message) {
    return `https://wa.me/${WHATSAPP_NUMBER}?text=${encodeURIComponent(message)}`;
}

function renderProducts() {
    const grid = document.getElementById("product-grid");
    const searchInput = document.getElementById("search-input");
    const searchText = searchInput.value.trim().toLowerCase();

    const filteredProducts = products.filter(product => {
        const matchesCategory =
            selectedCategory === "All" || product.category === selectedCategory;
        const matchesSearch =
            product.name.toLowerCase().includes(searchText) ||
            product.category.toLowerCase().includes(searchText) ||
            product.description.toLowerCase().includes(searchText);

        return matchesCategory && matchesSearch;
    });

    const resultsLabel = document.getElementById("results-label");
    resultsLabel.textContent =
        `${filteredProducts.length} ${filteredProducts.length === 1 ? "thoughtful pick" : "thoughtful picks"}`;

    if (filteredProducts.length === 0) {
        grid.innerHTML = `
            <div class="no-products">
                <span class="empty-symbol" aria-hidden="true">⌕</span>
                <h3>Nothing in this corner just yet.</h3>
                <p>Try a different search or browse everything in the collection.</p>
                <button class="text-link" type="button" onclick="resetProductFilters()">Show everything <span aria-hidden="true">→</span></button>
            </div>
        `;
        return;
    }

    grid.innerHTML = filteredProducts.map(product => `
        <article class="product-card">
            <div class="product-image-wrap">
                ${product.badge ? `<span class="product-badge">${escapeHtml(product.badge)}</span>` : ""}
                <img
                    class="product-image"
                    src="${escapeHtml(product.image)}"
                    alt="${escapeHtml(product.name)}"
                    loading="lazy">
                <button class="quick-add" type="button" aria-label="Add ${escapeHtml(product.name)} to your bag" onclick="addToCart(${product.id})">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
                </button>
            </div>
            <div class="product-info">
                <div class="product-overline">
                    <span class="product-category">${escapeHtml(product.category)}</span>
                    <span class="product-mark" aria-hidden="true">G/H PICK</span>
                </div>
                <h3>${escapeHtml(product.name)}</h3>
                <p class="product-description">${escapeHtml(product.description)}</p>
                <div class="product-card-bottom">
                    <div class="product-price">${formatPrice(product.price)}</div>
                    <button class="whatsapp-order" type="button" onclick="orderProduct(${product.id})">
                        Quick order <span aria-hidden="true">↗</span>
                    </button>
                </div>
                <div class="product-actions">
                    <button class="add-cart" type="button" onclick="addToCart(${product.id})">Add to bag</button>
                </div>
            </div>
        </article>
    `).join("");
}

function resetProductFilters() {
    selectedCategory = "All";
    document.getElementById("search-input").value = "";
    document.querySelectorAll(".filter-btn").forEach(button => {
        const isActive = button.textContent.trim() === "Everything";
        button.classList.toggle("active", isActive);
        button.setAttribute("aria-pressed", String(isActive));
    });
    renderProducts();
}

function filterProducts(category, button) {
    selectedCategory = category;

    document.querySelectorAll(".filter-btn").forEach(filterButton => {
        const isActive = filterButton === button;
        filterButton.classList.toggle("active", isActive);
        filterButton.setAttribute("aria-pressed", String(isActive));
    });

    renderProducts();
}

function renderCart() {
    sessionStorage.setItem("gadgethub-cart", JSON.stringify(
        cart.map(item => ({ id: item.id, quantity: item.quantity }))
    ));
    const cartCount = cart.reduce((total, item) => total + item.quantity, 0);
    const cartTotal = cart.reduce(
        (total, item) => total + item.price * item.quantity,
        0
    );

    document.getElementById("cart-count").textContent = cartCount;
    document.getElementById("cart-title-count").textContent =
        cartCount ? `(${cartCount})` : "";
    document.getElementById("cart-total").textContent =
        `Subtotal: ${formatPrice(cartTotal)}`;

    const cartItems = document.getElementById("cart-items");
    if (cart.length === 0) {
        cartItems.innerHTML = `
            <div class="empty-cart">
                <span class="empty-cart-icon" aria-hidden="true">＋</span>
                <h3>Your bag is taking a breather.</h3>
                <p>Find something useful, delightful, or a bit of both.</p>
                <button class="text-link" type="button" onclick="closeCart(); document.getElementById('products').scrollIntoView({ behavior: 'smooth' });">Explore the collection <span aria-hidden="true">→</span></button>
            </div>
        `;
        return;
    }

    cartItems.innerHTML = cart.map(item => `
        <div class="cart-item">
            <img class="cart-item-image" src="${escapeHtml(item.image)}" alt="">
            <div>
                <strong>${escapeHtml(item.name)}</strong>
                <p>${escapeHtml(item.category)} · ${formatPrice(item.price)} each</p>
                <div class="quantity-controls">
                    <button type="button" aria-label="Decrease quantity"
                        onclick="changeQuantity(${item.id}, -1)">−</button>
                    <span>${item.quantity}</span>
                    <button type="button" aria-label="Increase quantity"
                        onclick="changeQuantity(${item.id}, 1)">+</button>
                </div>
            </div>
            <strong>${formatPrice(item.price * item.quantity)}</strong>
        </div>
    `).join("");
}

function addToCart(productId) {
    const product = products.find(item => item.id === productId);
    if (!product) {
        throw new Error(`Unknown product ID: ${productId}`);
    }

    const cartItem = cart.find(item => item.id === productId);
    if (cartItem) {
        if (cartItem.quantity >= 99) {
            alert("You can add up to 99 of each product.");
            return;
        }
        cartItem.quantity += 1;
    } else {
        cart.push({ ...product, quantity: 1 });
    }

    renderCart();
    openCart();
}

function orderProduct(productId) {
    const product = products.find(item => item.id === productId);
    if (!product) {
        throw new Error(`Unknown product ID: ${productId}`);
    }

    const cartItem = cart.find(item => item.id === productId);
    if (!cartItem) {
        cart.push({ ...product, quantity: 1 });
    }

    renderCart();
    openCheckout();
}

function changeQuantity(productId, amount) {
    const item = cart.find(cartItem => cartItem.id === productId);
    if (!item) {
        throw new Error(`Product ${productId} is not in the cart.`);
    }

    if (amount > 0 && item.quantity >= 99) {
        return;
    }
    item.quantity += amount;
    if (item.quantity <= 0) {
        cart = cart.filter(cartItem => cartItem.id !== productId);
    }

    renderCart();
}

function openCart() {
    document.getElementById("cart-modal").classList.add("show");
}

function closeCart() {
    document.getElementById("cart-modal").classList.remove("show");
}

function closeCartOutside(event) {
    if (event.target.id === "cart-modal") {
        closeCart();
    }
}

function getCartTotal() {
    return cart.reduce((total, item) => total + item.price * item.quantity, 0);
}

function renderCheckoutSummary() {
    const itemCount = cart.reduce((total, item) => total + item.quantity, 0);
    document.getElementById("checkout-item-count").textContent =
        `${itemCount} ${itemCount === 1 ? "item" : "items"}`;
    document.getElementById("checkout-order-items").innerHTML = cart.map(item => `
        <div class="checkout-order-item">
            <div>
                <strong>${escapeHtml(item.name)}</strong>
                <p>${formatPrice(item.price)} × ${item.quantity}</p>
            </div>
            <strong>${formatPrice(item.price * item.quantity)}</strong>
        </div>
    `).join("");
    document.getElementById("checkout-summary-total").textContent =
        `Order total: ${formatPrice(getCartTotal())}`;
}

function openCheckout() {
    if (cart.length === 0) {
        alert("Your cart is empty. Add a product before checking out.");
        return;
    }

    renderCheckoutSummary();
    closeCart();
    document.getElementById("order-status").textContent = "";
    document.getElementById("checkout-modal").classList.add("show");
}

function closeCheckout() {
    document.getElementById("checkout-modal").classList.remove("show");
}

function closeCheckoutOutside(event) {
    if (event.target.id === "checkout-modal") {
        closeCheckout();
    }
}

function backToCart() {
    closeCheckout();
    openCart();
}

function requestLiveLocation() {
    const button = document.getElementById("location-button");
    const status = document.getElementById("location-status");

    if (!navigator.geolocation) {
        status.textContent =
            "Location access requires a secure connection (HTTPS or localhost). You can still enter your delivery address above.";
        return;
    }

    button.disabled = true;
    button.textContent = "Finding your location...";
    status.textContent = "Please allow location access in your browser.";

    navigator.geolocation.getCurrentPosition(
        position => {
            liveLocation = {
                latitude: position.coords.latitude,
                longitude: position.coords.longitude,
                accuracy: position.coords.accuracy
            };
            button.disabled = false;
            button.textContent = "Location captured ✓";
            status.textContent =
                `Location captured (about ±${Math.round(liveLocation.accuracy)} m). It will be saved with your order.`;
        },
        error => {
            liveLocation = null;
            const messages = {
                1: "Location permission was denied.",
                2: "Your location is currently unavailable.",
                3: "The location request timed out."
            };
            status.textContent =
                `${messages[error.code] || "Unable to get your location."} You can still enter your delivery address above.`;
            button.disabled = false;
            button.textContent = "Try again";
        },
        {
            enableHighAccuracy: true,
            timeout: 15000,
            maximumAge: 0
        }
    );
}

function submitCheckout(event) {
    event.preventDefault();

    if (cart.length === 0) {
        alert("Your cart is empty. Add a product before checking out.");
        closeCheckout();
        return;
    }

    const name = document.getElementById("customer-name").value.trim();
    const email = document.getElementById("customer-email").value.trim();
    const mobile = document.getElementById("customer-mobile").value.trim();
    const address = document.getElementById("customer-address").value.trim();
    const paymentMethod = document.getElementById("payment-method").value;

    if (!name || !email || !mobile || !address) {
        alert("Please complete your name, email, mobile number, and delivery address.");
        return;
    }

    if (!window.GADGETHUB.authenticated) {
        sessionStorage.setItem("gadgethub-checkout", JSON.stringify({
            name, email, mobile, address, paymentMethod
        }));
        window.location.href = window.GADGETHUB.loginUrl;
        return;
    }

    const submitButton = document.querySelector("#checkout-form button[type='submit']");
    const status = document.getElementById("order-status");
    submitButton.disabled = true;
    status.textContent = "Saving your order...";
    fetch(window.GADGETHUB.placeOrderUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": document.querySelector("#checkout-form [name=csrfmiddlewaretoken]").value
        },
        body: JSON.stringify({
            name,
            email,
            mobile,
            address,
            payment_method: paymentMethod,
            cart: cart.map(item => ({ id: item.id, quantity: item.quantity })),
            location: liveLocation
        })
    }).then(async response => {
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || "Unable to save the order.");
        }
        const orderLines = cart.map(item =>
            `${item.name} × ${item.quantity} — ${formatPrice(item.price * item.quantity)}`
        );
        const message = [
            `GadgetHub order #${data.order_id}`,
            `Name: ${name}`,
            `Email: ${email}`,
            `Mobile: ${mobile}`,
            `Delivery address: ${address}`,
            `Payment: ${document.getElementById("payment-method").selectedOptions[0].textContent}`,
            liveLocation
                ? `Location: https://maps.google.com/?q=${liveLocation.latitude},${liveLocation.longitude}`
                : "",
            "Items:",
            ...orderLines,
            `Total: ${formatPrice(Number(data.total))}`
        ].filter(Boolean).join("\n");
        sessionStorage.removeItem("gadgethub-checkout");
        sessionStorage.removeItem("gadgethub-cart");
        cart = [];
        renderCart();
        status.textContent =
            `Order #${data.order_id} was saved to your account. Payment is not processed online; the store will confirm payment and delivery details. `;
        const accountLink = document.createElement("a");
        accountLink.href = window.GADGETHUB.accountUrl;
        accountLink.textContent = "View your orders";
        status.append(accountLink);
        status.append(document.createTextNode(" · "));
        const whatsappLink = document.createElement("a");
        whatsappLink.href = getWhatsAppLink(message);
        whatsappLink.target = "_blank";
        whatsappLink.rel = "noopener noreferrer";
        whatsappLink.textContent = "Notify the store on WhatsApp";
        status.append(whatsappLink);
    }).catch(error => {
        status.textContent = error.message || "Unable to save the order. Please try again.";
    }).finally(() => {
        submitButton.disabled = false;
    });
}

document.getElementById("location-button").addEventListener("click", requestLiveLocation);
document.getElementById("checkout-form").addEventListener("submit", submitCheckout);
document.getElementById("contact-whatsapp").href =
    getWhatsAppLink("Hello GadgetHub! I need help with your products.");

try {
    const checkoutDraft = JSON.parse(sessionStorage.getItem("gadgethub-checkout") || "null");
    if (checkoutDraft) {
        document.getElementById("customer-name").value = checkoutDraft.name || "";
        document.getElementById("customer-email").value = checkoutDraft.email || "";
        document.getElementById("customer-mobile").value = checkoutDraft.mobile || "";
        document.getElementById("customer-address").value = checkoutDraft.address || "";
        document.getElementById("payment-method").value = checkoutDraft.paymentMethod || "cash_on_delivery";
        sessionStorage.removeItem("gadgethub-checkout");
    }
} catch (error) {
    sessionStorage.removeItem("gadgethub-checkout");
}

document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
        closeCheckout();
        closeCart();
    }
});

renderProducts();
renderCart();
