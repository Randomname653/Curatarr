import time
import os
import sys
from datetime import datetime, timedelta

# Add src to sys.path
sys.path.append(os.getcwd())

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database.models import Base, ProactiveMessage, User

# Use in-memory SQLite for benchmarking
engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

def setup_data(num_messages=1000):
    session = Session()
    user = User(plex_user_id="user1", plex_username="user1")
    session.add(user)
    session.commit()
    user_id = user.id

    for i in range(num_messages):
        msg = ProactiveMessage(
            user_id=user_id,
            trigger_type=f"trigger_{i % 10}",
            message=f"Message {i}",
            created_at=datetime.utcnow() - timedelta(hours=i % 72)
        )
        session.add(msg)
    session.commit()
    return user_id

def original_query(user_id, cooldown_cutoff):
    session = Session()
    recent_triggers = {
        m.trigger_type
        for m in session.query(ProactiveMessage)
        .filter(
            ProactiveMessage.user_id == user_id,
            ProactiveMessage.created_at >= cooldown_cutoff,
        )
        .all()
    }
    session.close()
    return recent_triggers

def optimized_query(user_id, cooldown_cutoff):
    session = Session()
    # Optimization: only fetch the trigger_type column
    recent_triggers = {
        r[0]
        for r in session.query(ProactiveMessage.trigger_type)
        .filter(
            ProactiveMessage.user_id == user_id,
            ProactiveMessage.created_at >= cooldown_cutoff,
        )
        .all()
    }
    session.close()
    return recent_triggers

def run_benchmark():
    num_messages = 5000
    user_id = setup_data(num_messages)
    cooldown_cutoff = datetime.utcnow() - timedelta(days=3)

    # Warm up
    original_query(user_id, cooldown_cutoff)
    optimized_query(user_id, cooldown_cutoff)

    iters = 100

    start = time.time()
    for _ in range(iters):
        original_query(user_id, cooldown_cutoff)
    end = time.time()
    avg_original = (end - start) / iters
    print(f"Original query average time: {avg_original:.6f}s")

    start = time.time()
    for _ in range(iters):
        optimized_query(user_id, cooldown_cutoff)
    end = time.time()
    avg_optimized = (end - start) / iters
    print(f"Optimized query average time: {avg_optimized:.6f}s")

    improvement = (avg_original - avg_optimized) / avg_original * 100
    print(f"Improvement: {improvement:.2f}%")

if __name__ == "__main__":
    run_benchmark()
