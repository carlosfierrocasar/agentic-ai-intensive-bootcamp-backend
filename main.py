import os
from typing import List, Optional
from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Column, Date, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

try:
    from sqlalchemy import JSON
except Exception:  # pragma: no cover
    JSON = None  # type: ignore

try:
    from sqlalchemy import DateTime
    from sqlalchemy.sql import func
except Exception:  # pragma: no cover
    DateTime = None  # type: ignore
    func = None  # type: ignore


# --------------------------------------------------------------------
# DB CONFIG
# --------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Helpful for debugging Render/Neon host issues
try:
    host_part = DATABASE_URL.split("@", 1)[1].split("/", 1)[0]
except Exception:
    host_part = "<unknown>"
print(f"Using DATABASE_URL host: {host_part}")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------
# Pydantic Schemas
# --------------------------------------------------------------------
class WeekProgress(BaseModel):
    week: int
    modules_completed: int = 0
    total_modules: int
    assessment_pct: int = 0


def _parse_start_date(v):
    """Accepts None, '', 'YYYY-MM-DD' strings, or date objects; returns date or None."""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except Exception as e:
            raise ValueError("start_date must be in YYYY-MM-DD format") from e
    raise ValueError("start_date must be a date or YYYY-MM-DD string")


class LearnerCreate(BaseModel):
    name: str
    source_role: str
    target_role: str
    start_week: int = Field(ge=1, le=7)
    start_date: Optional[date] = None
    # Optional: used for assessment webhook mapping
    email: Optional[str] = None

    @field_validator("start_date", mode="before")
    @classmethod
    def validate_start_date(cls, v):
        return _parse_start_date(v)


class LearnerUpdate(BaseModel):
    start_date: Optional[date] = None

    @field_validator("start_date", mode="before")
    @classmethod
    def validate_start_date(cls, v):
        return _parse_start_date(v)


class ProgressUpdate(BaseModel):
    items: List[WeekProgress]


class LearnerOut(BaseModel):
    id: int
    name: str
    source_role: str
    target_role: str
    start_week: int
    start_date: Optional[date] = None
    progress: List[WeekProgress]
    overall_modules_completed: int
    overall_modules_total: int
    overall_progress_pct: int


class AssessmentWebhook(BaseModel):
    email: str
    week: int = Field(ge=1, le=7)
    track: str
    score: int = Field(ge=0)


# --------------------------------------------------------------------
# SQLAlchemy Models
# --------------------------------------------------------------------
class Learner(Base):
    __tablename__ = "learners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    source_role = Column(String, nullable=False)
    target_role = Column(String, nullable=False)
    start_week = Column(Integer, nullable=False)
    start_date = Column(Date, nullable=True)
    # Used to associate Google Forms submissions to a learner
    email = Column(String, nullable=True, index=True)
    progress = Column(JSON, nullable=False) if JSON is not None else Column(String, nullable=False)


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    learner_id = Column(Integer, ForeignKey("learners.id"), nullable=False, index=True)
    week = Column(Integer, nullable=False, index=True)
    track = Column(String, nullable=False)
    score = Column(Integer, nullable=False)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now()) if DateTime is not None else Column(String)


# Create tables if they don't exist
Base.metadata.create_all(bind=engine)


# --------------------------------------------------------------------
# App / CORS
# --------------------------------------------------------------------
app = FastAPI(title="Agentic Bootcamp Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _week_totals() -> List[int]:
    return [5, 5, 5, 5, 5, 5, 4]


def _default_progress() -> List[dict]:
    totals = _week_totals()
    return [
        {"week": i + 1, "modules_completed": 0, "total_modules": totals[i], "assessment_pct": 0}
        for i in range(7)
    ]


def _overall(progress: List[dict]) -> dict:
    total = sum(int(p.get("total_modules", 0)) for p in progress)
    completed = sum(int(p.get("modules_completed", 0)) for p in progress)
    pct = int(round((completed / total) * 100)) if total else 0
    return {
        "overall_modules_completed": completed,
        "overall_modules_total": total,
        "overall_progress_pct": pct,
    }


def _normalize_progress(progress_value) -> List[dict]:
    progress = progress_value
    if isinstance(progress, str):
        import json

        try:
            progress = json.loads(progress)
        except Exception:
            progress = _default_progress()

    if not isinstance(progress, list):
        progress = _default_progress()

    totals = _week_totals()
    normalized: List[dict] = []
    for i in range(7):
        week_num = i + 1
        base = {"week": week_num, "modules_completed": 0, "total_modules": totals[i], "assessment_pct": 0}
        found = next((p for p in progress if isinstance(p, dict) and int(p.get("week", 0)) == week_num), None)
        if isinstance(found, dict):
            for k in base.keys():
                base[k] = found.get(k, base[k])
        normalized.append(base)

    return normalized


def _to_out(row: Learner) -> LearnerOut:
    normalized = _normalize_progress(row.progress)
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
# Routes
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
        email=(payload.email.strip().lower() if payload.email else None),
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
    """Update progress, but assessment_pct is server-controlled (webhook)."""
    row = db.query(Learner).filter(Learner.id == learner_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Learner not found")

    existing = _normalize_progress(row.progress)
    existing_assessment_by_week = {int(p["week"]): int(p.get("assessment_pct", 0)) for p in existing}

    sanitized_items: List[dict] = []
    for item in payload.items:
        d = item.model_dump()
        wk = int(d.get("week", 0))
        # Force assessment_pct to whatever the server already has
        d["assessment_pct"] = existing_assessment_by_week.get(wk, 0)
        sanitized_items.append(d)

    row.progress = sanitized_items
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


# --------------------------------------------------------------------
# Assessment Webhook (Google Apps Script -> Backend)
# --------------------------------------------------------------------
PASSING_SCORE = int(os.getenv("PASSING_SCORE", "7"))  # out of 10


@app.post("/assessment-webhook")
def assessment_webhook(data: AssessmentWebhook, db: Session = Depends(get_db)):
    email = data.email.strip().lower()

    learner = db.query(Learner).filter(Learner.email == email).first()
    if not learner:
        raise HTTPException(status_code=404, detail="Learner not found for email")

    assessment = Assessment(
        learner_id=learner.id,
        week=data.week,
        track=data.track,
        score=data.score,
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)

    passed = data.score >= PASSING_SCORE

    # Update assessment_pct: stays 0 until the first pass, then locks at 100
    progress = _normalize_progress(learner.progress)
    for p in progress:
        if int(p.get("week", 0)) == int(data.week):
            current_pct = int(p.get("assessment_pct", 0))
            p["assessment_pct"] = 100 if (passed or current_pct >= 100) else 0
            break

    learner.progress = progress
    db.add(learner)
    db.commit()

    return {"status": "saved", "assessment_id": assessment.id, "passed": passed}
