import os
from typing import List

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    ForeignKey,
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# --------------------------------------------------------------------
# DB CONFIG
# --------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://agentic_user:agentic_password@127.0.0.1:5432/agentic_db",
)

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --------------------------------------------------------------------
# SQLALCHEMY MODELS
# --------------------------------------------------------------------
class Learner(Base):
    __tablename__ = "learners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    source_role = Column(String, nullable=False)
    target_role = Column(String, nullable=False)
    start_week = Column(Integer, nullable=False)

    progress_items = relationship(
        "WeekProgress",
        back_populates="learner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WeekProgress(Base):
    __tablename__ = "week_progress"

    id = Column(Integer, primary_key=True, index=True)
    learner_id = Column(
        Integer,
        ForeignKey("learners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    week = Column(Integer, nullable=False)
    modules_completed = Column(Integer, default=0)
    total_modules = Column(Integer, default=5)
    assessment_pct = Column(Float, default=0.0)

    learner = relationship("Learner", back_populates="progress_items")


Base.metadata.create_all(bind=engine)


# --------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------
class WeekProgressBase(BaseModel):
    week: int = Field(..., ge=1, le=7)
    modules_completed: int = Field(0, ge=0)
    total_modules: int = Field(5, ge=0)
    assessment_pct: float = Field(0, ge=0, le=100)


class WeekProgressOut(WeekProgressBase):
    class Config:
        orm_mode = True


class LearnerBase(BaseModel):
    name: str
    source_role: str
    target_role: str
    start_week: int = Field(..., ge=1, le=7)


class LearnerCreate(LearnerBase):
    pass


class LearnerOut(LearnerBase):
    id: int
    progress: List[WeekProgressOut]
    overall_modules_completed: int
    overall_modules_total: int
    overall_progress_pct: float

    class Config:
        orm_mode = True


class ProgressUpdate(BaseModel):
    items: List[WeekProgressBase]


# --------------------------------------------------------------------
# FastAPI setup
# --------------------------------------------------------------------
app = FastAPI(title="Agentic Bootcamp Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
# Helper: create 7 weeks baseline
# --------------------------------------------------------------------
WEEK_TOTALS = {1: 5, 2: 5, 3: 5, 4: 5, 5: 5, 6: 5, 7: 4}


def ensure_full_progress(learner: Learner, db: Session) -> None:
    """Garantiza que el learner tenga 7 registros de progreso (1–7)."""
    existing_by_week = {p.week: p for p in learner.progress_items}
    changed = False

    for week in range(1, 8):
        if week not in existing_by_week:
            p = WeekProgress(
                learner_id=learner.id,
                week=week,
                modules_completed=0,
                total_modules=WEEK_TOTALS.get(week, 5),
                assessment_pct=0.0,
            )
            db.add(p)
            learner.progress_items.append(p)
            changed = True

    if changed:
        db.commit()
        db.refresh(learner)


def compute_overall(learner: Learner):
    total_modules = 0
    completed_modules = 0

    for p in learner.progress_items:
        total_modules += p.total_modules
        completed_modules += max(0, min(p.modules_completed, p.total_modules))

    overall_pct = 0.0
    if total_modules > 0:
        overall_pct = round((completed_modules / total_modules) * 100, 1)

    return completed_modules, total_modules, overall_pct


def learner_to_out(learner: Learner) -> LearnerOut:
    completed, total, pct = compute_overall(learner)

    # ordenar por semana siempre
    sorted_progress = sorted(learner.progress_items, key=lambda x: x.week)

    return LearnerOut(
        id=learner.id,
        name=learner.name,
        source_role=learner.source_role,
        target_role=learner.target_role,
        start_week=learner.start_week,
        progress=[WeekProgressOut.from_orm(p) for p in sorted_progress],
        overall_modules_completed=completed,
        overall_modules_total=total,
        overall_progress_pct=pct,
    )


# --------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------
@app.get("/learners", response_model=List[LearnerOut])
def list_learners(db: Session = Depends(get_db)):
    learners = db.query(Learner).order_by(Learner.id.asc()).all()
    for l in learners:
        ensure_full_progress(l, db)
    return [learner_to_out(l) for l in learners]


@app.post("/learners", response_model=LearnerOut)
def create_learner(payload: LearnerCreate, db: Session = Depends(get_db)):
    learner = Learner(
        name=payload.name,
        source_role=payload.source_role,
        target_role=payload.target_role,
        start_week=payload.start_week,
    )
    db.add(learner)
    db.commit()
    db.refresh(learner)

    # create progress baseline
    ensure_full_progress(learner, db)

    return learner_to_out(learner)


@app.delete("/learners/{learner_id}", status_code=204)
def delete_learner(learner_id: int, db: Session = Depends(get_db)):
    learner = db.query(Learner).filter(Learner.id == learner_id).first()
    if not learner:
        raise HTTPException(status_code=404, detail="Learner not found")
    db.delete(learner)
    db.commit()
    return


@app.put("/learners/{learner_id}/progress", response_model=LearnerOut)
def update_progress(
    learner_id: int, payload: ProgressUpdate, db: Session = Depends(get_db)
):
    learner = db.query(Learner).filter(Learner.id == learner_id).first()
    if not learner:
        raise HTTPException(status_code=404, detail="Learner not found")

    ensure_full_progress(learner, db)

    progress_by_week = {p.week: p for p in learner.progress_items}

    for item in payload.items:
        p = progress_by_week.get(item.week)
        if p is None:
            # Just in case
            p = WeekProgress(
                learner_id=learner.id,
                week=item.week,
            )
            db.add(p)
            learner.progress_items.append(p)

        p.modules_completed = max(0, min(item.modules_completed, item.total_modules))
        p.total_modules = max(0, item.total_modules)
        p.assessment_pct = max(0.0, min(item.assessment_pct, 100.0))

    db.commit()
    db.refresh(learner)

    return learner_to_out(learner)
