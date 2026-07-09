from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

scheduler = AsyncIOScheduler()

async def check_reminders():
    """
    Consulta la base de datos en busca de recordatorios próximos y envía templates de WhatsApp.
    """
    print(f"[{datetime.utcnow()}] Verificando recordatorios activos...")
    # TODO: Consultar la DB por recordatorios que vencen hoy
    # TODO: Llamar a la API de Meta WhatsApp para enviar el template de recordatorio
    pass

def start_scheduler():
    scheduler.add_job(check_reminders, 'interval', hours=24)
    scheduler.start()
    print("Scheduler iniciado.")
