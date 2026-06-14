from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional
from sqlalchemy import String, Text, Float, DateTime, Integer, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class DatasetDB(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    runs: Mapped[list["EvaluationRunDB"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")

class EvaluationRunDB(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(255), ForeignKey("datasets.id"), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    dataset: Mapped[DatasetDB] = relationship(back_populates="runs")
    results: Mapped[list["EvaluationResultDB"]] = relationship(back_populates="run", cascade="all, delete-orphan")

class EvaluationResultDB(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(255), ForeignKey("evaluation_runs.id"), nullable=False)
    example_id: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    prediction: Mapped[str] = mapped_column(Text, nullable=False)
    expected_output: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    evaluator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    run: Mapped[EvaluationRunDB] = relationship(back_populates="results")

    @property
    def metadata_dict(self) -> Dict[str, Any]:
        if not self.metadata_json:
            return {}
        try:
            return json.loads(self.metadata_json)
        except json.JSONDecodeError:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, val: Dict[str, Any]) -> None:
        self.metadata_json = json.dumps(val) if val is not None else None
