from django.urls import path, include

from home.admin import gadgethub_admin_site

urlpatterns = [
    path('admin/', gadgethub_admin_site.urls),
    path('', include('home.urls')),
]