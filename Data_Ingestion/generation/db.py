"""
Database Layer — Generation Module
====================================
Controls which database backend is used:

  LOCAL_DB=true   →  SQLite  (./local_storage/intellidraft.db)   ← default for dev
  LOCAL_DB=false  →  Any SQL DB via DATABASE_URL env var          ← production

DATABASE_URL examples:
  SQLite (default dev):  sqlite:///./local_storage/intellidraft.db
  Azure SQL:             mssql+pyodbc://user:pass@host/db?driver=ODBC+Driver+18+for+SQL+Server
  PostgreSQL:            postgresql+psycopg2://user:pass@host/db

Tables
------
  generation_jobs     — one per "generate document" request
  sections            — one per section within a job
  section_versions    — full Markdown content per version of each section
  section_comments    — user edit requests / approvals linked to a version
  templates           — reusable prompt templates (system + user-created)
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship

# ─────────────────────────────────────────────────────────────────────────────
# Connection URL — local SQLite vs production DB
# ─────────────────────────────────────────────────────────────────────────────

LOCAL_DB = os.environ.get("LOCAL_DB", "true").lower() == "true"

if LOCAL_DB:
    # Use absolute path so the DB lands in Data_Ingestion/local_storage/
    # regardless of which directory `adk web` or the Azure Function host is run from.
    _db_dir = Path(__file__).parent.parent / "local_storage"
    _db_dir.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite:///{(_db_dir / 'intellidraft.db').resolve()}"
else:
    DATABASE_URL = os.environ["DATABASE_URL"]   # set in .env for production


# ─────────────────────────────────────────────────────────────────────────────
# SQLAlchemy engine — thread-safe SQLite config for background generation thread
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine():
    if DATABASE_URL.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
        # Enable WAL mode for concurrent reads during background generation
        @event.listens_for(engine, "connect")
        def _set_wal(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
        return engine

    # Production: standard connection pool
    return create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
        Base.metadata.create_all(_engine)   # idempotent — creates missing tables only
    return _engine


def get_session() -> Session:
    """Return a new SQLAlchemy session. Caller must close it (use as context manager)."""
    return Session(get_engine())


# ─────────────────────────────────────────────────────────────────────────────
# ORM Models
# ─────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class GenerationJob(Base):
    """One per user-triggered document generation request."""
    __tablename__ = "generation_jobs"

    job_id             = Column(String(36),  primary_key=True)
    document_id        = Column(String(36),  nullable=False, index=True)
    status             = Column(String(20),  nullable=False, default="pending")
    # pending | in_progress | completed | failed
    document_type      = Column(String(100), nullable=False)
    output_format      = Column(String(20),  nullable=False, default="Word (.docx)")
    template_id        = Column(String(36),  nullable=True)
    language           = Column(String(50),  nullable=False, default="English")
    # User inputs snapshot (JSON) — stored so re-generation works without the original request
    user_inputs_json   = Column(Text,        nullable=True)
    error              = Column(Text,        nullable=True)
    total_sections     = Column(Integer,     default=0)
    completed_sections = Column(Integer,     default=0)
    created_at         = Column(DateTime,    default=datetime.utcnow)
    completed_at       = Column(DateTime,    nullable=True)

    sections = relationship(
        "Section", back_populates="job",
        cascade="all, delete-orphan",
        order_by="Section.order_index",
    )

    def to_dict(self, include_sections: bool = True) -> dict:
        d = {
            "job_id":             self.job_id,
            "document_id":        self.document_id,
            "status":             self.status,
            "document_type":      self.document_type,
            "output_format":      self.output_format,
            "template_id":        self.template_id,
            "language":           self.language,
            "error":              self.error,
            "total_sections":     self.total_sections,
            "completed_sections": self.completed_sections,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
            "completed_at":       self.completed_at.isoformat() if self.completed_at else None,
        }
        if include_sections:
            d["sections"] = [s.to_dict() for s in self.sections]
        return d


class Section(Base):
    """One row per section within a generation job."""
    __tablename__ = "sections"

    section_id      = Column(String(36),  primary_key=True)
    job_id          = Column(String(36),  ForeignKey("generation_jobs.job_id"), nullable=False, index=True)
    section_key     = Column(String(100), nullable=False)   # "executive_summary"
    section_title   = Column(String(200), nullable=False)   # "Executive Summary"
    order_index     = Column(Integer,     nullable=False)
    status          = Column(String(20),  nullable=False, default="pending")
    # pending | generating | completed | failed
    current_version = Column(Integer,     default=0)        # latest version_number
    error           = Column(Text,        nullable=True)
    created_at      = Column(DateTime,    default=datetime.utcnow)
    updated_at      = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    job      = relationship("GenerationJob", back_populates="sections")
    versions = relationship(
        "SectionVersion", back_populates="section",
        cascade="all, delete-orphan",
        order_by="SectionVersion.version_number",
    )
    comments = relationship(
        "SectionComment", back_populates="section",
        cascade="all, delete-orphan",
        order_by="SectionComment.created_at",
    )

    def latest_version(self) -> Optional["SectionVersion"]:
        if not self.versions:
            return None
        return max(self.versions, key=lambda v: v.version_number)

    def to_dict(self, include_versions: bool = True, include_comments: bool = True) -> dict:
        d = {
            "section_id":      self.section_id,
            "job_id":          self.job_id,
            "section_key":     self.section_key,
            "section_title":   self.section_title,
            "order_index":     self.order_index,
            "status":          self.status,
            "current_version": self.current_version,
            "error":           self.error,
            "created_at":      self.created_at.isoformat() if self.created_at else None,
            "updated_at":      self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_versions:
            d["versions"] = [v.to_dict() for v in self.versions]
        if include_comments:
            d["comments"] = [c.to_dict() for c in self.comments]
        return d


class SectionVersion(Base):
    """Full Markdown content for one version of a section."""
    __tablename__ = "section_versions"

    version_id          = Column(String(36),  primary_key=True)
    section_id          = Column(String(36),  ForeignKey("sections.section_id"), nullable=False, index=True)
    version_number      = Column(Integer,     nullable=False)   # 1, 2, 3 …
    content             = Column(Text,        nullable=False)   # Markdown
    word_count          = Column(Integer,     default=0)
    generation_prompt   = Column(Text,        nullable=True)    # full prompt sent to LLM
    generation_model    = Column(String(100), nullable=True)    # e.g. "azure/project-pulse-gpt-5"
    is_accepted         = Column(Boolean,     default=False)    # user explicitly approved
    trigger_comment_id  = Column(String(36),  nullable=True)    # comment that triggered regen
    created_at          = Column(DateTime,    default=datetime.utcnow)

    section = relationship("Section", back_populates="versions")

    def to_dict(self) -> dict:
        return {
            "version_id":         self.version_id,
            "section_id":         self.section_id,
            "version_number":     self.version_number,
            "content":            self.content,
            "word_count":         self.word_count,
            "generation_model":   self.generation_model,
            "is_accepted":        self.is_accepted,
            "trigger_comment_id": self.trigger_comment_id,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }


class SectionComment(Base):
    """User feedback / edit request on a specific section version."""
    __tablename__ = "section_comments"

    comment_id    = Column(String(36),  primary_key=True)
    section_id    = Column(String(36),  ForeignKey("sections.section_id"), nullable=False, index=True)
    version_number = Column(Integer,   nullable=False)    # which version this targets
    comment_text  = Column(Text,        nullable=False)
    comment_type  = Column(String(30),  default="edit_request")
    # edit_request | approval | rejection | note
    status        = Column(String(20),  default="pending")
    # pending | addressed | dismissed
    created_at    = Column(DateTime,    default=datetime.utcnow)

    section = relationship("Section", back_populates="comments")

    def to_dict(self) -> dict:
        return {
            "comment_id":     self.comment_id,
            "section_id":     self.section_id,
            "version_number": self.version_number,
            "comment_text":   self.comment_text,
            "comment_type":   self.comment_type,
            "status":         self.status,
            "created_at":     self.created_at.isoformat() if self.created_at else None,
        }


class Template(Base):
    """Reusable prompt templates — system-shipped + user-created."""
    __tablename__ = "templates"

    template_id       = Column(String(36),   primary_key=True)
    name              = Column(String(200),  nullable=False)
    document_type     = Column(String(100),  nullable=False, index=True)
    description       = Column(Text,         nullable=True)
    sections_config   = Column(Text,         nullable=False)   # JSON array of section defs
    system_instructions = Column(Text,       nullable=True)
    is_system         = Column(Boolean,      default=False)    # shipped vs user-created
    created_at        = Column(DateTime,     default=datetime.utcnow)
    updated_at        = Column(DateTime,     default=datetime.utcnow, onupdate=datetime.utcnow)

    def sections_list(self) -> list[dict]:
        return json.loads(self.sections_config or "[]")

    def to_dict(self) -> dict:
        return {
            "template_id":        self.template_id,
            "name":               self.name,
            "document_type":      self.document_type,
            "description":        self.description,
            "sections":           self.sections_list(),
            "system_instructions": self.system_instructions,
            "is_system":          self.is_system,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Project — stores Create New Project wizard data (Steps 1 & 2)
# ─────────────────────────────────────────────────────────────────────────────

class Project(Base):
    """
    One row per saved project.
    Ingested fields = everything the user fills in via the Create Project form.
    Stored in DB — frontend fetches via GET /api/projects/{id}/data, never
    reads back from the POST response body.
    """
    __tablename__ = "projects"

    # ── Identity ──────────────────────────────────────────────────────────────
    project_id    = Column(String(36),  primary_key=True)
    project_code  = Column(String(50),  nullable=True,  index=True)
    project_name  = Column(String(300), nullable=True,  index=True)
    business_unit = Column(String(100), nullable=True)

    # ── Core content ──────────────────────────────────────────────────────────
    business_priority    = Column(String(50),  nullable=True)
    problem_statement    = Column(Text,        nullable=True)
    project_objective    = Column(Text,        nullable=True)
    as_is_processes      = Column(Text,        nullable=True)
    proposed_solution    = Column(Text,        nullable=True)
    technical_landscape  = Column(Text,        nullable=True)

    # ── Optional fields ───────────────────────────────────────────────────────
    constraints           = Column(Text,       nullable=True)
    risks                 = Column(Text,       nullable=True)
    estimated_cost_crores = Column(String(50), nullable=True)   # stored as string e.g. "12.5"

    # ── Structured fields (serialised as JSON strings) ────────────────────────
    stakeholders_json  = Column(Text, nullable=True)   # [{"name":"...", "designation":"..."}]
    start_date         = Column(String(20), nullable=True)   # ISO: YYYY-MM-DD
    end_date           = Column(String(20), nullable=True)

    # ── Generation settings ───────────────────────────────────────────────────
    document_type            = Column(String(100), nullable=True, default="BRD")
    output_format            = Column(String(50),  nullable=True, default="Word (.docx)")
    additional_instructions  = Column(Text,        nullable=True)

    # ── Source documents (list of parsed document IDs) ────────────────────────
    document_ids_json = Column(Text, nullable=True)   # ["uuid1", "uuid2"]
    template_id       = Column(String(36), nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status     = Column(String(30), nullable=False, default="draft", index=True)
    # draft | ready | generating | completed
    job_id     = Column(String(36), nullable=True)   # linked GenerationJob.job_id
    created_at = Column(DateTime,   default=datetime.utcnow,  index=True)
    updated_at = Column(DateTime,   default=datetime.utcnow,  onupdate=datetime.utcnow)

    # ── Relationship to AI-derived fields ─────────────────────────────────────
    derived = relationship(
        "DerivedData",
        back_populates="project",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def stakeholders(self) -> list:
        try:
            return json.loads(self.stakeholders_json or "[]")
        except Exception:
            return []

    @property
    def document_ids(self) -> list:
        try:
            return json.loads(self.document_ids_json or "[]")
        except Exception:
            return []

    def to_ingested_dict(self) -> dict:
        """All ingested (user-entered) fields — used by GET /api/projects/{id}/data."""
        return {
            "project_id":             self.project_id,
            "project_code":           self.project_code           or "",
            "project_name":           self.project_name           or "",
            "business_unit":          self.business_unit          or "",
            "business_priority":      self.business_priority      or "",
            "problem_statement":      self.problem_statement      or "",
            "project_objective":      self.project_objective      or "",
            "stakeholders":           self.stakeholders,
            "start_date":             self.start_date             or "",
            "end_date":               self.end_date               or "",
            "as_is_processes":        self.as_is_processes        or "",
            "proposed_solution":      self.proposed_solution      or "",
            "constraints":            self.constraints            or "",
            "risks":                  self.risks                  or "",
            "technical_landscape":    self.technical_landscape    or "",
            "estimated_cost_crores":  self.estimated_cost_crores  or "",
            "document_type":          self.document_type          or "BRD",
            "output_format":          self.output_format          or "Word (.docx)",
            "additional_instructions": self.additional_instructions or "",
            "document_ids":           self.document_ids,
            "template_id":            self.template_id            or "",
        }

    def to_summary_dict(self) -> dict:
        """Lightweight summary for list views (dashboard project table)."""
        return {
            "project_id":    self.project_id,
            "project_name":  self.project_name  or "",
            "project_code":  self.project_code  or "",
            "business_unit": self.business_unit or "",
            "document_type": self.document_type or "BRD",
            "status":        self.status,
            "job_id":        self.job_id,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
            "updated_at":    self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_full_dict(self) -> dict:
        """Full project dict including lifecycle meta (used by GET /api/projects/{id})."""
        d = self.to_ingested_dict()
        d.update({
            "status":     self.status,
            "job_id":     self.job_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        })
        return d


# ─────────────────────────────────────────────────────────────────────────────
# DerivedData — AI-generated extended fields (one-to-one with Project)
# ─────────────────────────────────────────────────────────────────────────────

class DerivedData(Base):
    """
    AI-generated project fields populated by POST /api/projects/{id}/derive-fields.
    Also manually editable by the user via PUT /api/projects/{id}/data/derived.
    One row per project (upsert on generation run).
    """
    __tablename__ = "derived_data"

    project_id                  = Column(String(36), ForeignKey("projects.project_id"), primary_key=True)
    current_challenges          = Column(Text, nullable=True)
    to_be_process               = Column(Text, nullable=True)
    success_criteria            = Column(Text, nullable=True)
    business_requirements       = Column(Text, nullable=True)
    functional_requirements     = Column(Text, nullable=True)
    non_functional_requirements = Column(Text, nullable=True)
    industry_benchmarks         = Column(Text, nullable=True)
    workflow                    = Column(Text, nullable=True)
    analytics_requirements      = Column(Text, nullable=True)
    systems_involved            = Column(Text, nullable=True)
    data_sources                = Column(Text, nullable=True)
    constraints_dependencies    = Column(Text, nullable=True)
    generated_at                = Column(DateTime, nullable=True)   # set when AI populates
    updated_at                  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="derived")

    def to_dict(self) -> dict:
        return {
            "project_id":                  self.project_id,
            "current_challenges":          self.current_challenges          or "",
            "to_be_process":               self.to_be_process               or "",
            "success_criteria":            self.success_criteria            or "",
            "business_requirements":       self.business_requirements       or "",
            "functional_requirements":     self.functional_requirements     or "",
            "non_functional_requirements": self.non_functional_requirements or "",
            "industry_benchmarks":         self.industry_benchmarks         or "",
            "workflow":                    self.workflow                    or "",
            "analytics_requirements":      self.analytics_requirements      or "",
            "systems_involved":            self.systems_involved            or "",
            "data_sources":                self.data_sources                or "",
            "constraints_dependencies":    self.constraints_dependencies    or "",
            "generated_at":  self.generated_at.isoformat() if self.generated_at else None,
            "updated_at":    self.updated_at.isoformat()   if self.updated_at   else None,
        }
