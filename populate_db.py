import os
import sys
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

# Añadir el directorio raíz al path para que Python encuentre 'app'
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from app.models.database import SessionLocal, Usuario

# =========================================================================
# CONFIGURA AQUÍ LOS MIEMBROS DEL EQUIPO
#
# 'whatsapp_id': Es el número tal como llega desde WhatsApp.
# Normalmente para Argentina empieza con '549' seguido del código de área 
# y el número (sin el 15). 
# Ejemplo: si en tu log decía "User 5493624225651: Hola", tu whatsapp_id es '5493624225651'.
# =========================================================================
TEAM_MEMBERS = [
    {
        "nombre": "Philippe",
        "email": "philippedamau@gmail.com",
        "whatsapp_id": "5493624225651" # <- REEMPLAZAR AQUÍ CON EL NÚMERO 1
    },
    {
        "nombre": "Franco",
        "email": "francodamiansanchez10@gmail.com",
        "whatsapp_id": "5493624841354" # <- REEMPLAZAR AQUÍ CON EL NÚMERO 2
    },
    {
        "nombre": "Matías",
        "email": "matiasafernandez23@gmail.com",
        "whatsapp_id": "5493704052223" # <- REEMPLAZAR AQUÍ CON EL NÚMERO 3
    },
    {
        "nombre": "Mariano",
        "email": "marianoinsaurralde5@gmail.com",
        "whatsapp_id": "5493624002711" # <- REEMPLAZAR AQUÍ CON EL NÚMERO 4
    },
    {
        "nombre": "Sandra",
        "email": "sandralilianaacosta@gmail.com",
        "whatsapp_id": "5493794269996" # <- REEMPLAZAR AQUÍ CON EL NÚMERO 5
    }
]
# =========================================================================

def populate_team():
    db: Session = SessionLocal()
    try:
        print("Iniciando carga de usuarios...")
        nuevos = 0
        
        for member_data in TEAM_MEMBERS:
            # Buscar si ya existe por email o por whatsapp_id
            existente = db.query(Usuario).filter(
                (Usuario.email == member_data["email"]) | 
                (Usuario.whatsapp_id == member_data["whatsapp_id"])
            ).first()
            
            if existente:
                print(f"⚠️ El usuario {member_data['nombre']} ya existe (ID: {existente.id}). Actualizando datos...")
                existente.nombre = member_data["nombre"]
                existente.email = member_data["email"]
                existente.whatsapp_id = member_data["whatsapp_id"]
            else:
                nuevo_usuario = Usuario(**member_data)
                db.add(nuevo_usuario)
                nuevos += 1
                print(f"✅ Añadido: {member_data['nombre']} ({member_data['whatsapp_id']})")
        
        db.commit()
        print(f"🎉 Proceso terminado. Se crearon {nuevos} usuarios nuevos.")
        
    except IntegrityError as e:
        db.rollback()
        print(f"❌ Error de integridad en la base de datos: {e}")
    except Exception as e:
        db.rollback()
        print(f"❌ Ocurrió un error inesperado: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    populate_team()
