from src.database.connection import engine
from src.database.models import Base
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DB-Update")

def update_database():
    logger.info("Prüfe Datenbank auf neue Tabellen...")
    # create_all() erstellt NUR Tabellen, die noch nicht existieren. 
    # Bestehende Tabellen und Daten bleiben unangetastet!
    Base.metadata.create_all(bind=engine)
    logger.info("Fertig! Die Tabelle 'protected_media' wurde (falls nicht vorhanden) hinzugefügt.")

if __name__ == "__main__":
    update_database()