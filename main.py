from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import sessionmaker, declarative_base, Session
import os

# --------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
else:
    engine = create_engine("sqlite:///./app.db", connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Learner(Base):
    __tablename__ = "learners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    source_role = Column(String, nullable=False)
    target_role = Column(String, nullable=False)
    start_week = Column(Integer, nullable=False)


# --------------------------------------------------------------------
# APP
# --------------------------------------------------------------------

app = FastAPI(title="Agentic Bootcamp Backend")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------
# ENDPOINTS
# --------------------------------------------------------------------

@app.get("/learners")
def list_learners(db: Session = Depends(get_db)):
    return db.query(Learner).all()


@app.post("/learners")
def create_learner(payload: dict, db: Session = Depends(get_db)):
    learner = Learner(
        name=payload["name"],
        source_role=payload["source_role"],
        target_role=payload["target_role"],
        start_week=payload["start_week"],
    )
    db.add(learner)
    db.commit()
    db.refresh(learner)
    return learner


@app.delete("/learners/{learner_id}")
def delete_learner(learner_id: int, db: Session = Depends(get_db)):
    learner = db.query(Learner).filter(Learner.id == learner_id).first()
    if not learner:
        raise HTTPException(status_code=404, detail="Learner not found")
    db.delete(learner)
    db.commit()
    return {"status": "deleted"}
