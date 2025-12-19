import os
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import Session, sessionmaker, declarative_base

try:
    # SQLAlchemy 1.4/2.0 JSON type
    from sqlalchemy import JSON
except Exception:  # pragma: no cover
    JSON = None  # type: ignore


# --------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

# Render: if DATABASE_URL is not set, default to SQLite file (works on Render too)
if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
else:
    engine = create_engine(
        "sqlite:///./app.db",
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --------------------------------------------------------------------
# SCHEMAS
# --------------------------------------------------------------------

class WeekProgress(BaseModel):
    week: int
    modules_completed: int = 0
    total_modules: int
    assessment_pct: int = 0


class LearnerCreate(BaseModel):
    name: str
    source_role: str
    target_role: str
    start_week: int = Field(ge=1, le=7)


class ProgressUpdate(BaseModel):
    items: List[WeekProgress]


class LearnerOut(BaseModel):
    id: int
    name: str
    source_role: str
    target_role: str
    start_week: int
    progress: List[WeekProgress]
    overall_modules_completed: int
    overall_modules_total: int
    overall_progress_pct: int


# --------------------------------------------------------------------
# MODEL
# --------------------------------------------------------------------

def _week_totals() -> List[int]:
    # 7 weeks, total = 34 (matches UI fallback)
    return [5, 5, 5, 5, 5, 5, 4]


def _default_progress() -> List[dict]:
    totals = _week_totals()
    return [
        {
            "week": i + 1,
            "modules_completed": 0,
            "total_modules": totals[i],
            "assessment_pct": 0,
        }
        for i in range(7)
    ]


def _overall(progress: List[dict]) -> dict:
    total = sum(int(p.get("total_modules", 0)) for p in progress)
    completed = sum(int(p.get("modules_completed", 0)) for p in progress)
    # Keep it simple: overall progress = completed / total (0..100)
    pct = int(round((completed / total) * 100)) if total else 0
    return {
        "overall_modules_total": total,
        "overall_modules_completed": completed,
        "overall_progress_pct": pct,
    }


class Learner(Base):
    __tablename__ = "learners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    source_role = Column(String, nullable=False)
    target_role = Column(String, nullable=False)
    start_week = Column(Integer, nullable=False)

    # Store progress as JSON (Postgres) or TEXT-backed JSON (SQLite)
    if JSON is not None:
        progress = Column(JSON, nullable=False)
    else:
        # Fallback (should be rare)
        from sqlalchemy import Text
        progress = Column(Text, nullable=False)


# --------------------------------------------------------------------
# APP
# --------------------------------------------------------------------

app = FastAPI(title="Agentic Bootcamp Backend")


@app.on_event("startup")
def on_startup():
    # Ensure tables exist on fresh Render deployments
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


def _to_out(row: Learner) -> LearnerOut:
    # row.progress might already be list[dict] (JSON) or a string (fallback)
    progress = row.progress
    if isinstance(progress, str):
        import json
        try:
            progress = json.loads(progress)
        except Exception:
            progress = _default_progress()

    if not isinstance(progress, list):
        progress = _default_progress()

    # validate shape and fill missing keys
    totals = _week_totals()
    normalized = []
    for i in range(7):
        week_num = i + 1
        base = {
            "week": week_num,
            "modules_completed": 0,
            "total_modules": totals[i],
            "assessment_pct": 0,
        }
        found = next((p for p in progress if int(p.get("week", 0)) == week_num), None)
        if isinstance(found, dict):
            base.update({k: found.get(k, base[k]) for k in base.keys()})
        normalized.append(base)

    ov = _overall(normalized)
    return LearnerOut(
        id=row.id,
        name=row.name,
        source_role=row.source_role,
        target_role=row.target_role,
        start_week=row.start_week,
        progress=[WeekProgress(**p) for p in normalized],
        **ov,
    )


# --------------------------------------------------------------------
# ENDPOINTS
# --------------------------------------------------------------------

@app.get("/learners", response_model=List[LearnerOut])
def list_learners(db: Session = Depends(get_db)):
    rows = db.query(Learner).order_by(Learner.id.asc()).all()
    return [_to_out(r) for r in rows]


@app.post("/learners", response_model=LearnerOut)
def create_learner(payload: LearnerCreate, db: Session = Depends(get_db)):
    row = Learner(
        name=payload.name,
        source_role=payload.source_role,
        target_role=payload.target_role,
        start_week=payload.start_week,
        progress=_default_progress(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@app.delete("/learners/{learner_id}")
def delete_learner(learner_id: int, db: Session = Depends(get_db)):
    row = db.query(Learner).filter(Learner.id == learner_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Learner not found")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@app.put("/learners/{learner_id}/progress", response_model=LearnerOut)
def update_progress(learner_id: int, payload: ProgressUpdate, db: Session = Depends(get_db)):
    row = db.query(Learner).filter(Learner.id == learner_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Learner not found")

    # store as list[dict]
    row.progress = [item.dict() for item in payload.items]
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)