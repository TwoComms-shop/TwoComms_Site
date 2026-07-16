"""
Django admin configuration for accounts app.

Регистрация моделей UserProfile, FavoriteProduct, UserPoints в админ-панели.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils.html import format_html
from .models import UserProfile, FavoriteProduct, UserPoints


class UserProfileInline(admin.StackedInline):
    """Inline для отображения профиля пользователя."""
    model = UserProfile
    can_delete = False
    verbose_name = 'Профіль користувача'
    verbose_name_plural = 'Профіль'
    fields = (
        'phone', 'email', 'full_name',
        'city', 'np_office', 'pay_type',
        'telegram', 'telegram_id', 'instagram',
        'company_name', 'company_number', 'delivery_address', 'website',
        'payment_method', 'payment_details',
        'is_ubd', 'ubd_doc', 'avatar'
    )


class UserPointsInline(admin.TabularInline):
    """Inline для отображения баллов пользователя."""
    model = UserPoints
    can_delete = False
    verbose_name = 'Бали користувача'
    verbose_name_plural = 'Бали'
    readonly_fields = ('points', 'total_earned', 'total_spent')
    fields = ('points', 'total_earned', 'total_spent')
    max_num = 1


class FavoriteProductInline(admin.TabularInline):
    """Inline для отображения избранных товаров."""
    model = FavoriteProduct
    extra = 0
    verbose_name = 'Обраний товар'
    verbose_name_plural = 'Обрані товари'
    readonly_fields = ('product', 'created_at')
    fields = ('product', 'created_at')
    can_delete = True


# Расширяем стандартную админку User
class UserAdmin(BaseUserAdmin):
    """Расширенная админка для пользователей."""
    inlines = (UserProfileInline, UserPointsInline, FavoriteProductInline)
    list_display = ('username', 'email', 'first_name', 'last_name',
                    'is_staff', 'user_phone', 'user_points', 'date_joined')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'date_joined')

    def user_phone(self, obj):
        """Отображает телефон из профиля."""
        try:
            return obj.userprofile.phone or '—'
        except UserProfile.DoesNotExist:
            return '—'
    user_phone.short_description = 'Телефон'

    def user_points(self, obj):
        """Отображает баллы пользователя."""
        try:
            points = obj.points.points
            return format_html(
                '<span style="color: #10b981; font-weight: 600;">{} балів</span>',
                points
            )
        except UserPoints.DoesNotExist:
            return '—'
    user_points.short_description = 'Бали'


# Перерегистрируем User с нашей расширенной админкой
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """Административная панель для профилей пользователей."""
    list_display = ('user', 'phone', 'email', 'full_name', 'city',
                    'company_name', 'is_ubd')
    list_filter = ('is_ubd', 'pay_type', 'payment_method')
    search_fields = ('user__username', 'phone', 'email', 'full_name',
                     'company_name', 'telegram', 'instagram')
    readonly_fields = ('user', 'ubd_doc_download')

    fieldsets = (
        ('Основна інформація', {
            'fields': ('user', 'full_name', 'phone', 'email')
        }),
        ('Доставка', {
            'fields': ('city', 'np_office', 'delivery_address', 'pay_type')
        }),
        ('Соціальні мережі', {
            'fields': ('telegram', 'telegram_id', 'instagram')
        }),
        ('Компанія (для опту)', {
            'fields': ('company_name', 'company_number', 'website',
                       'payment_method', 'payment_details'),
            'classes': ('collapse',)
        }),
        ('УБД статус', {
            'fields': ('is_ubd', 'ubd_doc', 'ubd_doc_download'),
            'classes': ('collapse',)
        }),
        ('Інше', {
            'fields': ('avatar',),
            'classes': ('collapse',)
        }),
    )

    def ubd_doc_download(self, obj):
        if not obj or not obj.ubd_doc:
            return '—'
        from django.urls import reverse

        return format_html(
            '<a href="{}" target="_blank" rel="noopener">Відкрити захищений документ</a>',
            reverse('ubd_document', args=[obj.pk]),
        )
    ubd_doc_download.short_description = 'Захищений перегляд УБД'


@admin.register(FavoriteProduct)
class FavoriteProductAdmin(admin.ModelAdmin):
    """Административная панель для избранных товаров."""
    list_display = ('user', 'product', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'product__title')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


@admin.register(UserPoints)
class UserPointsAdmin(admin.ModelAdmin):
    """Административная панель для баллов пользователей."""
    list_display = ('user', 'points_display', 'total_earned', 'total_spent',
                    'updated_at')
    list_filter = ('updated_at',)
    search_fields = ('user__username',)
    readonly_fields = ('user', 'points', 'total_earned', 'total_spent',
                       'created_at', 'updated_at')

    fieldsets = (
        ('Користувач', {
            'fields': ('user',)
        }),
        ('Баланс балів', {
            'fields': ('points', 'total_earned', 'total_spent')
        }),
        ('Дати', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def points_display(self, obj):
        """Отображает баланс с форматированием."""
        color = '#10b981' if obj.points > 0 else '#6b7280'
        return format_html(
            '<span style="color: {}; font-weight: 700; font-size: 14px;">{} балів</span>',
            color, obj.points
        )
    points_display.short_description = 'Баланс'

    def has_add_permission(self, request):
        """Запрещаем ручное создание (создаются автоматически)."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Запрещаем удаление."""
        return False
