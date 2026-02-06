import os
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

try:
    # SQLAlchemy 1.4/2.0 JSON type
    from sqlalchemy import JSON
except Exception:  # pragma: no cover
    JSON = None  # type: ignore


# --------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

print(f"Using DATABASE_URL host: {DATABASE_URL.split('@')[1].split('/')[0]}")

engine = create_engine(DATABASE_URL)
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

    # Manual start date (NOT created_at). Editable later.
    # Format: YYYY-MM-DD
    start_date: Optional[str] = None

    @field_validator("start_date")
    @classmethod
    def validate_start_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        import datetime as _dt

        try:
            _dt.date.fromisoformat(v)
        except Exception:
            raise ValueError("start_date must be in YYYY-MM-DD format")
        return v


class LearnerUpdate(BaseModel):
    # Editable start date for existing learners
    start_date: Optional[str] = None

    @field_validator("start_date")
    @classmethod
    def validate_start_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        import datetime as _dt

        try:
            _dt.date.fromisoformat(v)
        except Exception:
            raise ValueError("start_date must be in YYYY-MM-DD format")
        return v


class ProgressUpdate(BaseModel):
    items: List[WeekProgress]


class LearnerOut(BaseModel):
    id: int
    name: str
    source_role: str
    target_role: str
    start_week: int

    # Manual, editable start date (YYYY-MM-DD)
    start_date: Optional[str] = None

    progress: List[WeekProgress]
    overall_modules_completed: int
    overall_modules_total: int
    overall_progress_pct: int


# --------------------------------------------------------------------
# MODEL HELPERS
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
    pct = int(round((completed / total) * 100)) if total else 0
    return {
        "overall_modules_total": total,
        "overall_modules_completed": completed,
        "overall_progress_pct": pct,
    }


# --------------------------------------------------------------------
# DB MODEL
# --------------------------------------------------------------------
class Learner(Base):
    __tablename__ = "learners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    source_role = Column(String, nullable=False)
    target_role = Column(String, nullable=False)
    start_week = Column(Integer, nullable=False)

    # Manual, editable start date (YYYY-MM-DD). Nullable for existing learners.
    start_date = Column(String, nullable=True)

    # Store progress as JSON (Postgres) or TEXT-backed JSON (SQLite fallback)
    if JSON is not None:
        progress = Column(JSON, nullable=False)
    else:
        from sqlalchemy import Text

        progress = Column(Text, nullable=False)


# --------------------------------------------------------------------
# APP
# --------------------------------------------------------------------
app = FastAPI(title="Agentic Bootcamp Backend")


@app.on_event("startup")
def on_startup():
    # Ensure tables exist on fresh deployments
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
    progress = row.progress

    # row.progress might be list[dict] (JSON) or a string (fallback)
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
    normalized: List[dict] = []

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
        start_date=row.start_date,
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
        start_date=payload.start_date,
        progress=_default_progress(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@app.patch("/learners/{learner_id}", response_model=LearnerOut)
def update_learner(learner_id: int, payload: LearnerUpdate, db: Session = Depends(get_db)):
    row = db.query(Learner).filter(Learner.id == learner_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Learner not found")

    # Only field we support today: start_date (manual + editable)
    row.start_date = payload.start_date

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
    row.progress = [item.model_dump() for item in payload.items]

    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)
