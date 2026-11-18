# 🛒 GadgetHub – E-Commerce Website

GadgetHub is a modern e-commerce website developed using Python and Django. It allows users to browse gadgets, search products, add items to a shopping cart, and place orders through WhatsApp.

### Technologies Used
- Python
- Django
- HTML
- CSS
- JavaScript
- SQLite

### Features
- Product listing and search
- Shopping cart management
- Quantity and price calculation
- Customer checkout form
- Customer details and live location
- WhatsApp order placement

### Installation and Setup

**1. Clone the repository**
```bash
git clone https://github.com/Rilwankhan07/GadgetHub.git
```

**2. Open the project folder**
```bash
cd GadgetHub
```

**3. Create a virtual environment**
```bash
py -m venv project-name
```

**4. Activate the virtual environment**
```powershell
.\project-name\Scripts\Activate.ps1
```

**5. Install Django and required packages**
```bash
python -m pip install -r requirements.txt
```

**6. Apply database migrations**
```bash
python manage.py migrate
```

**7. Run the project**
```bash
python manage.py runserver
```

**8. Open in your browser**
```text
http://127.0.0.1:8000/
```

### Razorpay online payments

Set the Razorpay Key ID and Key Secret from your Razorpay Dashboard as environment
variables before starting Django. Use test-mode credentials while developing; never
put the Key Secret in templates, JavaScript, or source control.

PowerShell:
```powershell
$env:RAZORPAY_KEY_ID = "rzp_test_your_key_id"
$env:RAZORPAY_KEY_SECRET = "your_test_key_secret"
python manage.py runserver
```

Razorpay Checkout displays the payment methods enabled for the configured account.
The backend verifies the Checkout signature and confirms the captured payment with
Razorpay before marking an order as paid. Configure automatic payment capture in the
Razorpay Dashboard; an uncaptured payment is not recorded as paid. Live payments
require live credentials and a correctly configured public HTTPS deployment.

### Author
Rilwankhan

GitHub: https://github.com/Rilwankhan07
