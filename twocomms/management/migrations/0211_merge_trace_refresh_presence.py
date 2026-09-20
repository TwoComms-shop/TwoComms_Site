from django.db import migrations


class Migration(migrations.Migration):
    """Join the independently released presence and trace-refresh branches."""

    dependencies = [
        ("management", "0209_journey_trace_refresh"),
        ("management", "0210_ig_presence_capability"),
    ]

    operations = []
