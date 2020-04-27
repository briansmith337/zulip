# Generated by Django 1.10.5 on 2017-05-10 05:59
from django.db import migrations
from django.db.backends.postgresql.schema import DatabaseSchemaEditor
from django.db.migrations.state import StateApps


def delete_old_scheduled_jobs(apps: StateApps, schema_editor: DatabaseSchemaEditor) -> None:
    """Delete any old scheduled jobs, to handle changes in the format of
    that table.  Ideally, we'd translate the jobs, but it's not really
    worth the development effort to save a few invitation reminders
    and day2 followup emails.
    """
    ScheduledJob = apps.get_model('zerver', 'ScheduledJob')
    ScheduledJob.objects.all().delete()

class Migration(migrations.Migration):

    dependencies = [
        ('zerver', '0078_service'),
    ]

    operations = [
        migrations.RunPython(delete_old_scheduled_jobs),
    ]
