from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

scheduler = AsyncIOScheduler()

async def check_reminders():
    """
    Checks the database for upcoming reminders and sends WhatsApp templates
    """
    print(f"[{datetime.utcnow()}] Checking for active reminders...")
    # TODO: Query DB for reminders due today
    # TODO: Make API call to Meta WhatsApp to send reminder template
    pass

def start_scheduler():
    scheduler.add_job(check_reminders, 'interval', hours=24)
    scheduler.start()
    print("Scheduler started.")
