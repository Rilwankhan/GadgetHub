from django.db import migrations


PRODUCTS = [
    ("Premium Smartphone", "Phones", "24999.00", "BEST SELLER", "Powerful performance with a stunning display.", "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?auto=format&fit=crop&w=700&q=85"),
    ("Wireless Headphones", "Audio", "2499.00", "POPULAR", "Enjoy clear sound and comfortable listening.", "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=700&q=85"),
    ("Premium Smart Watch", "Wearables", "3999.00", "TRENDING", "A stylish smartwatch for your everyday routine.", "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=700&q=85"),
    ("Modern Laptop", "Computers", "54999.00", "FEATURED", "Reliable performance for work and entertainment.", "https://images.unsplash.com/photo-1496181133206-80ce9b88a853?auto=format&fit=crop&w=700&q=85"),
    ("Digital Camera", "Accessories", "18499.00", "NEW", "Capture your favourite moments in detail.", "https://images.unsplash.com/photo-1516035069371-29a1b244cc32?auto=format&fit=crop&w=700&q=85"),
    ("Bluetooth Speaker", "Audio", "1799.00", "POPULAR", "Enjoy your music wherever you go.", "https://images.unsplash.com/photo-1608043152269-423dbba4e7e1?auto=format&fit=crop&w=700&q=85"),
    ("Wireless Earbuds", "Audio", "1999.00", "HOT DEAL", "Compact wireless earbuds for daily listening.", "https://images.unsplash.com/photo-1606220945770-b5b6c2c55bf1?auto=format&fit=crop&w=700&q=85"),
    ("Tablet Device", "Computers", "18999.00", "FEATURED", "A versatile tablet for work and entertainment.", "https://images.unsplash.com/photo-1544244015-0df4b3ffc6b0?auto=format&fit=crop&w=700&q=85"),
]


def seed_products(apps, schema_editor):
    Category = apps.get_model("home", "Category")
    Product = apps.get_model("home", "Product")
    database = schema_editor.connection.alias
    for name, category_name, price, badge, description, image in PRODUCTS:
        category, _ = Category.objects.using(database).get_or_create(name=category_name)
        Product.objects.using(database).get_or_create(
            name=name,
            defaults={
                "category_id": category.pk,
                "price": price,
                "badge": badge,
                "description": description,
                "image": image,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("home", "0001_initial")]

    operations = [migrations.RunPython(seed_products, migrations.RunPython.noop)]
