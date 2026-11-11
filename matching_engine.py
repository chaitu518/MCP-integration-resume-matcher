"""
matching_engine.py
==================

Pure, side-effect-free resume ↔ job scoring used by ``matching_agent.py``.

It has **no file-system or network access**. All I/O now happens through MCP
servers, which is the point of the Milestone 4 refactor. If your Milestone 3
used embeddings / ChromaDB hybrid search, swap :func:`score_candidate` for your
scorer; the graph does not change.

Score (0–100) = 50 × required-skill coverage
              + 20 × preferred-skill coverage
              + 20 × TF-IDF text similarity (job description ↔ resume)
              + 10 × experience fit (years vs. job minimum)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date

# canonical skill -> regex alternatives (matched case-insensitively on token boundaries)
SKILL_PATTERNS: dict[str, list[str]] = {
    "Java": [r"java(?!\s*script)"],
    "Spring Boot": [r"spring\s*boot"],
    "Hibernate": [r"hibernate", r"jpa"],
    "Kafka": [r"kafka"],
    "Microservices": [r"micro-?services?"],
    "SQL": [r"sql"],
    "MySQL": [r"mysql"],
    "PostgreSQL": [r"postgres(?:ql)?"],
    "MongoDB": [r"mongo(?:db)?"],
    "Redis": [r"redis"],
    "Docker": [r"docker"],
    "Kubernetes": [r"kubernetes", r"k8s", r"eks", r"aks", r"gke"],
    "AWS": [r"aws", r"amazon web services"],
    "Azure": [r"azure"],
    "GCP": [r"gcp", r"google cloud"],
    "REST APIs": [r"rest(?:ful)?\s*apis?", r"restful"],
    "gRPC": [r"grpc"],
    "GraphQL": [r"graphql"],
    "JUnit": [r"junit"],
    "CI/CD": [r"ci\s*/\s*cd", r"jenkins", r"github actions"],
    "Git": [r"git(?!hub actions)"],
    "System Design": [r"system design"],
    "Python": [r"python"],
    "Machine Learning": [r"machine learning", r"\bml\b"],
    "Deep Learning": [r"deep learning"],
    "NLP": [r"nlp", r"natural language processing"],
    "RAG": [r"rag", r"retrieval[- ]augmented"],
    "LangChain": [r"langchain"],
    "LangGraph": [r"langgraph"],
    "PyTorch": [r"pytorch"],
    "TensorFlow": [r"tensorflow"],
    "scikit-learn": [r"scikit-learn", r"sklearn"],
    "Pandas": [r"pandas"],
    "FastAPI": [r"fastapi"],
    "Vector Databases": [r"vector (?:databases?|dbs?|stores?)"],
    "ChromaDB": [r"chroma(?:db)?"],
    "Apache Spark": [r"(?:apache\s+)?spark", r"pyspark"],
    "Airflow": [r"airflow"],
    "Snowflake": [r"snowflake"],
    "dbt": [r"dbt"],
    "Scala": [r"scala"],
    "ETL": [r"etl"],
    "JavaScript": [r"javascript", r"\bjs\b"],
    "TypeScript": [r"typescript"],
    "React": [r"react(?:\.js)?"],
    "Next.js": [r"next\.?js"],
    "Node.js": [r"node\.?js"],
    "HTML": [r"html5?"],
    "CSS": [r"css3?"],
    "Prometheus": [r"prometheus"],
    "Grafana": [r"grafana"],
}

# having the key skill implies the listed skills
IMPLIES: dict[str, list[str]] = {
    "MySQL": ["SQL"], "PostgreSQL": ["SQL"], "Snowflake": ["SQL"],
    "PyTorch": ["Machine Learning", "Deep Learning"], "TensorFlow": ["Machine Learning", "Deep Learning"],
    "scikit-learn": ["Machine Learning"], "ChromaDB": ["Vector Databases"],
    "LangGraph": ["LangChain"], "Spring Boot": ["Java"], "Next.js": ["React"],
}

_COMPILED = {
    skill: re.compile("|".join(rf"(?<![a-z0-9+#.])(?:{p})(?![a-z0-9+#])" for p in pats), re.IGNORECASE)
    for skill, pats in SKILL_PATTERNS.items()
}
_STOP = set("a an and are as at be by for from has have in is it of on or our that the to will with you your "
            "we us this using use build built experience years year strong required".split())


def normalize_skill(skill: str) -> str:
    for canonical, rx in _COMPILED.items():
        if canonical.lower() == skill.lower() or rx.fullmatch(skill.strip()):
            return canonical
    return skill.strip()


def extract_skills(text: str) -> set[str]:
    found = {skill for skill, rx in _COMPILED.items() if rx.search(text)}
    for skill in list(found):
        found.update(IMPLIES.get(skill, []))
    return found


def estimate_years(text: str, today: date | None = None) -> float:
    """Explicit 'N years' wins; otherwise infer from the earliest 'YYYY - present/YYYY' range."""
    stated = [int(m) for m in re.findall(r"(\d{1,2})\+?\s*(?:years|yrs)\b", text, re.IGNORECASE)]
    if stated:
        return float(max(stated))
    this_year = (today or date.today()).year
    starts = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\s*[-–]\s*(?:present|current|now|(?:19|20)\d{2})",
                                         text, re.IGNORECASE)]
    return float(max(0, this_year - min(starts))) if starts else 0.0


def extract_profile(path: str, text: str) -> dict:
    lines = [ln.strip().lstrip("#").strip() for ln in text.splitlines() if ln.strip()]
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return {
        "resume_path": path,
        "candidate": lines[0] if lines else path,
        "headline": lines[1] if len(lines) > 1 else "",
        "email": email.group(0) if email else None,
        "skills": sorted(extract_skills(text)),
        "years_experience": estimate_years(text),
        "word_count": len(text.split()),
    }


# ---------------- TF-IDF cosine similarity (no external deps) ---------------- #


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z0-9+#.]*", text.lower()) if t not in _STOP and len(t) > 1]


def tfidf_similarities(query: str, docs: dict[str, str]) -> dict[str, float]:
    corpus = {k: Counter(_tokens(v)) for k, v in docs.items()}
    q = Counter(_tokens(query))
    n = len(corpus) + 1
    df: Counter = Counter()
    for tf in [*corpus.values(), q]:
        df.update(set(tf))
    idf = {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}

    def vec(tf: Counter) -> dict[str, float]:
        return {t: c * idf[t] for t, c in tf.items()}

    qv = vec(q)
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    out = {}
    for key, tf in corpus.items():
        dv = vec(tf)
        dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        out[key] = sum(qv[t] * dv.get(t, 0.0) for t in qv) / (qn * dn)
    return out


# ---------------- scoring ---------------- #


def tier(score: float) -> str:
    return "Strong" if score >= 75 else "Good" if score >= 55 else "Partial" if score >= 35 else "Weak"


def score_candidate(profile: dict, job: dict, similarity: float) -> dict:
    have = set(profile["skills"])
    required = [normalize_skill(s) for s in job.get("required_skills", [])]
    preferred = [normalize_skill(s) for s in job.get("preferred_skills", [])]
    req_hit = [s for s in required if s in have]
    pref_hit = [s for s in preferred if s in have]
    req_cov = len(req_hit) / len(required) if required else 1.0
    pref_cov = len(pref_hit) / len(preferred) if preferred else 1.0
    sim = min(1.0, similarity / 0.6)  # TF-IDF cosine of ~0.6 is already a near-identical match for resumes
    min_years = float(job.get("min_years") or 0)
    exp_fit = 1.0 if min_years <= 0 else min(1.0, profile["years_experience"] / min_years)
    score = round(50 * req_cov + 20 * pref_cov + 20 * sim + 10 * exp_fit, 1)
    missing = [s for s in required if s not in have]
    return {
        **profile,
        "score": score,
        "tier": tier(score),
        "breakdown": {
            "required_coverage": round(req_cov, 3),
            "preferred_coverage": round(pref_cov, 3),
            "text_similarity": round(similarity, 3),
            "experience_fit": round(exp_fit, 3),
        },
        "matched_skills": req_hit + pref_hit,
        "missing_skills": missing,
        "missing_preferred": [s for s in preferred if s not in have],
    }


def rank_candidates(profiles: dict[str, dict], documents: dict[str, str], job: dict) -> list[dict]:
    query = " ".join([job.get("title", ""), job.get("description", ""),
                      *job.get("required_skills", []), *job.get("preferred_skills", [])])
    sims = tfidf_similarities(query, {p: documents[p] for p in profiles if p in documents})
    scored = [score_candidate(prof, job, sims.get(path, 0.0)) for path, prof in profiles.items()]
    scored.sort(key=lambda c: (-c["score"], c["candidate"]))
    for i, c in enumerate(scored, 1):
        c["rank"] = i
    return scored


def template_explanation(c: dict, job: dict) -> str:
    req = job.get("required_skills", [])
    hit = [s for s in c["matched_skills"] if s in {normalize_skill(r) for r in req}]
    parts = [f"{c['tier']} match ({c['score']}/100): covers {len(hit)}/{len(req)} required skills"]
    if hit:
        parts[0] += f" ({', '.join(hit)})"
    if c["missing_skills"]:
        parts.append(f"missing {', '.join(c['missing_skills'])}")
    yrs, need = c["years_experience"], job.get("min_years", 0)
    parts.append(f"{yrs:g} yrs experience vs {need} required")
    if c["missing_preferred"] and len(c["missing_preferred"]) <= 3:
        parts.append(f"could strengthen with {', '.join(c['missing_preferred'])}")
    return "; ".join(parts) + "."
