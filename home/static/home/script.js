const WHATSAPP_NUMBER = "918608865811";

const products = [
    {
        id: 1,
        name: "Premium Smartphone",
        category: "Phones",
        price: 24999,
        badge: "BEST SELLER",
        description: "Powerful performance with a stunning display.",
        image: "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 2,
        name: "Wireless Headphones",
        category: "Audio",
        price: 2499,
        badge: "POPULAR",
        description: "Enjoy clear sound and comfortable listening.",
        image: "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 3,
        name: "Premium Smart Watch",
        category: "Wearables",
        price: 3999,
        badge: "TRENDING",
        description: "A stylish smartwatch for your everyday routine.",
        image: "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 4,
        name: "Modern Laptop",
        category: "Computers",
        price: 54999,
        badge: "FEATURED",
        description: "Reliable performance for work and entertainment.",
        image: "https://images.unsplash.com/photo-1496181133206-80ce9b88a853?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 5,
        name: "Digital Camera",
        category: "Accessories",
        price: 18499,
        badge: "NEW",
        description: "Capture your favourite moments in detail.",
        image: "https://images.unsplash.com/photo-1516035069371-29a1b244cc32?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 6,
        name: "Bluetooth Speaker",
        category: "Audio",
        price: 1799,
        badge: "POPULAR",
        description: "Enjoy your music wherever you go.",
        image: "https://images.unsplash.com/photo-1608043152269-423dbba4e7e1?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 7,
        name: "Wireless Earbuds",
        category: "Audio",
        price: 1999,
        badge: "HOT DEAL",
        description: "Compact wireless earbuds for daily listening.",
        image: "https://images.unsplash.com/photo-1606220945770-b5b6c2c55bf1?auto=format&fit=crop&w=700&q=85"
    },
    {
        id: 8,
        name: "Tablet Device",
        category: "Computers",
        price: 18999,
        badge: "FEATURED",
        description: "A versatile tablet for work and entertainment.",
        image: "https://images.unsplash.com/photo-1544244015-0df4b3ffc6b0?auto=format&fit=crop&w=700&q=85"
    }
];

let selectedCategory = "All";
let cart = [];
let liveLocation = null;

function formatPrice(amount) {
    return "₹" + amount.toLocaleString("en-IN");
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
            product.category.toLowerCase().includes(searchText);

        return matchesCategory && matchesSearch;
    });

    if (filteredProducts.length === 0) {
        grid.innerHTML = '<p class="no-products">No gadgets found. Try another search.</p>';
        return;
    }

    grid.innerHTML = filteredProducts.map(product => `
        <article class="product-card">
            <div class="product-image-wrap">
                <span class="product-badge">${product.badge}</span>
                <img
                    class="product-image"
                    src="${product.image}"
                    alt="${product.name}"
                    loading="lazy">
            </div>
            <div class="product-info">
                <span class="product-category">${product.category}</span>
                <h3>${product.name}</h3>
                <p class="product-description">${product.description}</p>
                <div class="product-price">${formatPrice(product.price)}</div>
                <div class="product-actions">
                    <button class="add-cart" type="button" onclick="addToCart(${product.id})">
                        Add to Cart
                    </button>
                    <button
                        class="whatsapp-order"
                        type="button"
                        onclick="orderProduct(${product.id})">
                        Order
                    </button>
                </div>
            </div>
        </article>
    `).join("");
}

function filterProducts(category, button) {
    selectedCategory = category;

    document.querySelectorAll(".filter-btn").forEach(filterButton => {
        filterButton.classList.toggle("active", filterButton === button);
    });

    renderProducts();
}

function renderCart() {
    const cartCount = cart.reduce((total, item) => total + item.quantity, 0);
    const cartTotal = cart.reduce(
        (total, item) => total + item.price * item.quantity,
        0
    );

    document.getElementById("cart-count").textContent = cartCount;
    document.getElementById("cart-total").textContent =
        `Total: ${formatPrice(cartTotal)}`;

    const cartItems = document.getElementById("cart-items");
    if (cart.length === 0) {
        cartItems.innerHTML = "<p>Your cart is empty.</p>";
        return;
    }

    cartItems.innerHTML = cart.map(item => `
        <div class="cart-item">
            <div>
                <strong>${item.name}</strong>
                <p>${formatPrice(item.price)} each</p>
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
                <strong>${item.name}</strong>
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
                `Location captured (about ±${Math.round(liveLocation.accuracy)} m). It will be added to your WhatsApp order.`;
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

    if (!/^\d{10,15}$/.test(WHATSAPP_NUMBER)) {
        alert("The WhatsApp number in script.js is invalid.");
        return;
    }

    const name = document.getElementById("customer-name").value.trim();
    const email = document.getElementById("customer-email").value.trim();
    const mobile = document.getElementById("customer-mobile").value.trim();
    const address = document.getElementById("customer-address").value.trim();

    if (!name || !email || !mobile || !address) {
        alert("Please complete your name, email, mobile number, and delivery address.");
        return;
    }

    const orderLines = cart.map((item, index) => [
        `📦 *Product ${index + 1}: ${item.name}*`,
        `🏷️ Category: ${item.category}`,
        `💰 Unit price: ${formatPrice(item.price)}`,
        `🔢 Quantity: ${item.quantity}`,
        `💵 Subtotal: ${formatPrice(item.price * item.quantity)}`
    ].join("\n"));

    const message = [
        "🛍️ *NEW ORDER — GADGETHUB* 🛍️",
        "",
        "👤 *CUSTOMER DETAILS*",
        "━━━━━━━━━━━━━━━━━━",
        `🙍 Name: ${name}`,
        `📧 Email: ${email}`,
        `📱 Mobile number: ${mobile}`,
        `🏠 Delivery address: ${address}`,
        liveLocation
            ? `📍 Live location: https://maps.google.com/?q=${liveLocation.latitude},${liveLocation.longitude}\n🎯 GPS accuracy: about ±${Math.round(liveLocation.accuracy)} metres`
            : "📍 Live location: Not shared (delivery address provided)",
        "",
        "🛒 *ORDER DETAILS*",
        "━━━━━━━━━━━━━━━━━━",
        ...orderLines.flatMap((line, index) =>
            index < orderLines.length - 1 ? [line, ""] : [line]
        ),
        "",
        "━━━━━━━━━━━━━━━━━━",
        `🧾 *ORDER TOTAL: ${formatPrice(getCartTotal())}*`,
        "",
        "🙏 Thank you for choosing GadgetHub!",
        "Please confirm availability, delivery charges, and payment details."
    ].join("\n");

    window.open(getWhatsAppLink(message), "_blank", "noopener,noreferrer");
    document.getElementById("order-status").textContent =
        "WhatsApp opened with your order details. Review the message and tap Send to submit your order.";
}

document.getElementById("location-button").addEventListener("click", requestLiveLocation);
document.getElementById("checkout-form").addEventListener("submit", submitCheckout);
document.getElementById("contact-whatsapp").href =
    getWhatsAppLink("Hello GadgetHub! I need help with your products.");

document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
        closeCheckout();
        closeCart();
    }
});

renderProducts();
renderCart();
