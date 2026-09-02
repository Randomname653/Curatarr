import time
import os
import sys

sys.path.append(os.getcwd())

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database.models import Base, LibraryConfig

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

def setup_data():
    session = Session()
    categories = ["movie", "show", "anime", "music"]
    for i in range(100):
        c = LibraryConfig(
            plex_section_key=str(i),
            media_category=categories[i % 4],
            plex_section_title=f"Section {i}",
            plex_section_type="1"
        )
        session.add(c)
    session.commit()

def run_benchmark():
    setup_data()

    # Original
    def original():
        session = Session()
        for _ in range(10): # simulate loop over titles
            sections = [lc.plex_section_key for lc in
                        session.query(LibraryConfig)
                        .filter(LibraryConfig.media_category == "movie").all()]
        session.close()
        return sections

    # Optimized
    def optimized():
        session = Session()
        # Hoist out of the loop
        sections = [r[0] for r in
                    session.query(LibraryConfig.plex_section_key)
                    .filter(LibraryConfig.media_category == "movie").all()]
        for _ in range(10):
            pass # Use cached sections
        session.close()
        return sections

    start = time.time()
    for _ in range(100):
        original()
    t_orig = time.time() - start

    start = time.time()
    for _ in range(100):
        optimized()
    t_opt = time.time() - start

    print(f"Original: {t_orig:.4f}s")
    print(f"Optimized: {t_opt:.4f}s")
    print(f"Improvement: {(t_orig - t_opt) / t_orig * 100:.1f}%")

if __name__ == "__main__":
    run_benchmark()
