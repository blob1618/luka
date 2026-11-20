"""Manual bootstrap or retry of the SEPA minorista snapshot."""
from app.services.sepa_ingest import ingest_latest


if __name__ == "__main__":
    print(ingest_latest())
